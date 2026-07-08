"""D1 acceptance — MCP Streamable HTTP transport on FastAPI.

Covers the unauthenticated 401 challenge (reusing the Phase B discovery path),
``tools/list`` over the real in-process MCP client for both an empty manifest
dir (zero tools — the literal D1 milestone) and the products manifest, and an
end-to-end ``tools/call`` that forwards the caller's bearer token to a mocked
upstream — verifying the full middleware → auth → transport → dispatcher path.
"""

from pathlib import Path

import anyio
import httpx
import pytest
import sentry_sdk
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport

from autods_mcp_server.sentry import init_sentry
from autods_mcp_server.settings import Settings
from tests.mcp_server.conftest import TEST_CLIENT_ID, TEST_POOL, mcp_client_session


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
    # 5 AutoDSApi ops + 5 ProductsResearch ops + 1 users op (get_current_user).
    assert len(by_name) == 11
    tool = by_name["upload_products"]
    assert tool.annotations.title == "Upload Products"
    assert tool.annotations.readOnlyHint is False
    # A ProductsResearch read endpoint is advertised read-only.
    assert by_name["get_winning_products"].annotations.readOnlyHint is True
    # The RD-68 self-identity op is advertised read-only.
    assert by_name["get_current_user"].annotations.readOnlyHint is True


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

    body = {"region": 1, "status": 1, "buy_site_id": 1, "new_products": [{"asin": "B0X"}]}
    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(
            "upload_products",
            {"store_ids": "store-1", "body": body},
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
    # The validated body is forwarded verbatim to the upstream.
    assert captured["body"] == '{"region":1,"status":1,"buy_site_id":1,"new_products":[{"asin":"B0X"}]}'


async def test_tool_call_without_auth_context_is_error(mcp_settings, make_mcp_app, empty_manifest_dir) -> None:
    """A bad/expired token never reaches call_tool — the route rejects it first."""
    settings = mcp_settings(manifest_dir=empty_manifest_dir)
    app, runtime = make_mcp_app(settings)

    # No token → the transport route returns 401 before any MCP handshake.
    with pytest.raises(Exception):  # noqa: B017 - client surfaces the 401 as a connection error
        async with mcp_client_session(app, runtime, token=None) as session:
            await session.list_tools()


class _NoopTransport(Transport):
    """Sentry transport that discards everything, so the SDK never hits the network."""

    def capture_envelope(self, envelope: object) -> None:  # pragma: no cover - trivial
        return None


async def test_mcp_initialize_works_with_sentry_initialized(
    env, mcp_settings, make_mcp_app, empty_manifest_dir, access_token, monkeypatch
) -> None:
    """RD-71 regression: an ``initialize`` POST must succeed with the Starlette /
    FastAPI Sentry integrations active.

    This is the case that shipped broken and passed every existing test: those
    integrations' request-info extractor reads the request body *before* the
    route runs, draining the ASGI receive stream so the Streamable-HTTP
    transport's own ``request.body()`` blocks/aborts. The bug was invisible in
    tests because ``init_sentry`` is a no-op locally, so nothing exercised
    Sentry-initialized + the real transport together — exactly what this does.

    Routes through the real ``init_sentry`` (only the network transport is
    stubbed) so it guards the production config, not a hand-rolled copy: drop the
    ``max_request_body_size="never"`` fix and this test fails.
    """
    # Build settings that satisfy init_sentry's non-local guard (it reads only
    # is_local / sentry_url). Kept separate from the app's local settings below —
    # the Sentry client is process-global, so init is independent of the app.
    env(
        MCP_ENV="staging",
        COGNITO_USER_POOL_ID=TEST_POOL,
        COGNITO_REGION="us-west-2",
        ALLOWED_COGNITO_CLIENT_IDS=f'["{TEST_CLIENT_ID}"]',
        COGNITO_PUBLIC_CLIENT_ID=TEST_CLIENT_ID,
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        FORCE_HTTPS="true",
        PUBLIC_HOSTNAME="mcp.test",
        REDIS_URL="redis://localhost:6379/0",
        SENTRY_URL="https://public@sentry.test/1",
    )
    sentry_settings = Settings()  # type: ignore[call-arg]

    # Route through the real init_sentry; only swap the network transport so no
    # event is ever sent, and capture the kwargs to assert the fix is in place.
    captured_init: dict[str, object] = {}
    real_init = sentry_sdk.init

    def _fake_init(*args: object, **kwargs: object) -> object:
        captured_init.update(kwargs)
        kwargs["transport"] = _NoopTransport()
        return real_init(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sentry_sdk, "init", _fake_init)

    try:
        init_sentry(sentry_settings)
        assert sentry_sdk.is_initialized()
        # The fix itself: the integration must be told never to read the body.
        assert captured_init["max_request_body_size"] == "never"

        # The behavioral guard: a real initialize handshake over the transport,
        # with Sentry active, must complete (mcp_client_session calls
        # session.initialize() before yielding).
        settings = mcp_settings(manifest_dir=empty_manifest_dir)
        app, runtime = make_mcp_app(settings)
        # Bound the drive: if the fix regresses so the body read blocks again,
        # fail loudly here instead of hanging CI (the fixed path finishes in <1s).
        with anyio.fail_after(30):
            async with mcp_client_session(app, runtime, token=access_token) as session:
                tools = await session.list_tools()
        assert tools.tools == []
    finally:
        client = sentry_sdk.get_client()
        client.close()
        sentry_sdk.get_global_scope().set_client(None)
