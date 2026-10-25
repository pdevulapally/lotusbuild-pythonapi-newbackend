"""Run-local write fence and cooperative cancellation checks."""
from contextlib import contextmanager
from contextvars import ContextVar

from app.execution.repository import LeaseLost

_current = ContextVar("run_guard", default=None)


class Guard:
    def __init__(self, repository, claim):
        self.repository, self.claim = repository, claim
        self.lost = False

    def checkpoint(self):
        if self.lost:
            raise LeaseLost("worker heartbeat failed")
        self.repository.checkpoint(self.claim)

    def action(self, operation, **data):
        try:
            self.checkpoint()
            return getattr(self.repository, operation)(self.claim, **data)
        except Exception as exc:
            from app.execution.errors import ExecutionStopped
            if isinstance(exc, ExecutionStopped):
                raise
            # A committed permit/result may have lost its reply. Never let the
            # loop's ordinary tool-error handler continue after a storage error.
            raise LeaseLost("action receipt authority unavailable") from None

    def save(self, session):
        self.checkpoint()
        self.repository.save(self.claim, session)


@contextmanager
def execution_context(guard):
    token = _current.set(guard)
    try:
        yield
    finally:
        _current.reset(token)


def checkpoint():
    guard = _current.get()
    if guard is None:
        raise LeaseLost("agent execution requires a durable worker claim")
    guard.checkpoint()


def save(session):
    guard = _current.get()
    if guard is None:
        raise LeaseLost("session writes require a durable worker claim")
    guard.save(session)


def action(operation, **data):
    guard = _current.get()
    if guard is None:
        raise LeaseLost("action execution requires a durable worker claim")
    return guard.action(operation, **data)


def prepare_actions(session, step, calls):
    result = action("prepare_actions", step=step, calls=calls)
    session.update(result["session"])
    return result


def run_id():
    guard = _current.get()
    return guard.claim.job_id if guard else None
