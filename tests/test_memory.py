import pytest

import app.agent.memory as memory_module


class FakeArrayUnion:
    """Minimal stand-in for firestore.ArrayUnion."""

    def __init__(self, values):
        self.values = values


class FakeDocRef:
    def __init__(self, store, key):
        self._store, self._key = store, key

    def get(self):
        return self

    @property
    def exists(self):
        return self._key in self._store

    def to_dict(self):
        return self._store.get(self._key)

    def set(self, data, merge=False):
        existing = dict(self._store.get(self._key, {})) if merge else {}
        for key, value in data.items():
            if isinstance(value, FakeArrayUnion):
                existing[key] = existing.get(key, []) + value.values
            else:
                existing[key] = value
        self._store[self._key] = existing

    def delete(self):
        self._store.pop(self._key, None)


class FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, key):
        return FakeDocRef(self._store, key)


class FakeDB:
    def __init__(self):
        self._collections: dict[str, dict] = {}

    def collection(self, name):
        return FakeCollection(self._collections.setdefault(name, {}))


@pytest.fixture(autouse=True)
def isolated_firestore(monkeypatch):
    monkeypatch.setattr(memory_module, "db", FakeDB())
    monkeypatch.setattr(memory_module.firestore, "ArrayUnion", FakeArrayUnion)


def test_append_note_then_get_memory():
    memory_module.append_note("u1", "likes dark mode")
    memory_module.append_note("u1", "prefers Python")
    assert memory_module.get_memory("u1") == ["likes dark mode", "prefers Python"]


def test_get_memory_for_unknown_user_is_empty():
    assert memory_module.get_memory("nobody") == []


def test_clear_memory():
    memory_module.append_note("u2", "note")
    memory_module.clear_memory("u2")
    assert memory_module.get_memory("u2") == []


if __name__ == "__main__":
    test_append_note_then_get_memory()
    test_get_memory_for_unknown_user_is_empty()
    test_clear_memory()
    print("ok")
