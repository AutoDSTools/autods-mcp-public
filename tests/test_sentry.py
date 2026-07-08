"""Tests for the Sentry integration (RD-66).

Cover the no-op contract (local / no DSN), the enabled init, the context
helpers (user identity, request id, tool call), the explicit captures for the
handled-failure paths, and — most importantly — that neither the bearer token
nor sensitive argument values ever reach an event.
"""

import logging
from collections.abc import Iterator
from typing import Any

import pytest
import sentry_sdk
from pydantic import SecretStr
from sentry_sdk.transport import Transport
from starlette.requests import ClientDisconnect

from autods_mcp_server import sentry as sentry_integration
from autods_mcp_server.auth import UserContext
from autods_mcp_server.settings import Settings

_BASE_ENV = {
    "COGNITO_USER_POOL_ID": "us-west-2_pool",
    "COGNITO_DOMAIN": "auth.example.com",
    "COGNITO_PUBLIC_CLIENT_ID": "public-client",
    "ALLOWED_COGNITO_CLIENT_IDS": '["public-client"]',
}


def _settings(env, **overrides: str) -> Settings:
    env(**{**_BASE_ENV, **overrides})
    return Settings()  # type: ignore[call-arg]


class _CapturingTransport(Transport):
    """Collects each event's serialized payload so tests can assert on it."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        super().__init__()
        self._events = events

    def capture_envelope(self, envelope: Any) -> None:
        for item in envelope.items:
            payload = item.payload.json
            if payload is not None:
                self._events.append(payload)


@pytest.fixture
def captured_events() -> Iterator[list[dict[str, Any]]]:
    """Init Sentry with a capturing transport; reset the global client after."""
    events: list[dict[str, Any]] = []
    sentry_sdk.init(
        dsn="https://public@sentry.autods.com/42",
        environment="staging",
        release="develop-1",
        transport=_CapturingTransport(events),
    )
    try:
        yield events
    finally:
        client = sentry_sdk.get_client()
        client.close()
        sentry_sdk.get_global_scope().set_client(None)


def _user() -> UserContext:
    return UserContext(
        sub="cognito-sub-1",
        email="user@example.com",
        groups=["sellers", "beta"],
        raw_token=SecretStr("SUPER-SECRET-BEARER-TOKEN"),
        autods_user_id="autods-999",
    )


# --- init: no-op contract -------------------------------------------------


def test_init_is_noop_in_local(env) -> None:
    settings = _settings(env, MCP_ENV="local", SENTRY_URL="https://p@sentry.autods.com/1")
    sentry_integration.init_sentry(settings)
    assert not sentry_sdk.is_initialized()


def test_init_is_noop_without_dsn(env) -> None:
    settings = _settings(env, MCP_ENV="staging", FORCE_HTTPS="true", PUBLIC_HOSTNAME="mcp.x", REDIS_URL="redis://x")
    assert settings.sentry_url is None
    sentry_integration.init_sentry(settings)
    assert not sentry_sdk.is_initialized()


def test_init_enables_in_non_local_with_dsn(env) -> None:
    settings = _settings(
        env,
        MCP_ENV="staging",
        FORCE_HTTPS="true",
        PUBLIC_HOSTNAME="mcp.x",
        REDIS_URL="redis://x",
        SENTRY_URL="https://public@sentry.autods.com/7",
    )
    try:
        sentry_integration.init_sentry(settings)
        assert sentry_sdk.is_initialized()
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.get_global_scope().set_client(None)


# --- settings -------------------------------------------------------------


def test_sentry_environment_defaults_to_mcp_env(env) -> None:
    assert _settings(env, MCP_ENV="local").sentry_environment == "local"


def test_sentry_environment_respects_explicit_value(env) -> None:
    settings = _settings(
        env,
        MCP_ENV="staging",
        FORCE_HTTPS="true",
        PUBLIC_HOSTNAME="mcp.x",
        REDIS_URL="redis://x",
        SENTRY_ENVIRONMENT="prod",
    )
    assert settings.sentry_environment == "prod"


# --- helpers are safe no-ops when Sentry isn't initialized ----------------


def test_helpers_are_noops_when_uninitialized() -> None:
    assert not sentry_sdk.is_initialized()
    # None of these should raise despite no active client.
    sentry_integration.identify_user(_user(), client_id="c")
    sentry_integration.set_request_id("req-1")
    sentry_integration.set_tool_context(tool_name="t", operation_id="t", arguments={})
    sentry_integration.capture_tool_error("m", error_type="upstream_error", tool_name="t")
    sentry_integration.capture_tool_exception(ValueError("x"), error_type="internal_error", tool_name="t")


# --- context enrichment ---------------------------------------------------


def test_identify_user_sets_identity_and_tags(captured_events) -> None:
    with sentry_sdk.isolation_scope():
        sentry_integration.identify_user(_user(), client_id="claude-client")
        sentry_sdk.capture_message("hello")
    event = captured_events[-1]
    # AutoDS user id (RD-63) is the stable id; the Cognito sub rides along.
    assert event["user"]["id"] == "autods-999"
    assert event["user"]["email"] == "user@example.com"
    assert event["user"]["cognito_sub"] == "cognito-sub-1"
    assert event["tags"]["client_id"] == "claude-client"
    assert event["tags"]["cognito_sub"] == "cognito-sub-1"
    assert event["tags"]["cognito_groups"] == "sellers,beta"


def test_identify_user_falls_back_to_sub_when_no_autods_id(captured_events) -> None:
    user = UserContext(sub="only-sub", raw_token=SecretStr("t"))
    with sentry_sdk.isolation_scope():
        sentry_integration.identify_user(user, client_id=None)
        sentry_sdk.capture_message("hello")
    event = captured_events[-1]
    assert event["user"]["id"] == "only-sub"
    assert "email" not in event["user"]
    assert "client_id" not in event.get("tags", {})


def test_request_id_and_tool_context_tags(captured_events) -> None:
    with sentry_sdk.isolation_scope():
        sentry_integration.set_request_id("req-42")
        sentry_integration.set_tool_context(
            tool_name="list_products", operation_id="list_products", arguments={"store_id": "s1"}
        )
        sentry_sdk.capture_message("hello")
    event = captured_events[-1]
    assert event["tags"]["request_id"] == "req-42"
    assert event["tags"]["mcp.tool"] == "list_products"
    assert event["tags"]["mcp.operation_id"] == "list_products"
    assert event["contexts"]["mcp_tool_call"]["arguments"] == {"store_id": "s1"}


# --- captures for the handled-failure paths -------------------------------


def test_capture_tool_exception_records_exception_and_upstream(captured_events) -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        sentry_integration.capture_tool_exception(
            exc, error_type="upstream_unreachable", tool_name="t", upstream_url="http://internal:8000"
        )
    event = captured_events[-1]
    assert event["exception"]["values"][-1]["type"] == "RuntimeError"
    assert event["tags"]["error_type"] == "upstream_unreachable"
    assert event["contexts"]["upstream"]["url"] == "http://internal:8000"


def test_capture_tool_error_records_message_level_and_detail(captured_events) -> None:
    sentry_integration.capture_tool_error(
        "upstream_error: HTTP 500",
        error_type="upstream_error",
        tool_name="t",
        upstream_url="http://internal:8000",
        upstream_status=500,
        detail={"msg": "boom"},
    )
    event = captured_events[-1]
    assert event["level"] == "error"
    assert event["tags"]["error_type"] == "upstream_error"
    assert event["contexts"]["upstream"]["status"] == 500
    assert event["contexts"]["upstream"]["detail"] == {"msg": "boom"}


# --- the security invariant: secrets never leave ---------------------------


def test_bearer_token_never_reaches_the_event(captured_events) -> None:
    import json

    with sentry_sdk.isolation_scope():
        sentry_integration.identify_user(_user(), client_id="claude-client")
        sentry_sdk.capture_message("hello")
    assert "SUPER-SECRET-BEARER-TOKEN" not in json.dumps(captured_events[-1])


def test_sensitive_arguments_are_redacted(captured_events) -> None:
    import json

    arguments = {
        "store_id": "s1",
        "password": "hunter2",
        "nested": {"access_token": "zzz", "ok": 1},
        "list": [{"secret": "q"}],
    }
    with sentry_sdk.isolation_scope():
        sentry_integration.set_tool_context(tool_name="t", operation_id="t", arguments=arguments)
        sentry_sdk.capture_message("hello")
    event = captured_events[-1]
    redacted = event["contexts"]["mcp_tool_call"]["arguments"]
    assert redacted["store_id"] == "s1"
    assert redacted["nested"]["ok"] == 1
    assert redacted["password"] == "[Filtered]"
    assert redacted["nested"]["access_token"] == "[Filtered]"
    assert redacted["list"][0]["secret"] == "[Filtered]"
    blob = json.dumps(event)
    assert "hunter2" not in blob and "zzz" not in blob


def test_upstream_detail_is_redacted(captured_events) -> None:
    sentry_integration.capture_tool_error(
        "upstream_error: HTTP 500",
        error_type="upstream_error",
        tool_name="t",
        detail={"password": "nested-secret", "msg": "boom"},
    )
    detail = captured_events[-1]["contexts"]["upstream"]["detail"]
    assert detail == {"password": "[Filtered]", "msg": "boom"}


# --- before_send: drop benign MCP client-disconnect noise (RD-71) ----------


def _log_record(name: str, message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=logging.ERROR, pathname=__file__, lineno=1, msg=message, args=(), exc_info=None
    )


def test_before_send_drops_streamable_http_disconnect_exception() -> None:
    """Case 1 (AUTODS-MCP-PUBLIC-3): logger.exception with a live ClientDisconnect."""
    hint = {"exc_info": (ClientDisconnect, ClientDisconnect(), None)}
    assert sentry_integration._drop_client_disconnect_noise({}, hint) is None


def test_before_send_drops_disconnect_wrapped_in_chain() -> None:
    """A ClientDisconnect reached only via the __context__/__cause__ chain still drops."""
    try:
        try:
            raise ClientDisconnect()
        except ClientDisconnect as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        hint = {"exc_info": (type(outer), outer, outer.__traceback__)}
    assert sentry_integration._drop_client_disconnect_noise({}, hint) is None


def test_before_send_drops_lowlevel_stream_relog() -> None:
    """Case 2 (AUTODS-MCP-PUBLIC-4): logger.error re-log with no exc_info."""
    hint = {"log_record": _log_record("mcp.server.lowlevel.server", "Received exception from stream: ")}
    assert sentry_integration._drop_client_disconnect_noise({}, hint) is None


def test_before_send_keeps_unrelated_exception() -> None:
    hint = {"exc_info": (ValueError, ValueError("boom"), None)}
    event = {"exception": {"values": [{"type": "ValueError"}]}}
    assert sentry_integration._drop_client_disconnect_noise(event, hint) is event


def test_before_send_keeps_same_message_from_other_logger() -> None:
    """The message-prefix match is scoped to the low-level session logger only."""
    hint = {"log_record": _log_record("some.other.logger", "Received exception from stream: ")}
    event = {"logger": "some.other.logger"}
    assert sentry_integration._drop_client_disconnect_noise(event, hint) is event


def test_before_send_keeps_other_error_from_stream_logger() -> None:
    """A genuine error from the low-level logger (different message) still reaches Sentry."""
    hint = {"log_record": _log_record("mcp.server.lowlevel.server", "Something actually broke")}
    event = {"logger": "mcp.server.lowlevel.server"}
    assert sentry_integration._drop_client_disconnect_noise(event, hint) is event


def test_before_send_keeps_event_without_hint() -> None:
    event = {"message": "plain capture_message"}
    assert sentry_integration._drop_client_disconnect_noise(event, {}) is event


@pytest.fixture
def filtered_events() -> Iterator[list[dict[str, Any]]]:
    """Like ``captured_events`` but wires the real ``before_send`` filter, so the
    LoggingIntegration -> before_send path is exercised end-to-end."""
    events: list[dict[str, Any]] = []
    sentry_sdk.init(
        dsn="https://public@sentry.autods.com/42",
        environment="staging",
        release="develop-1",
        transport=_CapturingTransport(events),
        before_send=sentry_integration._drop_client_disconnect_noise,
    )
    try:
        yield events
    finally:
        client = sentry_sdk.get_client()
        client.close()
        sentry_sdk.get_global_scope().set_client(None)


def test_end_to_end_disconnect_logs_are_dropped_but_real_errors_survive(filtered_events) -> None:
    """Drive the actual SDK loggers through the LoggingIntegration: both disconnect
    shapes are filtered while an unrelated ERROR log is still captured."""
    # Case 1: streamable_http logs the live disconnect via logger.exception.
    try:
        raise ClientDisconnect()
    except ClientDisconnect:
        logging.getLogger("mcp.server.streamable_http").exception("Error handling POST request")

    # Case 2: the low-level session re-logs it without an exception.
    logging.getLogger("mcp.server.lowlevel.server").error("Received exception from stream: ")

    # An unrelated genuine error must still reach Sentry.
    logging.getLogger("autods_mcp_server.something").error("real problem")

    messages = [e.get("logentry", {}).get("message") or e.get("message") for e in filtered_events]
    assert "Error handling POST request" not in messages
    assert not any((m or "").startswith("Received exception from stream") for m in messages)
    assert "real problem" in messages
