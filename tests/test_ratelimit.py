import redis
from fastapi import HTTPException

import app.ratelimit as ratelimit_module
from app.ratelimit import check_rate_limit


class FakeRedisPipeline:
    def __init__(self, store):
        self._store = store
        self._ops = []

    def incr(self, key):
        self._ops.append(("incr", key))
        return self

    def expire(self, key, seconds, nx=False):
        self._ops.append(("expire",))
        return self

    def execute(self):
        results = []
        for op in self._ops:
            if op[0] == "incr":
                key = op[1]
                self._store[key] = self._store.get(key, 0) + 1
                results.append(self._store[key])
            else:
                results.append(True)
        self._ops = []
        return results


class FakeRedis:
    def __init__(self):
        self._store: dict[str, int] = {}

    def pipeline(self):
        return FakeRedisPipeline(self._store)


class BrokenPipeline:
    def incr(self, key):
        return self

    def expire(self, key, seconds, nx=False):
        return self

    def execute(self):
        raise redis.ConnectionError("redis unreachable")


class BrokenRedis:
    def pipeline(self):
        return BrokenPipeline()


def test_under_limit_is_allowed():
    ratelimit_module._redis = FakeRedis()
    ratelimit_module.RATE_LIMITS["test_under"] = (3, 60)
    for _ in range(3):
        check_rate_limit("test_under", "user1")  # must not raise


def test_over_limit_raises_429():
    ratelimit_module._redis = FakeRedis()
    ratelimit_module.RATE_LIMITS["test_over"] = (2, 60)
    check_rate_limit("test_over", "user1")
    check_rate_limit("test_over", "user1")
    try:
        check_rate_limit("test_over", "user1")
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 429


def test_distinct_identities_have_separate_counters():
    ratelimit_module._redis = FakeRedis()
    ratelimit_module.RATE_LIMITS["test_identity"] = (1, 60)
    check_rate_limit("test_identity", "user_a")
    check_rate_limit("test_identity", "user_b")  # separate identity, must not raise


def test_redis_connection_error_fails_open():
    ratelimit_module._redis = BrokenRedis()
    ratelimit_module.RATE_LIMITS["test_broken"] = (1, 60)
    check_rate_limit("test_broken", "user1")
    check_rate_limit("test_broken", "user1")  # would be over limit if Redis were up - must still not raise


if __name__ == "__main__":
    test_under_limit_is_allowed()
    test_over_limit_raises_429()
    test_distinct_identities_have_separate_counters()
    test_redis_connection_error_fails_open()
    print("ok")
