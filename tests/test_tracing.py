import pytest

import app.agent.tracing as tracing_module
from app.agent.tracing import get_trace, record_step


class FakeDocRef:
    def __init__(self, store, key):
        self._store, self._key = store, key

    def set(self, data):
        self._store[self._key] = data

    def to_dict(self):
        return self._store.get(self._key)


class FakeSubcollection:
    def __init__(self, store):
        self._store = store

    def document(self, key):
        return FakeDocRef(self._store, key)

    def stream(self):
        return [FakeDocRef(self._store, key) for key in self._store]


class FakeSessionDocRef:
    def __init__(self, trace_store):
        self._trace_store = trace_store

    def collection(self, name):
        assert name == "trace"
        return FakeSubcollection(self._trace_store)


class FakeCollection:
    def __init__(self, stores):
        self._stores = stores

    def document(self, session_id):
        return FakeSessionDocRef(self._stores.setdefault(session_id, {}))


class FakeDB:
    def __init__(self):
        self._stores: dict[str, dict] = {}

    def collection(self, name):
        assert name == "agent_v2_sessions"
        return FakeCollection(self._stores)


@pytest.fixture(autouse=True)
def isolated_firestore(monkeypatch):
    monkeypatch.setattr(tracing_module, "db", FakeDB())


def test_record_and_get_trace_ordered_by_step():
    record_step(
        "s1", 1, tool_names=["bash"], duration_ms=120.0, input_tokens=10, output_tokens=5,
        provider="anthropic", model="claude-haiku-5-5",
    )
    record_step(
        "s1", 0, tool_names=[], duration_ms=80.0, input_tokens=8, output_tokens=2,
        provider="anthropic", model="claude-haiku-5-5",
    )
    trace = get_trace("s1")
    assert [t["step"] for t in trace] == [1, 0]
    assert trace[0]["tool"] == ["bash"]


if __name__ == "__main__":
    test_record_and_get_trace_ordered_by_step()
    print("ok")
