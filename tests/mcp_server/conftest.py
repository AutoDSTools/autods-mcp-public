"""Shared fixtures for the Phase D MCP runtime tests.

Provides a self-contained RSA signer / JWKS / token factory (so these tests
don't depend on the auth package's conftest), helpers to build manifests on
disk, and an in-process MCP client harness that drives the real Streamable HTTP
transport over an ``httpx.ASGITransport`` — exercising middleware, the Phase B
auth dependency, the transport route, and the call_tool → dispatcher path.
"""

import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from fastapi import FastAPI
from jwt.algorithms import RSAAlgorithm
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from autods_mcp_server.auth.dependency import jwks_dependency
from autods_mcp_server.auth.jwks import JWKSClient
from autods_mcp_server.mcp_transport import McpRuntime, build_runtime, mount_mcp
from autods_mcp_server.settings import Settings

TEST_POOL = "us-west-2_TESTPOOL"
TEST_ISSUER = f"https://cognito-idp.us-west-2.amazonaws.com/{TEST_POOL}"
TEST_CLIENT_ID = "test-client-id"


@dataclass(frozen=True)
class SigningKey:
    kid: str
    private_key: RSAPrivateKey

    def jwk(self) -> dict[str, Any]:
        pub_pem = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        jwk = json.loads(RSAAlgorithm.to_jwk(serialization.load_pem_public_key(pub_pem)))
        jwk.update(kid=self.kid, alg="RS256", use="sig")
        return jwk


@pytest.fixture(scope="session")
def signing_key() -> SigningKey:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return SigningKey(kid="primary-kid", private_key=private_key)


@pytest.fixture
def access_token(signing_key: SigningKey) -> str:
    """A freshly minted, signed Cognito-shaped access token."""
    now = int(time.time())
    return pyjwt.encode(
        {
            "sub": "user-1",
            "iss": TEST_ISSUER,
            "client_id": TEST_CLIENT_ID,
            "exp": now + 3600,
            "iat": now,
            "token_use": "access",
        },
        signing_key.private_key,
        algorithm="RS256",
        headers={"kid": signing_key.kid},
    )


@pytest.fixture
def jwks_client(signing_key: SigningKey) -> JWKSClient:
    jwks = {"keys": [signing_key.jwk()]}

    async def fetch(_url: str) -> dict[str, Any]:
        return jwks

    return JWKSClient(jwks_url="https://test/jwks.json", fetcher=fetch)


@pytest.fixture
def mcp_settings(env, tmp_path: Path) -> Callable[..., Settings]:
    """Factory: build local-env Settings, optionally pointed at a manifest dir.

    Defaults to the bundled ``manifests/`` (the vendored products manifest);
    pass ``manifest_dir`` (e.g. an empty dir) to override.
    """

    def _make(manifest_dir: Path | str | None = None, **overrides: str) -> Settings:
        values: dict[str, str] = {
            "MCP_ENV": "local",
            "COGNITO_USER_POOL_ID": TEST_POOL,
            "COGNITO_REGION": "us-west-2",
            "ALLOWED_COGNITO_CLIENT_IDS": f'["{TEST_CLIENT_ID}"]',
            "COGNITO_PUBLIC_CLIENT_ID": TEST_CLIENT_ID,
            "COGNITO_DOMAIN": "autods.auth.us-west-2.amazoncognito.com",
            "AUTODS_API_BASE_URL": "https://autods-api.test",
            "PRODUCTS_RESEARCH_BASE_URL": "https://products-research.test",
        }
        if manifest_dir is not None:
            values["MCP_MANIFEST_DIR"] = str(manifest_dir)
        values.update(overrides)
        env(**values)
        return Settings()  # type: ignore[call-arg]

    return _make


@pytest.fixture
def empty_manifest_dir(tmp_path: Path) -> Path:
    """An existing but empty manifest directory → zero registered tools."""
    directory = tmp_path / "empty_manifests"
    directory.mkdir()
    return directory


@pytest.fixture
def bundled_manifest_dir() -> Path:
    """The repo's committed ``manifests/`` (carries the vendored products.json)."""
    import autods_mcp_server

    return Path(autods_mcp_server.__file__).resolve().parents[2] / "manifests"


@pytest.fixture
def write_manifest(tmp_path: Path) -> Callable[..., Path]:
    """Factory: write a manifest dict to a fresh dir and return that dir."""

    def _make(manifest: dict[str, Any], *, filename: str = "test.json") -> Path:
        directory = tmp_path / "manifests"
        directory.mkdir(exist_ok=True)
        (directory / filename).write_text(json.dumps(manifest), encoding="utf-8")
        return directory

    return _make


@pytest.fixture
def make_mcp_app(jwks_client: JWKSClient) -> Callable[..., tuple[FastAPI, McpRuntime]]:
    """Build a FastAPI app with the MCP transport mounted and auth wired.

    ``upstream_handler`` (an ``httpx.MockTransport`` handler) stands in for the
    real upstream so call_tool tests are deterministic.
    """

    def _make(
        settings: Settings,
        upstream_handler: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> tuple[FastAPI, McpRuntime]:
        client = (
            httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler)) if upstream_handler is not None else None
        )
        runtime = build_runtime(settings, http_client=client)
        app = FastAPI()
        mount_mcp(app, runtime)
        app.dependency_overrides[jwks_dependency] = lambda: jwks_client
        return app, runtime

    return _make


@asynccontextmanager
async def mcp_client_session(
    app: FastAPI,
    runtime: McpRuntime,
    *,
    token: str | None,
) -> AsyncIterator[ClientSession]:
    """Drive the in-process app via the real Streamable HTTP MCP client.

    Runs the session manager's task group for the duration (ASGITransport does
    not trigger the app lifespan), then yields an initialized ``ClientSession``.
    The bearer token is baked into the ASGITransport-backed client so it rides
    every request the MCP client makes.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else None
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://mcp.test",
        headers=headers,
        timeout=30,
    )

    async with runtime.session_manager.run():
        async with (
            http_client,
            streamable_http_client("http://mcp.test/mcp", http_client=http_client) as (read, write, _get_session_id),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session
