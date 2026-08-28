"""MCP Streamable HTTP transport wiring (D1) and the runtime it serves.

This module turns a manifest registry into a live MCP server:

* :func:`build_runtime` loads manifests, runs the D5 annotation lint (refusing
  to build if any tool is mis-annotated) and the RD-90 instructions size lint,
  and assembles the registry, the concatenated server ``instructions``, the
  low-level MCP ``Server`` (with its ``on_list_tools`` / ``on_call_tool``
  handlers), the upstream HTTP client, the dispatcher, the shared Redis client
  + per-user rate limiter (F0/F1), and the ``StreamableHTTPSessionManager``.
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
and the SDK builds its own ``Request`` from that same scope, the ``on_call_tool``
handler reads the context back via ``ctx.request.state`` (the per-request
``ServerRequestContext``) and hands it to the dispatcher, which forwards the
user's bearer token upstream.
"""

import json
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
from mcp.server.lowlevel.server import ServerRequestContext
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from redis.asyncio import Redis

from autods_mcp_server import __version__
from autods_mcp_server.analytics import MixpanelClient, build_mixpanel
from autods_mcp_server.auth import UserContext, get_current_user
from autods_mcp_server.business_errors import BUSINESS_ERROR_KEY, detect_business_errors
from autods_mcp_server.dispatch import (
    DispatchError,
    DispatchResult,
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
from autods_mcp_server.manifests import (
    ManifestRegistry,
    assert_instructions_within_limit,
    build_instructions,
    load_manifests,
)
from autods_mcp_server.manifests.playbooks import (
    HANDLER_PLAYBOOK,
    PLAYBOOK_KEY,
    PlaybookRegistry,
    assert_playbooks_valid,
    build_playbook_index,
    build_playbook_registry,
    render_failure_hint,
    render_playbook_payload,
    render_success_hint,
)
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
# state, to be read back inside the on_call_tool handler.
_USER_CONTEXT_STATE_KEY = "mcp_user_context"

_audit_logger = get_logger("autods_mcp_server.audit")

# URI scheme + media type of the playbook resource mirror (RD-100 P3).
_PLAYBOOK_RESOURCE_SCHEME = "autods://playbook/"
_PLAYBOOK_MIME_TYPE = "text/markdown"


@dataclass
class McpRuntime:
    """Everything needed to serve the MCP transport for one app instance."""

    registry: ManifestRegistry
    playbooks: PlaybookRegistry
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

    Since mcp 2.x this is the *only* schema validation on the call path: the
    low-level server no longer validates arguments at all (see ``on_call_tool``).
    """
    validators: dict[str, Validator] = {}
    for tool in tools:
        cls = validator_for(tool.input_schema)
        cls.check_schema(tool.input_schema)
        validators[tool.name] = cls(tool.input_schema)
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

    A locally-handled operation has no upstream, so it reports
    ``local <handler> <op_id>`` — same shape, same event, no pretend URL.
    """
    operation = registry.get(op_id)
    if operation is None:
        return op_id
    if operation.handler is not None:
        return f"local {operation.handler} {operation.operation_id}"
    return f"{operation.base_url_key} {operation.method.upper()} {operation.path}"


def _playbook_result(playbooks: PlaybookRegistry, op_id: str, arguments: dict[str, Any]) -> DispatchResult:
    """The ``get_playbook`` local handler.

    Returns the same ``DispatchResult`` envelope a forwarded call produces, so
    everything downstream — the audit line, the success-result shape, the
    business-error pass — treats it identically and the client cannot tell a
    local operation from a forwarded one. A bare markdown ``TextContent`` would
    read more prettily, but a uniform envelope is worth more than unescaped
    newlines (a model reads ``\\n``-escaped markdown fine).

    Raises:
        MissingArgumentError: unknown/missing name. The tool's ``name`` enum
            normally rejects that at validation; this is the defensive path, and
            it reuses the dispatcher's own input-validation error so the caller
            gets the usual ``invalid_arguments`` rather than an internal error.
    """
    name = arguments.get("name")
    if not isinstance(name, str) or not name:
        raise MissingArgumentError(f"Operation '{op_id}' requires parameter 'name'.")
    playbook = playbooks.get(name)
    if playbook is None:
        raise MissingArgumentError(
            f"Unknown playbook '{name}'. Registered playbooks: {', '.join(playbooks.names()) or 'none'}."
        )
    return DispatchResult(operation_id=op_id, status=200, ok=True, data=render_playbook_payload(playbook))


def _with_failure_hint(result: types.CallToolResult, hint: str | None) -> types.CallToolResult:
    """Append a playbook's chain-consequence guidance to an error result.

    The failure path is a *different* code path from the success path and the
    half that actually pays off: ``error_result`` returns a flat ``TextContent``
    with no ``data`` and no ``structuredContent``, so guidance can only ride on
    the text. Without a hint the result is returned untouched — an error on a
    step with no ``on_failure``, or on a tool in no playbook at all, is
    byte-identical to what it was before playbooks existed.
    """
    if hint is None:
        return result
    block = result.content[0]
    if not isinstance(block, types.TextContent):  # pragma: no cover - error results are always text
        return result
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{block.text}\n{hint}")],
        is_error=True,
    )


def _success_result(payload: dict[str, Any]) -> types.CallToolResult:
    """Wrap a dispatcher payload in the result shape clients have always seen.

    Through mcp 1.x the low-level ``call_tool`` decorator did this wrapping for
    any handler that returned a plain dict: the payload verbatim as
    ``structuredContent``, plus one ``text`` block carrying the same payload as
    ``json.dumps(payload, indent=2)``. mcp 2.x removed the auto-wrapping and
    hands result shaping to the handler, so this reproduces that exact shape.
    It is the one place in the port where a change would be invisible — a
    reformat here (dropping ``indent=2``, say) alters every successful response
    with nothing raising anywhere — so ``test_transport`` pins it byte for byte.
    """
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=2))],
        structured_content=payload,
    )


def _build_server(
    registry: ManifestRegistry,
    playbooks: PlaybookRegistry,
    dispatcher: OperationDispatcher,
    rate_limiter: RateLimiter,
    mixpanel: MixpanelClient,
    instructions: str,
) -> Server:
    """Create the low-level MCP server with tool list/call handlers.

    Since mcp 2.x the handlers are constructor kwargs rather than decorators,
    take the per-request ``ServerRequestContext`` as their first argument, and
    return the full result type (``ListToolsResult`` / ``CallToolResult``).

    ``instructions`` is the concatenated manifest text (RD-90). The SDK carries
    it through ``create_initialization_options()`` into
    ``InitializeResult.instructions``, which clients surface in the model's
    system prompt — so passing it here is the whole delivery mechanism for that
    channel.
    """
    tools = build_tools(registry.list_operations(), playbooks)  # D5 + RD-100 lints run here.
    validator_by_name = _build_validators(tools)  # Compiles + boot-checks each inputSchema.

    async def on_list_tools(
        _ctx: ServerRequestContext[Any, Any],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    # RD-100 resource mirror. Every playbook is also readable as
    # ``autods://playbook/<name>`` with ``mimeType: text/markdown``, for hosts
    # that let a *user* attach a resource. It is a mirror, not the delivery
    # mechanism: ``resources/`` is host-mediated and behaves unevenly across
    # clients, which is why the runbook ships as a tool. Registering
    # ``on_list_resources`` is what declares the ``resources`` capability in the
    # handshake — RD-92 adds URIs to this list rather than declaring it again.
    resources = [
        types.Resource(
            uri=f"{_PLAYBOOK_RESOURCE_SCHEME}{playbook.name}",
            name=playbook.name,
            title=playbook.title,
            description=playbook.when_to_use,
            mime_type=_PLAYBOOK_MIME_TYPE,
        )
        for playbook in playbooks.list_playbooks()
    ]

    async def on_list_resources(
        _ctx: ServerRequestContext[Any, Any],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListResourcesResult:
        return types.ListResourcesResult(resources=resources)

    async def on_read_resource(
        _ctx: ServerRequestContext[Any, Any],
        params: types.ReadResourceRequestParams,
    ) -> types.ReadResourceResult:
        uri = str(params.uri)
        name = uri.removeprefix(_PLAYBOOK_RESOURCE_SCHEME) if uri.startswith(_PLAYBOOK_RESOURCE_SCHEME) else ""
        playbook = playbooks.get(name) if name else None
        if playbook is None:
            raise ValueError(f"Unknown resource '{uri}'.")
        # ``mime_type`` is set explicitly: handing back a bare string would
        # advertise ``text/plain`` and lose the markdown the body is written in.
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=params.uri, mime_type=_PLAYBOOK_MIME_TYPE, text=playbook.body)]
        )

    async def on_call_tool(
        ctx: ServerRequestContext[Any, Any],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        name = params.name
        arguments = params.arguments or {}
        user_context: UserContext | None = None
        # Whether the F2 audit line for this call has already been emitted, so
        # the catch-all below doesn't emit a second one for the same call.
        audited = False
        handler_start = time.perf_counter()

        def emit(
            *,
            upstream_url: str | None,
            upstream_status: int | None,
            latency_ms: float,
            error_type: str | None = None,
        ) -> None:
            nonlocal audited
            if user_context is None:  # nothing to attribute the call to
                return
            audited = True
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

        # mcp 1.x's ``call_tool`` decorator caught every handler exception and
        # turned it into an ``isError`` result, so the model saw the error text
        # and could self-correct. v2 lets it propagate as a top-level JSON-RPC
        # error the LLM never sees, so we own that guard now: an unanticipated
        # failure still comes back as the typed ``internal_error`` envelope the
        # rest of this module returns.
        try:
            # The auth seam: the SDK builds its ``Request`` from the same ASGI
            # scope the /mcp route ran on, and ``request.state`` is scope-backed,
            # so the ``UserContext`` the route stashed is readable here.
            request = ctx.request
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
            # here, never forwarded as an opaque upstream 4xx. Since mcp 2.x the
            # SDK performs no schema validation of its own, so this is the only
            # gate; it produces our typed ``invalid_arguments`` error and an audit
            # line, which is why it was already ours to run under 1.x
            # (``validate_input=False``).
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

            # Every playbook step that calls this operation — all of them, not
            # the first: an operation can belong to several chains, and nothing
            # in the request says which one the caller is following. The
            # renderers decide what is honest to say for the set (a specific
            # step for one chain, the candidates plus a get_playbook pointer for
            # several), so this stays a plain lookup. Empty for a non-chain tool.
            step_refs = playbooks.steps_for(name)

            start = time.perf_counter()
            try:
                # RD-100: the local-handler seam. It sits *after* the rate
                # limiter, the analytics event and argument validation, and
                # before the dispatcher — so a locally-served operation is
                # metered, tracked, validated and audited exactly like a
                # forwarded one, and the envelope it returns is
                # indistinguishable. The handler registry is closed (the boot
                # lint rejects any other value), so this can't become an
                # arbitrary dispatch table.
                operation = registry.get(name)
                if operation is not None and operation.handler == HANDLER_PLAYBOOK:
                    result = _playbook_result(playbooks, name, arguments)
                else:
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
                # The ambiguous failure, and the one the chain hint exists for:
                # an async write may have started even though no response came
                # back. "Retry on failure" without a way to check for partial
                # success duplicates the write.
                return _with_failure_hint(
                    error_result(
                        ERROR_UPSTREAM_UNREACHABLE,
                        "The upstream service could not be reached. Please try again later.",
                    ),
                    render_failure_hint(step_refs),
                )
            except (UnknownOperationError, DispatchError) as exc:
                # UnknownOperationError shouldn't happen (the tool name came off
                # our own advertised list), so it's an internal inconsistency,
                # not a user error.
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
                payload = result.model_dump()
                # RD-90: a 2xx whose payload carries a business rejection. The
                # hint lands *beside* ``data``, so ``data`` remains the upstream
                # payload verbatim; an operation without a ``business_errors``
                # block gets an untouched envelope.
                if operation is not None:
                    business_error = detect_business_errors(operation, payload.get("data"))
                    if business_error is not None:
                        payload[BUSINESS_ERROR_KEY] = business_error
                # RD-100: the per-step nudge, on the one channel that puts it in
                # front of the model at the moment it has just finished step N.
                # Same placement rule as ``business_error`` — beside ``data``,
                # never inside it. Absent on a non-chain tool and on a final
                # step, so a non-playbook envelope is byte-identical to before.
                hint = render_success_hint(step_refs)
                if hint is not None:
                    payload[PLAYBOOK_KEY] = hint
                return _success_result(payload)

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
            # A mapped upstream error is ambiguous for exactly the same reason a
            # transport failure is: a 5xx from an endpoint that starts an async
            # job says nothing about whether the job started.
            return _with_failure_hint(
                mapped.result,
                render_failure_hint(step_refs),
            )
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all, see above
            if not audited:
                emit(
                    upstream_url=None,
                    upstream_status=None,
                    latency_ms=round((time.perf_counter() - handler_start) * 1000, 2),
                    error_type=ERROR_INTERNAL,
                )
            capture_tool_exception(exc, error_type=ERROR_INTERNAL, tool_name=name)
            _audit_logger.exception("tool_call_unhandled_error", tool_name=name, op_id=name)
            return error_result(ERROR_INTERNAL, f"An internal error occurred while running tool '{name}'.")

    # ``version`` feeds the ``serverInfo`` stamp mcp 2.x attaches to every
    # result's ``_meta`` (spec #3002); left unset it advertises an empty string,
    # so it rides the same single-sourced ``__version__`` as the FastAPI app and
    # the Sentry release.
    return Server(
        "autods-mcp-server",
        version=__version__,
        instructions=instructions or None,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_read_resource=on_read_resource,
    )


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
        BodySchemaError: if a ``body_schema`` types a known integer-enum field as
            a string — likewise fatal at boot.
        BusinessErrorsError: if a ``business_errors`` block declares no paths, or
            its operation's ``notes`` never warn that ``ok`` is transport-level
            only (RD-90) — likewise fatal at boot.
        InstructionsTooLargeError: if the concatenated manifest ``instructions``
            exceed the size budget (RD-90) — likewise fatal at boot.
        OperationHandlerError: if an operation names both a local handler and an
            upstream, or neither (RD-100) — likewise fatal at boot.
        PlaybookError: if a playbook names an operation this server doesn't
            serve, has an unreachable step, leaves a destructive step's
            consequences undeclared, or renders text over a channel's size
            budget (RD-100) — likewise fatal at boot.
    """
    # Manifests are read once: the registry indexes their operations, and the
    # server ``instructions`` are the concatenation of their text blocks in the
    # loader's sorted-filename order (deterministic across replicas, which the
    # client's prompt cache depends on).
    manifests = load_manifests(settings.mcp_manifest_dir)
    registry = ManifestRegistry(manifests)
    # RD-100: the chains, from the ``playbooks/`` subdirectory of the same
    # manifest dir. ``load_manifests`` globs non-recursively, so the two loaders
    # never see each other's files. Lints run against the operation registry —
    # a chain can only name tools this server actually serves.
    playbooks = build_playbook_registry(settings.mcp_manifest_dir)
    assert_playbooks_valid(playbooks, registry)
    instructions = build_instructions(manifests, playbook_index=build_playbook_index(playbooks))
    assert_instructions_within_limit(instructions)
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
    server = _build_server(registry, playbooks, dispatcher, rate_limiter, mixpanel, instructions)
    # Stateless mode (F0): no per-session transport is retained between
    # requests, so any replica/worker can serve any request. json_response
    # stays off so the spec's SSE framing is still used for the single
    # request/response exchange.
    session_manager = StreamableHTTPSessionManager(app=server, stateless=True)
    return McpRuntime(
        registry=registry,
        playbooks=playbooks,
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
        # Stash the verified context where the on_call_tool handler will read it
        # (scope-backed, so the SDK's Request sees the same value).
        setattr(request.state, _USER_CONTEXT_STATE_KEY, user)
        return _SessionManagerResponse(runtime.session_manager)
