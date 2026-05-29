"""Async JWKS client for Cognito with TTL cache, refresh-on-kid-miss, and rate limit.

The cache has two refresh triggers:

1. **TTL expiry** — every ``ttl_seconds`` the next ``get_key`` call
   re-fetches the JWKS document. This is the steady-state heartbeat
   that surfaces routine key rotation.
2. **Unknown kid** — if a token references a ``kid`` we don't have,
   we refresh immediately so a new signing key Cognito just published
   doesn't have to wait for TTL expiry to be picked up.

Both triggers are rate-limited by ``min_refresh_interval``: every
refresh **attempt** (success or failure) starts a new throttle window,
so unknown-kid bursts can't amplify into outbound Cognito fetches and
a struggling Cognito can't amplify into 10s-per-request stalls on this
side. The worst-case fetch rate is bounded to ``1 / min_refresh_interval``.

When a request arrives during the throttle window and we've never had
a successful fetch, we surface ``JWKSUnavailable`` rather than
``UnknownKid`` — the cache being empty is a server-side condition, not
a "your token is bad" condition.
"""

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from jwt.algorithms import JWKDict

from autods_mcp_server.auth.exceptions import JWKSUnavailable, UnknownKid
from autods_mcp_server.settings import Settings

HttpFetcher = Callable[[str], Awaitable[dict[str, Any]]]

DEFAULT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_MIN_REFRESH_INTERVAL_SECONDS = 30.0


async def _default_http_fetch(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


class JWKSClient:
    """Caches JWKS keys by ``kid`` with TTL + refresh-on-kid-miss + rate-limit.

    Concurrency: a single ``asyncio.Lock`` serializes refreshes so a
    burst of misses produces one HTTP fetch instead of N. The
    fast-path (cache hit, TTL fresh) does not touch the lock.
    """

    def __init__(
        self,
        *,
        jwks_url: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        min_refresh_interval: float = DEFAULT_MIN_REFRESH_INTERVAL_SECONDS,
        fetcher: HttpFetcher | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._jwks_url = jwks_url
        self._ttl = ttl_seconds
        self._min_refresh_interval = min_refresh_interval
        self._fetch = fetcher or _default_http_fetch
        self._clock = clock
        self._cache: dict[str, JWKDict] = {}
        # Last *successful* fetch — drives TTL freshness.
        self._fetched_at: float | None = None
        # Last refresh *attempt* (success OR failure) — drives the rate
        # limit. Tracked separately so a Cognito outage doesn't bypass
        # the throttle just because no fetch ever succeeded.
        self._last_attempt_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def jwks_url(self) -> str:
        return self._jwks_url

    def _is_fresh(self) -> bool:
        return self._fetched_at is not None and (self._clock() - self._fetched_at) < self._ttl

    def _can_refresh(self) -> bool:
        """True if we've never attempted, or the min-refresh interval has elapsed.

        This is the rate-limit gate: it caps outbound JWKS fetches at
        ``1 / min_refresh_interval`` regardless of how many unknown-kid
        misses arrive AND regardless of whether prior attempts failed.
        """
        if self._last_attempt_at is None:
            return True
        return (self._clock() - self._last_attempt_at) >= self._min_refresh_interval

    async def get_key(self, kid: str) -> JWKDict:
        """Return the JWK for ``kid``, refreshing on miss/TTL expiry (rate-limited)."""
        if self._is_fresh() and kid in self._cache:
            return self._cache[kid]

        async with self._lock:
            # Re-check after acquiring the lock — a sibling coroutine
            # may have refreshed while we were waiting.
            if self._is_fresh() and kid in self._cache:
                return self._cache[kid]
            if self._can_refresh():
                await self._refresh()
            elif self._fetched_at is None:
                # We're inside the throttle window AND have never had a
                # successful fetch. Don't lie with UnknownKid — the
                # cache is empty because Cognito hasn't responded, not
                # because the kid is bogus. Surface as 503 upstream.
                raise JWKSUnavailable(
                    f"JWKS unavailable at {self._jwks_url}: refresh throttled "
                    f"and no prior successful fetch (retry after "
                    f"{self._min_refresh_interval}s)"
                )
            if kid not in self._cache:
                raise UnknownKid(f"kid={kid!r} not present in JWKS at {self._jwks_url}")
            return self._cache[kid]

    async def refresh(self) -> None:
        """Force a refresh from the JWKS endpoint (ignores the rate limit)."""
        async with self._lock:
            await self._refresh()

    async def _refresh(self) -> None:
        # Stamp the attempt *before* the fetch so a slow/failing Cognito
        # doesn't extend the throttle window by however long it took to
        # time out.
        self._last_attempt_at = self._clock()
        try:
            payload = await self._fetch(self._jwks_url)
        except (httpx.HTTPError, ValueError) as exc:
            # ValueError covers json.JSONDecodeError from httpx.Response.json()
            # when Cognito returns a non-JSON body (rare but observed in outages).
            raise JWKSUnavailable(f"Failed to fetch JWKS from {self._jwks_url}: {exc}") from exc
        keys = payload.get("keys")
        if not isinstance(keys, list):
            raise JWKSUnavailable(f"Malformed JWKS document at {self._jwks_url}: missing 'keys' list")
        # Replace, don't merge: a rotation can both add and remove kids
        # and we want the cache to mirror what Cognito currently
        # publishes.
        self._cache = {k["kid"]: k for k in keys if "kid" in k}
        self._fetched_at = self._clock()


_default_clients: dict[str, JWKSClient] = {}
# Guards _default_clients against concurrent first-time construction.
# FastAPI dispatches sync dependencies (incl. jwks_dependency) on the
# anyio thread pool, so two requests for a never-seen URL can land here
# from different threads. `threading.Lock` (not asyncio.Lock) is the
# right primitive because this function is sync.
_default_clients_lock = threading.Lock()


def get_jwks_client(settings: Settings) -> JWKSClient:
    """Lazy module-level cache, keyed by JWKS URL.

    Keying by URL (not just "is there one yet?") means a caller passing
    different settings — different pool/region — gets a different client.
    Same-settings repeat calls still return the same instance, so the
    in-process cache and rate limit stay shared across requests.
    """
    url = settings.cognito_jwks_url
    # Fast path: dict.get is atomic under the GIL; no lock needed for
    # the steady-state hit.
    client = _default_clients.get(url)
    if client is not None:
        return client
    with _default_clients_lock:
        # Re-check after acquiring the lock: a sibling thread may have
        # constructed the client while we were waiting.
        client = _default_clients.get(url)
        if client is None:
            client = JWKSClient(jwks_url=url)
            _default_clients[url] = client
        return client


def reset_jwks_client() -> None:
    """Test hook: drop all cached clients so the next get returns a fresh one."""
    with _default_clients_lock:
        _default_clients.clear()
