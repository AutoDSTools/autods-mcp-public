"""Shared URL helpers.

The public hostname we advertise (in OAuth metadata, in
``WWW-Authenticate`` headers, etc.) must not be steerable by attacker
``Host`` / ``X-Forwarded-Host`` headers. When ``PUBLIC_HOSTNAME`` is
configured (mandatory in staging/prod), we pin to it. Local dev can
fall back to request-derived host.
"""

from starlette.requests import Request

from autods_mcp_server.settings import Settings

# Single source of truth for the PRM endpoint path. Shared by the OAuth
# router that *serves* the document (``oauth.metadata``) and the 401
# ``WWW-Authenticate`` builder that *advertises* its URL
# (``auth.dependency``). Kept here — rather than in either package — to
# avoid an auth<->oauth import cycle, and so the challenge URL and the
# registered route can never drift apart.
PRM_PATH = "/.well-known/oauth-protected-resource"

# Single source of truth for the MCP transport path. The PRM ``resource``
# identifier (RFC 9728 / RFC 8707 audience) is this server's canonical MCP
# URL — ``{base_url}{MCP_PATH}`` — not the bare origin. The transport itself
# is mounted here in a later phase; advertising it now keeps the resource
# identifier and the route aligned from the start.
MCP_PATH = "/mcp"


def effective_base_url(request: Request, settings: Settings) -> str:
    """Resolve ``scheme://host`` for URLs we publish to clients.

    Pins to ``settings.public_hostname`` whenever it's set; falls back to
    request-derived scheme/host only in local dev.
    """
    if settings.public_hostname:
        scheme = "https" if settings.force_https else "http"
        return f"{scheme}://{settings.public_hostname}"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"
