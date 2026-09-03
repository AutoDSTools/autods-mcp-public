"""Call an MCP operation against a running server, for manual debugging.

Drives the real Streamable HTTP MCP client (the same stack the tests use), so
the ``initialize`` handshake and SSE framing are handled for you — you just pass
an operation and its arguments. Auth is obtained via the same OAuth flow the
Claude client uses (Authorization Code + PKCE against Cognito), with the token
cached locally so you only authorize in the browser once.

Usage:
    uv run python scripts/mcp_call.py --help                # this text, without signing in
    uv run python scripts/mcp_call.py list                  # list tool names
    uv run python scripts/mcp_call.py descriptors           # dump the FULL handshake payload as JSON (C3/C4/C5)
    uv run python scripts/mcp_call.py instructions          # print the server instructions from the handshake
    uv run python scripts/mcp_call.py resources             # list the resource URIs the server advertises
    uv run python scripts/mcp_call.py token                 # print an access token (for reuse: export T=$(...))
    uv run python scripts/mcp_call.py list_stores_api
    uv run python scripts/mcp_call.py get_playbook '{"name":"product_import"}'
    uv run python scripts/mcp_call.py get_bulk_action_items '{"store_ids":"1","bulk_action_id":123}'

Env:
    MCP_TOKEN     use this bearer token instead of signing in (see below)
    MCP_URL       server endpoint (default: http://localhost:2049/mcp)
    MCP_NO_CACHE  set to ignore (and overwrite) the cached token

**The endpoint and the credential come from different places, and only the
endpoint is on the command line.** ``MCP_URL`` says which server to call, while
the sign-in — endpoints, client_id, scopes, loopback redirect URI — is built
from the repo ``Settings``, i.e. from ``.env``. So a sign-in run from this
checkout always authorizes against whichever Cognito ``.env`` names (staging,
normally), *whatever* ``MCP_URL`` points at. That is a coupling, not a
guarantee of a match.

Any target other than ``.env``'s own environment therefore needs ``MCP_TOKEN``,
and the token has to come from a client already authorized there:

    MCP_TOKEN=$(uv run python scripts/mcp_token.py autods-public-prod) \\
      MCP_URL=https://mcp.autods.com/mcp uv run python scripts/mcp_call.py instructions

Without ``MCP_TOKEN`` this refuses to sign in when the target host advertises a
different authorization server than ``.env`` names, rather than minting a token
the target will only 401 — see ``_refuse_on_environment_mismatch``.
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
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from autods_mcp_server.settings import Settings

_DEFAULT_URL = "http://localhost:2049/mcp"

# Hosts for which ``.env`` is the right Cognito config by construction, so the
# discovery cross-check below is skipped rather than failed.
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def _cache_path(settings: Settings) -> Path:
    """Cache file for *this* Cognito environment.

    Keyed on the authorization endpoint and client id, because a single shared
    cache file is how a **staging** token silently ends up being sent to
    production: the token is served from cache, no browser opens, nothing warns,
    and the 401 that follows reads exactly like a broken release. Keying it means
    switching targets can only ever miss the cache, never hit the wrong entry.
    """
    fingerprint = f"{settings.cognito_authorization_endpoint}|{settings.cognito_public_client_id}"
    digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"autods_mcp_token_{digest}.json"


def _advertised_authorization_endpoint(mcp_url: str) -> str | None:
    """The authorization server the *target* advertises, or ``None`` if unknown.

    Asks the target for its RFC 8414 metadata — the same document section S3
    grades and the same one a real MCP client bootstraps from. Unauthenticated
    and cheap. ``None`` means the question could not be answered (unreachable,
    non-2xx, malformed), which is deliberately not treated as a mismatch.
    """
    origin = urllib.parse.urlunsplit(urllib.parse.urlsplit(mcp_url)[:2] + ("", "", ""))
    try:
        response = httpx.get(f"{origin}/.well-known/oauth-authorization-server", timeout=10)
        if response.status_code != 200:
            return None
        return response.json().get("authorization_endpoint")
    except (httpx.HTTPError, json.JSONDecodeError, ValueError):
        return None


def _refuse_on_environment_mismatch(mcp_url: str, settings: Settings) -> None:
    """Exit when signing in against ``.env`` cannot produce a token for the target.

    This is the guard for the trap the module docstring describes, and it has to
    run *before* the token cache is consulted, not just before the browser opens:
    the expensive version of this mistake is the silent one, where a cached
    staging token is handed to production without any sign-in happening at all.
    """
    host = urllib.parse.urlsplit(mcp_url).hostname or ""
    if host in _LOCAL_HOSTS:
        return

    advertised = _advertised_authorization_endpoint(mcp_url)
    if advertised is None:
        print(
            f"warning: {host} did not answer AS-metadata discovery, so the sign-in target could not be "
            f"confirmed. Signing in against {settings.cognito_domain} (from .env).",
            file=sys.stderr,
        )
        return

    def _origin(url: str) -> str:
        return urllib.parse.urlunsplit(urllib.parse.urlsplit(url)[:2] + ("", "", ""))

    if _origin(advertised) == _origin(settings.cognito_authorization_endpoint):
        return

    raise SystemExit(
        f"Refusing to sign in: {host} authorizes against {_origin(advertised)}, but this checkout's .env "
        f"names {_origin(settings.cognito_authorization_endpoint)}. A token minted here would only 401 there.\n"
        f"Pass a token from a client already authorized for that host instead:\n"
        f"  MCP_TOKEN=$(uv run python scripts/mcp_token.py <alias>) MCP_URL={mcp_url} "
        f"uv run python scripts/mcp_call.py <operation>"
    )


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


def get_token(mcp_url: str) -> str:
    """Return a (cached, unexpired) access token, running the OAuth flow if needed.

    Takes the target URL because the environment cross-check has to happen here,
    ahead of the cache read — see ``_refuse_on_environment_mismatch``.
    """
    settings = Settings()  # type: ignore[call-arg]
    _refuse_on_environment_mismatch(mcp_url, settings)

    cache = _cache_path(settings)
    if not os.environ.get("MCP_NO_CACHE") and cache.exists():
        cached = json.loads(cache.read_text())
        if cached.get("expires_at", 0) - 60 > time.time():
            return cached["access_token"]

    token = _oauth_token(settings)
    data = {
        "access_token": token["access_token"],
        "id_token": token["id_token"],
        "expires_at": time.time() + token.get("expires_in", 3600),
    }
    cache.write_text(json.dumps(data))
    return token["access_token"]


async def run_call(url: str, token: str, operation: str, arguments: dict) -> int:
    # httpx2, not httpx: the SDK's client transports run on httpx2 since mcp
    # 2.x and the two are not interchangeable — an httpx client passed here
    # degrades silently instead of raising. (The OAuth calls above stay on
    # httpx; they're plain requests, not MCP traffic.)
    http_client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    async with http_client:
        async with Client(streamable_http_client(url, http_client=http_client)) as client:
            if operation in ("list", "tools/list"):
                tools = await client.list_tools()
                for tool in tools.tools:
                    print(tool.name)
                return 0
            if operation in ("descriptors", "tools/list-full"):
                # ``list`` prints names only, which is not enough to grade
                # release-check C3/C4/C5 (descriptions, inputSchema, annotation
                # hints) or to diff the payload against what this checkout
                # builds — so this dumps the whole thing, including the
                # ``serverInfo`` version stamp that pins the deployed build.
                # ``by_alias``: 2.x model fields are snake_case with camelCase
                # aliases, so a bare model_dump would print ``input_schema``
                # rather than the wire shape a client actually received.
                listed = await client.list_tools()
                payload = {
                    "server_info": (
                        client.server_info.model_dump(by_alias=True, mode="json") if client.server_info else None
                    ),
                    "instructions": client.instructions,
                    "tools": [tool.model_dump(by_alias=True, mode="json") for tool in listed.tools],
                }
                print(json.dumps(payload, indent=2, default=str))
                return 0
            if operation == "instructions":
                # The server ``instructions`` as they arrived in this client's
                # InitializeResult — i.e. exactly the block a real client puts in
                # the model's system prompt (RD-90).
                print(client.instructions or "(the server advertised no instructions)")
                return 0
            if operation in ("resources", "resources/list"):
                # The playbook mirror (RD-100). Also the quickest check that the
                # server is declaring the ``resources`` capability at all.
                listed = await client.list_resources()
                for resource in listed.resources:
                    print(f"{resource.uri}\t{resource.mime_type}\t{resource.title or resource.name}")
                return 0
            result = await client.call_tool(operation, arguments)
            # ``by_alias``: 2.x model fields are snake_case, so a bare
            # model_dump() would print ``structured_content``/``is_error``
            # rather than what actually went over the wire.
            print(json.dumps(result.model_dump(by_alias=True, mode="json"), indent=2, default=str))
            return 1 if result.is_error else 0


def main() -> int:
    # Everything that can be decided from argv is decided before a token is
    # acquired. Authenticating first meant `--help`, or any typo, opened a
    # browser sign-in (and, with a token to hand, sent the typo upstream as an
    # operation_id — which is what filled Sentry with UnknownOperationError).
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    if argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    operation = argv[0]
    if operation.startswith("-"):
        print(f"Unknown option {operation!r}; pass an operation id, or --help.", file=sys.stderr)
        return 2
    try:
        arguments = json.loads(argv[1]) if len(argv) > 1 else {}
    except json.JSONDecodeError as exc:
        print(f"Arguments must be a JSON object: {exc}", file=sys.stderr)
        return 2
    if not isinstance(arguments, dict):
        print(f"Arguments must be a JSON object, got {type(arguments).__name__}.", file=sys.stderr)
        return 2

    url = os.environ.get("MCP_URL", _DEFAULT_URL)
    token = os.environ.get("MCP_TOKEN") or get_token(url)

    if operation == "token":
        print(token)
        return 0

    return asyncio.run(run_call(url, token, operation, arguments))


if __name__ == "__main__":
    raise SystemExit(main())
