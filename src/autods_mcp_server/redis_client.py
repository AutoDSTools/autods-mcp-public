"""Async Redis client factory (F0).

Production runs 2–10 replicas, so any cross-request state must be shared
rather than per-process. Redis is the shared store. For now the *only*
consumer is the per-user rate limiter (F1) — there is no shared MCP session
state, because the transport runs stateless (see ``mcp_transport``).

``create_redis`` builds a lazily-connecting client from ``settings.redis_url``.
``redis.asyncio.from_url`` does not open a socket until the first command, so
it is safe to construct synchronously at boot (inside ``build_runtime``) and
close in the app lifespan. When ``redis_url`` is unset (local dev only — the
settings validator forbids it in staging/prod) this returns ``None`` and the
caller falls back to the in-process limiter.
"""

from redis.asyncio import Redis

from autods_mcp_server.settings import Settings


def create_redis(settings: Settings) -> Redis | None:
    """Construct the shared async Redis client, or ``None`` if unconfigured.

    ``decode_responses=True`` so Lua script results and hash fields come back
    as ``str`` rather than ``bytes`` — the rate limiter works in plain numbers.

    ``protocol=2`` pins RESP2. redis-py >= 6 defaults to RESP3, whose connection
    handshake issues ``HELLO 3`` — a command only added in Redis 6.0. Our Redis
    is 5.x, which rejects it with ``unknown command HELLO`` and fails every
    connection, so the rate limiter silently falls open. RESP2 needs no ``HELLO``
    and the limiter uses no RESP3 feature, so this is a safe, deliberate pin.
    """
    if not settings.redis_url:
        return None
    return Redis.from_url(settings.redis_url, decode_responses=True, protocol=2)
