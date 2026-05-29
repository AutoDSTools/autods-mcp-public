"""B3 acceptance — FastAPI integration of get_current_user.

Exercises the 401-with-WWW-Authenticate path on missing/bad/expired
tokens and the happy path with a freshly minted, signed JWT.
"""

from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from autods_mcp_server.auth import (
    UserContext,
    build_www_authenticate,
    get_current_user,
    jwks_dependency,
    settings_dependency,
)
from autods_mcp_server.auth.jwks import JWKSClient
from autods_mcp_server.settings import Settings
from tests.auth.conftest import TEST_CLIENT_ID, TEST_JWKS_URL


@pytest.fixture
def auth_app(env, make_jwks) -> FastAPI:
    env(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="us-west-2_TESTPOOL",
        COGNITO_REGION="us-west-2",
        ALLOWED_COGNITO_CLIENT_IDS=f'["{TEST_CLIENT_ID}"]',
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID=TEST_CLIENT_ID,
    )

    async def fetch(_url: str) -> dict[str, Any]:
        return make_jwks()

    jwks_client = JWKSClient(jwks_url=TEST_JWKS_URL, fetcher=fetch)

    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: Annotated[UserContext, Depends(get_current_user)]) -> dict[str, Any]:
        return {"sub": user.sub, "email": user.email, "groups": user.groups}

    app.dependency_overrides[jwks_dependency] = lambda: jwks_client
    return app


@pytest.fixture
def client(auth_app: FastAPI) -> TestClient:
    return TestClient(auth_app)


def _assert_bearer_challenge(response, *, expected_error: str | None = None) -> None:
    assert response.status_code == 401
    challenge = response.headers.get("www-authenticate", "")
    assert challenge.startswith("Bearer ")
    assert 'resource_metadata="' in challenge
    assert "/.well-known/oauth-protected-resource" in challenge
    if expected_error:
        assert f'error="{expected_error}"' in challenge


def test_missing_authorization_header_returns_401_with_challenge(client: TestClient) -> None:
    response = client.get("/whoami")
    _assert_bearer_challenge(response, expected_error="invalid_request")


def test_malformed_authorization_header_returns_401(client: TestClient) -> None:
    response = client.get("/whoami", headers={"Authorization": "Basic abc"})
    _assert_bearer_challenge(response, expected_error="invalid_request")


def test_bad_token_returns_401_with_challenge(client: TestClient) -> None:
    response = client.get("/whoami", headers={"Authorization": "Bearer not.a.jwt"})
    _assert_bearer_challenge(response, expected_error="invalid_token")


def test_expired_token_returns_401_with_challenge(client: TestClient, make_token) -> None:
    token = make_token(exp_offset=-60)
    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    _assert_bearer_challenge(response, expected_error="invalid_token")


def test_disallowed_client_id_returns_401_with_invalid_token_challenge(client: TestClient, make_token) -> None:
    """Wire-level check that an `InvalidAudience` from `verify_token` maps to
    `invalid_token` with a description that pinpoints the `client_id` failure."""
    token = make_token(client_id="some-other-client")
    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    _assert_bearer_challenge(response, expected_error="invalid_token")
    description = response.json()["detail"]["error_description"]
    assert "client_id" in description


def test_wrong_issuer_returns_401_with_invalid_token_challenge(client: TestClient, make_token) -> None:
    """Wire-level check that an `InvalidIssuer` from `verify_token` maps to
    `invalid_token` with an issuer-specific description (distinct from the
    `client_id` description above)."""
    token = make_token(iss="https://cognito-idp.us-west-2.amazonaws.com/wrong-pool")
    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    _assert_bearer_challenge(response, expected_error="invalid_token")
    description = response.json()["detail"]["error_description"]
    assert "issuer" in description


def test_valid_token_returns_200_with_user_context(client: TestClient, make_token) -> None:
    token = make_token(extra_claims={"email": "alice@example.com", "cognito:groups": ["User", "Admin"]})
    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == "user-1"
    assert body["email"] == "alice@example.com"
    assert sorted(body["groups"]) == ["Admin", "User"]


def test_jwks_unavailable_returns_503(auth_app: FastAPI, make_token) -> None:
    """Transient JWKS fetch failure must surface as 503, not 401 — it's a server problem."""

    async def failing_fetch(_url: str) -> dict[str, Any]:
        raise httpx.ConnectError("cognito unreachable")

    failing_client = JWKSClient(jwks_url=TEST_JWKS_URL, fetcher=failing_fetch)
    auth_app.dependency_overrides[jwks_dependency] = lambda: failing_client

    token = make_token()
    with TestClient(auth_app) as test_client:
        response = test_client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    # Must NOT advertise a Bearer challenge — that would tell the client
    # to throw the token away and re-auth, when the right action is retry.
    assert "www-authenticate" not in {k.lower() for k in response.headers}


def test_prm_url_uses_public_hostname_not_request_host(env, make_jwks, make_token) -> None:
    """A hostile Host / X-Forwarded-Host must not steer the PRM URL."""
    env(
        MCP_ENV="staging",
        COGNITO_USER_POOL_ID="us-west-2_TESTPOOL",
        COGNITO_REGION="us-west-2",
        ALLOWED_COGNITO_CLIENT_IDS=f'["{TEST_CLIENT_ID}"]',
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        COGNITO_PUBLIC_CLIENT_ID=TEST_CLIENT_ID,
        FORCE_HTTPS="true",
        PUBLIC_HOSTNAME="mcp.autods.com",
    )

    async def fetch(_url: str) -> dict[str, Any]:
        return make_jwks()

    jwks_client = JWKSClient(jwks_url=TEST_JWKS_URL, fetcher=fetch)
    staging_settings = Settings()  # type: ignore[call-arg]

    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: Annotated[UserContext, Depends(get_current_user)]) -> dict[str, Any]:
        return {"sub": user.sub}

    app.dependency_overrides[settings_dependency] = lambda: staging_settings
    app.dependency_overrides[jwks_dependency] = lambda: jwks_client

    with TestClient(app) as test_client:
        response = test_client.get(
            "/whoami",
            headers={
                "Host": "evil.example.com",
                "X-Forwarded-Host": "evil.example.com",
                "X-Forwarded-Proto": "http",
            },
        )

    challenge = response.headers["www-authenticate"]
    assert 'resource_metadata="https://mcp.autods.com/.well-known/oauth-protected-resource"' in challenge
    assert "evil.example.com" not in challenge


def test_build_www_authenticate_format() -> None:
    """Header format is the spec-compliant comma-separated key=quoted-value list."""
    header = build_www_authenticate(
        "https://example.com/.well-known/oauth-protected-resource",
        error="invalid_token",
        error_description="Token expired.",
    )
    assert header == (
        'Bearer resource_metadata="https://example.com/.well-known/oauth-protected-resource", '
        'error="invalid_token", '
        'error_description="Token expired."'
    )


def test_build_www_authenticate_sanitizes_quotes_and_newlines() -> None:
    header = build_www_authenticate(
        "https://example.com/prm",
        error="invalid_token",
        error_description='oops "quoted"\r\nnewline',
    )
    description_value = header.split('error_description="', 1)[1].rsplit('"', 1)[0]
    assert '"' not in description_value
    assert "\n" not in header
    assert "\r" not in header


def test_build_www_authenticate_sanitizes_resource_metadata_url() -> None:
    """Defense-in-depth for the local-dev fallback path: a hostile `Host`
    header must not be able to smuggle a CR/LF/quote into the URL position."""
    header = build_www_authenticate('https://evil"injected\r\nX-Hack: yes/.well-known/oauth-protected-resource')
    url_value = header.split('resource_metadata="', 1)[1].split('"', 1)[0]
    assert '"' not in url_value
    assert "\r" not in header
    assert "\n" not in header
