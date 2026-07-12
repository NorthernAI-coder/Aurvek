import asyncio
import itertools

import pytest

import rediscfg


class FakeRedis:
    def __init__(self):
        self.entries = {}
        self._lock = asyncio.Lock()

    async def eval(self, script, numkeys, *args):
        assert numkeys == 1
        key = args[0]

        async with self._lock:
            bucket = self.entries.setdefault(key, {})
            if script == rediscfg._RATE_LIMIT_SCRIPT:
                now = float(args[1])
                cutoff = float(args[2])
                limit = int(args[3])
                member = args[5]
                self._prune(bucket, cutoff)
                if len(bucket) >= limit:
                    return [0, len(bucket)]
                bucket[member] = now
                return [1, len(bucket)]

            if script == rediscfg._RATE_LIMIT_STATUS_SCRIPT:
                cutoff = float(args[1])
                self._prune(bucket, cutoff)
                oldest = min(bucket.values()) if bucket else ""
                return [len(bucket), oldest]

        raise AssertionError("Unexpected Redis script")

    @staticmethod
    def _prune(bucket, cutoff):
        expired = [member for member, score in bucket.items() if score <= cutoff]
        for member in expired:
            del bucket[member]


class FailingRedis:
    async def eval(self, *args, **kwargs):
        raise ConnectionError("Redis unavailable")


@pytest.fixture(autouse=True)
def reset_local_fallback(monkeypatch):
    rediscfg._local_rate_limit_buckets.clear()
    monkeypatch.setattr(rediscfg, "_local_rate_limit_lock", asyncio.Lock())


@pytest.mark.asyncio
async def test_atomic_limiter_counts_concurrent_requests_with_same_timestamp(monkeypatch):
    fake_redis = FakeRedis()
    unique_suffixes = itertools.count()
    monkeypatch.setattr(rediscfg, "redis_client", fake_redis)
    monkeypatch.setattr(rediscfg.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(rediscfg.time, "time_ns", lambda: 1_000_000_000)
    monkeypatch.setattr(
        rediscfg.secrets,
        "token_hex",
        lambda _length: f"{next(unique_suffixes):016x}",
    )

    results = await asyncio.gather(
        *(
            rediscfg.check_rate_limit(42, limit=120, window_minutes=1)
            for _ in range(150)
        )
    )

    assert sum(results) == 120
    assert len(fake_redis.entries["rate_limit:ai_call:42"]) == 120


@pytest.mark.asyncio
async def test_limiter_uses_bounded_local_fallback_when_redis_fails(monkeypatch):
    monkeypatch.setattr(rediscfg, "redis_client", FailingRedis())
    monkeypatch.setattr(rediscfg.time, "time", lambda: 2_000.0)

    results = await asyncio.gather(
        *(
            rediscfg.check_rate_limit(7, action="paid_call", limit=30)
            for _ in range(40)
        )
    )

    assert sum(results) == 30
    status = await rediscfg.get_rate_limit_status(7, action="paid_call", limit=30)
    assert status["current"] == 30
    assert status["remaining"] == 0
    assert status["reset_time"] == 2_060


@pytest.mark.asyncio
async def test_status_and_expiry_follow_sliding_window(monkeypatch):
    fake_redis = FakeRedis()
    clock = {"now": 100.0}
    monkeypatch.setattr(rediscfg, "redis_client", fake_redis)
    monkeypatch.setattr(rediscfg.time, "time", lambda: clock["now"])

    assert await rediscfg.check_rate_limit(3, limit=2)
    assert await rediscfg.check_rate_limit(3, limit=2)
    assert not await rediscfg.check_rate_limit(3, limit=2)

    clock["now"] = 110.2
    status = await rediscfg.get_rate_limit_status(3, limit=2)
    assert status == {
        "current": 2,
        "limit": 2,
        "remaining": 0,
        "reset_time": 160,
        "window_minutes": 1,
    }

    clock["now"] = 160.1
    assert await rediscfg.check_rate_limit(3, limit=2)


@pytest.mark.asyncio
async def test_limiter_rejects_invalid_configuration():
    with pytest.raises(ValueError):
        await rediscfg.check_rate_limit(1, limit=0)
    with pytest.raises(ValueError):
        await rediscfg.get_rate_limit_status(1, window_minutes=0)
