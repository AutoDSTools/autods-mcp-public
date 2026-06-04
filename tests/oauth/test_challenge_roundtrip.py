"""C5 acceptance — 401 WWW-Authenticate URL resolves to a real PRM document.

Phase B established the `WWW-Authenticate: Bearer resource_metadata="..."`
header on 401. Phase C lights up the URL that header advertises. This
test closes the loop: a client following the spec from an unauthenticated
request lands on a parseable PRM document on the second hop.
"""

from typing import Annotated
from urllib.parse import urlparse

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from autods_mcp_server.auth import (
    UserContext,
    get_current_user,
    settings_dependency,
)
from autods_mcp_server.middleware import OriginAllowlistMiddleware
from autods_mcp_server.oauth import router as oauth_router
from autods_mcp_server.settings import Settings


def test_unauthenticated_request_advertises_resolvable_prm(env) -> None:
    """The PRM URL handed back in WWW-Authenticate must dereference to a real document.

    A client implementing the MCP OAuth spec follows the header URL to
    discover the resource metadata. If that URL 404s or 503s, the whole
    discovery flow is dead. This is the wire-level smoke test that ties
    Phase B (challenge) and Phase C (PRM endpoint) together.

    The unauthenticated request is rejected before JWKS lookup, so this
    test deliberately avoids constructing any JWKS plumbing.
    """
    env(
        MCP_ENV="staging",
        COGNITO_USER_POOL_ID="us-west-2_TESTPOOL",
        COGNITO_REGION="us-west-2",
        ALLOWED_COGNITO_CLIENT_IDS='["test-client"]',
        COGNITO_PUBLIC_CLIENT_ID="test-client",
        COGNITO_DOMAIN="autods-staging.auth.us-west-2.amazoncognito.com",
        MCP_REGISTRATION_REDIRECT_URIS='["https://claude.ai/api/mcp/auth_callback"]',
        FORCE_HTTPS="true",
        PUBLIC_HOSTNAME="mcp.autods.com",
    )
    settings = Settings()  # type: ignore[call-arg]

    app = FastAPI()
    app.add_middleware(OriginAllowlistMiddleware, settings=settings)
    app.include_router(oauth_router)

    @app.get("/mcp")
    async def mcp_root(_user: Annotated[UserContext, Depends(get_current_user)]) -> dict[str, str]:
        return {"ok": "true"}

    app.dependency_overrides[settings_dependency] = lambda: settings

    with TestClient(app) as client:
        challenge_response = client.get(
            "/mcp",
            headers={
                "origin": "https://claude.ai",
                "host": "mcp.autods.com",
                "x-forwarded-proto": "https",
            },
        )
        assert challenge_response.status_code == 401
        challenge = challenge_response.headers["www-authenticate"]
        prm_url = challenge.split('resource_metadata="', 1)[1].split('"', 1)[0]
        assert urlparse(prm_url).hostname == "mcp.autods.com"

        # Now follow the URL the server told us to follow.
        prm_response = client.get(
            urlparse(prm_url).path,
            headers={
                "origin": "https://claude.ai",
                "host": "mcp.autods.com",
                "x-forwarded-proto": "https",
            },
        )
        assert prm_response.status_code == 200
        prm_body = prm_response.json()
        assert prm_body["resource"] == "https://mcp.autods.com/mcp"
        assert prm_body["authorization_servers"] == ["https://mcp.autods.com"]
