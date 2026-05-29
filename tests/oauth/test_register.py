"""C4 acceptance — POST /oauth/register DCR shim."""

import pytest

from autods_mcp_server.settings import Settings
from tests.oauth.conftest import STAGING_PUBLIC_CLIENT_ID


def test_register_returns_pre_created_client_id(local_settings: Settings, client_factory) -> None:
    with client_factory(local_settings) as client:
        response = client.post(
            "/oauth/register",
            headers={"origin": "http://localhost:6274"},
            json={
                "redirect_uris": ["http://localhost:6274/oauth/callback"],
                "client_name": "MCP Inspector",
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
    assert response.status_code == 201
    body = response.json()
    assert body["client_id"] == STAGING_PUBLIC_CLIENT_ID
    assert body["redirect_uris"] == ["http://localhost:6274/oauth/callback"]
    assert body["token_endpoint_auth_method"] == "none"
    assert isinstance(body["client_id_issued_at"], int)
    assert "authorization_code" in body["grant_types"]
    assert "code" in body["response_types"]


def test_register_rejects_redirect_uri_not_in_allowlist(local_settings: Settings, client_factory) -> None:
    with client_factory(local_settings) as client:
        response = client.post(
            "/oauth/register",
            headers={"origin": "http://localhost:6274"},
            json={"redirect_uris": ["http://evil.example/steal-the-code"]},
        )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_redirect_uri"
    assert "evil.example" in detail["error_description"]


def test_register_rejects_when_any_uri_is_disallowed(local_settings: Settings, client_factory) -> None:
    """A request mixing one allowed and one disallowed URI is fully rejected.

    Otherwise an attacker can sneak a hostile URI in next to a legitimate
    one and the legitimate one would mask the rejection.
    """
    with client_factory(local_settings) as client:
        response = client.post(
            "/oauth/register",
            headers={"origin": "http://localhost:6274"},
            json={
                "redirect_uris": [
                    "http://localhost:6274/oauth/callback",
                    "http://evil.example/steal-the-code",
                ]
            },
        )
    assert response.status_code == 400


def test_register_requires_at_least_one_redirect_uri(local_settings: Settings, client_factory) -> None:
    with client_factory(local_settings) as client:
        response = client.post(
            "/oauth/register",
            headers={"origin": "http://localhost:6274"},
            json={"redirect_uris": []},
        )
    assert response.status_code == 422  # pydantic min_length validation


def test_register_ignores_extra_metadata_fields(local_settings: Settings, client_factory) -> None:
    """Unknown RFC 7591 fields (software_id, contacts, ...) are accepted but ignored."""
    with client_factory(local_settings) as client:
        response = client.post(
            "/oauth/register",
            headers={"origin": "http://localhost:6274"},
            json={
                "redirect_uris": ["http://localhost:8000/callback"],
                "software_id": "deadbeef",
                "software_version": "1.2.3",
                "contacts": ["dev@example.com"],
                "scope": "openid email",
            },
        )
    assert response.status_code == 201


def test_register_returns_400_when_redirect_allowlist_empty(env, client_factory) -> None:
    """Empty MCP_REGISTRATION_REDIRECT_URIS → the shim can't validate anything; surface as 400.

    (COGNITO_PUBLIC_CLIENT_ID is a required setting, so the "no client id"
    misconfiguration can no longer occur — Settings refuses to construct.)
    """
    env(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="us-west-2_TESTPOOL",
        ALLOWED_COGNITO_CLIENT_IDS='["c"]',
        COGNITO_PUBLIC_CLIENT_ID="c",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        # MCP_REGISTRATION_REDIRECT_URIS deliberately unset → empty allowlist.
    )
    settings = Settings()  # type: ignore[call-arg]
    with client_factory(settings) as client:
        response = client.post(
            "/oauth/register",
            headers={"origin": "http://localhost:6274"},
            json={"redirect_uris": ["http://localhost:8000/callback"]},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_client_metadata"


def test_register_requires_allowed_origin(staging_settings: Settings, client_factory) -> None:
    with client_factory(staging_settings) as client:
        response = client.post(
            "/oauth/register",
            headers={
                "origin": "https://evil.example",
                "host": "mcp.autods.com",
                "x-forwarded-proto": "https",
            },
            json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]},
        )
    assert response.status_code == 403


def test_settings_reject_public_client_id_not_in_allowed_list(env) -> None:
    """The settings-level invariant: we can't advertise a client whose tokens we'd reject."""
    env(
        MCP_ENV="local",
        COGNITO_USER_POOL_ID="us-west-2_TESTPOOL",
        ALLOWED_COGNITO_CLIENT_IDS='["only-this-one"]',
        COGNITO_PUBLIC_CLIENT_ID="some-other-id",
        COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
    )
    with pytest.raises(ValueError) as excinfo:
        Settings()  # type: ignore[call-arg]
    assert "COGNITO_PUBLIC_CLIENT_ID" in str(excinfo.value)
