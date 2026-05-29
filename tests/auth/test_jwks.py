"""B1 acceptance — JWKS cache, refresh-on-kid-miss, rotation, TTL, throttling."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from autods_mcp_server.auth.exceptions import JWKSUnavailable, UnknownKid
from autods_mcp_server.auth.jwks import JWKSClient, get_jwks_client
from autods_mcp_server.settings import Settings
from tests.auth.conftest import TEST_JWKS_URL, SigningKey


def _stub_fetcher(*payloads: dict[str, Any]):
    """Return a fetcher that yields each payload in order; counts calls."""
    state = {"calls": 0}
    queue = list(payloads)

    async def fetch(_url: str) -> dict[str, Any]:
        state["calls"] += 1
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    return fetch, state


def _fake_clock():
    now = {"t": 0.0}

    def clock() -> float:
        return now["t"]

    def advance(delta: float) -> None:
        now["t"] += delta

    return clock, advance


async def test_first_call_fetches_then_cache_hit(make_jwks, signing_key: SigningKey) -> None:
    fetch, state = _stub_fetcher(make_jwks(signing_key))
    client = JWKSClient(jwks_url=TEST_JWKS_URL, fetcher=fetch)

    first = await client.get_key(signing_key.kid)
    second = await client.get_key(signing_key.kid)

    assert first["kid"] == signing_key.kid
    assert second is first  # same dict from cache
    assert state["calls"] == 1


async def test_unknown_kid_triggers_refresh_then_raises(make_jwks, signing_key: SigningKey) -> None:
    clock, advance = _fake_clock()
    fetch, state = _stub_fetcher(make_jwks(signing_key))
    client = JWKSClient(jwks_url=TEST_JWKS_URL, fetcher=fetch, clock=clock, min_refresh_interval=10.0)

    # Prime the cache so the next call is "fresh but missing".
    await client.get_key(signing_key.kid)
    assert state["calls"] == 1

    # Advance past the throttle window so the miss can drive a refresh.
    advance(15.0)
    with pytest.raises(UnknownKid):
        await client.get_key("nonexistent-kid")
    # Refreshed once on the miss, even though TTL hadn't expired.
    assert state["calls"] == 2


async def test_unknown_kid_within_throttle_window_does_not_refresh(make_jwks, signing_key: SigningKey) -> None:
    """Repeated unknown kids inside the throttle window must NOT amplify into fetches."""
    clock, advance = _fake_clock()
    fetch, state = _stub_fetcher(make_jwks(signing_key))
    client = JWKSClient(jwks_url=TEST_JWKS_URL, fetcher=fetch, clock=clock, min_refresh_interval=30.0)

    await client.get_key(signing_key.kid)
    assert state["calls"] == 1

    # Several unknown-kid requests within the throttle window: each must
    # raise UnknownKid without triggering a new fetch.
    advance(5.0)
    for kid in ("kid-a", "kid-b", "kid-c"):
        with pytest.raises(UnknownKid):
            await client.get_key(kid)
    assert state["calls"] == 1


async def test_key_rotation_refresh_picks_up_new_kid(
    make_jwks, signing_key: SigningKey, rotated_signing_key: SigningKey
) -> None:
    # First fetch publishes the primary key only; second fetch
    # publishes the rotated key alongside it.
    clock, advance = _fake_clock()
    fetch, state = _stub_fetcher(
        make_jwks(signing_key),
        make_jwks(signing_key, rotated_signing_key),
    )
    client = JWKSClient(jwks_url=TEST_JWKS_URL, fetcher=fetch, clock=clock, min_refresh_interval=10.0)

    await client.get_key(signing_key.kid)
    assert state["calls"] == 1

    # Advance past the throttle window so the unknown rotated kid can
    # drive a refresh.
    advance(15.0)
    rotated = await client.get_key(rotated_signing_key.kid)
    assert rotated["kid"] == rotated_signing_key.kid
    assert state["calls"] == 2


async def test_ttl_expiry_triggers_refresh(make_jwks, signing_key: SigningKey) -> None:
    clock, advance = _fake_clock()
    fetch, state = _stub_fetcher(make_jwks(signing_key))
    client = JWKSClient(jwks_url=TEST_JWKS_URL, fetcher=fetch, ttl_seconds=60, clock=clock, min_refresh_interval=10.0)

    await client.get_key(signing_key.kid)
    assert state["calls"] == 1

    # Within TTL → no extra fetch.
    advance(30.0)
    await client.get_key(signing_key.kid)
    assert state["calls"] == 1

    # Past TTL → fetches again, even though the kid is in the cache.
    advance(31.0)  # t=61
    await client.get_key(signing_key.kid)
    assert state["calls"] == 2


async def test_concurrent_misses_coalesce_into_one_fetch(make_jwks, signing_key: SigningKey) -> None:
    fetch_started = asyncio.Event()
    fetch_release = asyncio.Event()
    call_count = {"n": 0}

    async def slow_fetch(_url: str) -> dict[str, Any]:
        call_count["n"] += 1
        fetch_started.set()
        await fetch_release.wait()
        return make_jwks(signing_key)

    client = JWKSClient(jwks_url=TEST_JWKS_URL, fetcher=slow_fetch)

    task_a = asyncio.create_task(client.get_key(signing_key.kid))
    task_b = asyncio.create_task(client.get_key(signing_key.kid))

    await fetch_started.wait()
    fetch_release.set()

    a, b = await asyncio.gather(task_a, task_b)
    assert a["kid"] == signing_key.kid
    assert b["kid"] == signing_key.kid
    assert call_count["n"] == 1


async def test_malformed_jwks_payload_raises_jwks_unavailable(signing_key: SigningKey) -> None:
    fetch, _ = _stub_fetcher({"not_keys": []})
    client = JWKSClient(jwks_url=TEST_JWKS_URL, fetcher=fetch)
    with pytest.raises(JWKSUnavailable):
        await client.get_key(signing_key.kid)


async def test_httpx_failure_raises_jwks_unavailable(signing_key: SigningKey) -> None:
    """Transient JWKS fetch errors must surface as JWKSUnavailable (not InvalidSignature)."""

    async def failing_fetch(_url: str) -> dict[str, Any]:
        raise httpx.ConnectError("connection refused")

    client = JWKSClient(jwks_url=TEST_JWKS_URL, fetcher=failing_fetch)
    with pytest.raises(JWKSUnavailable):
        await client.get_key(signing_key.kid)


async def test_json_decode_failure_raises_jwks_unavailable(signing_key: SigningKey) -> None:
    """A non-JSON body from Cognito (httpx.Response.json() → JSONDecodeError) is JWKSUnavailable."""

    async def bad_json_fetch(_url: str) -> dict[str, Any]:
        raise json.JSONDecodeError("Expecting value", "<not json>", 0)

    client = JWKSClient(jwks_url=TEST_JWKS_URL, fetcher=bad_json_fetch)
    with pytest.raises(JWKSUnavailable):
        await client.get_key(signing_key.kid)


async def test_failing_fetches_are_rate_limited(signing_key: SigningKey) -> None:
    """A failing JWKS endpoint must NOT be re-fetched inside the throttle window.

    Otherwise a Cognito outage turns each incoming token into a fresh
    blocking httpx call — amplifying outage into self-DoS.
    """
    clock, advance = _fake_clock()
    call_count = {"n": 0}

    async def failing_fetch(_url: str) -> dict[str, Any]:
        call_count["n"] += 1
        raise httpx.ConnectError("cognito unreachable")

    client = JWKSClient(
        jwks_url=TEST_JWKS_URL,
        fetcher=failing_fetch,
        clock=clock,
        min_refresh_interval=30.0,
    )

    # First call: actual fetch attempt → JWKSUnavailable.
    with pytest.raises(JWKSUnavailable):
        await client.get_key(signing_key.kid)
    assert call_count["n"] == 1

    # Inside the throttle window: still JWKSUnavailable, but NO new fetch.
    advance(5.0)
    with pytest.raises(JWKSUnavailable):
        await client.get_key(signing_key.kid)
    assert call_count["n"] == 1

    # After the throttle window: a fresh attempt is allowed.
    advance(30.0)  # t = 35
    with pytest.raises(JWKSUnavailable):
        await client.get_key(signing_key.kid)
    assert call_count["n"] == 2


async def test_throttled_with_prior_success_raises_unknown_kid(make_jwks, signing_key: SigningKey) -> None:
    """If we DO have cached keys from a prior success, throttled-miss is UnknownKid.

    The diagnostic distinction matters: empty-cache-throttled is a server
    problem (503); has-keys-but-not-this-kid-throttled is the same as
    the regular unknown-kid case (401).
    """
    clock, advance = _fake_clock()
    fetch, state = _stub_fetcher(make_jwks(signing_key))
    client = JWKSClient(jwks_url=TEST_JWKS_URL, fetcher=fetch, clock=clock, min_refresh_interval=30.0)

    # Prime the cache.
    await client.get_key(signing_key.kid)
    assert state["calls"] == 1

    # Inside throttle window, unknown kid → UnknownKid (not JWKSUnavailable).
    advance(5.0)
    with pytest.raises(UnknownKid):
        await client.get_key("nonexistent-kid")
    assert state["calls"] == 1


async def test_force_refresh_ignores_throttle(make_jwks, signing_key: SigningKey) -> None:
    """`refresh()` is the explicit-control path and must always fetch."""
    clock, _ = _fake_clock()
    fetch, state = _stub_fetcher(make_jwks(signing_key))
    client = JWKSClient(jwks_url=TEST_JWKS_URL, fetcher=fetch, clock=clock, min_refresh_interval=60.0)

    await client.refresh()
    await client.refresh()  # still inside the throttle window
    assert state["calls"] == 2


async def test_default_fetcher_calls_cognito_via_httpx(httpx_mock, make_jwks, signing_key: SigningKey) -> None:
    """Cover the production HTTP path — `JWKSClient` constructed without a fetcher
    must fetch via `httpx.AsyncClient` and parse the JWKS document."""
    httpx_mock.add_response(url=TEST_JWKS_URL, method="GET", json=make_jwks(signing_key))

    client = JWKSClient(jwks_url=TEST_JWKS_URL)
    key = await client.get_key(signing_key.kid)

    assert key["kid"] == signing_key.kid


async def test_default_fetcher_raises_jwks_unavailable_on_http_error(httpx_mock, signing_key: SigningKey) -> None:
    """A 5xx from Cognito must surface as `JWKSUnavailable` (via httpx's
    `raise_for_status`), not as a leaked `HTTPStatusError`."""
    httpx_mock.add_response(url=TEST_JWKS_URL, method="GET", status_code=503)

    client = JWKSClient(jwks_url=TEST_JWKS_URL)
    with pytest.raises(JWKSUnavailable):
        await client.get_key(signing_key.kid)


def test_get_jwks_client_is_thread_safe_under_concurrent_first_calls() -> None:
    """Concurrent first-time `get_jwks_client` calls (e.g., from FastAPI's
    sync-dep thread pool) must all observe the same instance — no two
    requests should end up with different `JWKSClient`s racing on the same URL."""
    from concurrent.futures import ThreadPoolExecutor

    from autods_mcp_server.auth.jwks import reset_jwks_client

    settings = Settings(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="us-west-2_RACEPOOL",
        COGNITO_REGION="us-west-2",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID="public-client",
        ALLOWED_COGNITO_CLIENT_IDS=["public-client"],
    )
    reset_jwks_client()

    with ThreadPoolExecutor(max_workers=16) as pool:
        clients = list(pool.map(lambda _: get_jwks_client(settings), range(64)))

    # All callers see the same singleton — no losing-thread client leaked through.
    assert len({id(c) for c in clients}) == 1


def test_get_jwks_client_caches_per_url() -> None:
    """Different settings → different client; same settings → same instance."""
    settings_a = Settings(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="us-west-2_POOLA",
        COGNITO_REGION="us-west-2",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID="public-client",
        ALLOWED_COGNITO_CLIENT_IDS=["public-client"],
    )
    settings_b = Settings(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="us-west-2_POOLB",
        COGNITO_REGION="us-west-2",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID="public-client",
        ALLOWED_COGNITO_CLIENT_IDS=["public-client"],
    )

    client_a = get_jwks_client(settings_a)
    client_b = get_jwks_client(settings_b)
    client_a_again = get_jwks_client(settings_a)

    assert client_a is not client_b
    assert client_a.jwks_url != client_b.jwks_url
    assert client_a_again is client_a
