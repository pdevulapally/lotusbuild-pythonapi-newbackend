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


def run_id():
    guard = _current.get()
    return guard.claim.job_id if guard else None
