"""A5 acceptance — HTTPS-only request guard."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from autods_mcp_server.middleware import HttpsOnlyMiddleware
from autods_mcp_server.settings import Settings

# COGNITO_DOMAIN + COGNITO_PUBLIC_CLIENT_ID are required in every environment
# (and the public client id must be allowlisted). These HTTPS-guard tests
# don't exercise OAuth, but Settings won't construct without them.
_OAUTH_REQUIRED = {
    "COGNITO_DOMAIN": "autods.auth.us-west-2.amazoncognito.com",
    "COGNITO_PUBLIC_CLIENT_ID": "public-client",
    "ALLOWED_COGNITO_CLIENT_IDS": ["public-client"],
}


def _app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.add_middleware(HttpsOnlyMiddleware, settings=settings)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def test_local_env_allows_plain_http() -> None:
    settings = Settings(MCP_ENV="local", COGNITO_USER_POOL_ID="staging_pool_id", **_OAUTH_REQUIRED)
    with TestClient(_app(settings)) as client:
        response = client.get("/health")
    assert response.status_code == 200


def test_non_local_rejects_request_without_https_proto() -> None:
    settings = Settings(
        MCP_ENV="staging",
        COGNITO_USER_POOL_ID="staging_pool_id",
        FORCE_HTTPS="true",
        PUBLIC_HOSTNAME="example.com",
        **_OAUTH_REQUIRED,
    )
    with TestClient(_app(settings)) as client:
        response = client.get("/health")
    assert response.status_code == 403
    assert response.json()["error"] == "https_required"


def test_non_local_accepts_request_with_https_proto() -> None:
    settings = Settings(
        MCP_ENV="staging",
        COGNITO_USER_POOL_ID="staging_pool_id",
        FORCE_HTTPS="true",
        PUBLIC_HOSTNAME="example.com",
        **_OAUTH_REQUIRED,
    )
    with TestClient(_app(settings)) as client:
        response = client.get("/health", headers={"x-forwarded-proto": "https"})
    assert response.status_code == 200


def test_non_local_rejects_request_with_http_proto() -> None:
    settings = Settings(
        MCP_ENV="prod",
        COGNITO_USER_POOL_ID="prod_pool_id",
        FORCE_HTTPS="true",
        PUBLIC_HOSTNAME="example.com",
        **_OAUTH_REQUIRED,
    )
    with TestClient(_app(settings)) as client:
        response = client.get("/health", headers={"x-forwarded-proto": "http"})
    assert response.status_code == 403
