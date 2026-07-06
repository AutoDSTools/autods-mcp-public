"""Shared pytest fixtures and environment hygiene.

Every test must start with a clean environment so that one test's
MCP_ENV / FORCE_HTTPS leaks don't bleed into the next. We snapshot env
once per test and restore it on teardown.
"""

from collections.abc import Iterator

import pytest

from autods_mcp_server import settings as settings_module
from autods_mcp_server.auth import reset_jwks_client

_MANAGED_ENV_VARS = (
    "MCP_ENV",
    "AUTODS_API_BASE_URL",
    "PRODUCTS_RESEARCH_BASE_URL",
    "COGNITO_USER_POOL_ID",
    "COGNITO_REGION",
    "ALLOWED_COGNITO_CLIENT_IDS",
    "COGNITO_DOMAIN",
    "COGNITO_PUBLIC_CLIENT_ID",
    "MCP_OAUTH_SCOPES",
    "MCP_REGISTRATION_REDIRECT_URIS",
    "ALLOWED_ORIGINS",
    "PUBLIC_HOSTNAME",
    "FORCE_HTTPS",
    "LOG_LEVEL",
    "REDIS_URL",
    "RATE_LIMIT_PER_MINUTE",
    "RATE_LIMIT_PER_HOUR",
    "MIXPANEL_TOKEN",
    "COGNITO_ATTR_NEGATIVE_CACHE_TTL_SECONDS",
    "COGNITO_ATTR_POSITIVE_CACHE_TTL_SECONDS",
    "SENTRY_URL",
    "SENTRY_ENVIRONMENT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for var in _MANAGED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    settings_module.reset_settings_cache()
    reset_jwks_client()
    yield
    settings_module.reset_settings_cache()
    reset_jwks_client()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Convenience setter that also resets the settings cache."""

    def _set(**values: str) -> None:
        for key, value in values.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        settings_module.reset_settings_cache()

    return _set


@pytest.fixture(autouse=True)
def _restore_cwd(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run tests from tmp_path so a stray repo-root .env can't leak in."""
    monkeypatch.chdir(tmp_path)
    yield
