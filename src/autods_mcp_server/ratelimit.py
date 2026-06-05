"""Per-user token-bucket rate limiting (F1).

Each user (keyed by ``user.sub``) is limited by *two* token buckets that apply
simultaneously — a call is allowed only if it fits under **both** the
per-minute and the per-hour ceiling. A token bucket (rather than a fixed
window) gives smooth refill and a meaningful ``retry_after``: the caller learns
exactly how long until the next token is available.

Two backends share one algorithm:

* :class:`RedisRateLimiter` — the production path. The bucket check-and-consume
  runs as a single Lua script so it is atomic across all replicas (the whole
  point of F0's shared Redis). On a Redis outage it **fails open** — losing
  rate limiting for the duration is preferable to failing every tool call.
* :class:`InMemoryRateLimiter` — the local-dev fallback when ``REDIS_URL`` is
  unset. Same math, process-local state, an ``asyncio.Lock`` for atomicity.

The pure :func:`evaluate_buckets` function holds the bucket math so it can be
unit-tested directly; the in-memory backend calls it, and the Lua script
mirrors it line-for-line.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from redis.asyncio import Redis
from redis.exceptions import RedisError

from autods_mcp_server.logging import get_logger
from autods_mcp_server.settings import Settings

_logger = get_logger("autods_mcp_server.ratelimit")

# Redis key namespace for buckets, so they're easy to spot / flush and never
# collide with anything else sharing the instance.
_KEY_PREFIX = "mcp:rl"


@dataclass(frozen=True)
class BucketSpec:
    """One token bucket: ``capacity`` tokens, refilling at ``refill_rate``/sec."""

    name: str
    capacity: float
    refill_rate: float  # tokens per second


@dataclass
class RateLimitResult:
    """Outcome of one ``acquire`` — ``retry_after`` is seconds until a retry
    could succeed (0 when allowed)."""

    allowed: bool
    retry_after: float = 0.0


@dataclass
class _BucketState:
    tokens: float
    ts: float


def evaluate_buckets(
    states: dict[str, _BucketState],
    specs: list[BucketSpec],
    now: float,
    cost: float = 1.0,
) -> tuple[bool, dict[str, _BucketState], float]:
    """Refill every bucket to ``now``, then allow iff *all* hold ``cost`` tokens.

    Returns ``(allowed, new_states, retry_after)``. ``new_states`` always
    reflects the refill (and the deduction when allowed) so the caller can
    persist it. ``retry_after`` is the max wait across deficient buckets.
    """
    refilled: dict[str, float] = {}
    allowed = True
    retry_after = 0.0
    for spec in specs:
        state = states.get(spec.name)
        tokens = spec.capacity if state is None else state.tokens
        ts = now if state is None else state.ts
        elapsed = max(0.0, now - ts)
        tokens = min(spec.capacity, tokens + elapsed * spec.refill_rate)
        refilled[spec.name] = tokens
        if tokens < cost:
            allowed = False
            retry_after = max(retry_after, (cost - tokens) / spec.refill_rate)

    new_states = {
        spec.name: _BucketState(tokens=refilled[spec.name] - (cost if allowed else 0.0), ts=now) for spec in specs
    }
    return allowed, new_states, retry_after


class RateLimiter:
    """Base interface: consume one token for ``key`` across all buckets."""

    async def acquire(self, key: str) -> RateLimitResult:  # pragma: no cover - interface
        raise NotImplementedError


class InMemoryRateLimiter(RateLimiter):
    """Process-local token buckets. Local-dev fallback only — state is not
    shared across workers, so it must never run in a multi-replica deploy."""

    def __init__(self, specs: list[BucketSpec], *, clock: Callable[[], float] = time.monotonic) -> None:
        self._specs = specs
        self._clock = clock
        self._states: dict[str, dict[str, _BucketState]] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: str) -> RateLimitResult:
        if not self._specs:
            return RateLimitResult(allowed=True)
        async with self._lock:
            now = self._clock()
            allowed, new_states, retry_after = evaluate_buckets(self._states.get(key, {}), self._specs, now)
            self._states[key] = new_states
            return RateLimitResult(allowed=allowed, retry_after=retry_after)


# Atomic multi-bucket check-and-consume, loaded from ``ratelimit.lua`` (kept in
# its own file so editors lint/highlight it as Lua). It mirrors
# ``evaluate_buckets`` line-for-line; see that file's header for the
# KEYS/ARGV/return contract.
_BUCKET_LUA = (Path(__file__).resolve().parent / "ratelimit.lua").read_text(encoding="utf-8")


class RedisRateLimiter(RateLimiter):
    """Shared token buckets in Redis. Atomic per call via a Lua script;
    fails open (allows the call) if Redis is unreachable."""

    def __init__(self, redis: Redis, specs: list[BucketSpec], *, clock: Callable[[], float] = time.time) -> None:
        self._redis = redis
        self._specs = specs
        self._clock = clock
        # register_script handles the EVALSHA→EVAL (NOSCRIPT) fallback for us.
        self._script = redis.register_script(_BUCKET_LUA)

    async def acquire(self, key: str) -> RateLimitResult:
        if not self._specs:
            return RateLimitResult(allowed=True)
        keys = [f"{_KEY_PREFIX}:{key}:{spec.name}" for spec in self._specs]
        args: list[float | str] = [self._clock(), 1]
        for spec in self._specs:
            args.extend((spec.capacity, spec.refill_rate))
        try:
            allowed_flag, retry_ms = await self._script(keys=keys, args=args)
        except RedisError as exc:
            # Fail open: a Redis blip must not take down every tool call.
            _logger.warning("rate_limiter_redis_unavailable", error=str(exc))
            return RateLimitResult(allowed=True)
        return RateLimitResult(allowed=bool(int(allowed_flag)), retry_after=int(retry_ms) / 1000.0)


def build_bucket_specs(settings: Settings) -> list[BucketSpec]:
    """Derive the per-minute and per-hour buckets from settings.

    A non-positive ceiling disables that bucket (treated as unlimited), so a
    zero in config never produces a divide-by-zero refill rate.
    """
    specs: list[BucketSpec] = []
    if settings.rate_limit_per_minute > 0:
        specs.append(BucketSpec("minute", settings.rate_limit_per_minute, settings.rate_limit_per_minute / 60.0))
    if settings.rate_limit_per_hour > 0:
        specs.append(BucketSpec("hour", settings.rate_limit_per_hour, settings.rate_limit_per_hour / 3600.0))
    return specs


def build_rate_limiter(settings: Settings, redis: Redis | None) -> RateLimiter:
    """Pick the Redis-backed limiter when a client is configured, else the
    in-process fallback (local dev only — the settings validator forbids a
    missing ``REDIS_URL`` in staging/prod)."""
    specs = build_bucket_specs(settings)
    if redis is not None:
        return RedisRateLimiter(redis, specs)
    return InMemoryRateLimiter(specs)
