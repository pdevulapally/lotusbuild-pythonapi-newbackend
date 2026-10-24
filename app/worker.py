"""Dedicated durable worker: python -m app.worker (never the API lifespan).

Started jobs are not automatically replayed after an expired lease. External
calls cannot be transactionally rolled back; interrupted jobs need reconciliation.
"""
import logging
import os
import signal
import threading
import uuid

from app.execution.context import Guard, execution_context
from app.execution.repository import CancelRequested, ExecutionStopped, LeaseLost, runs

logger = logging.getLogger(__name__)


def execute(job, session):
    # Lazy import lets offline worker tests run without provider initialization.
    from app.agent.loop import resume_turn, run_turn
    from app.sandbox.manager import manager
    from app.execution.context import checkpoint

    checkpoint()
    if job["operation"] == "create":
        manager.create(session["id"], session["kind"])
        checkpoint()
        run_turn(session)
    elif job["operation"] == "message":
        run_turn(session, system_prompt=session.get("system_prompt"))
    elif job["operation"] == "approve":
        resume_turn(session, job["payload"]["decisions"])
    else:
        raise ValueError("unsupported job operation")


def process_claim(repository, claim, handler=execute, heartbeat_interval=10, cleanup_handler=None):
    guard = Guard(repository, claim)
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(heartbeat_interval):
            try:
                repository.heartbeat(claim)
            except Exception:
                # Fail closed on outages. An in-flight remote effect may finish,
                # but this worker must not start further effects or save state.
                guard.lost = True
                if cleanup_handler is not None:
                    try:
                        repository.cleanup_once(cleanup_handler, claim.owner)
                    except Exception:
                        pass  # Intent remains durable for another poller.
                return

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()
    try:
        job, session = repository.start(claim)
        with execution_context(guard):
            handler(job, session)
            guard.checkpoint()
        repository.finish(claim)
    except CancelRequested:
        try:
            repository.finish(claim)
        except LeaseLost:
            pass
    except LeaseLost:
        # Another worker/recovery owns state now. Never write an error over it.
        pass
    except Exception:
        logger.error("run execution failed", extra={"job_id": claim.job_id})
        try:
            repository.finish(claim, failed=True)
        except ExecutionStopped:
            pass
    finally:
        stopped.set()
        heartbeat_thread.join(timeout=heartbeat_interval + 1)


def run_once(repository=runs, owner=None, handler=execute):
    owner = owner or uuid.uuid4().hex
    from app.sandbox.manager import manager
    repository.cleanup_once(manager.close, owner)
    for job_id in repository.candidates():
        claim = repository.claim(job_id, owner)
        if claim:
            process_claim(repository, claim, handler, cleanup_handler=manager.close)
            return True
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
            if not run_once(owner=owner):
                stopping.wait(1)
        except Exception:
            logger.error("durable worker polling failed")
            stopping.wait(5)


if __name__ == "__main__":
    main()
