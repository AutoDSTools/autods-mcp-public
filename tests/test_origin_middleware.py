"""A4 acceptance — Origin allowlist + DNS-rebinding Host check.

Uses /health as the stand-in protected route, per the ticket's A4 note.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autods_mcp_server.middleware import OriginAllowlistMiddleware
from autods_mcp_server.settings import Settings


def _app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        OriginAllowlistMiddleware,
        settings=settings,
        protected_patterns=("/health",),
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


@pytest.fixture
def staging_settings() -> Settings:
    return Settings(
        MCP_ENV="staging",
        COGNITO_USER_POOL_ID="staging_pool_id",
        FORCE_HTTPS="true",
        PUBLIC_HOSTNAME="mcp.autods.com",
    )


def test_missing_origin_rejected(staging_settings: Settings) -> None:
    with TestClient(_app(staging_settings)) as client:
        response = client.get("/health", headers={"host": "mcp.autods.com"})
    assert response.status_code == 403
    assert response.json()["error"] == "origin_missing"


def test_foreign_origin_rejected(staging_settings: Settings) -> None:
    with TestClient(_app(staging_settings)) as client:
        response = client.get(
            "/health",
            headers={
                "origin": "https://evil.example",
                "host": "mcp.autods.com",
            },
        )
    assert response.status_code == 403
    assert response.json()["error"] == "origin_not_allowed"


def test_allowed_origin_passes_through(staging_settings: Settings) -> None:
    with TestClient(_app(staging_settings)) as client:
        response = client.get(
            "/health",
            headers={
                "origin": "https://claude.ai",
                "host": "mcp.autods.com",
            },
        )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_host_mismatch_with_present_origin_rejected(staging_settings: Settings) -> None:
    """DNS-rebinding defense: Origin allowed but Host header points elsewhere."""
    with TestClient(_app(staging_settings)) as client:
        response = client.get(
            "/health",
            headers={
                "origin": "https://claude.ai",
                # Browser was rebound to 127.0.0.1; the request lands on
                # us via a different Host header than our public name.
                "host": "127.0.0.1:8000",
            },
        )
    assert response.status_code == 403
    assert response.json()["error"] == "host_mismatch"


def test_localhost_wildcard_accepts_random_port() -> None:
    settings = Settings(MCP_ENV="local", COGNITO_USER_POOL_ID="staging_pool_id")
    with TestClient(_app(settings)) as client:
        response = client.get(
            "/health",
            headers={
                "origin": "http://localhost:53291",
                "host": "localhost:8000",
            },
        )
    assert response.status_code == 200


def test_non_protected_path_skips_origin_check(staging_settings: Settings) -> None:
    """A request to a path that isn't in protected_patterns goes through untouched."""
    app = FastAPI()
    app.add_middleware(
        OriginAllowlistMiddleware,
        settings=staging_settings,
        protected_patterns=("/mcp", "/mcp/*", "/.well-known/*"),
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
