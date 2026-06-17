"""Mixpanel product-analytics integration (RD-63).

Emits a usage event for the public MCP server:

* **MCP Call Received** — on each tool call that arrives from a client.

The shape mirrors AutoDSApi's ``MixPanelIntegration`` so the two projects'
events line up in Mixpanel:

* the **AutoDS user id is the ``distinct_id``** (the first positional arg to
  ``Mixpanel.track``), never an event property;
* ``time`` is an integer UTC epoch-seconds value in the event properties;
* event names are Title Case strings and property names are Title Case with
  spaces (e.g. ``Remote Endpoint``).

Tracking is **fire-and-forget and fails open**: the underlying ``mixpanel`` SDK
does a *blocking* HTTP POST, so every send is offloaded to a worker thread and
any error is swallowed (logged, never raised) — a Mixpanel outage must not fail
auth or a tool call. When no token is configured the client is a no-op, so the
server boots and runs locally with analytics disabled.

Because the send is best-effort, it is also **bounded** so a slow/hung Mixpanel
can't pin worker threads or grow an unbounded backlog: the SDK send is capped
with a request timeout + minimal retries, concurrent in-flight sends are capped
(excess events are dropped), and the shutdown drain is time-bounded.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from autods_mcp_server.logging import get_logger
from autods_mcp_server.settings import Settings

_logger = get_logger("autods_mcp_server.analytics")

EVENT_MCP_CALL_RECEIVED = "MCP Call Received"

# Bound the blocking SDK send (offloaded to a worker thread). The SDK defaults to
# *no* request timeout and 4 retries, so a slow/hung Mixpanel could pin the thread
# indefinitely; cap both. Analytics is best-effort, so a dropped retry is cheaper
# than a held thread.
_REQUEST_TIMEOUT_SECONDS = 3.0
_RETRY_LIMIT = 1

# Hard ceiling on concurrent in-flight sends. Past this we drop the event rather
# than let a backlog of blocked sends grow the pending set / thread-pool queue
# during a Mixpanel outage (best-effort analytics — shed load, never block).
_MAX_PENDING = 256

# Upper bound on how long shutdown waits for in-flight sends to flush.
_DRAIN_TIMEOUT_SECONDS = 10.0


class _Tracker(Protocol):
    """The slice of ``mixpanel.Mixpanel`` we use (so tests can substitute a fake)."""

    def track(self, distinct_id: str, event_name: str, properties: dict[str, Any] | None = ...) -> None: ...


def _epoch_seconds(when: datetime) -> int:
    """UTC epoch seconds for ``when`` — the value Mixpanel's ``time`` expects."""
    return int(when.timestamp())


class MixpanelClient:
    """Fire-and-forget wrapper over the Mixpanel SDK.

    Construct with ``tracker=None`` for a disabled (no-op) client — the local
    default when ``MIXPANEL_TOKEN`` is unset.
    """

    def __init__(self, tracker: _Tracker | None) -> None:
        self._tracker = tracker
        # Strong refs to in-flight send tasks: asyncio keeps only weak refs to
        # tasks, so without this a fire-and-forget send could be GC'd mid-flight.
        self._pending: set[asyncio.Task[None]] = set()

    @property
    def enabled(self) -> bool:
        return self._tracker is not None

    def track_mcp_call_received(
        self,
        distinct_id: str,
        *,
        remote_endpoint: str,
        when: datetime | None = None,
    ) -> None:
        """Emit "MCP Call Received" for ``distinct_id`` (the AutoDS user id).

        ``remote_endpoint`` is the upstream endpoint the server forwards to —
        the templated ``METHOD /path`` (never the substituted URL).
        """
        self._emit(distinct_id, EVENT_MCP_CALL_RECEIVED, {"Remote Endpoint": remote_endpoint}, when)

    def _emit(self, distinct_id: str, event_name: str, properties: dict[str, Any], when: datetime | None) -> None:
        if self._tracker is None:
            return
        params = {**properties, "time": _epoch_seconds(when or datetime.now(UTC))}
        self._schedule(self._send, distinct_id, event_name, params)

    def _send(self, distinct_id: str, event_name: str, properties: dict[str, Any]) -> None:
        """Blocking send, exception-isolated — the thread-pool target."""
        try:
            self._tracker.track(distinct_id, event_name, properties)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 — tracking must never raise into the caller.
            _logger.warning("mixpanel_track_failed", event=event_name, error=str(exc))

    def _schedule(self, fn: Callable[..., None], *args: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (e.g. a synchronous caller / direct unit test):
            # run inline. ``fn`` is already exception-isolated.
            fn(*args)
            return
        if len(self._pending) >= _MAX_PENDING:
            # Best-effort analytics: shed load rather than grow an unbounded
            # backlog of blocked sends (e.g. during a Mixpanel outage).
            _logger.warning("mixpanel_pending_overflow", pending=len(self._pending))
            return
        task = loop.create_task(asyncio.to_thread(fn, *args))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self) -> None:
        """Await in-flight sends on app shutdown, bounded so a slow Mixpanel can't
        hold up shutdown (best effort — any stragglers past the timeout are
        abandoned)."""
        if not self._pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._pending, return_exceptions=True),
                timeout=_DRAIN_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            _logger.warning("mixpanel_drain_timeout", pending=len(self._pending))


def build_mixpanel(settings: Settings) -> MixpanelClient:
    """Build the Mixpanel client from settings (no-op when no token is set)."""
    if not settings.mixpanel_token:
        return MixpanelClient(None)
    # Imported lazily so the dependency is only touched when tracking is enabled.
    from mixpanel import Consumer, Mixpanel

    # Cap the blocking send: the SDK defaults to no timeout + 4 retries, which
    # could pin a worker thread on a slow/hung Mixpanel (see the constants above).
    consumer = Consumer(request_timeout=_REQUEST_TIMEOUT_SECONDS, retry_limit=_RETRY_LIMIT)
    return MixpanelClient(Mixpanel(settings.mixpanel_token, consumer=consumer))
