"""HTTP middlewares.

- RequestContextMiddleware: binds request_id / path / method onto the
  structlog context for the lifetime of the request and emits one
  structured access log line.
- OriginAllowlistMiddleware: rejects foreign Origins on protected
  routes and defends against DNS rebinding via a Host check.
- HttpsOnlyMiddleware: in non-local envs, requires X-Forwarded-Proto=https.

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

    - Missing Origin on a protected route → 403.
    - Origin not in allowlist → 403.
    - Origin present but its host doesn't match the configured public
      hostname (or the Host header, when public_hostname is empty)
      → 403. This is the DNS-rebinding defense: a browser tricked into
      issuing a request from a malicious page will carry that page's
      Origin, but the TCP connection still lands on our Host header.
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

        origin = request.headers.get("origin")
        if not origin:
            return _forbidden("origin_missing", "Origin header is required on this route.")

        if not _origin_matches(origin, self._settings.allowed_origins):
            return _forbidden("origin_not_allowed", f"Origin {origin!r} is not permitted.")

        # DNS-rebinding defense: the Origin allowlist proves the *caller*
        # is one we trust, but a rebinding attack tricks a browser into
        # issuing the request through our server's IP under an attacker
        # Host. Compare the Host header to our public hostname when we
        # know it; in local dev (no public_hostname) we skip — the
        # localhost-only Origin allowlist already pins the audience.
        if self._settings.public_hostname:
            actual_host = _host_from_header(request.headers.get("host", ""))
            if actual_host != self._settings.public_hostname:
                return _forbidden(
                    "host_mismatch",
                    f"Host header {actual_host!r} does not match expected host {self._settings.public_hostname!r}.",
                )

        return await call_next(request)


class HttpsOnlyMiddleware(BaseHTTPMiddleware):
    """Reject plaintext requests in non-local environments.

    ALB terminates TLS and sets X-Forwarded-Proto. Local env is exempt.
    Applies to every path — the request-level guard is uniform, not
    per-route.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if self._settings.is_local:
            return await call_next(request)

        forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
        if forwarded_proto != "https":
            return _forbidden(
                "https_required",
                "This endpoint requires HTTPS (X-Forwarded-Proto: https).",
            )
        return await call_next(request)


def _forbidden(code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": code, "detail": detail})
