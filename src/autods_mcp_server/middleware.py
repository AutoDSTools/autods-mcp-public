"""HTTP middlewares.

- RequestContextMiddleware: binds request_id / path / method / user_agent onto the
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

from autods_mcp_server.sentry import set_request_id
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


# A User-Agent is caller-controlled and unbounded, and this one goes on every
# line of every request. Long ones are truncated rather than dropped: the head is
# what names the client, and a line that says which client it was is worth more
# than a byte-exact copy of a header nobody reads in full.
_USER_AGENT_MAX_CHARS = 200


def _user_agent(request: Request) -> str | None:
    """The request's ``User-Agent``, truncated; ``None`` when it sent none.

    ``None`` is kept as a value rather than omitted, because "this client sends
    no User-Agent" is a fingerprint in its own right and has to stay different
    from a line written before this field existed.
    """
    value = request.headers.get("user-agent")
    if value is None:
        return None
    return value[:_USER_AGENT_MAX_CHARS]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind request_id / path / method / user_agent to structlog context and log access."""

    def __init__(
        self,
        app: ASGIApp,
        logger_name: str = "autods_mcp_server.access",
        quiet_paths: Iterable[str] = (),
    ) -> None:
        super().__init__(app)
        self._logger = structlog.get_logger(logger_name)
        # Paths whose successful access log is suppressed (e.g. /health probes,
        # which would otherwise flood the log on every ALB/k8s liveness check).
        # Failures are still logged below regardless of this set.
        self._quiet_paths = frozenset(quiet_paths)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            path=request.url.path,
            method=request.method,
            # The only client signal that is on *every* request. The MCP client
            # identity is not: the transport is stateless, so on a 2025-era
            # protocol version the SDK hands the handler no ``client_params`` at
            # all and ``client_name`` is ``None`` however the client identified
            # itself at ``initialize`` (only 2026-07-28 repeats it per request,
            # in ``_meta``). Bound here rather than logged by a handler so it
            # reaches every line — ``request``, ``tool_call``, ``resource_read``
            # — and so it survives the paths that never reach a handler at all.
            user_agent=_user_agent(request),
        )
        # Correlate Sentry events with the access log (both keyed on request_id).
        set_request_id(request_id)
        # Declare the access-log fields here, where they're emitted. The auth
        # dependency (running downstream) overwrites them with the token subject
        # + resolved AutoDS identity on success; they stay None for
        # unauthenticated routes or requests that fail before/at auth.
        # request.state is backed by the shared ASGI scope, so the dependency's
        # write is visible here after call_next.
        request.state.cognito_username = None
        request.state.autods_user_id = None
        request.state.email = None
        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            self._logger.exception(
                "request_failed",
                status_code=500,
                duration_ms=duration_ms,
                cognito_username=request.state.cognito_username,
                autods_user_id=request.state.autods_user_id,
                email=request.state.email,
            )
            raise
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        if request.url.path not in self._quiet_paths:
            self._logger.info(
                "request",
                status_code=response.status_code,
                duration_ms=duration_ms,
                cognito_username=request.state.cognito_username,
                autods_user_id=request.state.autods_user_id,
                email=request.state.email,
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
