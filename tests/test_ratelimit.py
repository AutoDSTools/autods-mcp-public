"""F1 acceptance — per-user token-bucket rate limiting.

Unit tests pin the bucket math (in-process backend, fake clock). Integration
tests run the real Lua against ``fakeredis`` and assert the limit is shared
across two limiter instances pointed at one Redis — i.e. enforced cluster-wide,
not per-process. A final test covers the fail-open behaviour on a Redis error.
"""

import fakeredis
import fakeredis.aioredis as fakeaioredis
import pytest
from redis.exceptions import RedisError

from autods_mcp_server.ratelimit import (
    BucketSpec,
    InMemoryRateLimiter,
    RedisRateLimiter,
    build_bucket_specs,
)
from autods_mcp_server.settings import Settings


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# --- in-process bucket math -------------------------------------------------


async def test_in_memory_single_bucket_allows_up_to_capacity() -> None:
    clock = _FakeClock()
    limiter = InMemoryRateLimiter([BucketSpec("b", capacity=2, refill_rate=1.0)], clock=clock)

    assert (await limiter.acquire("u")).allowed is True
    assert (await limiter.acquire("u")).allowed is True
    third = await limiter.acquire("u")
    assert third.allowed is False
    # One token refills in 1s at 1 token/sec.
    assert third.retry_after == pytest.approx(1.0, abs=0.01)


async def test_in_memory_bucket_refills_over_time() -> None:
    clock = _FakeClock()
    limiter = InMemoryRateLimiter([BucketSpec("b", capacity=1, refill_rate=1.0)], clock=clock)

    assert (await limiter.acquire("u")).allowed is True
    assert (await limiter.acquire("u")).allowed is False
    clock.advance(1.0)
    assert (await limiter.acquire("u")).allowed is True


async def test_in_memory_both_buckets_must_allow() -> None:
    """The tighter bucket (hour) gates even when the looser (minute) has room."""
    clock = _FakeClock()
    limiter = InMemoryRateLimiter(
        [
            BucketSpec("minute", capacity=5, refill_rate=5 / 60),
            BucketSpec("hour", capacity=3, refill_rate=3 / 3600),
        ],
        clock=clock,
    )
    assert [(await limiter.acquire("u")).allowed for _ in range(3)] == [True, True, True]
    blocked = await limiter.acquire("u")
    assert blocked.allowed is False
    # Hour bucket refills 1 token in 3600/3 = 1200s.
    assert blocked.retry_after == pytest.approx(1200.0, rel=0.01)


async def test_in_memory_separate_keys_are_independent() -> None:
    clock = _FakeClock()
    limiter = InMemoryRateLimiter([BucketSpec("b", capacity=1, refill_rate=1.0)], clock=clock)
    assert (await limiter.acquire("alice")).allowed is True
    # bob has his own bucket — alice exhausting hers doesn't affect him.
    assert (await limiter.acquire("bob")).allowed is True
    assert (await limiter.acquire("alice")).allowed is False


# --- Redis backend ----------------------------------------------------------


def _redis_pair() -> tuple[fakeaioredis.FakeRedis, fakeaioredis.FakeRedis]:
    """Two async clients sharing one in-memory server (≈ two replicas)."""
    server = fakeredis.FakeServer()
    return (
        fakeaioredis.FakeRedis(server=server, decode_responses=True),
        fakeaioredis.FakeRedis(server=server, decode_responses=True),
    )


async def test_redis_limit_is_shared_across_instances() -> None:
    a, b = _redis_pair()
    specs = [BucketSpec("minute", capacity=2, refill_rate=2 / 60)]
    clock = _FakeClock()  # frozen → no refill between calls
    limiter_a = RedisRateLimiter(a, specs, clock=clock)
    limiter_b = RedisRateLimiter(b, specs, clock=clock)

    # Instance A consumes the whole bucket.
    assert (await limiter_a.acquire("u")).allowed is True
    assert (await limiter_a.acquire("u")).allowed is True
    # Instance B sees the shared, exhausted bucket and rejects.
    blocked = await limiter_b.acquire("u")
    assert blocked.allowed is False
    assert blocked.retry_after > 0

    await a.aclose()
    await b.aclose()


async def test_redis_refills_after_advancing_clock() -> None:
    a, _ = _redis_pair()
    specs = [BucketSpec("minute", capacity=1, refill_rate=1.0)]
    clock = _FakeClock()
    limiter = RedisRateLimiter(a, specs, clock=clock)

    assert (await limiter.acquire("u")).allowed is True
    assert (await limiter.acquire("u")).allowed is False
    clock.advance(1.0)
    assert (await limiter.acquire("u")).allowed is True
    await a.aclose()


async def test_redis_fails_open_on_error() -> None:
    a, _ = _redis_pair()
    limiter = RedisRateLimiter(a, [BucketSpec("b", capacity=1, refill_rate=1.0)])

    async def _boom(*_args: object, **_kwargs: object) -> object:
        raise RedisError("connection lost")

    limiter._script = _boom  # type: ignore[assignment]
    # A Redis outage must not block tool calls.
    assert (await limiter.acquire("u")).allowed is True
    await a.aclose()


# --- settings → specs -------------------------------------------------------


def test_build_bucket_specs_from_settings() -> None:
    settings = Settings(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="p",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID="c",
        ALLOWED_COGNITO_CLIENT_IDS=["c"],
        RATE_LIMIT_PER_MINUTE=60,
        RATE_LIMIT_PER_HOUR=1000,
    )
    specs = build_bucket_specs(settings)
    by_name = {s.name: s for s in specs}
    assert by_name["minute"].capacity == 60
    assert by_name["minute"].refill_rate == pytest.approx(1.0)
    assert by_name["hour"].capacity == 1000
    assert by_name["hour"].refill_rate == pytest.approx(1000 / 3600)


def test_build_bucket_specs_skips_non_positive_limits() -> None:
    settings = Settings(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="p",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID="c",
        ALLOWED_COGNITO_CLIENT_IDS=["c"],
        RATE_LIMIT_PER_MINUTE=0,
        RATE_LIMIT_PER_HOUR=0,
    )
    assert build_bucket_specs(settings) == []
