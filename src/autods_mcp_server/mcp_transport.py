"""MCP Streamable HTTP transport wiring (D1) and the runtime it serves.

This module turns a manifest registry into a live MCP server:

* :func:`build_runtime` loads manifests, runs the D5 annotation lint (refusing
  to build if any tool is mis-annotated), and assembles the registry, the
  low-level MCP ``Server`` (with ``list_tools`` / ``call_tool`` handlers), the
  upstream HTTP client, the dispatcher, the shared Redis client + per-user rate
  limiter (F0/F1), and the ``StreamableHTTPSessionManager``.
* :func:`mount_mcp` mounts the transport at ``/mcp`` on a FastAPI app behind the
  Phase B auth dependency, and registers the session manager's lifespan.

**Stateless transport (F0).** The session manager runs ``stateless=True``: each
request gets a fresh transport that is terminated after the response, and no
session is retained between requests. Production runs 2–10 replicas × 5 uvicorn
workers, and a stateful MCP session is a live coroutine + in-memory streams
pinned to one worker — so a follow-up request landing elsewhere would fail with
``Session not found``. Stateless removes that failure mode (any worker serves
any request) and the unbounded per-worker session accumulation. The trade-off
is the server→client GET SSE / resumability stream, which this server — a
synchronous upstream REST forwarder — does not use.

**Auth seam.** The ``/mcp`` route depends on ``get_current_user`` — so an
unauthenticated request gets the same RFC 6750 ``401 + WWW-Authenticate``
challenge as any protected route, which is exactly what MCP clients follow to
discover the OAuth flow. On success the verified ``UserContext`` is stashed on
``request.state``; because Starlette backs ``request.state`` with ``scope["state"]``
and the SDK builds its own ``Request`` from that same scope, the ``call_tool``
handler reads the context back via ``server.request_context.request.state`` and
hands it to the dispatcher, which forwards the user's bearer token upstream.
"""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Request, Response
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from redis.asyncio import Redis

from autods_mcp_server.analytics import MixpanelClient, build_mixpanel
from autods_mcp_server.auth import UserContext, get_current_user
from autods_mcp_server.dispatch import (
    DispatchError,
    MissingArgumentError,
    OperationDispatcher,
    UnknownOperationError,
    UpstreamRequestError,
    create_http_client,
)
from autods_mcp_server.errors import (
    ERROR_INTERNAL,
    ERROR_INVALID_ARGUMENTS,
    ERROR_RATE_LIMITED,
    ERROR_UPSTREAM_UNREACHABLE,
    error_result,
    map_upstream_error,
    rate_limited_result,
)
from autods_mcp_server.identity import (
    CachedIdentityResolver,
    SelfIdentityResolver,
    build_identity_resolver,
)
from autods_mcp_server.logging import get_logger
from autods_mcp_server.manifests import ManifestRegistry, build_registry
from autods_mcp_server.ratelimit import RateLimiter, build_rate_limiter
from autods_mcp_server.redis_client import create_redis
from autods_mcp_server.sentry import (
    capture_tool_error,
    capture_tool_exception,
    set_tool_context,
)
from autods_mcp_server.settings import Settings
from autods_mcp_server.tools import build_tools
from autods_mcp_server.urls import MCP_PATH

# Key under which the verified UserContext is stashed on the request scope's
# state, to be read back inside the call_tool handler.
_USER_CONTEXT_STATE_KEY = "mcp_user_context"

_audit_logger = get_logger("autods_mcp_server.audit")


@dataclass
class McpRuntime:
    """Everything needed to serve the MCP transport for one app instance."""

    registry: ManifestRegistry
    server: Server
    session_manager: StreamableHTTPSessionManager
    dispatcher: OperationDispatcher
    http_client: httpx.AsyncClient
    rate_limiter: RateLimiter
    redis: Redis | None
    mixpanel: MixpanelClient
    # Uncached self-identity lookup (RD-68) + the cached resolver (RD-63) that
    # wraps it; the auth dependency uses the cached one.
    self_identity_resolver: SelfIdentityResolver
    identity_resolver: CachedIdentityResolver


def _emit_audit(
    *,
    tool_name: str,
    op_id: str,
    cognito_username: str,
    autods_user_id: str | None,
    email: str | None,
    upstream_url: str | None,
    upstream_status: int | None,
    latency_ms: float,
    error_type: str | None = None,
) -> None:
    """F2: one structured audit line per tool call.

    ``ts`` and ``request_id`` are carried automatically — the ``timestamp`` is
    added by the structlog processor chain, and ``request_id`` rides the
    contextvars bound by ``RequestContextMiddleware``. Payload bodies are never
    logged (PII risk).
    """
    fields: dict[str, Any] = {
        "cognito_username": cognito_username,
        "autods_user_id": autods_user_id,
        "email": email,
        "tool_name": tool_name,
        "op_id": op_id,
        "upstream_url": upstream_url,
        "upstream_status": upstream_status,
        "latency_ms": latency_ms,
    }
    if error_type is not None:
        fields["error_type"] = error_type
    _audit_logger.info("tool_call", **fields)


def _build_validators(tools: list[types.Tool]) -> dict[str, Validator]:
    """Compile one reusable jsonschema validator per tool ``inputSchema``.

    Built once at boot so the per-request path only matches the instance — the
    convenience ``jsonschema.validate`` would otherwise recompile the validator
    and re-check the schema against its meta-schema on every call. ``check_schema``
    runs here too, so a structurally invalid authored schema fails at boot
    (alongside the D5 lint) rather than as a per-request 500.
    """
    validators: dict[str, Validator] = {}
    for tool in tools:
        cls = validator_for(tool.inputSchema)
        cls.check_schema(tool.inputSchema)
        validators[tool.name] = cls(tool.inputSchema)
    return validators


def _validate_arguments(arguments: dict[str, Any], validator: Validator) -> str | None:
    """Validate ``arguments`` against a tool's compiled ``inputSchema`` validator.

    Returns a short, safe error message naming the offending field, or ``None``
    when the arguments are valid. The jsonschema message echoes only the bad
    value and the violated constraint (e.g. ``'active' is not of type
    'integer'``) — no internal detail — so it's safe to surface to the client.
    """
    error = next(iter(validator.iter_errors(arguments)), None)
    if error is None:
        return None
    field = "/".join(str(part) for part in error.absolute_path) or "(root)"
    return f"Invalid value for '{field}': {error.message}"


def _remote_endpoint(registry: ManifestRegistry, op_id: str) -> str:
    """The upstream endpoint a tool forwards to, for the "MCP Call Received" event.

    The *templated* ``base_url_key METHOD /path`` (e.g.
    ``autods_api POST /products/{store_ids}/``) — never the substituted URL,
    which would embed store ids / query values (high cardinality + request
    data). Falls back to the tool name if the op can't be resolved.
    """
    operation = registry.get(op_id)
    if operation is None:
        return op_id
    return f"{operation.base_url_key} {operation.method.upper()} {operation.path}"


def _build_server(
    registry: ManifestRegistry,
    dispatcher: OperationDispatcher,
    rate_limiter: RateLimiter,
    mixpanel: MixpanelClient,
) -> Server:
    """Create the low-level MCP server with tool list/call handlers."""
    server: Server = Server("autods-mcp-server")
    tools = build_tools(registry.list_operations())  # D5 lint runs here.
    validator_by_name = _build_validators(tools)  # Compiles + boot-checks each inputSchema.

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return tools

    # ``validate_input=False``: the SDK would validate ``arguments`` against the
    # tool's ``inputSchema`` and return a generic "Input validation error". We
    # validate ourselves instead so a bad body becomes our typed
    # ``invalid_arguments`` error (consistent with the rest of this module) and
    # is recorded in the audit log — still rejected before any upstream call.
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any] | types.CallToolResult:
        request = server.request_context.request
        user_context: UserContext | None = None
        if request is not None:
            user_context = getattr(request.state, _USER_CONTEXT_STATE_KEY, None)
        if user_context is None:
            # The /mcp route always sets this; reaching here means the transport
            # was driven without the auth seam — treat as an internal error.
            return error_result(ERROR_INTERNAL, f"No authenticated user context for tool '{name}'.")

        # Record the tool call ("the request the client was making") on the
        # Sentry scope. The single POST /mcp handler dispatches every tool, so the
        # Starlette/FastAPI integrations can't attribute it automatically.
        set_tool_context(tool_name=name, operation_id=name, arguments=arguments)

        def emit(
            *,
            upstream_url: str | None,
            upstream_status: int | None,
            latency_ms: float,
            error_type: str | None = None,
        ) -> None:
            _emit_audit(
                tool_name=name,
                op_id=name,
                cognito_username=user_context.sub,
                autods_user_id=user_context.autods_user_id,
                email=user_context.email,
                upstream_url=upstream_url,
                upstream_status=upstream_status,
                latency_ms=latency_ms,
                error_type=error_type,
            )

        # F1 — per-user rate limit, enforced before any upstream work.
        decision = await rate_limiter.acquire(user_context.sub)
        if not decision.allowed:
            emit(
                upstream_url=None,
                upstream_status=None,
                latency_ms=0.0,
                error_type=ERROR_RATE_LIMITED,
            )
            return rate_limited_result(decision.retry_after)

        # "MCP Call Received" — fires once the call clears the rate limiter (RD-63),
        # so a rate-limited / abusive caller can't drive unbounded tracking work.
        # The event is keyed on the AutoDS user id; if that's unresolved we skip
        # tracking entirely rather than emit an event keyed on the Cognito sub.
        if user_context.autods_user_id is not None:
            mixpanel.track_mcp_call_received(
                user_context.autods_user_id,
                remote_endpoint=_remote_endpoint(registry, name),
            )

        # Validate arguments (incl. the typed request body) against the tool's
        # inputSchema before any upstream work — a malformed body is rejected
        # here, never forwarded as an opaque upstream 4xx.
        validator = validator_by_name.get(name)
        if validator is not None:
            validation_error = _validate_arguments(arguments, validator)
            if validation_error is not None:
                emit(
                    upstream_url=None,
                    upstream_status=None,
                    latency_ms=0.0,
                    error_type=ERROR_INVALID_ARGUMENTS,
                )
                return error_result(ERROR_INVALID_ARGUMENTS, validation_error)

        start = time.perf_counter()
        try:
            result = await dispatcher.dispatch(name, arguments, user_context)
        except MissingArgumentError as exc:
            # Our own input validation — the message is safe to surface.
            emit(
                upstream_url=None,
                upstream_status=None,
                latency_ms=round((time.perf_counter() - start) * 1000, 2),
                error_type=ERROR_INVALID_ARGUMENTS,
            )
            return error_result(ERROR_INVALID_ARGUMENTS, str(exc))
        except UpstreamRequestError as exc:
            # Transport-level failure (timeout, connection) — no response body.
            emit(
                upstream_url=exc.upstream_url or None,
                upstream_status=None,
                latency_ms=round((time.perf_counter() - start) * 1000, 2),
                error_type=ERROR_UPSTREAM_UNREACHABLE,
            )
            capture_tool_exception(
                exc,
                error_type=ERROR_UPSTREAM_UNREACHABLE,
                tool_name=name,
                upstream_url=exc.upstream_url or None,
            )
            return error_result(
                ERROR_UPSTREAM_UNREACHABLE,
                "The upstream service could not be reached. Please try again later.",
            )
        except (UnknownOperationError, DispatchError) as exc:
            # UnknownOperationError shouldn't happen (the SDK validated the tool
            # name), so it's an internal inconsistency, not a user error.
            emit(
                upstream_url=None,
                upstream_status=None,
                latency_ms=round((time.perf_counter() - start) * 1000, 2),
                error_type=ERROR_INTERNAL,
            )
            capture_tool_exception(exc, error_type=ERROR_INTERNAL, tool_name=name)
            return error_result(ERROR_INTERNAL, str(exc))

        latency_ms = round((time.perf_counter() - start) * 1000, 2)

        if result.ok:
            emit(
                upstream_url=result.upstream_url or None,
                upstream_status=result.status,
                latency_ms=latency_ms,
            )
            return result.model_dump()

        # F3 — map an upstream non-2xx to a safe, typed MCP error.
        mapped = map_upstream_error(result.status, result.data)
        emit(
            upstream_url=result.upstream_url or None,
            upstream_status=result.status,
            latency_ms=latency_ms,
            error_type=mapped.error_type,
        )
        if mapped.log_full is not None:
            # 5xx / unexpected 3xx: the user message is generic, so record the
            # full upstream detail server-side for debugging (still no request
            # payload).
            _audit_logger.warning(
                "upstream_error_detail",
                op_id=name,
                upstream_url=result.upstream_url or None,
                upstream_status=result.status,
                detail=mapped.log_full,
            )
            capture_tool_error(
                f"{mapped.error_type}: upstream returned HTTP {result.status} for tool '{name}'",
                error_type=mapped.error_type,
                tool_name=name,
                upstream_url=result.upstream_url or None,
                upstream_status=result.status,
                detail=mapped.log_full,
            )
        return mapped.result

    return server


def build_runtime(
    settings: Settings,
    *,
    http_client: httpx.AsyncClient | None = None,
    redis: Redis | None = None,
    rate_limiter: RateLimiter | None = None,
    mixpanel: MixpanelClient | None = None,
    identity_resolver: CachedIdentityResolver | None = None,
) -> McpRuntime:
    """Assemble the MCP runtime for ``settings`` (manifests, server, dispatcher).

    ``http_client`` lets callers (tests) inject an upstream client backed by a
    mock transport; production passes ``None`` and gets the default client.
    ``redis`` / ``rate_limiter`` are likewise injectable for tests — production
    passes ``None`` and the limiter is built from ``settings`` (Redis-backed
    when ``REDIS_URL`` is set, in-process otherwise). ``mixpanel`` /
    ``identity_resolver`` (RD-63) are injectable too — production passes ``None``
    and they're built from ``settings`` (Mixpanel a no-op without a token; the
    cached identity resolver's L2 sharing the runtime's Redis).

    Raises:
        ToolAnnotationError: if any manifest operation fails the D5 lint — this
            propagates out of ``create_app`` so the process refuses to boot.
    """
    registry = build_registry(settings.mcp_manifest_dir)
    http_client = http_client or create_http_client()
    redis = redis if redis is not None else create_redis(settings)
    rate_limiter = rate_limiter or build_rate_limiter(settings, redis)
    dispatcher = OperationDispatcher(registry, settings, http_client)
    # RD-68: resolve the caller's own id/name/email via AutoDSApi's
    # ``get_current_user`` operation (the forwarded token, no privileged creds).
    self_identity_resolver = SelfIdentityResolver(dispatcher)
    # RD-63: Mixpanel analytics (no-op without a token) + the cached identity
    # resolver (L2 shares the runtime's Redis), wrapping the RD-68 lookup. The
    # cached resolver is stashed on app.state by mount_mcp so the auth dependency
    # can reach it.
    mixpanel = mixpanel if mixpanel is not None else build_mixpanel(settings)
    identity_resolver = (
        identity_resolver
        if identity_resolver is not None
        else build_identity_resolver(settings, redis, self_identity_resolver)
    )
    server = _build_server(registry, dispatcher, rate_limiter, mixpanel)
    # Stateless mode (F0): no per-session transport is retained between
    # requests, so any replica/worker can serve any request. json_response
    # stays off so the spec's SSE framing is still used for the single
    # request/response exchange.
    session_manager = StreamableHTTPSessionManager(app=server, stateless=True)
    return McpRuntime(
        registry=registry,
        server=server,
        session_manager=session_manager,
        dispatcher=dispatcher,
        http_client=http_client,
        rate_limiter=rate_limiter,
        redis=redis,
        mixpanel=mixpanel,
        self_identity_resolver=self_identity_resolver,
        identity_resolver=identity_resolver,
    )


class _SessionManagerResponse(Response):
    """A Response whose ASGI ``__call__`` delegates to the MCP session manager.

    Returning this from a FastAPI route lets the route run dependencies (auth)
    first, then hand the *original* scope/receive/send to the streamable-HTTP
    transport — the request body is still unread, so the transport parses it.
    """

    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self._session_manager = session_manager
        # FastAPI's route handler reads ``.background`` on the returned response
        # before invoking it as ASGI; we don't run a Response body, so it's None.
        self.background = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self._session_manager.handle_request(scope, receive, send)


@asynccontextmanager
async def mcp_lifespan(runtime: McpRuntime) -> AsyncIterator[None]:
    """Run the session manager's task group; close the HTTP + Redis clients on exit."""
    async with runtime.session_manager.run():
        try:
            yield
        finally:
            await runtime.mixpanel.drain()  # flush in-flight tracking (best effort)
            await runtime.http_client.aclose()
            if runtime.redis is not None:
                await runtime.redis.aclose()


def mount_mcp(app: FastAPI, runtime: McpRuntime) -> None:
    """Mount the authenticated ``/mcp`` transport route on ``app``."""

    # The auth dependency (get_current_user) reads the cached identity resolver
    # off request.app.state to resolve the AutoDS identity (autods_user_id +
    # email) for the audit log and the "MCP Call Received" event (RD-63). The
    # uncached lookup is exposed too for any direct consumer (RD-68).
    app.state.identity_resolver = runtime.identity_resolver
    app.state.self_identity_resolver = runtime.self_identity_resolver

    @app.api_route(
        MCP_PATH,
        methods=["GET", "POST", "DELETE"],
        include_in_schema=False,
        response_model=None,
    )
    async def mcp_endpoint(
        request: Request,
        user: Annotated[UserContext, Depends(get_current_user)],
    ) -> Response:
        # Stash the verified context where the call_tool handler will read it
        # (scope-backed, so the SDK's Request sees the same value).
        setattr(request.state, _USER_CONTEXT_STATE_KEY, user)
        return _SessionManagerResponse(runtime.session_manager)
