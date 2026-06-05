"""MCP tool-error construction and upstream error mapping (F1 + F3).

Every failure a client sees is a ``CallToolResult`` with ``isError=True`` and a
short, safe text message prefixed by a stable ``error_type`` token (so an LLM
or a client can branch without parsing prose). Two sources feed this module:

* **F1 rate limiting** — :func:`rate_limited_result` carries ``retry_after``.
* **F3 upstream mapping** — :func:`map_upstream_error` turns an upstream HTTP
  status into a user-facing error, deliberately *not* leaking upstream bodies
  for 5xx (generic message; the caller logs the full detail) and *sanitizing*
  4xx detail so stack traces / internal hints never reach the client.

``error_type`` strings double as the ``error_type`` field in the F2 audit log.
"""

import math
from dataclasses import dataclass
from typing import Any

from mcp import types

# Stable error-type tokens (also used as the audit log's ``error_type``).
ERROR_RATE_LIMITED = "rate_limited"
ERROR_UNAUTHENTICATED = "unauthenticated"
ERROR_FORBIDDEN = "forbidden"
ERROR_UPSTREAM_CLIENT = "upstream_client_error"
ERROR_UPSTREAM = "upstream_error"
ERROR_UPSTREAM_UNREACHABLE = "upstream_unreachable"
ERROR_INVALID_ARGUMENTS = "invalid_arguments"
ERROR_INTERNAL = "internal_error"

# Cap sanitized upstream detail so a hostile/huge body can't bloat the message.
_MAX_DETAIL_LEN = 200
# Markers that indicate the upstream leaked internals we must not forward.
_LEAK_MARKERS = ("traceback", 'file "', "/usr/", "/app/", "  at ", "sqlalchemy", "psycopg")


def error_result(error_type: str, message: str) -> types.CallToolResult:
    """Build an ``isError`` tool result whose text is ``"{error_type}: {message}"``."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{error_type}: {message}")],
        isError=True,
    )


def rate_limited_result(retry_after: float) -> types.CallToolResult:
    """F1: the per-user limit is exhausted. ``retry_after`` is seconds (ceil'd)."""
    seconds = max(1, math.ceil(retry_after))
    return error_result(
        ERROR_RATE_LIMITED,
        f"Rate limit exceeded. Retry after {seconds} second{'s' if seconds != 1 else ''}.",
    )


def _sanitize_detail(data: Any) -> str | None:
    """Pull a short, single-line, leak-free message out of an upstream body.

    Returns ``None`` when nothing safe can be extracted — the caller then falls
    back to a bare status message.
    """
    candidate: str | None = None
    if isinstance(data, str):
        candidate = data
    elif isinstance(data, dict):
        for field in ("error_description", "detail", "message", "error", "title"):
            value = data.get(field)
            if isinstance(value, str) and value.strip():
                candidate = value
                break
    if not candidate:
        return None

    flattened = " ".join(candidate.split())
    if any(marker in flattened.lower() for marker in _LEAK_MARKERS):
        return None
    if len(flattened) > _MAX_DETAIL_LEN:
        flattened = flattened[:_MAX_DETAIL_LEN].rstrip() + "…"
    return flattened or None


@dataclass
class MappedUpstreamError:
    """Result of mapping an upstream non-2xx response.

    ``result`` is what the client sees; ``error_type`` feeds the audit log;
    ``log_full`` is the unsanitized upstream body to record server-side (set
    only for 5xx, where the user message is intentionally generic).
    """

    result: types.CallToolResult
    error_type: str
    log_full: Any = None


def map_upstream_error(status: int, data: Any) -> MappedUpstreamError:
    """F3: map an upstream HTTP status to a user-facing MCP error.

    Precondition: the caller has already decided the response is an error
    (``not result.ok``). 2xx must never reach here.
    """
    if status == 401:
        return MappedUpstreamError(
            error_result(
                ERROR_UNAUTHENTICATED,
                "The upstream service rejected your authorization (HTTP 401). "
                "Your session may have expired — re-authenticate and try again.",
            ),
            ERROR_UNAUTHENTICATED,
        )
    if status == 403:
        return MappedUpstreamError(
            error_result(
                ERROR_FORBIDDEN,
                "You don't have permission to perform this operation (HTTP 403).",
            ),
            ERROR_FORBIDDEN,
        )
    if 400 <= status < 500:
        detail = _sanitize_detail(data)
        message = f"The upstream rejected the request (HTTP {status})."
        if detail:
            message = f"{message} {detail}"
        return MappedUpstreamError(error_result(ERROR_UPSTREAM_CLIENT, message), ERROR_UPSTREAM_CLIENT)

    if 300 <= status < 400:
        # Unexpected redirect: the dispatcher runs with follow_redirects=False
        # (a 3xx must not bounce the forwarded bearer token to another host),
        # so a 3xx reaching the client means the upstream is misconfigured —
        # an upstream-side problem the user can't act on. Never echo the body /
        # Location (internal hostnames); log the full detail server-side.
        return MappedUpstreamError(
            error_result(
                ERROR_UPSTREAM,
                f"The upstream service returned an unexpected redirect (HTTP {status}).",
            ),
            ERROR_UPSTREAM,
            log_full=data,
        )

    # 5xx (and any other non-2xx that isn't a 3xx/4xx): never echo the upstream
    # body — it may carry stack traces / internal hostnames. Generic message
    # to the user; full detail is logged by the caller.
    return MappedUpstreamError(
        error_result(
            ERROR_UPSTREAM,
            f"The upstream service encountered an error (HTTP {status}). Please try again later.",
        ),
        ERROR_UPSTREAM,
        log_full=data,
    )
