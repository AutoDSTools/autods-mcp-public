"""A5 acceptance — settings refuse to boot in non-local without FORCE_HTTPS."""

import pytest
from pydantic import ValidationError

from autods_mcp_server.settings import McpEnv, Settings


def test_without_mcp_env_fails() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(COGNITO_USER_POOL_ID="staging_pool_id")
    message = str(excinfo.value)
    assert message.startswith("1 validation error for Settings\nMCP_ENV")


def test_without_cognito_user_pool_id_fails() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(MCP_ENV="local")
    message = str(excinfo.value)
    assert message.startswith("1 validation error for Settings\nCOGNITO_USER_POOL_ID")


def test_local_defaults_dont_require_force_https() -> None:
    settings = Settings(MCP_ENV="local", COGNITO_USER_POOL_ID="staging_pool_id")
    assert settings.mcp_env is McpEnv.local
    assert settings.is_local is True
    assert settings.force_https is False


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_non_local_without_force_https_fails(env: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(MCP_ENV=env, COGNITO_USER_POOL_ID=f"{env}_pool_id", PUBLIC_HOSTNAME="example.com")
    message = str(excinfo.value)
    assert "FORCE_HTTPS" in message
    assert env in message


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_non_local_without_public_hostname_fails(env: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(MCP_ENV=env, COGNITO_USER_POOL_ID=f"{env}_pool_id", FORCE_HTTPS="true")
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
    )
    assert settings.mcp_env.value == env
    assert settings.force_https is True
    assert settings.is_local is False


def test_allowed_origins_local_includes_localhost_wildcard() -> None:
    settings = Settings(MCP_ENV="local", COGNITO_USER_POOL_ID="staging_pool_id")
    assert "http://localhost:*" in settings.allowed_origins


def test_allowed_origins_prod_excludes_dev_clients() -> None:
    settings = Settings(
        MCP_ENV="prod",
        COGNITO_USER_POOL_ID="prod_pool_id",
        FORCE_HTTPS="true",
        PUBLIC_HOSTNAME="example.com",
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
    )
    assert any("inspector" in o for o in settings.allowed_origins)


def test_allowed_origins_override_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON list in ALLOWED_ORIGINS env var overrides per-env defaults."""
    monkeypatch.setenv("MCP_ENV", "local")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "staging_pool_id")
    monkeypatch.setenv("ALLOWED_ORIGINS", '["https://example.test"]')
    settings = Settings()
    assert settings.allowed_origins == ["https://example.test"]
