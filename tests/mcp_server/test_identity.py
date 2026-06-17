"""Unit tests for the self-identity resolver (RD-68).

Drives a real :class:`OperationDispatcher` over an ``httpx.MockTransport`` so the
resolver exercises the same dispatch/parse path as a tool call. Asserts: the
caller's id/name/email are parsed from the single-element ``/users/list/``
payload, the caller's token is forwarded, and every failure mode (non-2xx,
transport error, malformed/empty payload) fails open to ``None``.
"""

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from autods_mcp_server.auth import UserContext
from autods_mcp_server.dispatch import OperationDispatcher
from autods_mcp_server.identity import (
    SELF_IDENTITY_OPERATION_ID,
    SelfIdentity,
    SelfIdentityResolver,
)
from autods_mcp_server.manifests import ManifestRegistry
from autods_mcp_server.manifests.schema import Manifest
from autods_mcp_server.settings import Settings


@pytest.fixture
def user() -> UserContext:
    return UserContext(sub="sub-1", raw_token=SecretStr("the-user-token"))


def _registry() -> ManifestRegistry:
    manifest = Manifest.model_validate(
        {
            "server_name": "users",
            "base_url_key": "autods_api",
            "operations": [
                {
                    "operation_id": SELF_IDENTITY_OPERATION_ID,
                    "method": "GET",
                    "path": "/users/list/",
                    "parameters": [],
                    "annotations": {"title": "Get Current User", "readOnlyHint": True},
                }
            ],
        }
    )
    return ManifestRegistry([manifest])


def _resolver(mcp_settings, handler) -> SelfIdentityResolver:
    settings: Settings = mcp_settings()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    dispatcher = OperationDispatcher(_registry(), settings, client)
    return SelfIdentityResolver(dispatcher)


async def test_resolves_identity_from_single_element_list(mcp_settings, user) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=[{"id": 999, "name": "Alice", "email": "alice@example.com"}])

    resolver = _resolver(mcp_settings, handler)

    identity = await resolver.resolve(user)

    assert identity == SelfIdentity(user_id="999", name="Alice", email="alice@example.com")
    # The numeric upstream id is stringified for a stable identity key.
    assert isinstance(identity.user_id, str)
    # Hit /users/list/ with the caller's own forwarded token.
    assert captured["url"].endswith("/users/list/")
    assert captured["auth"] == "Bearer the-user-token"


async def test_accepts_bare_object_payload(mcp_settings, user) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 7, "name": "Bob", "email": "bob@example.com"})

    resolver = _resolver(mcp_settings, handler)

    assert await resolver.resolve(user) == SelfIdentity(user_id="7", name="Bob", email="bob@example.com")


async def test_missing_optional_fields_are_none(mcp_settings, user) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": 42}])

    resolver = _resolver(mcp_settings, handler)

    assert await resolver.resolve(user) == SelfIdentity(user_id="42", name=None, email=None)


async def test_non_2xx_fails_open(mcp_settings, user) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "blocked"})

    resolver = _resolver(mcp_settings, handler)

    assert await resolver.resolve(user) is None


async def test_transport_error_fails_open(mcp_settings, user) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream down")

    resolver = _resolver(mcp_settings, handler)

    assert await resolver.resolve(user) is None


@pytest.mark.parametrize("payload", [[], [{"name": "no id"}], {"name": "no id"}, "not json", None])
async def test_malformed_payload_fails_open(mcp_settings, user, payload) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        if payload == "not json":
            return httpx.Response(200, text="not json", headers={"content-type": "text/plain"})
        return httpx.Response(200, json=payload)

    resolver = _resolver(mcp_settings, handler)

    assert await resolver.resolve(user) is None
