"""MCP OAuth spec endpoints.

Phase C of the Public MCP epic (RD-53). Implements the discovery surface
MCP clients (Claude, Cursor, MCP Inspector) use to bootstrap OAuth:

- ``GET /.well-known/oauth-protected-resource`` — RFC 9728 PRM. The
  resource server declares which authorization servers can mint tokens
  for it and which scopes it supports.
- ``GET /.well-known/oauth-authorization-server`` — RFC 8414 AS metadata.
  We act as a thin proxy in front of Cognito Hosted UI: authorize/token
  endpoints point at Cognito, but ``registration_endpoint`` points back
  at our DCR shim so clients receive a fixed Cognito client_id rather
  than attempting a (Cognito-unsupported) DCR call.
- ``POST /oauth/register`` — RFC 7591 DCR shim. Returns the pre-created
  public Cognito client_id and echoes back validated ``redirect_uris``.
"""

from autods_mcp_server.oauth.metadata import (
    AS_METADATA_PATH,
    PRM_PATH,
    AuthorizationServerMetadata,
    ProtectedResourceMetadata,
    build_as_metadata,
    build_prm,
)
from autods_mcp_server.oauth.registration import (
    DCR_PATH,
    ClientRegistrationRequest,
    ClientRegistrationResponse,
    register_client,
)
from autods_mcp_server.oauth.routes import router

__all__ = [
    "AS_METADATA_PATH",
    "DCR_PATH",
    "PRM_PATH",
    "AuthorizationServerMetadata",
    "ClientRegistrationRequest",
    "ClientRegistrationResponse",
    "ProtectedResourceMetadata",
    "build_as_metadata",
    "build_prm",
    "register_client",
    "router",
]
