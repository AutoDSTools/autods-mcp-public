"""D4 + D6 acceptance — operation dispatcher and multi-source routing.

Uses an ``httpx.MockTransport`` upstream so each dispatch is deterministic.
Asserts URL construction (path/query params), bearer-token forwarding, JSON
body forwarding, and that operations route to different upstreams by
``base_url_key`` within one registry (D6).
"""

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from autods_mcp_server.auth import UserContext
from autods_mcp_server.dispatch import (
    MissingArgumentError,
    OperationDispatcher,
    UnknownOperationError,
    UpstreamRequestError,
)
from autods_mcp_server.manifests import ManifestRegistry
from autods_mcp_server.manifests.schema import Manifest
from autods_mcp_server.settings import Settings


@pytest.fixture
def user() -> UserContext:
    return UserContext(sub="user-1", raw_token=SecretStr("the-user-token"))


def _settings(mcp_settings) -> Settings:
    return mcp_settings()


def _registry(*operations: dict[str, Any], base_url_key: str = "autods_api") -> ManifestRegistry:
    manifest = Manifest.model_validate(
        {"server_name": "demo", "base_url_key": base_url_key, "operations": list(operations)}
    )
    return ManifestRegistry([manifest])


def _dispatcher(registry: ManifestRegistry, settings: Settings, handler) -> OperationDispatcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OperationDispatcher(registry, settings, client)


async def test_get_builds_url_query_and_forwards_token(mcp_settings, user) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    registry = _registry(
        {
            "operation_id": "ai_history",
            "method": "GET",
            "path": "/ai/{product_id}/history",
            "parameters": [
                {"name": "product_id", "in": "path", "required": True, "schema_type": "str"},
                {"name": "limit", "in": "query", "required": False, "schema_type": "int"},
            ],
            "annotations": {"title": "Hist", "readOnlyHint": True},
        }
    )
    dispatcher = _dispatcher(registry, _settings(mcp_settings), handler)

    result = await dispatcher.dispatch("ai_history", {"product_id": "abc123", "limit": 5}, user)

    assert captured["method"] == "GET"
    assert captured["url"] == "https://autods-api.test/ai/abc123/history?limit=5"
    assert captured["auth"] == "Bearer the-user-token"
    assert result.ok is True
    assert result.status == 200
    assert result.data == {"ok": True}


async def test_post_forwards_json_body(mcp_settings, user) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(202, json={"task_id": "t1"})

    registry = _registry(
        {
            "operation_id": "import_untracked",
            "method": "POST",
            "path": "/products/import",
            "has_json_body": True,
            "request_body_required": True,
            "annotations": {"title": "Import", "readOnlyHint": False, "destructiveHint": False},
        }
    )
    dispatcher = _dispatcher(registry, _settings(mcp_settings), handler)

    result = await dispatcher.dispatch("import_untracked", {"body": {"file_url": "http://x/y.csv"}}, user)

    assert captured["url"] == "https://autods-api.test/products/import"
    assert captured["body"] == '{"file_url":"http://x/y.csv"}'
    assert "application/json" in captured["content_type"]
    assert result.status == 202


async def test_routes_to_different_upstreams_by_base_url_key(mcp_settings, user) -> None:
    """D6: one registry, two ops, two upstreams resolved from Settings."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.scheme}://{request.url.host}")
        return httpx.Response(200, json={})

    registry = _registry(
        {
            "operation_id": "on_autods_api",
            "method": "GET",
            "path": "/a",
            "base_url_key": "autods_api",
            "annotations": {"title": "A", "readOnlyHint": True},
        },
        {
            "operation_id": "on_products_research",
            "method": "GET",
            "path": "/b",
            "base_url_key": "products_research",
            "annotations": {"title": "B", "readOnlyHint": True},
        },
    )
    dispatcher = _dispatcher(registry, _settings(mcp_settings), handler)

    await dispatcher.dispatch("on_autods_api", {}, user)
    await dispatcher.dispatch("on_products_research", {}, user)

    assert seen == ["https://autods-api.test", "https://products-research.test"]


async def test_unknown_operation_raises(mcp_settings, user) -> None:
    dispatcher = _dispatcher(_registry(), _settings(mcp_settings), lambda r: httpx.Response(200))
    with pytest.raises(UnknownOperationError):
        await dispatcher.dispatch("nope", {}, user)


async def test_missing_required_param_raises(mcp_settings, user) -> None:
    registry = _registry(
        {
            "operation_id": "needs_id",
            "method": "GET",
            "path": "/x/{id}",
            "parameters": [{"name": "id", "in": "path", "required": True, "schema_type": "str"}],
            "annotations": {"title": "X", "readOnlyHint": True},
        }
    )
    dispatcher = _dispatcher(registry, _settings(mcp_settings), lambda r: httpx.Response(200))
    with pytest.raises(MissingArgumentError):
        await dispatcher.dispatch("needs_id", {}, user)


async def test_header_param_and_omitted_optional(mcp_settings, user) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["x_region"] = request.headers.get("x-region")
        captured["url"] = str(request.url)
        return httpx.Response(200, json={})

    registry = _registry(
        {
            "operation_id": "with_header",
            "method": "GET",
            "path": "/h",
            "parameters": [
                {"name": "x-region", "in": "header", "required": True, "schema_type": "str"},
                {"name": "page", "in": "query", "required": False, "schema_type": "int"},
            ],
            "annotations": {"title": "H", "readOnlyHint": True},
        }
    )
    dispatcher = _dispatcher(registry, _settings(mcp_settings), handler)

    await dispatcher.dispatch("with_header", {"x-region": "us"}, user)

    assert captured["x_region"] == "us"
    # Omitted optional query param doesn't appear on the URL.
    assert captured["url"] == "https://autods-api.test/h"


async def test_missing_required_body_raises(mcp_settings, user) -> None:
    registry = _registry(
        {
            "operation_id": "needs_body",
            "method": "POST",
            "path": "/b",
            "has_json_body": True,
            "request_body_required": True,
            "annotations": {"title": "B", "readOnlyHint": False, "destructiveHint": False},
        }
    )
    dispatcher = _dispatcher(registry, _settings(mcp_settings), lambda r: httpx.Response(200))
    with pytest.raises(MissingArgumentError, match="request body"):
        await dispatcher.dispatch("needs_body", {}, user)


async def test_non_json_response_returns_text(mcp_settings, user) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream boom", headers={"content-type": "text/plain"})

    registry = _registry(
        {
            "operation_id": "plain",
            "method": "GET",
            "path": "/p",
            "annotations": {"title": "P", "readOnlyHint": True},
        }
    )
    dispatcher = _dispatcher(registry, _settings(mcp_settings), handler)

    result = await dispatcher.dispatch("plain", {}, user)
    assert result.ok is False
    assert result.status == 500
    assert result.data == "upstream boom"


async def test_upstream_transport_error_raises_dispatch_error(mcp_settings, user) -> None:
    """A transport-level failure (timeout, connection error) becomes an
    ``UpstreamRequestError`` — a ``DispatchError`` the transport turns into a
    clean MCP tool error rather than letting it escape ``call_tool``."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("upstream did not respond", request=request)

    registry = _registry(
        {
            "operation_id": "slow",
            "method": "GET",
            "path": "/p",
            "annotations": {"title": "P", "readOnlyHint": True},
        }
    )
    dispatcher = _dispatcher(registry, _settings(mcp_settings), handler)

    with pytest.raises(UpstreamRequestError, match="slow"):
        await dispatcher.dispatch("slow", {}, user)
