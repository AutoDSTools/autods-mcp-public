"""D1 acceptance — MCP Streamable HTTP transport on FastAPI.

Covers the unauthenticated 401 challenge (reusing the Phase B discovery path),
``tools/list`` over the real in-process MCP client for both an empty manifest
dir (zero tools — the literal D1 milestone) and the products manifest, and an
end-to-end ``tools/call`` that forwards the caller's bearer token to a mocked
upstream — verifying the full middleware → auth → transport → dispatcher path.
"""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.mcp_server.conftest import mcp_client_session


def test_unauthenticated_mcp_request_gets_bearer_challenge(mcp_settings, make_mcp_app, empty_manifest_dir) -> None:
    settings = mcp_settings(manifest_dir=empty_manifest_dir)
    app, _runtime = make_mcp_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"accept": "application/json, text/event-stream"},
        )

    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert ".well-known/oauth-protected-resource" in challenge


async def test_empty_manifest_dir_lists_zero_tools(
    mcp_settings, make_mcp_app, empty_manifest_dir, access_token
) -> None:
    settings = mcp_settings(manifest_dir=empty_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        tools = await session.list_tools()

    assert tools.tools == []


async def test_products_manifest_lists_annotated_tools(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        tools = await session.list_tools()

    by_name = {tool.name: tool for tool in tools.tools}
    assert len(by_name) == 5
    tool = by_name["upload_products"]
    assert tool.annotations.title == "Upload Products"
    assert tool.annotations.readOnlyHint is False


async def test_tool_call_forwards_bearer_to_upstream(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    captured: dict[str, str | None] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.content.decode() or None
        return httpx.Response(200, json={"task_id": "abc"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(
            "upload_products",
            {"store_ids": "store-1", "body": {"title": "Widget"}},
        )

    assert result.isError is False
    assert result.structuredContent == {
        "operation_id": "upload_products",
        "status": 200,
        "ok": True,
        "data": {"task_id": "abc"},
    }
    assert captured["url"] == "https://autods-api.test/products/store-1/"
    assert captured["auth"] == f"Bearer {access_token}"
    assert captured["body"] == '{"title":"Widget"}'


async def test_tool_call_without_auth_context_is_error(mcp_settings, make_mcp_app, empty_manifest_dir) -> None:
    """A bad/expired token never reaches call_tool — the route rejects it first."""
    settings = mcp_settings(manifest_dir=empty_manifest_dir)
    app, runtime = make_mcp_app(settings)

    # No token → the transport route returns 401 before any MCP handshake.
    with pytest.raises(Exception):  # noqa: B017 - client surfaces the 401 as a connection error
        async with mcp_client_session(app, runtime, token=None) as session:
            await session.list_tools()
