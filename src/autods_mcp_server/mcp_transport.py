"""MCP Streamable HTTP transport wiring (D1) and the runtime it serves.

This module turns a manifest registry into a live MCP server:

* :func:`build_runtime` loads manifests, runs the D5 annotation lint (refusing
  to build if any tool is mis-annotated), and assembles the registry, the
  low-level MCP ``Server`` (with ``list_tools`` / ``call_tool`` handlers), the
  upstream HTTP client, the dispatcher, and the ``StreamableHTTPSessionManager``.
* :func:`mount_mcp` mounts the transport at ``/mcp`` on a FastAPI app behind the
  Phase B auth dependency, and registers the session manager's lifespan.

**Auth seam.** The ``/mcp`` route depends on ``get_current_user`` — so an
unauthenticated request gets the same RFC 6750 ``401 + WWW-Authenticate``
challenge as any protected route, which is exactly what MCP clients follow to
discover the OAuth flow. On success the verified ``UserContext`` is stashed on
``request.state``; because Starlette backs ``request.state`` with ``scope["state"]``
and the SDK builds its own ``Request`` from that same scope, the ``call_tool``
handler reads the context back via ``server.request_context.request.state`` and
hands it to the dispatcher, which forwards the user's bearer token upstream.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Request, Response
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from autods_mcp_server.auth import UserContext, get_current_user
from autods_mcp_server.dispatch import DispatchError, OperationDispatcher, create_http_client
from autods_mcp_server.manifests import ManifestRegistry, build_registry
from autods_mcp_server.settings import Settings
from autods_mcp_server.tools import build_tools
from autods_mcp_server.urls import MCP_PATH

# Key under which the verified UserContext is stashed on the request scope's
# state, to be read back inside the call_tool handler.
_USER_CONTEXT_STATE_KEY = "mcp_user_context"


@dataclass
class McpRuntime:
    """Everything needed to serve the MCP transport for one app instance."""

    registry: ManifestRegistry
    server: Server
    session_manager: StreamableHTTPSessionManager
    dispatcher: OperationDispatcher
    http_client: httpx.AsyncClient


def _build_server(registry: ManifestRegistry, dispatcher: OperationDispatcher) -> Server:
    """Create the low-level MCP server with tool list/call handlers."""
    server: Server = Server("autods-mcp-server")
    tools = build_tools(registry.list_operations())  # D5 lint runs here.

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any] | types.CallToolResult:
        request = server.request_context.request
        user_context: UserContext | None = None
        if request is not None:
            user_context = getattr(request.state, _USER_CONTEXT_STATE_KEY, None)
        if user_context is None:
            # The /mcp route always sets this; reaching here means the transport
            # was driven without the auth seam — treat as an internal error.
            return _error_result(f"No authenticated user context for tool '{name}'.")

        try:
            result = await dispatcher.dispatch(name, arguments, user_context)
        except DispatchError as exc:
            return _error_result(str(exc))
        return result.model_dump()

    return server


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], isError=True)


def build_runtime(settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> McpRuntime:
    """Assemble the MCP runtime for ``settings`` (manifests, server, dispatcher).

    ``http_client`` lets callers (tests) inject an upstream client backed by a
    mock transport; production passes ``None`` and gets the default client.

    Raises:
        ToolAnnotationError: if any manifest operation fails the D5 lint — this
            propagates out of ``create_app`` so the process refuses to boot.
    """
    registry = build_registry(settings.mcp_manifest_dir)
    http_client = http_client or create_http_client()
    dispatcher = OperationDispatcher(registry, settings, http_client)
    server = _build_server(registry, dispatcher)
    # Stateful mode (the default) supports the GET SSE stream the MCP spec
    # defines, alongside POST. json_response stays off so the spec's SSE
    # framing is used.
    session_manager = StreamableHTTPSessionManager(app=server)
    return McpRuntime(
        registry=registry,
        server=server,
        session_manager=session_manager,
        dispatcher=dispatcher,
        http_client=http_client,
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
    """Run the session manager's task group and close the HTTP client on exit."""
    async with runtime.session_manager.run():
        try:
            yield
        finally:
            await runtime.http_client.aclose()


def mount_mcp(app: FastAPI, runtime: McpRuntime) -> None:
    """Mount the authenticated ``/mcp`` transport route on ``app``."""

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
