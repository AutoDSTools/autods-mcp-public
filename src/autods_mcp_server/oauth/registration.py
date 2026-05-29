"""Dynamic Client Registration shim (RFC 7591).

Cognito does not implement DCR. MCP clients (Claude, Inspector) however
*require* a DCR endpoint to bootstrap — they refuse to start the OAuth
flow without one. This shim plugs that gap by returning a pre-created
public Cognito ``client_id`` for every registration request, while
validating the requested ``redirect_uris`` against an allowlist that
mirrors what's configured on Cognito.

Why exact-match (not glob): every redirect URI the OAuth flow uses must
*already* be on the Cognito client; if we accept a glob the authorize
step will simply fail at Cognito with no helpful error. Matching exactly
catches the misconfiguration here, where we can return a useful 400.
"""

import time

from pydantic import BaseModel, ConfigDict, Field

from autods_mcp_server.settings import Settings

DCR_PATH = "/oauth/register"


class ClientRegistrationRequest(BaseModel):
    """The subset of RFC 7591 fields the shim cares about.

    ``extra='ignore'`` keeps the body permissive — Claude / Inspector send
    a number of optional metadata fields (``client_name``, ``scope``,
    ``software_id``...) we don't act on, but rejecting them would be a
    pointless interop break. Anything we don't read is dropped (not retained
    on the model), so it can't leak back out through serialization.
    """

    model_config = ConfigDict(extra="ignore")

    redirect_uris: list[str] = Field(min_length=1)


class ClientRegistrationResponse(BaseModel):
    """RFC 7591 §3.2.1 successful registration response."""

    client_id: str
    client_id_issued_at: int
    redirect_uris: list[str]
    token_endpoint_auth_method: str = "none"
    grant_types: list[str] = Field(default_factory=lambda: ["authorization_code", "refresh_token"])
    response_types: list[str] = Field(default_factory=lambda: ["code"])


class RegistrationError(Exception):
    """Raised when a registration request can't be honoured.

    ``error`` follows RFC 7591 §3.2.2 (``invalid_redirect_uri``,
    ``invalid_client_metadata``); ``description`` is human-readable.
    """

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


def register_client(
    request: ClientRegistrationRequest,
    *,
    settings: Settings,
) -> ClientRegistrationResponse:
    """Validate redirect URIs against the allowlist and return the public client.

    ``cognito_public_client_id`` is a required setting, so it's always present
    by the time we get here.

    Raises:
        RegistrationError: if the redirect-URI allowlist is empty
            (misconfiguration), or if the requested ``redirect_uris`` contain
            entries not on the allowlist.
    """
    if not settings.mcp_registration_redirect_uris:
        raise RegistrationError(
            "invalid_client_metadata",
            "DCR shim is not configured (MCP_REGISTRATION_REDIRECT_URIS is empty).",
        )

    allowlist = set(settings.mcp_registration_redirect_uris)
    rejected = [uri for uri in request.redirect_uris if uri not in allowlist]
    if rejected:
        raise RegistrationError(
            "invalid_redirect_uri",
            f"redirect_uris not in allowlist: {rejected!r}",
        )

    return ClientRegistrationResponse(
        client_id=settings.cognito_public_client_id,
        client_id_issued_at=int(time.time()),
        redirect_uris=list(request.redirect_uris),
    )
