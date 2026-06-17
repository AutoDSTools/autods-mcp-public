"""Unit tests for the Mixpanel analytics client (RD-63).

Assert the AutoDSApi-compatible event shape: the AutoDS user id is the
``distinct_id`` (not a property), ``time`` is integer epoch seconds, event names
are Title Case, and tracking is fire-and-forget + exception-isolated.
"""

import asyncio
import threading
from datetime import UTC, datetime
from typing import Any

import autods_mcp_server.analytics as analytics
from autods_mcp_server.analytics import (
    EVENT_MCP_CALL_RECEIVED,
    MixpanelClient,
    build_mixpanel,
)
from autods_mcp_server.settings import Settings

_WHEN = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
_WHEN_EPOCH = int(_WHEN.timestamp())


class _FakeTracker:
    def __init__(self, *, raise_on_track: bool = False) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._raise = raise_on_track

    def track(self, distinct_id: str, event_name: str, properties: dict[str, Any] | None = None) -> None:
        if self._raise:
            raise RuntimeError("mixpanel down")
        self.calls.append((distinct_id, event_name, properties or {}))


async def test_mcp_call_received_carries_remote_endpoint_and_time() -> None:
    tracker = _FakeTracker()
    client = MixpanelClient(tracker)

    client.track_mcp_call_received("999", remote_endpoint="autods_api POST /products/{store_ids}/", when=_WHEN)
    await client.drain()

    distinct_id, event_name, properties = tracker.calls[0]
    assert distinct_id == "999"
    assert event_name == EVENT_MCP_CALL_RECEIVED
    assert properties == {"Remote Endpoint": "autods_api POST /products/{store_ids}/", "time": _WHEN_EPOCH}


async def test_disabled_client_is_a_noop() -> None:
    client = MixpanelClient(None)
    assert client.enabled is False

    client.track_mcp_call_received("999", remote_endpoint="x", when=_WHEN)
    await client.drain()  # must not raise


async def test_tracking_failure_is_swallowed() -> None:
    tracker = _FakeTracker(raise_on_track=True)
    client = MixpanelClient(tracker)

    client.track_mcp_call_received("999", remote_endpoint="x", when=_WHEN)
    # A Mixpanel outage must not surface to the caller.
    await client.drain()


class _BlockingTracker:
    """A tracker whose ``track`` blocks (in the worker thread) until released —
    lets a test hold sends in-flight to exercise the pending cap / drain timeout."""

    def __init__(self) -> None:
        self.calls = 0
        self._release = threading.Event()

    def track(self, distinct_id: str, event_name: str, properties: dict[str, Any] | None = None) -> None:
        self.calls += 1
        self._release.wait(timeout=2.0)

    def release(self) -> None:
        self._release.set()


async def test_pending_overflow_drops_excess_events(monkeypatch) -> None:
    """Past the in-flight cap, events are dropped rather than queued unboundedly."""
    monkeypatch.setattr(analytics, "_MAX_PENDING", 1)
    tracker = _BlockingTracker()
    client = MixpanelClient(tracker)

    # First send occupies the single pending slot (blocks in its worker thread).
    client.track_mcp_call_received("1", remote_endpoint="x", when=_WHEN)
    # Second send sees the cap already reached → dropped, never scheduled.
    client.track_mcp_call_received("2", remote_endpoint="x", when=_WHEN)

    tracker.release()
    await client.drain()

    assert tracker.calls == 1  # only the first send ran; the second was shed


async def test_drain_is_time_bounded(monkeypatch) -> None:
    """A still-blocked send must not hold up shutdown past the drain timeout."""
    monkeypatch.setattr(analytics, "_DRAIN_TIMEOUT_SECONDS", 0.05)
    tracker = _BlockingTracker()
    client = MixpanelClient(tracker)

    client.track_mcp_call_received("1", remote_endpoint="x", when=_WHEN)
    # Send is still blocked; drain must return on its own timeout, not hang.
    await asyncio.wait_for(client.drain(), timeout=1.0)

    tracker.release()  # let the abandoned worker thread exit


def test_build_mixpanel_disabled_without_token(env) -> None:
    env(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="us-west-2_TESTPOOL",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID="c",
        ALLOWED_COGNITO_CLIENT_IDS='["c"]',
    )
    assert build_mixpanel(Settings()).enabled is False  # type: ignore[call-arg]


def test_build_mixpanel_enabled_with_token(env) -> None:
    env(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="us-west-2_TESTPOOL",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID="c",
        ALLOWED_COGNITO_CLIENT_IDS='["c"]',
        MIXPANEL_TOKEN="tok-123",
    )
    client = build_mixpanel(Settings())  # type: ignore[call-arg]
    assert client.enabled is True
    # The SDK send is bounded by a request timeout (not the SDK default of None).
    consumer = client._tracker._consumer  # type: ignore[union-attr]
    assert consumer._request_timeout == analytics._REQUEST_TIMEOUT_SECONDS
