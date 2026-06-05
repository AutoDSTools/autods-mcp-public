"""Call an MCP operation against a running server, for manual debugging.

Drives the real Streamable HTTP MCP client (the same stack the tests use), so
the ``initialize`` handshake and SSE framing are handled for you — you just pass
an operation and its arguments. Auth is obtained via the same OAuth flow the
Claude client uses (Authorization Code + PKCE against Cognito), with the token
cached locally so you only authorize in the browser once.

Usage:
    uv run python scripts/mcp_call.py list                  # list tool names
    uv run python scripts/mcp_call.py token                 # print an access token (for reuse: export T=$(...))
    uv run python scripts/mcp_call.py list_stores_api
    uv run python scripts/mcp_call.py get_bulk_action_items '{"store_ids":"1","bulk_action_id":123}'

Env:
    MCP_TOKEN     use this bearer token instead of running the OAuth flow
    MCP_URL       server endpoint (default: http://localhost:2049/mcp)
    MCP_NO_CACHE  set to ignore (and overwrite) the cached token

Endpoints, client_id, scopes, and the loopback redirect URI are read from the
repo ``Settings`` (i.e. from ``.env``), so this matches what the app client accepts.
"""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import tempfile
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from autods_mcp_server.settings import Settings

_CACHE = Path(tempfile.gettempdir()) / "autods_mcp_token.json"


def _loopback_redirect(settings: Settings) -> str:
    """Pick a registered ``http://localhost:.../...`` redirect for the local flow."""
    for uri in settings.mcp_registration_redirect_uris:
        host = urllib.parse.urlparse(uri).hostname
        if host in ("localhost", "127.0.0.1"):
            return uri
    raise SystemExit("No loopback redirect URI is registered (MCP_REGISTRATION_REDIRECT_URIS).")


class _CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            _CallbackHandler.code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>Authorization complete - you can close this tab.</body></html>")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args) -> None:  # silence the default request logging
        pass


def _oauth_token(settings: Settings) -> dict:
    """Run Authorization Code + PKCE against Cognito; return the token response."""
    redirect_uri = _loopback_redirect(settings)
    parsed = urllib.parse.urlparse(redirect_uri)
    host, port = parsed.hostname, parsed.port or 80

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    authorize_url = (
        settings.cognito_authorization_endpoint
        + "?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": settings.cognito_public_client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(settings.mcp_oauth_scopes),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )

    print(
        f"Opening browser to authorize (listening on {host}:{port}{parsed.path}):\n{authorize_url}\n",
        file=sys.stderr,
    )
    webbrowser.open(authorize_url)

    server = HTTPServer((host, port), _CallbackHandler)
    while _CallbackHandler.code is None:
        server.handle_request()
    code = _CallbackHandler.code

    resp = httpx.post(
        settings.cognito_token_endpoint,
        data={
            "grant_type": "authorization_code",
            "client_id": settings.cognito_public_client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise SystemExit(f"Token exchange failed ({resp.status_code}): {resp.text}")
    return resp.json()


def get_token() -> str:
    """Return a (cached, unexpired) access token, running the OAuth flow if needed."""
    if not os.environ.get("MCP_NO_CACHE") and _CACHE.exists():
        cached = json.loads(_CACHE.read_text())
        if cached.get("expires_at", 0) - 60 > time.time():
            return cached["access_token"]

    settings = Settings()  # type: ignore[call-arg]
    token = _oauth_token(settings)
    data = {
        "access_token": token["access_token"],
        "id_token": token["id_token"],
        "expires_at": time.time() + token.get("expires_in", 3600),
    }
    _CACHE.write_text(json.dumps(data))
    return token["access_token"]


async def run_call(url: str, token: str, operation: str, arguments: dict) -> int:
    http_client = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    async with http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if operation in ("list", "tools/list"):
                    tools = await session.list_tools()
                    for tool in tools.tools:
                        print(tool.name)
                    return 0
                result = await session.call_tool(operation, arguments)
                print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))
                return 1 if result.isError else 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    operation = sys.argv[1]
    arguments = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    token = os.environ.get("MCP_TOKEN") or get_token()

    if operation == "token":
        print(token)
        return 0

    url = os.environ.get("MCP_URL", "http://localhost:2049/mcp")
    return asyncio.run(run_call(url, token, operation, arguments))


if __name__ == "__main__":
    raise SystemExit(main())
