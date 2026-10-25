"""Dedicated durable worker: python -m app.worker (never the API lifespan).

Started jobs are not automatically replayed after an expired lease. External
calls cannot be transactionally rolled back; interrupted jobs need reconciliation.
"""
import logging
import os
import signal
import threading
import uuid

from app.execution.repository import CancelRequested, ExecutionStopped, LeaseLost, runs
from app.execution.errors import SafetyStop
from app.execution.supervision import ProcessStopped, supervise, supervise_cleanup

logger = logging.getLogger(__name__)


def execute(job, session):
    # Lazy import lets offline worker tests run without provider initialization.
    from app.agent.loop import resume_turn, run_turn
    from app.sandbox.manager import manager
    from app.execution.context import checkpoint

    checkpoint()
    if job["operation"] == "create":
        from app.billing.model_select import selected_model, require_model_kind, validate_selection
        from app.billing.entitlements import resolve_entitlements
        validate_selection(session)
        if session.get("model") is not None:
            require_model_kind(selected_model(session, resolve_entitlements(session["uid"])), session["kind"])
        manager.create(session["id"], session["kind"])
        checkpoint()
        run_turn(session)
    elif job["operation"] == "message":
        run_turn(session, system_prompt=session.get("system_prompt"))
    elif job["operation"] == "approve":
        resume_turn(session, job["payload"]["decisions"])
    else:
        raise ValueError("unsupported job operation")


def process_claim(repository, claim, handler=execute, heartbeat_interval=10,
                  *, stopping=None, execution=supervise, on_started=None):
    try:
        execution(repository, claim, handler, heartbeat_interval=heartbeat_interval,
                  stopping=stopping, on_started=on_started)
    except CancelRequested:
        try:
            repository.finish(claim)
        except LeaseLost:
            pass
    except LeaseLost:
        # A rejected child result can leave our own job active. Recheck through
        # the same authoritative finish; a changed/expired fence still rejects.
        try:
            repository.finish(claim, failed=True, recovery=ProcessStopped("authority_lost").recovery)
        except ExecutionStopped:
            pass
    except Exception as exc:
        logger.error("run execution failed", extra={"job_id": claim.job_id})
        try:
            repository.finish(claim, failed=True, recovery=exc.recovery if isinstance(exc, SafetyStop) else None)
        except ExecutionStopped:
            pass


def run_once(repository=runs, owner=None, handler=execute, *, stopping=None, queue_authorizer=None):
    owner = owner or uuid.uuid4().hex
    if stopping is not None and stopping.is_set():
        return False
    repository.cleanup_once(lambda sid: supervise_cleanup(sid, stopping=stopping), owner)
    for job_id in repository.candidates():
        if stopping is not None and stopping.is_set():
            return False
        claim = repository.claim(job_id, owner)
        if claim:
            process_claim(repository, claim, handler, stopping=stopping)
            return True
    # No stored bearer or assumed background auth. Automatic promotion is
    # explicitly closed until a trusted fresh subject-status reader is supplied.
    if queue_authorizer is not None:
        for candidate in repository.queue_candidates():
            if stopping is not None and stopping.is_set():
                return False
            try:
                authorization = queue_authorizer(candidate)
                repository.dispatch_next(candidate["session_id"], candidate["uid"],
                                         "worker:" + candidate["id"], authorization)
                return True
            except Exception as exc:
                # One blocked account must not starve the bounded batch. No
                # effect executed here; pending intent and counters are retained.
                logger.warning("queued turn was not admitted", extra={"error_type": type(exc).__name__})
    return False


def main():
    # Providers/network policy, billing and artifacts are not yet release-ready.
    # This guard prevents accidentally enabling paid autonomous execution merely
    # by deploying the foundation branch.
    if os.environ.get("ENABLE_EXPERIMENTAL_WORKER") != "1":
        raise SystemExit("worker disabled: isolated development only; set ENABLE_EXPERIMENTAL_WORKER=1")
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    owner = uuid.uuid4().hex
    while not stopping.is_set():
        try:
            if not run_once(owner=owner, stopping=stopping):
                stopping.wait(1)
        except Exception:
            logger.error("durable worker polling failed")
            stopping.wait(5)


if __name__ == "__main__":
    main()
