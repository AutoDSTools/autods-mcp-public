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
    # 5 AutoDSApi ops + 5 ProductsResearch ops + 1 users op (get_current_user)
    # + 1 locally-served op (get_playbook).
    assert len(by_name) == 12
    tool = by_name["upload_products"]
    assert tool.annotations.title == "Upload Products"
    assert tool.annotations.read_only_hint is False
    # A ProductsResearch read endpoint is advertised read-only.
    assert by_name["get_winning_products"].annotations.read_only_hint is True
    # The RD-68 self-identity op is advertised read-only.
    assert by_name["get_current_user"].annotations.read_only_hint is True


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

    assert result.is_error is False
    assert result.structured_content == {
        "operation_id": "upload_products",
        "status": 200,
        "ok": True,
        "data": {"task_id": "abc"},
        # RD-100: upload_products is step 1 of the `product_import` chain, so
        # the envelope carries the per-step nudge beside `data`.
        "playbook": {
            "name": "product_import",
            "step": "1/3",
            "next": ["get_bulk_action_items"],
            "incomplete_alone": "Nothing is in the store until the bulk job finishes.",
            "runbook": 'get_playbook("product_import")',
        },
    }
    assert captured["url"] == "https://autods-api.test/products/store-1/"
    assert captured["auth"] == f"Bearer {access_token}"
    # The validated body is forwarded verbatim to the upstream.
    assert captured["body"] == '{"region":1,"status":1,"buy_site_id":1,"new_products":[{"asin":"B0X"}]}'


async def test_success_result_shape_matches_the_1x_wire_format(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """RD-99 golden output: the success path must serialize exactly as it did on mcp 1.x.

    Through 1.x the SDK's ``call_tool`` decorator wrapped a handler's plain dict
    into ``structuredContent`` plus one ``text`` block of
    ``json.dumps(payload, indent=2)``. 2.x dropped that wrapping and
    ``_success_result`` reproduces it by hand — the one change in the port that
    live clients could see with *nothing raising anywhere* if it drifted (a
    compact ``json.dumps``, a different block order, a missing
    ``structuredContent``). The literal below was captured from a 1.29.0 run of
    this call, so it pins the bytes rather than the intent.

    The call is ``publish_drafts_to_marketplace`` rather than the originally
    captured ``upload_products`` because that operation has since become step 1
    of a playbook, and a chain step's envelope carries a ``playbook`` sibling of
    ``data`` (RD-100). Only the ``operation_id`` differs from the capture — this
    is the *un-extended* envelope, which is what has to stay byte-stable, and
    the test below pins the extended one separately.
    """

    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"task_id": "abc", "nested": {"n": 1}, "list": [1, 2]})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    body = {"product_status": 1}
    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("publish_drafts_to_marketplace", {"store_ids": "store-1", "body": body})

    payload = {
        "operation_id": "publish_drafts_to_marketplace",
        "status": 200,
        "ok": True,
        "data": {"task_id": "abc", "nested": {"n": 1}, "list": [1, 2]},
    }
    assert result.is_error is False
    # AC: an operation in no playbook gets an envelope identical to pre-RD-100.
    assert result.structured_content == payload
    assert len(result.content) == 1
    block = result.content[0]
    assert block.type == "text"
    assert block.text == (
        '{\n  "operation_id": "publish_drafts_to_marketplace",\n  "status": 200,\n  "ok": true,\n'
        '  "data": {\n    "task_id": "abc",\n    "nested": {\n      "n": 1\n    },\n'
        '    "list": [\n      1,\n      2\n    ]\n  }\n}'
    )
    # …and the same shape on the wire, camelCase aliases included: mcp 2.x model
    # fields are snake_case, so a hand-rolled ``model_dump()`` without
    # ``by_alias=True`` would silently emit ``structured_content``.
    wire = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert wire["structuredContent"] == payload
    assert wire["isError"] is False


async def test_unanticipated_handler_error_becomes_a_typed_error_result(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token, monkeypatch
) -> None:
    """An exception the handler doesn't anticipate must still reach the model.

    mcp 1.x's ``call_tool`` decorator caught everything and returned
    ``isError=True``, so the LLM saw the message and could self-correct. 2.x
    lets a handler exception propagate as a top-level JSON-RPC error the model
    never sees, so ``on_call_tool`` carries its own catch-all. Simulated here by
    making the rate limiter — the first thing the handler awaits after auth —
    blow up in a way no ``except`` clause on the path names.
    """
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async def boom(_sub: str) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(runtime.rate_limiter, "acquire", boom)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("get_current_user", {})

    assert result.is_error is True
    assert result.content[0].text.startswith("internal_error: ")
    # The exception text itself never reaches the client.
    assert "kaboom" not in result.content[0].text


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
