"""A5 acceptance — settings refuse to boot in non-local without FORCE_HTTPS."""

import pytest
from pydantic import ValidationError

from autods_mcp_server.settings import McpEnv, Settings

# COGNITO_DOMAIN + COGNITO_PUBLIC_CLIENT_ID are required in every environment,
# and the public client id must be in the allowlist. Tests that aren't about
# these fields still must supply them for Settings to construct, so isolate
# the field-under-test by spreading this baseline into the call.
_OAUTH = {
    "COGNITO_DOMAIN": "autods.auth.us-west-2.amazoncognito.com",
    "COGNITO_PUBLIC_CLIENT_ID": "public-client",
    "ALLOWED_COGNITO_CLIENT_IDS": ["public-client"],
}


def test_without_mcp_env_fails() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(COGNITO_USER_POOL_ID="staging_pool_id", **_OAUTH)
    message = str(excinfo.value)
    assert message.startswith("1 validation error for Settings\nMCP_ENV")


def test_without_cognito_user_pool_id_fails() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(MCP_ENV="local", **_OAUTH)
    message = str(excinfo.value)
    assert message.startswith("1 validation error for Settings\nCOGNITO_USER_POOL_ID")


def test_without_cognito_domain_fails() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            MCP_ENV="local",
            COGNITO_USER_POOL_ID="staging_pool_id",
            COGNITO_PUBLIC_CLIENT_ID="public-client",
            ALLOWED_COGNITO_CLIENT_IDS=["public-client"],
        )
    assert "COGNITO_DOMAIN" in str(excinfo.value)


def test_without_cognito_public_client_id_fails() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            MCP_ENV="local",
            COGNITO_USER_POOL_ID="staging_pool_id",
            COGNITO_DOMAIN="autods.auth.us-west-2.amazoncognito.com",
        )
    assert "COGNITO_PUBLIC_CLIENT_ID" in str(excinfo.value)


def test_default_oauth_scopes() -> None:
    settings = Settings(MCP_ENV="local", COGNITO_USER_POOL_ID="staging_pool_id", **_OAUTH)
    assert settings.mcp_oauth_scopes == ["email", "openid", "phone", "profile"]


def test_local_defaults_dont_require_force_https() -> None:
    settings = Settings(MCP_ENV="local", COGNITO_USER_POOL_ID="staging_pool_id", **_OAUTH)
    assert settings.mcp_env is McpEnv.local
    assert settings.is_local is True
    assert settings.force_https is False


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_non_local_without_force_https_fails(env: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(MCP_ENV=env, COGNITO_USER_POOL_ID=f"{env}_pool_id", PUBLIC_HOSTNAME="example.com", **_OAUTH)
    message = str(excinfo.value)
    assert "FORCE_HTTPS" in message
    assert env in message


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_non_local_without_public_hostname_fails(env: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(MCP_ENV=env, COGNITO_USER_POOL_ID=f"{env}_pool_id", FORCE_HTTPS="true", **_OAUTH)
    message = str(excinfo.value)
    assert "PUBLIC_HOSTNAME" in message
    assert env in message


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_non_local_with_force_https_and_public_hostname_boots(env: str) -> None:
    settings = Settings(
        MCP_ENV=env,
        COGNITO_USER_POOL_ID=f"{env}_pool_id",
        FORCE_HTTPS="true",
        PUBLIC_HOSTNAME="example.com",
        REDIS_URL="redis://localhost:6379/0",
        **_OAUTH,
    )
    assert settings.mcp_env.value == env
    assert settings.force_https is True
    assert settings.is_local is False


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_non_local_without_redis_url_fails(env: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            MCP_ENV=env,
            COGNITO_USER_POOL_ID=f"{env}_pool_id",
            FORCE_HTTPS="true",
            PUBLIC_HOSTNAME="example.com",
            **_OAUTH,
        )
    message = str(excinfo.value)
    assert "REDIS_URL" in message
    assert env in message


def test_rate_limit_defaults() -> None:
    settings = Settings(MCP_ENV="local", COGNITO_USER_POOL_ID="staging_pool_id", **_OAUTH)
    assert settings.rate_limit_per_minute == 60
    assert settings.rate_limit_per_hour == 1000
    assert settings.redis_url is None


def test_allowed_origins_local_includes_localhost_wildcard() -> None:
    settings = Settings(MCP_ENV="local", COGNITO_USER_POOL_ID="staging_pool_id", **_OAUTH)
    assert "http://localhost:*" in settings.allowed_origins


def test_allowed_origins_prod_excludes_dev_clients() -> None:
    settings = Settings(
        MCP_ENV="prod",
        COGNITO_USER_POOL_ID="prod_pool_id",
        FORCE_HTTPS="true",
        PUBLIC_HOSTNAME="example.com",
        REDIS_URL="redis://localhost:6379/0",
        **_OAUTH,
    )
    assert "https://claude.com" in settings.allowed_origins
    assert "https://claude.ai" in settings.allowed_origins
    assert all("inspector" not in o for o in settings.allowed_origins)


def test_allowed_origins_staging_includes_inspector() -> None:
    settings = Settings(
        MCP_ENV="staging",
        COGNITO_USER_POOL_ID="staging_pool_id",
        FORCE_HTTPS="true",
        PUBLIC_HOSTNAME="example.com",
        REDIS_URL="redis://localhost:6379/0",
        **_OAUTH,
    )
    assert any("inspector" in o for o in settings.allowed_origins)


def test_allowed_origins_override_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON list in ALLOWED_ORIGINS env var overrides per-env defaults."""
    monkeypatch.setenv("MCP_ENV", "local")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "staging_pool_id")
    monkeypatch.setenv("ALLOWED_ORIGINS", '["https://example.test"]')
    monkeypatch.setenv("COGNITO_DOMAIN", "autods.auth.us-west-2.amazoncognito.com")
    monkeypatch.setenv("COGNITO_PUBLIC_CLIENT_ID", "public-client")
    monkeypatch.setenv("ALLOWED_COGNITO_CLIENT_IDS", '["public-client"]')
    settings = Settings()
    assert settings.allowed_origins == ["https://example.test"]
