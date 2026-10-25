"""Spawned execution with bounded JSON RPC; repository policy stays in the parent."""
import contextlib
import json
import multiprocessing
import os
import queue
import re
import threading
import time

from app.execution.containment import contain, protect_parent_death
from app.execution.context import Guard, execution_context
from app.execution.errors import (
    CancelRequested, LeaseLost, OutputLimitExceeded, SafetyStop, ToolOutcomeUncertain,
)
from app.execution.limits import bounded_json, bounded_session, MAX_SESSION_BYTES

MAX_IPC_BYTES = MAX_SESSION_BYTES + 16_000
POLL_SECONDS = 0.1
AUTHORITY_SECONDS = 1.0
JOIN_SECONDS = 2.0
_SAFETY = {kind.reason: kind for kind in (OutputLimitExceeded, ToolOutcomeUncertain)}
_EOF, _INVALID = object(), object()
_FIELDS = {"ready": {"kind"}, "done": {"kind"}, "cancel": {"kind"},
           "failed": {"kind"}, "result": {"kind", "value"}, "safety": {"kind", "recovery"},
           "checkpoint": {"kind", "sequence"}, "save": {"kind", "sequence", "session"},
           "prepare_actions": {"kind", "sequence", "step", "calls"},
           "permit_action": {"kind", "sequence", "action_id", "name", "input"},
           "record_action_result": {"kind", "sequence", "action_id", "result_digest"}}


class ProcessStopped(SafetyStop):
    reason = "execution_process_stopped"

    def __init__(self, cause):
        super().__init__()
        self.recovery["cause"] = cause


def _send(connection, message):
    bounded_json(message, MAX_IPC_BYTES)
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_IPC_BYTES:
        raise OutputLimitExceeded()
    connection.send_bytes(data)


def _receive(connection):
    data = connection.recv_bytes(MAX_IPC_BYTES)
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid execution protocol") from None
    bounded_json(value, MAX_IPC_BYTES)
    if not isinstance(value, dict):
        raise ValueError("invalid execution protocol")
    return value


class _ParentRepository:
    """Narrow execution RPC, bound to the parent's claim (never child input)."""
    def __init__(self, connection):
        self.connection = connection
        self.sequence = 0

    def _request(self, kind, **data):
        self.sequence += 1
        _send(self.connection, {"kind": kind, "sequence": self.sequence, **data})
        result = _receive(self.connection)
        if result.get("sequence") != self.sequence:
            raise LeaseLost("invalid execution response")
        if result.get("kind") == "cancel":
            raise CancelRequested("cancellation requested")
        if result.get("kind") != "ok":
            raise LeaseLost("execution authority lost")
        return result

    def prepare_actions(self, claim, **data):
        return self._request("prepare_actions", **data)["result"]

    def permit_action(self, claim, **data):
        self._request("permit_action", **data)

    def record_action_result(self, claim, **data):
        self._request("record_action_result", **data)

    def checkpoint(self, claim):
        self._request("checkpoint")

    def save(self, claim, session):
        updated = self._request("save", session=session)["session"]
        # Replace, rather than merge, so consumed append/compaction signals do
        # not survive the parent's acknowledgement in the child process.
        session.clear()
        session.update(updated)


def _child(connection, parent_pid, claim, handler):
    # No provider/client objects or credentials are passed across this boundary.
    # SDK imports remain inside the trusted handler, after containment/launch.
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            protect_parent_death(parent_pid)
            _send(connection, {"kind": "ready"})
            launch = _receive(connection)
            if launch.get("kind") != "launch":
                return
            guard = Guard(_ParentRepository(connection), claim) if claim is not None else None
            with execution_context(guard) if guard else contextlib.nullcontext():
                result = handler(launch["job"], launch["session"])
                if claim is None and result is not None:
                    _send(connection, {"kind": "result", "value": result})
            _send(connection, {"kind": "done"})
        except SafetyStop as exc:
            _send(connection, {"kind": "safety", "recovery": exc.recovery})
        except CancelRequested:
            _send(connection, {"kind": "cancel"})
        except LeaseLost:
            pass
        except BaseException:
            # Tracebacks and SDK exception strings can contain private data.
            try:
                _send(connection, {"kind": "failed"})
            except Exception:
                pass
        finally:
            connection.close()


def _stop(process, containment):
    containment_failed = False
    if containment:
        try:
            containment.close()  # Confirm owned Windows tree exit before return.
        except Exception:
            containment_failed = True
    if process.is_alive():
        process.terminate()
    process.join(JOIN_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(JOIN_SECONDS)
    if process.is_alive():
        raise ProcessStopped("termination_unconfirmed")
    if containment_failed:
        raise ProcessStopped("containment_close_unconfirmed")


class _Transport:
    """Blocking pipe operations never occupy the parent's supervision loop."""
    def __init__(self, connection):
        self.connection, self.closed = connection, threading.Event()
        self.incoming, self.outgoing = queue.Queue(1), queue.Queue(1)
        self.threads = [threading.Thread(target=self._read, daemon=True),
                        threading.Thread(target=self._write, daemon=True)]
        for thread in self.threads:
            thread.start()

    def _put(self, target, value):
        while not self.closed.is_set():
            try:
                target.put(value, timeout=POLL_SECONDS)
                return
            except queue.Full:
                pass

    def _read(self):
        try:
            while not self.closed.is_set():
                self._put(self.incoming, _receive(self.connection))
        except EOFError:
            self._put(self.incoming, _EOF)
        except (OSError, ValueError, SafetyStop):
            self._put(self.incoming, _INVALID)

    def _write(self):
        try:
            while not self.closed.is_set():
                try:
                    message = self.outgoing.get(timeout=POLL_SECONDS)
                except queue.Empty:
                    continue
                _send(self.connection, message)
        except (EOFError, OSError, ValueError, SafetyStop):
            self._put(self.incoming, _INVALID)

    def close(self):
        self.closed.set()
        self.connection.close()
        for thread in self.threads:
            thread.join(JOIN_SECONDS)


class _OwnedChild:
    """One spawn/transport/watch/reap implementation for runs and cleanup calls."""
    def __init__(self, handler, claim, deadline_seconds, lease_seconds, stopping, on_started=None):
        context = multiprocessing.get_context("spawn")
        self.parent, self.child = context.Pipe()
        self.process = context.Process(target=_child, args=(self.child, os.getpid(), claim, handler), daemon=True)
        self.stopping = stopping or threading.Event()
        self.deadline = time.monotonic() + max(0, deadline_seconds)
        self.lease_until = time.monotonic() + max(0, lease_seconds)
        self.cause, self.watch_done, self.lock = [], threading.Event(), threading.Lock()
        self.containment = self.transport = self.watcher = None
        self.failure = None
        self.on_started = on_started

    def __enter__(self):
        try:
            self.process.start()
            self.child.close()
            self.containment = contain(self.process)
            self.watcher = threading.Thread(target=self._watch, daemon=True)
            self.watcher.start()
            if self.on_started:
                self.on_started(self.process)
            self.transport = _Transport(self.parent)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _watch(self):
        while not self.watch_done.wait(POLL_SECONDS):
            if self.stopping.is_set() or time.monotonic() >= min(self.deadline, self.lease_until):
                self.cause.append("worker_shutdown" if self.stopping.is_set() else "authority_timeout")
                try:
                    self.reap()
                except SafetyStop:
                    pass  # reap() latched the first physical failure centrally.
                return

    def check(self):
        if self.cause or self.stopping.is_set():
            raise ProcessStopped(self.cause[-1] if self.cause else "worker_shutdown")

    def receive(self):
        self.check()
        try:
            message = self.transport.incoming.get(timeout=POLL_SECONDS)
        except queue.Empty:
            return None
        self.check()
        if message is _EOF:
            return {"kind": "transport_eof"}
        if message is _INVALID:
            raise ProcessStopped("invalid_protocol")
        kind = message.get("kind")
        if not isinstance(kind, str) or set(message) != _FIELDS.get(kind):
            raise ProcessStopped("invalid_protocol")
        return message

    def send(self, message):
        self.check()
        self.transport.outgoing.put_nowait(message)

    def renew(self, seconds):
        self.lease_until = time.monotonic() + max(0, seconds)

    def reap(self):
        # No database or IPC work under the kill lock.
        with self.lock:
            try:
                _stop(self.process, self.containment)
            except Exception as exc:
                failure = exc if isinstance(exc, ProcessStopped) else ProcessStopped("termination_unconfirmed")
                self.failure = self.failure or failure
                self.cause.append(failure.recovery["cause"])
                self.stopping.set()  # Never clear this latch on a successful retry.
                raise failure from None

    def __exit__(self, *args):
        self.child.close()
        self.watch_done.set()
        if self.watcher:
            self.watcher.join(JOIN_SECONDS)
        try:
            if self.process.pid is not None:
                try:
                    self.reap()
                finally:
                    if not self.process.is_alive():
                        self.process.close()
        finally:
            if self.transport:
                self.transport.close()
            else:
                self.parent.close()
        if self.failure is not None:
            raise self.failure  # A successful retry cannot erase an earlier failure.


def _outcome(message):
    kind = message["kind"]
    if kind == "safety":
        recovery = message["recovery"]
        if (not isinstance(recovery, dict) or set(recovery) - {"reason", "tool"} or
                not isinstance(recovery.get("reason"), str) or
                ("tool" in recovery and (not isinstance(recovery["tool"], str) or
                 not re.fullmatch(r"[a-z_]{1,64}", recovery["tool"])))):
            raise ProcessStopped("invalid_protocol")
        error = _SAFETY.get(recovery["reason"])
        if error:
            from app.agent.tools import TOOL_SPECS, VIRTUAL_TOOL_SPECS
            if "tool" in recovery and recovery["tool"] not in {spec.name for spec in TOOL_SPECS + VIRTUAL_TOOL_SPECS}:
                raise ProcessStopped("invalid_protocol")
            raise error(recovery.get("tool"))
    raise ProcessStopped("cancellation_requested" if kind == "cancel" else
                         "ipc_closed" if kind == "transport_eof" else "handler_failed")


def supervise(repository, claim, handler, *, stopping=None, heartbeat_interval=10, on_started=None):
    """Stage terminal data until owned containment exits (Linux: direct child)."""
    if stopping is not None and stopping.is_set():
        return  # Not started: the leased claim is safely reclaimable.
    job, session = repository.start(claim)
    terminal, launched, done_at, sequence, eof = None, False, None, 0, False
    next_check, next_heartbeat = 0.0, time.monotonic() + heartbeat_interval
    try:
        with _OwnedChild(handler, claim, job["run_deadline"] - repository.clock(),
                         job["lease_expires_at"] - repository.clock(), stopping, on_started) as owned:
            while True:
                owned.check()
                now = time.monotonic()
                if now >= next_check:
                    repository.checkpoint(claim)
                    next_check = now + AUTHORITY_SECONDS
                if now >= next_heartbeat:
                    expiry = repository.heartbeat(claim)
                    owned.renew(expiry - repository.clock())
                    next_heartbeat = now + heartbeat_interval
                message = owned.receive()
                if message:
                    kind = message["kind"]
                    if kind == "ready" and not launched:
                        repository.checkpoint(claim)
                        owned.send({"kind": "launch", "job": job, "session": session})
                        launched = True
                    elif kind in {"checkpoint", "save", "prepare_actions", "permit_action", "record_action_result"}:
                        if (not launched or done_at is not None or terminal is not None or
                                type(message.get("sequence")) is not int or message["sequence"] != sequence + 1):
                            raise ProcessStopped("invalid_protocol")
                        sequence += 1
                        repository.checkpoint(claim)
                        response = {"kind": "ok", "sequence": sequence}
                        if kind == "save":
                            candidate = message["session"]
                            bounded_session(candidate)
                            if candidate.get("status") in {"idle", "awaiting_approval"}:
                                terminal = candidate
                            else:
                                repository.save(claim, candidate)
                            response["session"] = candidate
                        elif kind in {"prepare_actions", "permit_action", "record_action_result"}:
                            data = {key: value for key, value in message.items() if key not in {"kind", "sequence"}}
                            response["result"] = getattr(repository, kind)(claim, **data)
                        owned.send(response)
                    elif kind == "done" and launched and done_at is None:
                        done_at = time.monotonic()
                    elif kind == "transport_eof" and done_at is not None:
                        eof = True
                    else:
                        _outcome(message)
                if done_at is not None:
                    if owned.process.is_alive() or not eof:
                        if time.monotonic() - done_at >= JOIN_SECONDS:
                            raise ProcessStopped("exit_timeout")
                    else:
                        if owned.process.exitcode != 0 or terminal is None:
                            raise ProcessStopped("missing_terminal_state")
                        # Reap the entire owned containment BEFORE blocking DB
                        # finalization or publishing reusable capacity.
                        owned.reap()
                        owned.check()
                        repository.checkpoint(claim)
                        repository.save(claim, terminal)
                        repository.finish(claim)
                        repository.confirm_quiescent(claim)
                        return
    except CancelRequested:
        raise ProcessStopped("cancellation_requested") from None
    except (LeaseLost, SafetyStop):
        raise
    except Exception:
        raise ProcessStopped("supervision_failed") from None


def _close_sandbox(job, session):
    from app.sandbox.manager import manager
    manager.close(job["session_id"])


def supervise_cleanup(session_id, *, stopping=None, handler=_close_sandbox, timeout=30):
    """Same owned process boundary; cleanup_once still owns durable settlement."""
    with _OwnedChild(handler, None, timeout, timeout, stopping) as owned:
        launched, done_at, eof = False, None, False
        while True:
            message = owned.receive()
            if message:
                kind = message["kind"]
                if kind == "ready" and not launched:
                    owned.send({"kind": "launch", "job": {"session_id": session_id}, "session": {}})
                    launched = True
                elif kind == "done" and launched and done_at is None:
                    done_at = time.monotonic()
                elif kind == "transport_eof" and done_at is not None:
                    eof = True
                else:
                    _outcome(message)
            if done_at is not None:
                if not owned.process.is_alive() and eof:
                    if owned.process.exitcode != 0:
                        raise ProcessStopped("child_exited")
                    owned.reap()
                    owned.check()
                    return
                if time.monotonic() - done_at >= JOIN_SECONDS:
                    raise ProcessStopped("exit_timeout")


def supervise_read(request, *, handler, timeout, on_started=None):
    """Read-only helper using the same owned spawn/transport/containment/reaper.

    Only one bounded result plus done/clean exit is accepted. A result cannot
    grant its own validity deadline and is never accepted before owned exit.
    """
    with _OwnedChild(handler, None, timeout, timeout, None, on_started=on_started) as owned:
        launched, result, done_at, eof = False, None, None, False
        while True:
            message = owned.receive()
            if message:
                kind = message["kind"]
                if kind == "ready" and not launched:
                    owned.send({"kind": "launch", "job": request, "session": {}})
                    launched = True
                elif kind == "result" and launched and result is None and done_at is None:
                    bounded_json(message["value"], 2048)
                    if not isinstance(message["value"], dict):
                        raise ProcessStopped("invalid_read_result")
                    result = message["value"]
                elif kind == "done" and launched and done_at is None and result is not None:
                    done_at = time.monotonic()
                elif kind == "transport_eof" and done_at is not None:
                    eof = True
                else:
                    raise ProcessStopped("invalid_read_protocol")
            if done_at is not None:
                if not owned.process.is_alive() and eof:
                    if owned.process.exitcode != 0:
                        raise ProcessStopped("reader_exited")
                    owned.reap()
                    owned.check()
                    return result
                if time.monotonic() - done_at >= JOIN_SECONDS:
                    raise ProcessStopped("reader_exit_timeout")
