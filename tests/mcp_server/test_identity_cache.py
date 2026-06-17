"""Unit tests for the cached identity resolver + two-tier cache (RD-63).

Covers positive caching (TTL'd so a changed email refreshes), negative caching
(shorter TTL), L1→L2→upstream ordering, L2 sharing across workers, fail-open on
lookup error, and a Redis outage falling through to the upstream lookup. The
underlying ``SelfIdentityResolver`` is stubbed (no dispatcher/AutoDSApi) and L2
uses an async fakeredis.
"""

import pytest
from fakeredis import aioredis
from pydantic import SecretStr

from autods_mcp_server.auth import UserContext
from autods_mcp_server.identity import (
    CachedIdentityResolver,
    SelfIdentity,
    build_identity_resolver,
)
from autods_mcp_server.settings import Settings


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _StubResolver:
    """Stands in for SelfIdentityResolver: records calls, returns a fixed result."""

    def __init__(self, result: SelfIdentity | None, *, raises: bool = False) -> None:
        self.result = result
        self.raises = raises
        self.calls = 0

    async def resolve(self, user_context: UserContext) -> SelfIdentity | None:
        self.calls += 1
        if self.raises:
            raise RuntimeError("upstream boom")
        return self.result


def _ctx(sub: str) -> UserContext:
    return UserContext(sub=sub, raw_token=SecretStr("tok"))


@pytest.fixture
def redis() -> aioredis.FakeRedis:
    return aioredis.FakeRedis(decode_responses=True)


def _resolver(stub, *, redis=None, clock=None, negative_ttl=21600, positive_ttl=86400) -> CachedIdentityResolver:
    return CachedIdentityResolver(
        stub,
        redis=redis,
        negative_ttl_seconds=negative_ttl,
        positive_ttl_seconds=positive_ttl,
        clock=clock or (lambda: 1000.0),
    )


async def test_positive_lookup_is_cached_in_l1(redis) -> None:
    stub = _StubResolver(SelfIdentity("999", "Alice", "alice@example.com"))
    resolver = _resolver(stub, redis=redis)

    first = await resolver.resolve(_ctx("sub-1"))
    second = await resolver.resolve(_ctx("sub-1"))

    assert first == SelfIdentity("999", "Alice", "alice@example.com")
    assert second == first
    assert stub.calls == 1  # second served from L1


async def test_positive_entry_has_ttl_in_redis(redis) -> None:
    stub = _StubResolver(SelfIdentity("999", "Alice", "alice@example.com"))
    resolver = _resolver(stub, redis=redis, positive_ttl=86400)

    await resolver.resolve(_ctx("sub-1"))

    ttl = await redis.ttl("mcp:identity:attrs:sub-1")
    assert 0 < ttl <= 86400


async def test_positive_l1_entry_expires_and_refreshes(redis) -> None:
    """A positive entry refreshes after its TTL so a changed email is re-fetched."""
    stub = _StubResolver(SelfIdentity("999", "Alice", "alice@example.com"))
    clock = _Clock()
    resolver = _resolver(stub, clock=clock, positive_ttl=86400)  # L1-only

    await resolver.resolve(_ctx("sub-1"))
    clock.now += 86401  # advance past the positive TTL
    await resolver.resolve(_ctx("sub-1"))

    assert stub.calls == 2  # re-fetched after expiry


async def test_negative_lookup_is_cached_with_ttl(redis) -> None:
    stub = _StubResolver(None)
    resolver = _resolver(stub, redis=redis, negative_ttl=3600)

    first = await resolver.resolve(_ctx("sub-2"))
    second = await resolver.resolve(_ctx("sub-2"))

    assert first is None
    assert second is None
    assert stub.calls == 1  # negative served from L1 the second time
    ttl = await redis.ttl("mcp:identity:attrs:sub-2")
    assert 0 < ttl <= 3600


async def test_negative_l1_entry_expires_and_retries(redis) -> None:
    stub = _StubResolver(None)
    clock = _Clock()
    resolver = _resolver(stub, clock=clock, negative_ttl=3600)  # L1-only

    await resolver.resolve(_ctx("sub-3"))
    clock.now += 3601  # advance past the negative TTL
    await resolver.resolve(_ctx("sub-3"))

    assert stub.calls == 2  # re-fetched after expiry


async def test_l2_hit_populates_l1_for_a_second_worker(redis) -> None:
    """A cold worker reads the positive entry from shared Redis (L2) without
    hitting the upstream."""
    warm = _resolver(_StubResolver(SelfIdentity("999", "Alice", "alice@example.com")), redis=redis)
    await warm.resolve(_ctx("sub-1"))

    cold_stub = _StubResolver(None)  # would return None if it ever ran
    cold = _resolver(cold_stub, redis=redis)
    result = await cold.resolve(_ctx("sub-1"))

    assert result == SelfIdentity("999", "Alice", "alice@example.com")
    assert cold_stub.calls == 0  # served from L2, upstream never touched


async def test_lookup_failure_fails_open(redis) -> None:
    stub = _StubResolver(None, raises=True)
    resolver = _resolver(stub, redis=redis)

    result = await resolver.resolve(_ctx("sub-4"))

    assert result is None  # fail open, no raise
    assert stub.calls == 1


async def test_redis_outage_falls_back_to_upstream() -> None:
    """An L2 error must not block the request — it falls through to the upstream."""

    class _BrokenRedis:
        async def get(self, *_a, **_k):
            raise ConnectionError("redis down")

        async def set(self, *_a, **_k):
            raise ConnectionError("redis down")

    stub = _StubResolver(SelfIdentity("999", "Alice", "alice@example.com"))
    resolver = _resolver(stub, redis=_BrokenRedis())

    result = await resolver.resolve(_ctx("sub-6"))

    assert result == SelfIdentity("999", "Alice", "alice@example.com")
    assert stub.calls == 1


def test_build_identity_resolver_reads_ttls_from_settings(env) -> None:
    env(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="us-west-2_TESTPOOL",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID="c",
        ALLOWED_COGNITO_CLIENT_IDS='["c"]',
        COGNITO_ATTR_NEGATIVE_CACHE_TTL_SECONDS="60",
        COGNITO_ATTR_POSITIVE_CACHE_TTL_SECONDS="120",
    )
    resolver = build_identity_resolver(Settings(), redis=None, resolver=_StubResolver(None))  # type: ignore[call-arg]
    assert resolver._negative_ttl == 60
    assert resolver._positive_ttl == 120
