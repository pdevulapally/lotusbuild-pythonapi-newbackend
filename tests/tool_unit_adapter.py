"""Explicit receipt stub for isolated handler unit tests only."""
import pytest


@pytest.fixture(autouse=True)
def handler_receipt_adapter(monkeypatch):
    import app.execution.context as context
    original = context.action
    def invoke(operation, **data):
        if context._current.get() is not None:
            return original(operation, **data)
        # These legacy unit tests isolate SDK/schema behavior. Durable dispatch
        # tests use a real Guard and do not import this fixture.
        return None
    monkeypatch.setattr(context, "action", invoke)
