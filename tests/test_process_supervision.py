"""Actual spawned local processes, parent-owned repository, no remote effects."""
import json
import multiprocessing
import os
from pathlib import Path
import threading
import time

import pytest

from app.execution.repository import (
    CLEANUP_COLLECTION, CommandError, JOBS_COLLECTION, LEASE_SECONDS, LeaseLost,
)
from app.execution.slots import SLOTS_COLLECTION
from app.store import SESSIONS_COLLECTION
from app.worker import process_claim
from test_durable_runs import doc, env
from spawn_stubs import stub, owner_parent, cleanup_success, cleanup_hang


def prepare(env, tmp_path, mode):
    repo, _, _ = env
    marker, terminal = tmp_path / "child.pid", tmp_path / "terminal"
    session = repo.submit("alice", "spawn", "create", {"task": json.dumps({
        "mode": mode, "marker": str(marker), "terminal_marker": str(terminal)}), "kind": "code"})
    return session, repo.claim(session["active_run_id"], "worker"), marker, terminal


def wait_for(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("local process milestone timed out")


def launch(repo, claim, stopping=None):
    processes, failures = [], []

    def run():
        try:
            process_claim(repo, claim, stub, stopping=stopping, on_started=processes.append)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, processes, failures


def joined(thread, processes, failures):
    thread.join(12)
    assert not thread.is_alive(), "supervisor did not return"
    assert not failures
    assert len(processes) == 1
    with pytest.raises(ValueError, match="closed"):
        processes[0].is_alive()  # close() happens only after join/termination.


@pytest.mark.parametrize("mode", ["success", "updates", "approval"])
def test_spawned_completion_commits_only_after_clean_exit(env, tmp_path, mode):
    repo, db, _ = env
    session, claim, marker, _ = prepare(env, tmp_path, mode)
    thread, processes, failures = launch(repo, claim)
    joined(thread, processes, failures)
    current = doc(db, SESSIONS_COLLECTION, session["id"])
    assert marker.exists() and int(marker.read_text()) != os.getpid()
    assert current["status"] == ("awaiting_approval" if mode == "approval" else "idle")
    assert not current["recovery_required"]
    assert not doc(db, SLOTS_COLLECTION, "alice")["runs"]
    if mode == "updates":
        assert current["revision"] == 4 and current["plan"][0]["status"] == "done"


@pytest.mark.parametrize("mode", ["crash", "terminal_crash", "no_terminal", "after_terminal",
                                 "oversized_frame", "deep_frame", "replayed_sequence", "foreign_identity", "done_hang",
                                 "done_bad_frame", "done_spoofed_eof"])
def test_spawned_invalid_or_crashed_result_cannot_publish_success(env, tmp_path, mode):
    repo, db, _ = env
    session, claim, _, _ = prepare(env, tmp_path, mode)
    thread, processes, failures = launch(repo, claim)
    joined(thread, processes, failures)
    current = doc(db, SESSIONS_COLLECTION, session["id"])
    assert current["status"] == "error" and current["recovery_required"]
    assert current["recovery"]["reason"] == "execution_process_stopped"
    assert doc(db, CLEANUP_COLLECTION, session["id"])["state"] == "queued"
    assert doc(db, SLOTS_COLLECTION, "alice")["runs"] == [claim.job_id]


@pytest.mark.parametrize("trigger", ["deadline", "cancel", "shutdown", "stale"])
@pytest.mark.parametrize("mode", ["hang", "terminal_hang"])
def test_owned_hang_is_killed_without_releasing_uncertain_capacity(env, tmp_path, monkeypatch, trigger, mode):
    import app.execution.repository as module
    monkeypatch.setattr(module, "MAX_RUN_SECONDS", 20)
    repo, db, clock = env
    session, claim, marker, terminal = prepare(env, tmp_path, mode)
    stopping = threading.Event()
    thread, processes, failures = launch(repo, claim, stopping)
    try:
        wait_for(terminal.exists if mode == "terminal_hang" else marker.exists)
        assert doc(db, JOBS_COLLECTION, claim.job_id)["state"] == "running"
        with pytest.raises(CommandError):
            repo.submit("alice", "competing", "message", {"content": "next"}, session["id"])
        if trigger == "deadline":
            clock[0] += 20
        elif trigger == "cancel":
            repo.cancel(session["id"], "alice")
        elif trigger == "shutdown":
            stopping.set()
        else:
            clock[0] += LEASE_SECONDS + 1
            assert repo.claim(claim.job_id, "restart") is None
        joined(thread, processes, failures)
    finally:
        stopping.set()
        thread.join(10)
    current = doc(db, SESSIONS_COLLECTION, session["id"])
    assert current["recovery_required"] and current["status"] == ("cancelled" if trigger == "cancel" else "error")
    assert doc(db, SLOTS_COLLECTION, "alice")["runs"] == [claim.job_id]
    assert repo.claim(claim.job_id, "restart") is None
    with pytest.raises(LeaseLost):
        repo.save(claim, dict(current, status="idle"))


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object parent-death integration")
def test_abrupt_parent_kill_stops_its_owned_child(env, tmp_path):
    import ctypes
    from ctypes import wintypes as w

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes, api.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    api.WaitForSingleObject.argtypes, api.WaitForSingleObject.restype = [w.HANDLE, w.DWORD], w.DWORD
    api.TerminateProcess.argtypes, api.TerminateProcess.restype = [w.HANDLE, w.UINT], w.BOOL
    api.CloseHandle.argtypes = [w.HANDLE]
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    marker = tmp_path / "orphan.pid"
    owner = context.Process(target=owner_parent, args=(send, str(marker)))
    child_handle = None
    try:
        owner.start()
        send.close()
        assert receive.poll(15)
        child_pid = receive.recv()  # Trusted test fixture, not runtime IPC.
        wait_for(marker.exists)
        assert int(marker.read_text()) == child_pid
        # Capture a handle while this known test-created child is alive. Any
        # emergency cleanup uses that handle, never a later/reused PID lookup.
        child_handle = api.OpenProcess(0x100000 | 0x1000 | 1, False, child_pid)
        assert child_handle
        owner.kill()
        owner.join(5)
        assert not owner.is_alive()
        assert api.WaitForSingleObject(child_handle, 5000) == 0
    finally:
        if owner.pid and owner.is_alive():
            owner.kill()
            owner.join(5)
        if child_handle:
            if api.WaitForSingleObject(child_handle, 0) != 0:
                api.TerminateProcess(child_handle, 73)
                api.WaitForSingleObject(child_handle, 5000)
            api.CloseHandle(child_handle)
        owner.close()
        receive.close()


def test_real_elapsed_deadline_kills_local_hang_and_retains_budget(env, tmp_path, monkeypatch):
    from app.billing.budgets import BudgetLedger, RESERVATIONS_COLLECTION
    from app.billing.plans import ModelEntry
    import app.execution.repository as module

    repo, db, _ = env
    repo.clock = time.time
    monkeypatch.setattr(module, "MAX_RUN_SECONDS", 2)
    ledger = BudgetLedger(db, transaction_runner=db.transaction)
    reservation = ledger.reserve("alice", ModelEntry("stub", "stub", None),
        {"requests_per_day": 10, "tokens_per_month": 1000}, 100)
    session, claim, _, _ = prepare(env, tmp_path, "hang")
    thread, processes, failures = launch(repo, claim)
    joined(thread, processes, failures)
    assert doc(db, SESSIONS_COLLECTION, session["id"])["recovery_required"]
    assert doc(db, SLOTS_COLLECTION, "alice")["runs"] == [claim.job_id]
    assert doc(db, RESERVATIONS_COLLECTION, reservation)["state"] == "reserved"


def test_shutdown_watcher_still_terminates_child_when_parent_pipe_read_blocks(env, tmp_path, monkeypatch):
    import app.execution.supervision as module
    repo, _, _ = env
    _, claim, _, _ = prepare(env, tmp_path, "hang")
    reading, release, stopping = threading.Event(), threading.Event(), threading.Event()
    original = module._receive

    def stalled(connection):
        reading.set()
        release.wait(10)
        return original(connection)

    monkeypatch.setattr(module, "_receive", stalled)  # Fresh child imports are unaffected.
    thread, processes, failures = launch(repo, claim, stopping)
    try:
        assert reading.wait(5)
        wait_for(lambda: bool(processes))
        stopping.set()
        from multiprocessing.connection import wait
        assert wait([processes[0].sentinel], timeout=5)
    finally:
        release.set()
        stopping.set()
        joined(thread, processes, failures)


def test_deadline_watcher_terminates_child_while_parent_database_call_blocks(env, tmp_path, monkeypatch):
    import app.execution.repository as module
    from multiprocessing.connection import wait
    repo, db, _ = env
    repo.clock = time.time
    monkeypatch.setattr(module, "MAX_RUN_SECONDS", 3)
    session, claim, marker, _ = prepare(env, tmp_path, "hang")
    original, blocked, release = repo.checkpoint, threading.Event(), threading.Event()

    def stalled(passed):
        if marker.exists():
            blocked.set()
            release.wait(10)
        return original(passed)

    monkeypatch.setattr(repo, "checkpoint", stalled)
    thread, processes, failures = launch(repo, claim)
    try:
        assert blocked.wait(5)
        assert wait([processes[0].sentinel], timeout=5), "DB I/O must not block local termination"
    finally:
        release.set()
        joined(thread, processes, failures)
    assert doc(db, SESSIONS_COLLECTION, session["id"])["recovery_required"]


def test_shutdown_before_start_leaves_leased_work_reclaimable(env, tmp_path):
    repo, db, _ = env
    session, claim, marker, _ = prepare(env, tmp_path, "success")
    stopping = threading.Event()
    stopping.set()
    process_claim(repo, claim, stub, stopping=stopping)
    assert not marker.exists()
    assert doc(db, JOBS_COLLECTION, claim.job_id)["state"] == "leased"
    assert not doc(db, SESSIONS_COLLECTION, session["id"])["recovery_required"]


@pytest.mark.parametrize("hang", [False, True])
def test_cleanup_uses_same_bounded_spawn_boundary(tmp_path, hang):
    from app.execution.supervision import ProcessStopped, supervise_cleanup
    marker = tmp_path / "cleanup.pid"
    if hang:
        with pytest.raises(ProcessStopped):
            supervise_cleanup(str(marker), handler=cleanup_hang, timeout=2)
    else:
        supervise_cleanup(str(marker), handler=cleanup_success, timeout=10)
    assert marker.exists() and int(marker.read_text()) != os.getpid()


@pytest.mark.skipif(os.name != "nt", reason="Windows owned descendant containment")
@pytest.mark.parametrize("block_final_save", [False, True])
def test_descendants_are_stopped_before_terminal_publication(env, tmp_path, monkeypatch, block_final_save):
    import ctypes
    from ctypes import wintypes as w
    from multiprocessing.connection import wait
    repo, db, _ = env
    session, claim, marker, _ = prepare(env, tmp_path, "descendant_success")
    descendant = Path(str(marker) + ".descendant")
    reached, release, stopping = threading.Event(), threading.Event(), threading.Event()
    original = repo.save

    def save(passed, candidate):
        if candidate["status"] == "idle":
            reached.set()
            if block_final_save:
                release.wait(10)
        return original(passed, candidate)

    monkeypatch.setattr(repo, "save", save)
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes, api.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    api.WaitForSingleObject.argtypes, api.WaitForSingleObject.restype = [w.HANDLE, w.DWORD], w.DWORD
    api.CloseHandle.argtypes = [w.HANDLE]
    thread, processes, failures = launch(repo, claim, stopping)
    handle = None
    try:
        wait_for(descendant.exists)
        handle = api.OpenProcess(0x100000, False, int(descendant.read_text()))
        assert handle and api.WaitForSingleObject(handle, 0) == 258
        Path(str(marker) + ".allow_terminal").write_text("continue")
        assert reached.wait(10)
        assert api.WaitForSingleObject(handle, 0) == 0
        if block_final_save:
            assert doc(db, JOBS_COLLECTION, claim.job_id)["state"] == "running"
            stopping.set()
            # The root and its descendants were already reaped before save.
            assert wait([processes[0].sentinel], timeout=2)
    finally:
        release.set()
        joined(thread, processes, failures)
        if handle:
            api.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Owned Windows containment failure injection")
@pytest.mark.parametrize("watchdog", [False, True])
def test_first_containment_failure_latches_stop_even_if_retry_succeeds(env, tmp_path, monkeypatch, watchdog):
    import app.execution.containment as containment
    import app.execution.supervision as supervision
    import app.execution.repository as repository
    repo, db, _ = env
    if watchdog:
        monkeypatch.setattr(repository, "MAX_RUN_SECONDS", 2)
    session, claim, _, _ = prepare(env, tmp_path, "hang" if watchdog else "success")
    calls, stopping = [], threading.Event()

    class OneShotFailure:
        def __init__(self, process):
            self.owned = containment.WindowsJob(process)

        def close(self):
            self.owned.close()  # Really stop only this test's owned processes.
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("synthetic confirmation failure after stop")

    monkeypatch.setattr(supervision, "contain", OneShotFailure)
    thread, processes, failures = launch(repo, claim, stopping)
    joined(thread, processes, failures)
    assert len(calls) >= 2 and stopping.is_set()
    current = doc(db, SESSIONS_COLLECTION, session["id"])
    assert current["status"] == "error" and current["recovery_required"]
    assert current["recovery"]["cause"] == "containment_close_unconfirmed"
    assert doc(db, SLOTS_COLLECTION, "alice")["runs"] == [claim.job_id]
