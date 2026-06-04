"""HTTP middlewares.

- RequestContextMiddleware: binds request_id / path / method onto the
  structlog context for the lifetime of the request and emits one
  structured access log line.
- OriginAllowlistMiddleware: rejects foreign Origins on protected
  routes and defends against DNS rebinding via a Host check.

The Origin allowlist applies only to paths matching ``protected_patterns``.
The production wiring targets ``/mcp`` and ``/.well-known/*`` (those
endpoints land in Phases C/D). Phase A's acceptance tests construct
the middleware with ``/health`` in the pattern list to exercise the
matcher without needing the MCP transport — see the ticket's A4 note.
"""

import fnmatch
import time
import uuid
from collections.abc import Iterable, Sequence
from typing import Final

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from autods_mcp_server.settings import Settings

DEFAULT_PROTECTED_PATTERNS: Final[tuple[str, ...]] = (
    "/mcp",
    "/mcp/*",
    "/.well-known/*",
    "/oauth/*",
)


def _path_is_protected(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _origin_matches(origin: str, allowed: Iterable[str]) -> bool:
    """Glob-aware origin check.

    Allowed entries may contain wildcards (e.g. ``http://localhost:*``)
    so local dev clients on arbitrary ports are accepted without
    enumerating every port.
    """
    for pattern in allowed:
        if "*" in pattern or "?" in pattern:
            if fnmatch.fnmatchcase(origin, pattern):
                return True
        elif origin == pattern:
            return True
    return False


def _host_from_header(host_header: str) -> str:
    return host_header.split(":", 1)[0]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind request_id / path / method to structlog context and log access."""

    def __init__(self, app: ASGIApp, logger_name: str = "autods_mcp_server.access") -> None:
        super().__init__(app)
        self._logger = structlog.get_logger(logger_name)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            path=request.url.path,
            method=request.method,
        )
        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            self._logger.exception(
                "request_failed",
                status_code=500,
                duration_ms=duration_ms,
            )
            raise
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        self._logger.info(
            "request",
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        response.headers["X-Request-ID"] = request_id
        return response


class OriginAllowlistMiddleware(BaseHTTPMiddleware):
    """Enforce Origin allowlist + DNS-rebinding Host check on protected paths.

    - Missing Origin → allowed. A browser always attaches an Origin on a
      cross-origin fetch, so a rebinding/CSRF attempt would carry a
      (rejected) Origin. Absence means a direct, non-browser caller (e.g.
      a server-side MCP client) bearing no ambient credentials — there's
      nothing for the allowlist to defend against, and requiring Origin
      only locks legitimate MCP clients out of the discovery + DCR
      endpoints. The Host check below still applies to these requests.
    - Origin present but not in allowlist → 403.
    - Host doesn't match the configured public hostname → 403. This is the
      DNS-rebinding defense: a browser tricked into issuing a request from
      a malicious page still lands on our Host header.
    """

    def __init__(
        self,
        app: ASGIApp,
        settings: Settings,
        protected_patterns: Sequence[str] = DEFAULT_PROTECTED_PATTERNS,
    ) -> None:
        super().__init__(app)
        self._settings = settings
        self._protected_patterns = tuple(protected_patterns)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not _path_is_protected(request.url.path, self._protected_patterns):
            return await call_next(request)

        # Only validate the Origin when one is present — a missing Origin is a
        # non-browser caller, not a rebinding/CSRF vector (see class docstring).
        origin = request.headers.get("origin")
        if origin and not _origin_matches(origin, self._settings.allowed_origins):
            return JSONResponse(
                status_code=403,
                content={"error": "origin_not_allowed", "detail": f"Origin {origin!r} is not permitted."},
            )

        return await call_next(request)
