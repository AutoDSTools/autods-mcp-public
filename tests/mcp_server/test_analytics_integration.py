"""End-to-end: the tool-call event fires and logs carry the AutoDS id (RD-63).

Drives the real middleware → auth → identity-resolution → transport → call_tool
path with an injected Mixpanel client (fake tracker). The upstream is mocked to
serve both AutoDSApi's ``get_current_user`` (``/users/list/``) and the tool
endpoint, so the real ``CachedIdentityResolver`` resolves the caller's id/email
from the forwarded token (no AWS/Cognito admin). Asserts:

* "MCP Call Received" fires, keyed by ``autods_user_id``;
* "MCP Call Received" carries the templated upstream endpoint;
* the ``tool_call`` audit line carries ``autods_user_id`` + ``email``.
"""

from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import FastAPI
from structlog.testing import capture_logs

from autods_mcp_server import mcp_transport
from autods_mcp_server.analytics import MixpanelClient
from autods_mcp_server.auth.dependency import jwks_dependency
from autods_mcp_server.mcp_transport import build_runtime, mount_mcp
from tests.mcp_server.conftest import mcp_client_session

_VALID_UPLOAD_BODY = {"region": 1, "status": 1, "buy_site_id": 1, "new_products": [{"asin": "B0TEST123"}]}


def _upstream(request: httpx.Request) -> httpx.Response:
    # AutoDSApi's get_current_user (/users/list/) returns just the caller.
    if request.url.path.endswith("/users/list/"):
        return httpx.Response(200, json=[{"id": 999, "name": "Alice", "email": "alice@example.com"}])
    return httpx.Response(200, json={"task_id": "abc"})


class _FakeTracker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def track(self, distinct_id: str, event_name: str, properties: dict[str, Any] | None = None) -> None:
        self.calls.append((distinct_id, event_name, properties or {}))


async def test_events_fire_and_audit_carries_autods_identity(
    mcp_settings,
    bundled_manifest_dir: Path,
    jwks_client,
    access_token: str,
    monkeypatch,
) -> None:
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    tracker = _FakeTracker()
    mixpanel = MixpanelClient(tracker)
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(_upstream))
    runtime = build_runtime(settings, http_client=upstream, mixpanel=mixpanel)
    app = FastAPI()
    mount_mcp(app, runtime)
    app.dependency_overrides[jwks_dependency] = lambda: jwks_client

    with capture_logs() as logs:
        monkeypatch.setattr(mcp_transport, "_audit_logger", structlog.get_logger("audit-analytics"))
        async with mcp_client_session(app, runtime, token=access_token) as session:
            await session.call_tool("upload_products", {"store_ids": "s1", "body": _VALID_UPLOAD_BODY})
    await mixpanel.drain()

    by_event = {name: (distinct_id, props) for distinct_id, name, props in tracker.calls}

    # The tool-call event fired, keyed by the resolved AutoDS user id (not the sub).
    assert "MCP Call Received" in by_event
    assert all(distinct_id == "999" for distinct_id, _name, _props in tracker.calls)

    # "MCP Call Received" carries the templated upstream endpoint (no substituted ids).
    _distinct, call_props = by_event["MCP Call Received"]
    assert call_props["Remote Endpoint"] == "autods_api POST /products/{store_ids}/"
    assert "time" in call_props

    # The audit line carries the AutoDS identity alongside cognito_username.
    audit = [line for line in logs if line.get("event") == "tool_call"]
    assert len(audit) == 1
    assert audit[0]["cognito_username"] == "user-1"
    assert audit[0]["autods_user_id"] == "999"
    assert audit[0]["email"] == "alice@example.com"
