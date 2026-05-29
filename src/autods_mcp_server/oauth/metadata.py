"""OAuth metadata documents — PRM (RFC 9728) and AS metadata (RFC 8414).

The two builders are deliberately kept simple. ``resource`` and ``issuer``
must match the URL the document is fetched from (otherwise compliant
clients reject the document), so we derive both from
``urls.effective_base_url`` which pins to ``PUBLIC_HOSTNAME`` whenever it's
configured.

URL fields are typed ``str``, not ``pydantic.AnyUrl``: ``AnyUrl`` normalises
a bare-origin URL by appending a trailing slash (``https://host`` →
``https://host/``), which would break the byte-identity these documents
require — RFC 8414 §3.3 mandates ``issuer`` be identical to the value the
``/.well-known`` URL was built from, and RFC 9728 / RFC 8707 require the same
of ``resource``. The values are already computed from validated settings via
``effective_base_url``, so there's no untrusted input to validate here.
"""

from pydantic import BaseModel, Field

from autods_mcp_server.oauth.registration import DCR_PATH
from autods_mcp_server.urls import MCP_PATH, PRM_PATH

AS_METADATA_PATH = "/.well-known/oauth-authorization-server"

# ``PRM_PATH`` is re-exported (oauth/__init__ surfaces it); its canonical
# definition lives in ``urls`` alongside the 401-challenge builder that
# advertises it.
__all__ = [
    "AS_METADATA_PATH",
    "PRM_PATH",
    "AuthorizationServerMetadata",
    "ProtectedResourceMetadata",
    "build_as_metadata",
    "build_prm",
]


class ProtectedResourceMetadata(BaseModel):
    """RFC 9728 Protected Resource Metadata.

    Only the fields MCP clients actually consume — we don't pad the
    document with optional fields that have no consumer here.
    """

    resource: str
    authorization_servers: list[str]
    scopes_supported: list[str]
    bearer_methods_supported: list[str] = Field(default_factory=lambda: ["header"])


class AuthorizationServerMetadata(BaseModel):
    """RFC 8414 Authorization Server Metadata.

    We're a *proxy* AS in front of Cognito:

    - ``authorization_endpoint`` / ``token_endpoint`` point at Cognito
      Hosted UI directly — the OAuth user-agent dance never touches us.
    - ``registration_endpoint`` points at our DCR shim because Cognito
      doesn't speak DCR; clients must get a fixed client_id from us.
    - ``issuer`` matches the URL this document is fetched from (RFC 8414
      §3.3 case-sensitive equality requirement).

    Token verification in this server still validates against Cognito's
    ``iss`` (``cognito_issuer`` in settings), which differs from the
    ``issuer`` advertised here — that mismatch is by design and follows
    the standard MCP OAuth proxy pattern.
    """

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    registration_endpoint: str
    scopes_supported: list[str]
    response_types_supported: list[str] = Field(default_factory=lambda: ["code"])
    grant_types_supported: list[str] = Field(
        default_factory=lambda: ["authorization_code", "refresh_token"],
    )
    code_challenge_methods_supported: list[str] = Field(default_factory=lambda: ["S256"])
    token_endpoint_auth_methods_supported: list[str] = Field(default_factory=lambda: ["none"])


def build_prm(*, base_url: str, scopes: list[str]) -> ProtectedResourceMetadata:
    """Build the PRM document for this resource server.

    ``resource`` is this server's canonical MCP URL (``{base_url}{MCP_PATH}``)
    — the audience tokens are minted for under RFC 8707, and the identifier
    the ``/.well-known/oauth-protected-resource`` path is derived from.

    ``authorization_servers`` points at ``base_url`` (our proxy AS issuer), so
    MCP clients discover AS metadata via our own
    ``/.well-known/oauth-authorization-server`` endpoint (where we control the
    ``registration_endpoint`` value). If we instead pointed at Cognito's issuer
    URL, clients would try to fetch AS metadata from Cognito — which doesn't
    expose RFC 8414 metadata.
    """
    return ProtectedResourceMetadata(
        resource=f"{base_url}{MCP_PATH}",
        authorization_servers=[base_url],
        scopes_supported=list(scopes),
    )


def build_as_metadata(
    *,
    base_url: str,
    authorization_endpoint: str,
    token_endpoint: str,
    jwks_uri: str,
    scopes: list[str],
) -> AuthorizationServerMetadata:
    """Build the AS metadata document advertised as our proxy AS."""
    return AuthorizationServerMetadata(
        issuer=base_url,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        jwks_uri=jwks_uri,
        registration_endpoint=f"{base_url}{DCR_PATH}",
        scopes_supported=list(scopes),
    )
