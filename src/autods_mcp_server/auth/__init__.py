"""Authentication primitives — Cognito JWT verification.

Phase B of the Public MCP epic (RD-52). Closes the JWT-validation gap
in ``AutoDSApi/helper/auth.py`` by switching from manual claim checks
to PyJWT's native ``verify_exp / verify_aud / verify_iss``.
"""

from autods_mcp_server.auth.dependency import (
    PRM_PATH,
    UserContext,
    build_www_authenticate,
    get_current_user,
    jwks_dependency,
    settings_dependency,
)
from autods_mcp_server.auth.exceptions import (
    AuthError,
    InvalidAudience,
    InvalidIssuer,
    InvalidSignature,
    JWKSUnavailable,
    MalformedToken,
    TokenExpired,
    UnknownKid,
)
from autods_mcp_server.auth.jwks import (
    JWKSClient,
    get_jwks_client,
    reset_jwks_client,
)
from autods_mcp_server.auth.verify import Claims, verify_token

__all__ = [
    "PRM_PATH",
    "AuthError",
    "Claims",
    "InvalidAudience",
    "InvalidIssuer",
    "InvalidSignature",
    "JWKSClient",
    "JWKSUnavailable",
    "MalformedToken",
    "TokenExpired",
    "UnknownKid",
    "UserContext",
    "build_www_authenticate",
    "get_current_user",
    "get_jwks_client",
    "jwks_dependency",
    "reset_jwks_client",
    "settings_dependency",
    "verify_token",
]
