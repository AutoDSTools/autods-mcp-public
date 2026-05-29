"""C3 acceptance — GET /.well-known/oauth-authorization-server."""

import pytest

from autods_mcp_server.settings import Settings


def test_as_metadata_points_endpoints_at_cognito_and_dcr_at_us(staging_settings: Settings, client_factory) -> None:
    with client_factory(staging_settings) as client:
        response = client.get(
            "/.well-known/oauth-authorization-server",
            headers={
                "origin": "https://claude.ai",
                "host": "mcp.autods.com",
                "x-forwarded-proto": "https",
            },
        )
    assert response.status_code == 200
    body = response.json()
    # No trailing slash — issuer must be byte-identical to the value the
    # AS-metadata well-known URL is built from (RFC 8414 §3.3).
    assert body["issuer"] == "https://mcp.autods.com"
    assert body["authorization_endpoint"] == "https://autods-staging.auth.us-west-2.amazoncognito.com/oauth2/authorize"
    assert body["token_endpoint"] == "https://autods-staging.auth.us-west-2.amazoncognito.com/oauth2/token"
    assert body["registration_endpoint"] == "https://mcp.autods.com/oauth/register"
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["token_endpoint_auth_methods_supported"] == ["none"]
    assert body["scopes_supported"] == ["email", "openid", "phone", "profile"]
    assert body["jwks_uri"].endswith("/.well-known/jwks.json")


def test_as_metadata_normalises_bare_domain(local_settings: Settings, client_factory) -> None:
    """Operators can supply COGNITO_DOMAIN as a bare hostname; we add the scheme."""
    with client_factory(local_settings) as client:
        response = client.get(
            "/.well-known/oauth-authorization-server",
            headers={"origin": "http://localhost:6274"},
        )
    body = response.json()
    assert body["authorization_endpoint"].startswith("https://autods-staging.auth.us-west-2.amazoncognito.com/")


def test_as_metadata_requires_allowed_origin(staging_settings: Settings, client_factory) -> None:
    with client_factory(staging_settings) as client:
        response = client.get(
            "/.well-known/oauth-authorization-server",
            headers={
                "origin": "https://evil.example",
                "host": "mcp.autods.com",
                "x-forwarded-proto": "https",
            },
        )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "domain_value",
    [
        "https://autods.auth.us-west-2.amazoncognito.com",
        "autods.auth.us-west-2.amazoncognito.com",
        "https://autods.auth.us-west-2.amazoncognito.com/",
    ],
)
def test_cognito_endpoints_computed_consistently(domain_value: str, env) -> None:
    """Bare / schemed / trailing-slash forms all collapse to the same endpoints."""
    env(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="us-west-2_TESTPOOL",
        ALLOWED_COGNITO_CLIENT_IDS='["c"]',
        COGNITO_PUBLIC_CLIENT_ID="c",
        COGNITO_DOMAIN=domain_value,
    )
    settings = Settings()  # type: ignore[call-arg]
    assert settings.cognito_authorization_endpoint == "https://autods.auth.us-west-2.amazoncognito.com/oauth2/authorize"
    assert settings.cognito_token_endpoint == "https://autods.auth.us-west-2.amazoncognito.com/oauth2/token"
