"""Phase F acceptance at the transport seam.

Exercises the full middleware → auth → transport → call_tool path with a mocked
upstream:

* **F0** — the session manager is stateless; no session is retained between
  requests.
* **F1** — the per-user rate limit short-circuits the call with a ``rate_limited``
  error once the bucket is exhausted.
* **F2** — one structured ``tool_call`` audit line per call, all fields present,
  no payload body.
* **F3** — upstream 401/403/4xx/5xx map to typed, sanitized MCP errors.
"""

import json
from pathlib import Path

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from autods_mcp_server import mcp_transport
from autods_mcp_server.logging import configure_logging
from autods_mcp_server.ratelimit import BucketSpec, InMemoryRateLimiter
from autods_mcp_server.settings import Settings
from tests.mcp_server.conftest import mcp_client_session


def _ok_upstream(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"task_id": "abc"})


# --- F0: stateless transport ------------------------------------------------


async def test_transport_is_stateless_and_retains_no_sessions(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=_ok_upstream)

    assert runtime.session_manager.stateless is True

    async with mcp_client_session(app, runtime, token=access_token) as session:
        await session.call_tool("upload_products", {"store_ids": "s1", "body": {"t": "x"}})

    # Nothing is kept between requests — the dict that pins stateful sessions
    # to a worker stays empty, so any replica/worker can serve any request.
    assert runtime.session_manager._server_instances == {}


# --- F1: per-user rate limiting ---------------------------------------------


async def test_rate_limit_blocks_after_capacity(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    # Capacity 1, slow refill → the second call within the test is blocked.
    limiter = InMemoryRateLimiter([BucketSpec("minute", capacity=1, refill_rate=1 / 60)])
    app, runtime = make_mcp_app(settings, upstream_handler=_ok_upstream, rate_limiter=limiter)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        first = await session.call_tool("upload_products", {"store_ids": "s1", "body": {"t": "x"}})
        second = await session.call_tool("upload_products", {"store_ids": "s1", "body": {"t": "x"}})

    assert first.isError is False
    assert second.isError is True
    assert second.content[0].text.startswith("rate_limited: ")
    assert "Retry after" in second.content[0].text


# --- F2: audit logging ------------------------------------------------------


async def test_successful_call_emits_one_audit_line(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token, monkeypatch
) -> None:
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=_ok_upstream)

    with capture_logs() as logs:
        # Fresh proxy so it binds to capture_logs' processor (defeats any
        # cached bound logger from an earlier test).
        monkeypatch.setattr(mcp_transport, "_audit_logger", structlog.get_logger("audit-test"))
        async with mcp_client_session(app, runtime, token=access_token) as session:
            await session.call_tool("upload_products", {"store_ids": "s1", "body": {"secret": "x"}})

    audit = [line for line in logs if line.get("event") == "tool_call"]
    assert len(audit) == 1
    line = audit[0]
    assert line["user_sub"] == "user-1"
    assert line["tool_name"] == "upload_products"
    assert line["op_id"] == "upload_products"
    assert line["upstream_url"] == "https://autods-api.test/products/s1/"
    assert line["upstream_status"] == 200
    assert "latency_ms" in line
    assert "error_type" not in line  # success → omitted
    # No payload bodies are ever logged (PII risk).
    assert "body" not in line
    assert "arguments" not in line
    assert "secret" not in json.dumps(line)


def test_audit_line_carries_request_id_and_timestamp(env, capsys) -> None:
    """Rendered through the real processor chain, the audit line has ts +
    request_id (from contextvars) alongside the explicit fields."""
    env(
        MCP_ENV="staging",
        COGNITO_USER_POOL_ID="us-west-2_TESTPOOL",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID="c",
        ALLOWED_COGNITO_CLIENT_IDS='["c"]',
        FORCE_HTTPS="true",
        PUBLIC_HOSTNAME="mcp.autods.com",
        REDIS_URL="redis://localhost:6379/0",
    )
    configure_logging(Settings())  # staging → JSON renderer

    structlog.contextvars.bind_contextvars(request_id="req-xyz")
    try:
        # Unique name → fresh proxy → binds to the JSON config just set.
        structlog.get_logger("autods_mcp_server.audit.contract").info(
            "tool_call",
            user_sub="u",
            tool_name="t",
            op_id="t",
            upstream_url="https://x",
            upstream_status=200,
            latency_ms=1.0,
        )
    finally:
        structlog.contextvars.clear_contextvars()

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    for field in (
        "request_id",
        "timestamp",
        "user_sub",
        "tool_name",
        "op_id",
        "upstream_url",
        "upstream_status",
        "latency_ms",
    ):
        assert field in line, f"missing audit field: {field}"
    assert line["request_id"] == "req-xyz"


async def test_upstream_5xx_audit_records_detail_separately(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token, monkeypatch
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "db pool exhausted at pg-internal:5432"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    with capture_logs() as logs:
        monkeypatch.setattr(mcp_transport, "_audit_logger", structlog.get_logger("audit-test2"))
        async with mcp_client_session(app, runtime, token=access_token) as session:
            result = await session.call_tool("upload_products", {"store_ids": "s1", "body": {"t": "x"}})

    # User sees a generic error; internal hostname is not leaked.
    assert result.isError is True
    assert result.content[0].text.startswith("upstream_error: ")
    assert "pg-internal" not in result.content[0].text

    audit = [line for line in logs if line.get("event") == "tool_call"]
    assert audit[0]["error_type"] == "upstream_error"
    assert audit[0]["upstream_status"] == 503
    # Full detail is preserved in a separate server-side log line.
    detail_lines = [line for line in logs if line.get("event") == "upstream_error_detail"]
    assert detail_lines and "pg-internal" in json.dumps(detail_lines[0]["detail"])


# --- F3: upstream error mapping (end-to-end) --------------------------------


@pytest.mark.parametrize(
    ("status", "prefix"),
    [
        (401, "unauthenticated: "),
        (403, "forbidden: "),
        (422, "upstream_client_error: "),
    ],
)
async def test_upstream_client_errors_map_to_typed_results(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token, status: int, prefix: str
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "store_id is required"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("upload_products", {"store_ids": "s1", "body": {"t": "x"}})

    assert result.isError is True
    assert result.content[0].text.startswith(prefix)


async def test_upstream_4xx_forwards_sanitized_detail(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "store_id is required"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("upload_products", {"store_ids": "s1", "body": {"t": "x"}})

    assert "store_id is required" in result.content[0].text
