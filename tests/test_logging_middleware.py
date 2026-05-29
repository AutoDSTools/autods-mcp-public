"""A3 acceptance — one structured log line per request with request_id/path/method."""

import io
import json
import logging

import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autods_mcp_server.logging import configure_logging
from autods_mcp_server.middleware import RequestContextMiddleware
from autods_mcp_server.settings import Settings


def test_one_structured_log_line_per_request(monkeypatch) -> None:
    monkeypatch.setenv("MCP_ENV", "staging")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "staging_pool_id")
    monkeypatch.setenv("FORCE_HTTPS", "true")
    monkeypatch.setenv("PUBLIC_HOSTNAME", "example.com")
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
