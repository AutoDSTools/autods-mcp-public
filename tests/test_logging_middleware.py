"""A3 acceptance — one structured log line per request with request_id/path/method."""

import io
import json
import logging

import structlog
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from autods_mcp_server.logging import configure_logging
from autods_mcp_server.middleware import _USER_AGENT_MAX_CHARS, RequestContextMiddleware
from autods_mcp_server.settings import Settings


def test_one_structured_log_line_per_request(monkeypatch) -> None:
    monkeypatch.setenv("MCP_ENV", "staging")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "staging_pool_id")
    monkeypatch.setenv("FORCE_HTTPS", "true")
    monkeypatch.setenv("PUBLIC_HOSTNAME", "example.com")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("COGNITO_DOMAIN", "autods.auth.us-west-2.amazoncognito.com")
    monkeypatch.setenv("COGNITO_PUBLIC_CLIENT_ID", "public-client")
    monkeypatch.setenv("ALLOWED_COGNITO_CLIENT_IDS", '["public-client"]')
    settings = Settings()
    configure_logging(settings)

    # Re-point structlog at an in-memory buffer for assertion.
    buffer = io.StringIO()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
        cache_logger_on_first_use=False,
    )

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200

    output = buffer.getvalue().strip().splitlines()
    request_lines = [line for line in output if '"event": "request"' in line or '"event":"request"' in line]
    assert len(request_lines) == 1, f"expected exactly one access log line, got {output!r}"

    record = json.loads(request_lines[0])
    assert record["event"] == "request"
    assert record["path"] == "/health"
    assert record["method"] == "GET"
    assert record["request_id"]
    assert record["status_code"] == 200
    # Unauthenticated route: the field is present but null.
    assert record["cognito_username"] is None


def test_request_log_carries_cognito_username(monkeypatch) -> None:
    settings = Settings(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="staging_pool_id",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID="public-client",
        ALLOWED_COGNITO_CLIENT_IDS=["public-client"],
    )
    configure_logging(settings)

    buffer = io.StringIO()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
        cache_logger_on_first_use=False,
    )

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    # Stand in for the auth dependency, which sets this on request.state.
    @app.get("/protected")
    async def protected(request: Request) -> dict[str, str]:
        request.state.cognito_username = "sub-abc-123"
        return {"status": "ok"}

    with TestClient(app) as client:
        response = client.get("/protected")
    assert response.status_code == 200

    output = buffer.getvalue().strip().splitlines()
    request_lines = [line for line in output if '"event": "request"' in line or '"event":"request"' in line]
    assert len(request_lines) == 1, f"expected exactly one access log line, got {output!r}"

    record = json.loads(request_lines[0])
    assert record["cognito_username"] == "sub-abc-123"


def test_user_agent_is_bound_for_every_line_of_the_request(monkeypatch) -> None:
    """The only client signal present on every request.

    The MCP client identity is not: the transport is stateless, so a request on
    a 2025-era protocol version reaches the handlers with no ``client_params``
    and logs ``client_name: null`` whatever the client called itself at
    ``initialize``. Bound to the contextvars rather than passed to one logger
    call, so it reaches `tool_call` and `resource_read` too — asserted here on a
    second line emitted from inside the route, which is what those handlers are.
    """
    settings = Settings(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="staging_pool_id",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID="public-client",
        ALLOWED_COGNITO_CLIENT_IDS=["public-client"],
    )
    configure_logging(settings)

    buffer = io.StringIO()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
        cache_logger_on_first_use=False,
    )

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        structlog.get_logger("test").info("handler_line")
        return {"status": "ok"}

    with TestClient(app) as client:
        response = client.get("/health", headers={"user-agent": "claude-ai/0.1.0"})
    assert response.status_code == 200

    records = [json.loads(line) for line in buffer.getvalue().strip().splitlines()]
    by_event = {record["event"]: record for record in records}
    assert by_event["request"]["user_agent"] == "claude-ai/0.1.0"
    assert by_event["handler_line"]["user_agent"] == "claude-ai/0.1.0"


def test_a_long_user_agent_is_truncated_not_dropped(monkeypatch) -> None:
    """It is caller-controlled, unbounded, and on every line of every request.

    Truncated rather than dropped, because the head is the part that names the
    client — which is the whole point of the field.
    """
    settings = Settings(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="staging_pool_id",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID="public-client",
        ALLOWED_COGNITO_CLIENT_IDS=["public-client"],
    )
    configure_logging(settings)

    buffer = io.StringIO()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
        cache_logger_on_first_use=False,
    )

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(app) as client:
        client.get("/health", headers={"user-agent": "claude-ai/" + "x" * 5000})
        client.get("/health", headers={"user-agent": ""})

    records = [json.loads(line) for line in buffer.getvalue().strip().splitlines()]
    long_line, empty_line = (record for record in records if record["event"] == "request")
    assert len(long_line["user_agent"]) == _USER_AGENT_MAX_CHARS
    assert long_line["user_agent"].startswith("claude-ai/")
    # A header that is present but empty is not the same as one never sent, and
    # neither is dropped.
    assert empty_line["user_agent"] == ""


def test_explicit_request_id_is_preserved(monkeypatch) -> None:
    settings = Settings(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="staging_pool_id",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID="public-client",
        ALLOWED_COGNITO_CLIENT_IDS=["public-client"],
    )
    configure_logging(settings)

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(app) as client:
        response = client.get("/health", headers={"x-request-id": "rid-1234"})
    assert response.headers["x-request-id"] == "rid-1234"
