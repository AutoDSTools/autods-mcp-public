"""Fixtures for the Phase E (E3) end-to-end smoke suite.

These tests stand the *real* server up (real Cognito JWT verification, real
upstream HTTP) with ``MCP_ENV=staging`` and drive it through the real MCP
Streamable HTTP client, so they require live staging credentials and network
access. They are therefore **opt-in**: the whole suite is skipped unless
``RUN_STAGING_E2E=1`` and the required staging env vars are present (see
``_REQUIRED_VARS``). This keeps ``uv run pytest`` green on a laptop / CI box
that has no staging secrets while still giving operators a one-command
end-to-end check (``RUN_STAGING_E2E=1 uv run pytest tests/e2e``).

Token acquisition uses Cognito ``USER_PASSWORD_AUTH`` (InitiateAuth) against a
test user, so the app client referenced by ``E2E_COGNITO_CLIENT_ID`` must have
that auth flow enabled and its id must be the one the public server accepts
(``ALLOWED_COGNITO_CLIENT_IDS``).

Required env vars (all must be set alongside ``RUN_STAGING_E2E=1``):

* ``E2E_COGNITO_USERNAME`` / ``E2E_COGNITO_PASSWORD`` — the staging test user.
* ``E2E_COGNITO_CLIENT_ID`` — Cognito app client id (USER_PASSWORD_AUTH-enabled).
* ``E2E_COGNITO_USER_POOL_ID`` — the user pool that mints the token.
* ``E2E_COGNITO_DOMAIN`` — Cognito Hosted UI domain (for Settings).

Optional:

* ``E2E_COGNITO_REGION`` (default ``us-west-2``).
* ``E2E_COGNITO_CLIENT_SECRET`` — only if the app client has a secret (adds the
  ``SECRET_HASH`` to InitiateAuth).
* ``AUTODS_API_BASE_URL`` / ``PRODUCTS_RESEARCH_BASE_URL`` — staging upstreams
  (default to the production hostnames baked into Settings).
* ``E2E_STORE_IDS`` — comma-separated AutoDS store ids for the store-scoped ops;
  when unset those ops are skipped rather than failed.
* ``E2E_INCLUDE_WRITES=1`` — also exercise the write ops (upload_products,
  publish_drafts_to_marketplace). Off by default so the smoke run never mutates
  staging data.
"""

import base64
import hashlib
import hmac
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi import FastAPI

from autods_mcp_server import settings as settings_module
from autods_mcp_server.mcp_transport import McpRuntime, build_runtime, mount_mcp
from autods_mcp_server.settings import Settings

# Env vars that must all be present (in addition to RUN_STAGING_E2E=1) for the
# suite to run; any missing one skips the whole module.
_REQUIRED_VARS = (
    "E2E_COGNITO_USERNAME",
    "E2E_COGNITO_PASSWORD",
    "E2E_COGNITO_CLIENT_ID",
    "E2E_COGNITO_USER_POOL_ID",
    "E2E_COGNITO_DOMAIN",
)


@dataclass(frozen=True)
class StagingConfig:
    username: str
    password: str
    client_id: str
    client_secret: str | None
    user_pool_id: str
    domain: str
    region: str
    store_ids: str | None
    include_writes: bool
    autods_api_base_url: str | None = None
    products_research_base_url: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


@pytest.fixture(scope="module")
def staging_config() -> StagingConfig:
    """Read staging config from the environment, or skip the suite."""
    if os.environ.get("RUN_STAGING_E2E") != "1":
        pytest.skip("staging e2e is opt-in; set RUN_STAGING_E2E=1 to run it")
    missing = [name for name in _REQUIRED_VARS if not os.environ.get(name)]
    if missing:
        pytest.skip(f"staging e2e missing required env vars: {', '.join(missing)}")

    return StagingConfig(
        username=os.environ["E2E_COGNITO_USERNAME"],
        password=os.environ["E2E_COGNITO_PASSWORD"],
        client_id=os.environ["E2E_COGNITO_CLIENT_ID"],
        client_secret=os.environ.get("E2E_COGNITO_CLIENT_SECRET"),
        user_pool_id=os.environ["E2E_COGNITO_USER_POOL_ID"],
        domain=os.environ["E2E_COGNITO_DOMAIN"],
        region=os.environ.get("E2E_COGNITO_REGION", "us-west-2"),
        store_ids=os.environ.get("E2E_STORE_IDS"),
        include_writes=os.environ.get("E2E_INCLUDE_WRITES") == "1",
        autods_api_base_url=os.environ.get("AUTODS_API_BASE_URL"),
        products_research_base_url=os.environ.get("PRODUCTS_RESEARCH_BASE_URL"),
    )


def _secret_hash(username: str, client_id: str, client_secret: str) -> str:
    """Cognito SECRET_HASH = base64(HMAC-SHA256(secret, username + client_id))."""
    digest = hmac.new(
        client_secret.encode("utf-8"),
        (username + client_id).encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


@pytest.fixture(scope="module")
def access_token(staging_config: StagingConfig) -> str:
    """Acquire a real Cognito access token via USER_PASSWORD_AUTH.

    Hits the cognito-idp data-plane endpoint directly (no boto3 dependency).
    Skips — rather than fails — if the user pool answers with an auth challenge
    (e.g. NEW_PASSWORD_REQUIRED), since that's an environment-setup issue, not a
    server regression.
    """
    auth_params: dict[str, str] = {
        "USERNAME": staging_config.username,
        "PASSWORD": staging_config.password,
    }
    if staging_config.client_secret:
        auth_params["SECRET_HASH"] = _secret_hash(
            staging_config.username, staging_config.client_id, staging_config.client_secret
        )

    response = httpx.post(
        f"https://cognito-idp.{staging_config.region}.amazonaws.com/",
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        },
        json={
            "AuthFlow": "USER_PASSWORD_AUTH",
            "ClientId": staging_config.client_id,
            "AuthParameters": auth_params,
        },
        timeout=30,
    )
    if response.status_code != 200:
        pytest.skip(f"Cognito InitiateAuth failed ({response.status_code}): {response.text[:300]}")

    body = response.json()
    result = body.get("AuthenticationResult")
    if not result or "AccessToken" not in result:
        pytest.skip(f"Cognito returned no AccessToken (challenge={body.get('ChallengeName')!r})")
    return result["AccessToken"]


@pytest.fixture
def staging_settings(staging_config: StagingConfig, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Build the staging Settings the server boots with.

    The token's ``client_id`` is the app client above, so it must be in
    ``ALLOWED_COGNITO_CLIENT_IDS`` (and, per the settings validator, also be the
    public client id). Rate limits are disabled (0) so the limiter never touches
    Redis even though staging Settings require ``REDIS_URL`` to be set.
    """
    env = {
        "MCP_ENV": "staging",
        "FORCE_HTTPS": "true",
        "PUBLIC_HOSTNAME": "mcp.test",
        "REDIS_URL": "redis://localhost:6379/0",
        "RATE_LIMIT_PER_MINUTE": "0",
        "RATE_LIMIT_PER_HOUR": "0",
        "COGNITO_USER_POOL_ID": staging_config.user_pool_id,
        "COGNITO_REGION": staging_config.region,
        "COGNITO_DOMAIN": staging_config.domain,
        "ALLOWED_COGNITO_CLIENT_IDS": f'["{staging_config.client_id}"]',
        "COGNITO_PUBLIC_CLIENT_ID": staging_config.client_id,
    }
    if staging_config.autods_api_base_url:
        env["AUTODS_API_BASE_URL"] = staging_config.autods_api_base_url
    if staging_config.products_research_base_url:
        env["PRODUCTS_RESEARCH_BASE_URL"] = staging_config.products_research_base_url

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings_module.reset_settings_cache()
    # Build the singleton get_current_user / jwks_dependency read from, so the
    # route verifies the real token against the real Cognito JWKS.
    return settings_module.get_settings()


@pytest.fixture
async def staging_app(staging_settings: Settings) -> AsyncIterator[tuple[FastAPI, McpRuntime]]:
    """The real server: MCP runtime (real upstream client) + the /mcp route.

    Auth is left intact (no jwks override), so requests are verified against the
    live Cognito user pool. The upstream HTTP client makes real calls to the
    configured staging services.
    """
    runtime = build_runtime(staging_settings)
    app = FastAPI()
    mount_mcp(app, runtime)
    try:
        yield app, runtime
    finally:
        await runtime.http_client.aclose()
        if runtime.redis is not None:
            await runtime.redis.aclose()


@pytest.fixture(autouse=True)
def _allow_e2e_env_vars() -> Iterator[None]:
    """The root ``_clean_env`` only manages a fixed allowlist; the ``E2E_*`` and
    ``RUN_STAGING_E2E`` vars fall outside it and so survive untouched. This
    fixture documents that dependency and is a no-op placeholder for it."""
    yield
