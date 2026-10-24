import datetime
import pytest

import app.idempotency as idempotency_module
from app.idempotency import IdempotencyConflict, run_idempotent


class FakeDocRef:
    def __init__(self, store, key):
        self._store, self._key = store, key

    def get(self, transaction=None):
        return self

    @property
    def exists(self):
        return self._key in self._store

    def to_dict(self):
        return self._store.get(self._key)

    def set(self, data):
        self._store[self._key] = data

    def delete(self):
        self._store.pop(self._key, None)


class FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, key):
        return FakeDocRef(self._store, key)


class FakeTransaction:
    def set(self, ref, data):
        ref.set(data)


class FakeDB:
    def __init__(self):
        self._collections: dict[str, dict] = {}

    def collection(self, name):
        return FakeCollection(self._collections.setdefault(name, {}))

    def transaction(self):
        return FakeTransaction()


# Swap in a fake Firestore client and a no-op @transactional decorator - the real
# decorator drives retries against a live Firestore Transaction's begin/commit,
# which this offline test has no need to exercise; the state-machine logic in
# run_idempotent (missing/completed/fresh/stale/exception) is what's under test.
@pytest.fixture(autouse=True)
def isolated_firestore(monkeypatch):
    monkeypatch.setattr(idempotency_module, "db", FakeDB())
    monkeypatch.setattr(idempotency_module.firestore, "transactional", lambda fn: fn)


def test_first_call_runs_fn_and_caches():
    calls = []

    def fn():
        calls.append(1)
        return 200, {"ok": True}

    result = run_idempotent("u1", "scope", "key1", fn)
    assert result.replayed is False
    assert result.body == {"ok": True}
    assert len(calls) == 1


def test_repeat_call_replays_cached_result_without_rerunning():
    calls = []

    def fn():
        calls.append(1)
        return 200, {"ok": True}

    run_idempotent("u1", "scope", "key2", fn)
    result = run_idempotent("u1", "scope", "key2", fn)
    assert result.replayed is True
    assert result.body == {"ok": True}
    assert len(calls) == 1


def test_fresh_in_progress_raises_conflict():
    ref = idempotency_module.db.collection(idempotency_module.COLLECTION).document("u1:scope:key3")
    ref.set({"status": "in_progress", "created_at": idempotency_module._now()})
    try:
        run_idempotent("u1", "scope", "key3", lambda: (200, {}))
        assert False, "expected IdempotencyConflict"
    except IdempotencyConflict:
        pass


def test_stale_in_progress_is_retried():
    ref = idempotency_module.db.collection(idempotency_module.COLLECTION).document("u1:scope:key4")
    stale_age = idempotency_module.IDEMPOTENCY_STALE_SECONDS + 10
    ref.set({"status": "in_progress", "created_at": idempotency_module._now() - datetime.timedelta(seconds=stale_age)})

    calls = []

    def fn():
        calls.append(1)
        return 200, {"retried": True}

    result = run_idempotent("u1", "scope", "key4", fn)
    assert result.replayed is False
    assert result.body == {"retried": True}
    assert len(calls) == 1


def test_exception_deletes_record_allowing_retry():
    def failing():
        raise RuntimeError("boom")

    try:
        run_idempotent("u1", "scope", "key5", failing)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass

    calls = []

    def fn():
        calls.append(1)
        return 200, {"ok": True}

    result = run_idempotent("u1", "scope", "key5", fn)
    assert result.replayed is False
    assert len(calls) == 1


if __name__ == "__main__":
    test_first_call_runs_fn_and_caches()
    test_repeat_call_replays_cached_result_without_rerunning()
    test_fresh_in_progress_raises_conflict()
    test_stale_in_progress_is_retried()
    test_exception_deletes_record_allowing_retry()
    print("ok")
