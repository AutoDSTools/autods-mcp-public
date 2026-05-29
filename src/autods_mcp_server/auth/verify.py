"""Cognito JWT verification.

The reference implementation in ``AutoDSApi/helper/auth.py`` decoded
tokens with ``options={"verify_exp": False, "verify_aud": False}`` and
re-implemented the ``exp`` / ``aud`` checks by hand. This module closes
the ``exp`` / ``iss`` part of that gap by delegating to PyJWT.

We deliberately do **not** use PyJWT's ``verify_aud``: Cognito access
tokens (what MCP clients send to this resource server) don't carry an
``aud`` claim — they carry ``client_id`` instead. We validate that
claim manually below.
"""

import json

import jwt as pyjwt
from jwt.algorithms import AllowedRSAKeys, JWKDict, RSAAlgorithm
from pydantic import BaseModel, ConfigDict, Field

from autods_mcp_server.auth.exceptions import (
    InvalidAudience,
    InvalidIssuer,
    InvalidSignature,
    MalformedToken,
    TokenExpired,
)
from autods_mcp_server.auth.jwks import JWKSClient
from autods_mcp_server.settings import Settings


class Claims(BaseModel):
    """Parsed JWT claims with the Cognito-access-token fields we care about."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    sub: str
    iss: str
    client_id: str
    exp: int
    iat: int | None = None
    token_use: str | None = None
    # Cognito access tokens don't carry `email` by default — only ID tokens do.
    # This stays None unless the user pool client is configured to surface
    # email into access tokens via custom scopes.
    email: str | None = None
    groups: list[str] = Field(default_factory=list, alias="cognito:groups")


def _load_public_key(jwk: JWKDict) -> AllowedRSAKeys:
    # ``RSAAlgorithm.from_jwk`` accepts a JSON string. PyJWK is the
    # newer API but ``from_jwk`` is the one that's stable across PyJWT
    # 2.x minor versions, so it's safer for the lock file we ship.
    return RSAAlgorithm.from_jwk(json.dumps(jwk))


async def verify_token(token: str, *, jwks: JWKSClient, settings: Settings) -> Claims:
    """Verify a Cognito-issued access token and return its parsed claims.

    Raises:
        MalformedToken: header parse failed or ``kid`` missing.
        UnknownKid: ``kid`` not in JWKS after a forced refresh.
        InvalidSignature: signature did not match the JWK.
        TokenExpired: ``exp`` is in the past.
        InvalidAudience: ``client_id`` missing or not in
            ``allowed_cognito_client_ids``.
        InvalidIssuer: ``iss`` does not match the configured pool.
    """
    try:
        header = pyjwt.get_unverified_header(token)
    except pyjwt.PyJWTError as exc:
        raise MalformedToken(f"Could not parse JWT header: {exc}") from exc

    kid = header.get("kid")
    if not kid:
        raise MalformedToken("JWT header is missing 'kid'")

    jwk = await jwks.get_key(kid)
    public_key = _load_public_key(jwk)

    try:
        raw_claims = pyjwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],
            issuer=settings.cognito_issuer,
            options={
                "verify_signature": True,
                "verify_exp": True,
                # Cognito access tokens have no `aud`; we check
                # `client_id` ourselves below.
                "verify_aud": False,
                "verify_iss": True,
                "require": ["exp", "iss"],
            },
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise TokenExpired(str(exc)) from exc
    except pyjwt.InvalidIssuerError as exc:
        raise InvalidIssuer(str(exc)) from exc
    except pyjwt.InvalidSignatureError as exc:
        raise InvalidSignature(str(exc)) from exc
    except (
        pyjwt.MissingRequiredClaimError,
        pyjwt.InvalidAlgorithmError,
        pyjwt.DecodeError,
    ) as exc:
        # Structurally invalid (missing required claim, disallowed alg,
        # undecodable body) — these are not signature failures and shouldn't
        # be reported as such in logs/metrics.
        raise MalformedToken(str(exc)) from exc
    except pyjwt.PyJWTError as exc:
        raise InvalidSignature(f"JWT validation failed: {exc}") from exc

    # Defense-in-depth: reject non-access tokens (ID, refresh-shaped) explicitly
    # instead of relying on the absence of client_id to filter them out.
    token_use = raw_claims.get("token_use")
    if token_use != "access":
        raise InvalidAudience(f"token_use={token_use!r}, expected 'access'")

    client_id = raw_claims.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise InvalidAudience("Missing 'client_id' claim (not a Cognito access token?)")
    if client_id not in settings.allowed_cognito_client_ids:
        raise InvalidAudience(f"client_id {client_id!r} is not in allowed_cognito_client_ids")

    return Claims.model_validate(raw_claims)
