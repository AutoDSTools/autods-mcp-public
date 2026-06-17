"""Self-identity resolution (RD-68).

Resolves the **caller's own** AutoDS identity — user id, name, email — by calling
the AutoDSApi ``GET /users/list/`` operation with the caller's own forwarded
bearer token. Despite its name, that endpoint returns just the authenticated
user (a single-element list), so it acts as a "get me" lookup that needs no
privileged credentials: the server simply reuses the token it already forwards.

This is the identity source other features build on (e.g. analytics / log
enrichment). It reuses the :class:`OperationDispatcher` — same token forwarding,
upstream routing, and response parsing as any tool call — and **fails open**:
any dispatch error, non-2xx response, or malformed payload resolves to ``None``
and is logged, never raised, so identity resolution can never break auth or a
tool call.
"""

import json
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from autods_mcp_server.auth import UserContext
from autods_mcp_server.dispatch import DispatchError, OperationDispatcher
from autods_mcp_server.logging import get_logger
from autods_mcp_server.settings import Settings

_logger = get_logger("autods_mcp_server.identity")

# The manifest operation id for AutoDSApi ``GET /users/list/`` (see
# ``manifests/users.json``) — the "get me" lookup this resolver dispatches.
SELF_IDENTITY_OPERATION_ID = "get_current_user"


@dataclass(frozen=True)
class SelfIdentity:
    """The caller's own identity, as resolved from AutoDSApi."""

    user_id: str
    name: str | None = None
    email: str | None = None


class SelfIdentityResolver:
    """Resolve the caller's own ``user_id`` / ``name`` / ``email`` from AutoDSApi."""

    def __init__(
        self,
        dispatcher: OperationDispatcher,
        *,
        operation_id: str = SELF_IDENTITY_OPERATION_ID,
    ) -> None:
        self._dispatcher = dispatcher
        self._operation_id = operation_id

    async def resolve(self, user_context: UserContext) -> SelfIdentity | None:
        """Resolve ``user_context``'s own identity, or ``None`` (fail-open).

        Dispatches the ``get_current_user`` operation with the caller's forwarded
        token. **Never raises**: a dispatch failure, a non-2xx upstream response
        (e.g. a blocked account), or a payload we can't parse all degrade to
        ``None`` rather than propagating into the request path.
        """
        try:
            result = await self._dispatcher.dispatch(self._operation_id, {}, user_context)
        except DispatchError as exc:
            _logger.warning("self_identity_dispatch_failed", error=str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 — resolution must never raise into auth/tool calls.
            _logger.warning("self_identity_unexpected_error", error=str(exc))
            return None

        if not result.ok:
            # A non-2xx (auth/permission/upstream) — don't surface it, just skip.
            _logger.warning("self_identity_lookup_non_2xx", status=result.status)
            return None

        return self._parse(result.data)

    @staticmethod
    def _parse(data: Any) -> SelfIdentity | None:
        """Extract the identity from the upstream payload (fail-open on shape).

        ``GET /users/list/`` returns the caller as a single-element list —
        ``[{"id": ..., "name": ..., "email": ...}]`` — but we also accept a bare
        object defensively. Anything else (empty, missing id, wrong type) yields
        ``None``.
        """
        record: Any = None
        if isinstance(data, list):
            record = data[0] if data else None
        elif isinstance(data, dict):
            record = data
        if not isinstance(record, dict):
            return None

        raw_id = record.get("id")
        if raw_id is None:
            return None

        return SelfIdentity(
            user_id=str(raw_id),
            name=record.get("name"),
            email=record.get("email"),
        )


# --- cached resolver (RD-63) -----------------------------------------------

_KEY_PREFIX = "mcp:identity:attrs"

# Hard cap on the in-process (L1) cache so an open-ended user population can't
# grow it without bound; least-recently-used entries are evicted past this.
_DEFAULT_MAX_L1_ENTRIES = 10_000

# Sentinel distinguishing "not in cache" from "cached as a negative (None)".
_MISS = object()


@dataclass
class _L1Entry:
    identity: SelfIdentity | None  # None == negative
    expires_at: float  # both positive and negative entries expire (different TTLs)


class CachedIdentityResolver:
    """Two-tier cache over :class:`SelfIdentityResolver`, keyed by Cognito ``sub``.

    The expensive part of resolving a caller's identity is the upstream
    ``get_current_user`` call. This caches the result so warm requests never hit
    AutoDSApi again, mirroring the rate limiter's posture:

    * **L1** — an in-process dict (per worker), the hot path.
    * **L2** — shared Redis (``mcp:identity:attrs:{sub}``), so the lookup is
      shared across replicas and survives a worker restart. Absent in local dev
      (no ``REDIS_URL``), where the resolver runs L1-only.

    The ``sub`` → ``user_id`` mapping is immutable, but the cached ``email`` /
    ``name`` are not (a user can change them), so **positive entries are cached
    with a TTL** (default 24h) and refreshed on expiry — bounding how stale a
    logged value can be. **Negative entries** (unresolved user or a transient
    lookup failure) are cached with a shorter TTL (default 6h) so
    not-yet-resolvable users and transient errors get retried sooner.

    Everything **fails open**: any error resolves to ``None`` and is logged,
    never raised. There are deliberately **no stampede locks**: concurrent cold
    lookups for the same ``sub`` may issue duplicate upstream calls, acceptable
    at this volume.
    """

    def __init__(
        self,
        resolver: SelfIdentityResolver,
        *,
        redis: Redis | None = None,
        negative_ttl_seconds: int = 21600,
        positive_ttl_seconds: int = 86400,
        clock: Callable[[], float] = time.monotonic,
        max_l1_entries: int = _DEFAULT_MAX_L1_ENTRIES,
    ) -> None:
        self._resolver = resolver
        self._redis = redis
        self._negative_ttl = negative_ttl_seconds
        self._positive_ttl = positive_ttl_seconds
        self._clock = clock
        self._max_l1_entries = max_l1_entries
        # OrderedDict so we can evict least-recently-used entries once the cache
        # is full (insertion/access order == LRU order).
        self._l1: OrderedDict[str, _L1Entry] = OrderedDict()

    async def resolve(self, user_context: UserContext) -> SelfIdentity | None:
        """Resolve ``user_context``'s identity (cache key ``sub``), fail-open.

        **Never raises** — auth and tool calls depend on this, so any unexpected
        error degrades to ``None`` rather than propagating into the request path.
        """
        try:
            return await self._resolve(user_context)
        except Exception as exc:  # noqa: BLE001 — the resolver must never raise into auth.
            _logger.warning("identity_resolve_failed", sub=user_context.sub, error=str(exc))
            return None

    async def _resolve(self, user_context: UserContext) -> SelfIdentity | None:
        sub = user_context.sub
        l1 = self._l1_get(sub)
        if l1 is not _MISS:
            return l1  # type: ignore[return-value]

        l2 = await self._l2_get(sub)
        if l2 is not _MISS:
            self._l1_put(sub, l2)  # type: ignore[arg-type]
            return l2  # type: ignore[return-value]

        identity = await self._resolver.resolve(user_context)
        await self._l2_put(sub, identity)
        self._l1_put(sub, identity)
        return identity

    # --- L1 (in-process) ----------------------------------------------------

    def _l1_get(self, sub: str) -> SelfIdentity | None | object:
        entry = self._l1.get(sub)
        if entry is None:
            return _MISS
        if self._clock() >= entry.expires_at:
            del self._l1[sub]
            return _MISS
        self._l1.move_to_end(sub)  # mark as most-recently-used
        return entry.identity

    def _l1_put(self, sub: str, identity: SelfIdentity | None) -> None:
        ttl = self._positive_ttl if identity is not None else self._negative_ttl
        self._l1[sub] = _L1Entry(identity=identity, expires_at=self._clock() + ttl)
        self._l1.move_to_end(sub)  # most-recently-used
        # Bound the cache: drop least-recently-used entries once over capacity.
        while len(self._l1) > self._max_l1_entries:
            self._l1.popitem(last=False)

    # --- L2 (Redis) ---------------------------------------------------------

    def _redis_key(self, sub: str) -> str:
        return f"{_KEY_PREFIX}:{sub}"

    async def _l2_get(self, sub: str) -> SelfIdentity | None | object:
        if self._redis is None:
            return _MISS
        try:
            raw = await self._redis.get(self._redis_key(sub))
        except Exception as exc:  # noqa: BLE001 — a Redis blip skips L2, never fails the request.
            _logger.warning("identity_l2_get_failed", sub=sub, error=str(exc))
            return _MISS
        if raw is None:
            return _MISS
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return _MISS
        if payload.get("neg"):
            return None
        user_id = payload.get("user_id")
        if not user_id:
            # Malformed / foreign positive entry — treat as a miss so we re-fetch
            # and repopulate rather than raising into the request path.
            return _MISS
        return SelfIdentity(user_id=user_id, name=payload.get("name"), email=payload.get("email"))

    async def _l2_put(self, sub: str, identity: SelfIdentity | None) -> None:
        if self._redis is None:
            return
        try:
            if identity is None:
                # Negative: expire so not-yet-resolvable users / transient errors retry.
                await self._redis.set(self._redis_key(sub), json.dumps({"neg": True}), ex=self._negative_ttl)
            else:
                # Positive: TTL'd so a changed email/name refreshes (the id is
                # immutable but the cached email/name are not).
                await self._redis.set(
                    self._redis_key(sub),
                    json.dumps({"user_id": identity.user_id, "name": identity.name, "email": identity.email}),
                    ex=self._positive_ttl,
                )
        except Exception as exc:  # noqa: BLE001 — a Redis write failure must not fail the request.
            _logger.warning("identity_l2_put_failed", sub=sub, error=str(exc))


def build_identity_resolver(
    settings: Settings,
    redis: Redis | None,
    resolver: SelfIdentityResolver,
) -> CachedIdentityResolver:
    """Build the cached identity resolver from settings, sharing the runtime's Redis (L2)."""
    return CachedIdentityResolver(
        resolver,
        redis=redis,
        negative_ttl_seconds=settings.cognito_attr_negative_cache_ttl_seconds,
        positive_ttl_seconds=settings.cognito_attr_positive_cache_ttl_seconds,
    )
