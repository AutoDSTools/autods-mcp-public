"""Shared fixtures for OAuth endpoint tests.

Each test builds its own ``Settings`` via the ``env`` fixture from the
parent conftest and constructs an isolated ``FastAPI`` app — mirroring
the auth-tests pattern. Origin/HTTPS middleware is mounted so the tests
exercise the real wiring (PRM, AS metadata, DCR all sit behind it).
"""

import json
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autods_mcp_server.middleware import OriginAllowlistMiddleware
from autods_mcp_server.oauth import router as oauth_router
from autods_mcp_server.settings import Settings

STAGING_PUBLIC_CLIENT_ID = "staging-public-client"
# Single source of truth for the redirect-URI allowlist used by the
# fixtures below. ``staging`` only ships the remote (https) callbacks;
# ``local`` additionally allows the loopback Inspector/dev callbacks.
STAGING_REDIRECT_URIS = [
    "https://claude.com/api/mcp/auth_callback",
    "https://claude.ai/api/mcp/auth_callback",
]
LOCAL_REDIRECT_URIS = [
    "http://localhost:6274/oauth/callback",
    "http://localhost:8000/callback",
]
ALL_REDIRECT_URIS = [*STAGING_REDIRECT_URIS, *LOCAL_REDIRECT_URIS]


@pytest.fixture
def make_oauth_app() -> Callable[[Settings], FastAPI]:
    def _make(settings: Settings) -> FastAPI:
        app = FastAPI()
        app.add_middleware(OriginAllowlistMiddleware, settings=settings)
        app.include_router(oauth_router)

        from autods_mcp_server.auth.dependency import settings_dependency

        app.dependency_overrides[settings_dependency] = lambda: settings
        return app

    return _make


@pytest.fixture
def local_settings(env) -> Settings:
    """Local-env settings with all Phase C knobs wired."""
    env(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="us-west-2_TESTPOOL",
        COGNITO_REGION="us-west-2",
        ALLOWED_COGNITO_CLIENT_IDS=f'["{STAGING_PUBLIC_CLIENT_ID}"]',
        COGNITO_PUBLIC_CLIENT_ID=STAGING_PUBLIC_CLIENT_ID,
        COGNITO_DOMAIN="autods-staging.auth.us-west-2.amazoncognito.com",
        MCP_REGISTRATION_REDIRECT_URIS=json.dumps(ALL_REDIRECT_URIS),
    )
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def staging_settings(env) -> Settings:
    """Staging-env settings — PUBLIC_HOSTNAME pins the advertised URL."""
    env(
        MCP_ENV="staging",
        COGNITO_USER_POOL_ID="us-west-2_TESTPOOL",
        COGNITO_REGION="us-west-2",
        ALLOWED_COGNITO_CLIENT_IDS=f'["{STAGING_PUBLIC_CLIENT_ID}"]',
        COGNITO_PUBLIC_CLIENT_ID=STAGING_PUBLIC_CLIENT_ID,
        COGNITO_DOMAIN="https://autods-staging.auth.us-west-2.amazoncognito.com",
        MCP_REGISTRATION_REDIRECT_URIS=json.dumps(STAGING_REDIRECT_URIS),
        FORCE_HTTPS="true",
        PUBLIC_HOSTNAME="mcp.autods.com",
        REDIS_URL="redis://localhost:6379/0",
    )
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def client_factory(make_oauth_app) -> Callable[[Settings], TestClient]:
    def _make(settings: Settings) -> TestClient:
        return TestClient(make_oauth_app(settings))

    return _make
