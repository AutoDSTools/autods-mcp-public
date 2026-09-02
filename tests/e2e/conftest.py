"""Fixtures for the end-to-end suites.

Two different things live under ``tests/e2e``, and they differ in *what* they
point at:

* ``test_staging_smoke.py`` (Phase E / E3) stands the **real server up in this
  process** (real Cognito JWT verification, real upstream HTTP) with
  ``MCP_ENV=staging`` and drives it through the real MCP Streamable HTTP
  client. It tests the code in the checkout against live upstreams.
* ``test_release_checks_s.py`` probes a **deployed** server over the network
  (``https://mcp-staging.autods.com`` by default) and asserts nothing about
  the local code except its version. It is section *S* of
  ``docs/release-checks.md``, automated.
* ``test_release_checks_c.py`` is section *C* of the same checklist: it opens a
  real authenticated handshake against that deployed server and diffs the
  payload — tool set, descriptions, ``inputSchema``, annotations, instructions,
  resources — against what this checkout's manifests build. It needs a token
  but **no fixtures** (no store, no product, no entitlement), which is what
  keeps it a test rather than prose. Unlike section S it *is* coupled to the
  local code: run it from the released commit, or the diff reports the
  checkout's own unreleased changes as drift.

The first group requires live staging credentials, the second only network
access. Both are **opt-in**: the in-process suite is skipped unless
``RUN_STAGING_E2E=1`` and the required staging env vars are present (see
``_REQUIRED_VARS``); the deployed probes are skipped unless
``RUN_RELEASE_CHECKS=1`` *or* ``RUN_STAGING_E2E=1``. This keeps ``uv run
pytest`` green on a laptop / CI box that has no staging secrets while still
giving operators a one-command end-to-end check (``RUN_STAGING_E2E=1 uv run
pytest tests/e2e``) and a one-command post-release probe (``make
release-checks``).

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

Env vars for the deployed release checks (section S) — all optional, and no
credentials among them; section S is the *unauthenticated* surface:

* ``RUN_RELEASE_CHECKS=1`` — the gate (``RUN_STAGING_E2E=1`` also opens it).
* ``MCP_RELEASE_BASE_URL`` — origin of the deployed server under test
  (default ``https://mcp-staging.autods.com``). No trailing slash, no ``/mcp``.
* ``E2E_EXPECTED_COGNITO_DOMAIN`` / ``E2E_REGISTERED_REDIRECT_URI`` — override
  the per-environment expectations in ``KNOWN_ENVIRONMENTS`` below (needed
  only when probing a host that table doesn't know).

Section C additionally needs a bearer token for that deployed host, and takes
whichever is available:

* ``MCP_TOKEN`` — preferred, and what ``scripts/mcp_call.py token`` prints. No
  new secrets, so a section-C run works wherever a ``.env`` already does.
* otherwise the ``E2E_COGNITO_*`` password grant above, so CI can run it
  non-interactively.
"""

import asyncio
import base64
import hashlib
import hmac
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from autods_mcp_server import __version__
from autods_mcp_server import settings as settings_module
from autods_mcp_server.manifests.instructions import assert_instructions_within_limit, build_instructions
from autods_mcp_server.manifests.loader import ManifestRegistry, load_manifests
from autods_mcp_server.manifests.playbooks import (
    assert_playbooks_valid,
    build_playbook_index,
    build_playbook_registry,
)
from autods_mcp_server.mcp_transport import (
    _PLAYBOOK_MIME_TYPE,
    _PLAYBOOK_RESOURCE_SCHEME,
    McpRuntime,
    build_runtime,
    mount_mcp,
)
from autods_mcp_server.settings import Settings
from autods_mcp_server.tools import build_tools

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


# --------------------------------------------------------------------------
# Deployed-server probes (docs/release-checks.md section S)
# --------------------------------------------------------------------------

DEFAULT_RELEASE_BASE_URL = "https://mcp-staging.autods.com"

# An unregistered redirect URI, used to prove the DCR shim actually rejects.
# It must never appear in any environment's MCP_REGISTRATION_REDIRECT_URIS.
UNREGISTERED_REDIRECT_URI = "https://release-check.invalid/oauth/callback"


@dataclass(frozen=True)
class DeployedEnvironment:
    """Per-environment facts an S check compares the live answers against.

    These mirror the helm values in ``autods-mcp-deploy``
    (``values-staging.yaml`` / ``values-prod.yaml``). They're duplicated here
    rather than derived because the point of the check is to catch the
    deployment drifting from what it's supposed to be — deriving them from the
    server's own answer would make every assertion trivially true.
    """

    cognito_hosted_ui_base_url: str
    # A redirect URI that *is* on this environment's allowlist. Claude Code's
    # fixed loopback callback is registered in every environment, so it's the
    # stable choice.
    registered_redirect_uri: str


KNOWN_ENVIRONMENTS: dict[str, DeployedEnvironment] = {
    "mcp-staging.autods.com": DeployedEnvironment(
        cognito_hosted_ui_base_url="https://auth-staging.autods.com",
        registered_redirect_uri="http://localhost:2048/callback",
    ),
    "mcp.autods.com": DeployedEnvironment(
        cognito_hosted_ui_base_url="https://auth.autods.com",
        registered_redirect_uri="http://localhost:2048/callback",
    ),
}


@dataclass(frozen=True)
class ReleaseTarget:
    """The deployed server under test, plus what it is expected to answer."""

    base_url: str
    cognito_hosted_ui_base_url: str | None
    registered_redirect_uri: str | None

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).netloc

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    @property
    def prm_url(self) -> str:
        return f"{self.base_url}/.well-known/oauth-protected-resource"

    @property
    def as_metadata_url(self) -> str:
        return f"{self.base_url}/.well-known/oauth-authorization-server"

    @property
    def registration_url(self) -> str:
        return f"{self.base_url}/oauth/register"


@pytest.fixture(scope="session")
def release_target() -> ReleaseTarget:
    """Resolve which deployment to probe, or skip the section."""
    if os.environ.get("RUN_RELEASE_CHECKS") != "1" and os.environ.get("RUN_STAGING_E2E") != "1":
        pytest.skip("release checks are opt-in; set RUN_RELEASE_CHECKS=1 to run them")

    base_url = os.environ.get("MCP_RELEASE_BASE_URL", DEFAULT_RELEASE_BASE_URL).rstrip("/")
    known = KNOWN_ENVIRONMENTS.get(urlsplit(base_url).netloc)
    return ReleaseTarget(
        base_url=base_url,
        cognito_hosted_ui_base_url=(
            os.environ.get("E2E_EXPECTED_COGNITO_DOMAIN") or (known.cognito_hosted_ui_base_url if known else None)
        ),
        registered_redirect_uri=(
            os.environ.get("E2E_REGISTERED_REDIRECT_URI") or (known.registered_redirect_uri if known else None)
        ),
    )


@pytest.fixture(scope="session")
def probe(release_target: ReleaseTarget) -> Iterator[httpx.Client]:
    """A plain HTTP client for the deployed server — the ``curl`` of section S.

    ``follow_redirects=False`` so a redirect is a finding, not something the
    client papers over, and no ``Origin`` header is sent (the Origin allowlist
    middleware only rejects a *foreign* Origin, and real ``curl`` sends none).
    """
    with httpx.Client(
        timeout=30,
        follow_redirects=False,
        headers={"user-agent": "autods-mcp-release-checks"},
    ) as client:
        yield client


# --------------------------------------------------------------------------
# Deployed handshake vs this checkout (docs/release-checks.md section C)
# --------------------------------------------------------------------------

# ``tests/e2e/conftest.py`` -> ``tests/e2e`` -> ``tests`` -> repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_DIR = REPO_ROOT / "manifests"


@dataclass(frozen=True)
class Handshake:
    """What a real client actually received from the deployed server.

    ``resources`` is ``None`` when ``resources/list`` failed, with the reason in
    ``resources_error`` — the server dropping the ``resources`` capability is a
    C8 finding, and capturing it here keeps that failure from taking C3/C4 down
    with it.
    """

    server_version: str
    instructions: str
    tools: dict[str, dict]
    resources: list[dict] | None
    resources_error: str | None


@dataclass(frozen=True)
class LocalBuild:
    """What this checkout's manifests say a client *should* receive.

    Built the same way ``build_runtime`` builds it (same loaders, same lints,
    same ``build_tools`` call), so a difference against :class:`Handshake` is
    a difference in the deployed build — not in how the two were assembled.
    """

    server_version: str
    instructions: str
    tools: dict[str, dict]
    resources: list[dict]


@pytest.fixture(scope="module")
def release_access_token(request: pytest.FixtureRequest) -> str:
    """A bearer token for the deployed server, from whichever source exists.

    ``MCP_TOKEN`` first — that is what ``scripts/mcp_call.py token`` prints, so
    an operator already holding a cached OAuth token needs no new secrets. Only
    when it is unset does this fall back to the ``access_token`` password grant,
    which skips the module if the ``E2E_COGNITO_*`` vars are absent.

    A 401 out of the handshake almost always means a **stale** ``MCP_TOKEN``,
    not a broken release — refresh it before reading anything into the failure.
    """
    token = os.environ.get("MCP_TOKEN")
    if token:
        return token
    return request.getfixturevalue("access_token")


@pytest.fixture(scope="module")
def deployed_handshake(release_target: ReleaseTarget, release_access_token: str) -> Handshake:
    """Connect to the deployed server once and capture the whole handshake.

    Synchronous on purpose: it drives its own loop with ``asyncio.run`` so the
    fixture can be module-scoped without pinning an event-loop scope for the
    package (``asyncio_default_fixture_loop_scope`` is unset by design).
    """

    async def _fetch() -> Handshake:
        headers = {"Authorization": f"Bearer {release_access_token}"}
        async with httpx2.AsyncClient(headers=headers, timeout=60) as http_client:
            async with Client(streamable_http_client(release_target.mcp_url, http_client=http_client)) as client:
                listed = await client.list_tools()
                resources: list[dict] | None = None
                resources_error: str | None = None
                try:
                    listed_resources = await client.list_resources()
                    resources = [r.model_dump(by_alias=True, mode="json") for r in listed_resources.resources]
                except Exception as exc:  # noqa: BLE001 - the reason is the finding
                    resources_error = f"{type(exc).__name__}: {exc}"
                return Handshake(
                    server_version=(client.server_info.version if client.server_info else ""),
                    instructions=client.instructions or "",
                    tools={t.name: t.model_dump(by_alias=True, mode="json") for t in listed.tools},
                    resources=resources,
                    resources_error=resources_error,
                )

    return asyncio.run(_fetch())


@pytest.fixture(scope="module")
def local_build() -> LocalBuild:
    """Rebuild the handshake payload from ``manifests/`` at the current checkout."""
    manifests = load_manifests(MANIFEST_DIR)
    registry = ManifestRegistry(manifests)
    playbooks = build_playbook_registry(MANIFEST_DIR)
    assert_playbooks_valid(playbooks, registry)
    instructions = build_instructions(manifests, playbook_index=build_playbook_index(playbooks))
    assert_instructions_within_limit(instructions)
    tools = build_tools(registry.list_operations(), playbooks)
    resources = [
        {
            "uri": f"{_PLAYBOOK_RESOURCE_SCHEME}{playbook.name}",
            "name": playbook.name,
            "title": playbook.title,
            "description": playbook.when_to_use,
            "mimeType": _PLAYBOOK_MIME_TYPE,
        }
        for playbook in playbooks.list_playbooks()
    ]
    return LocalBuild(
        server_version=__version__,
        instructions=instructions,
        tools={t.name: t.model_dump(by_alias=True, mode="json") for t in tools},
        resources=resources,
    )
