"""Typed exceptions raised by the auth layer.

Each concrete subclass maps to one failure mode in ``verify_token`` so
callers (the FastAPI dependency, logging, future tooling) can branch on
type rather than parse strings.
"""


class AuthError(Exception):
    """Base class for all auth failures."""


class MalformedToken(AuthError):
    """Token is not a well-formed JWT (header parse failed, missing kid, etc.)."""


class UnknownKid(AuthError):
    """Token's ``kid`` is not present in the JWKS even after a forced refresh."""


class InvalidSignature(AuthError):
    """JWT signature did not verify against the JWK matched by ``kid``."""


class TokenExpired(AuthError):
    """``exp`` claim is in the past."""


class InvalidAudience(AuthError):
    """``client_id`` is not in ``settings.allowed_cognito_client_ids``.

    Cognito access tokens carry the issuing client in ``client_id`` (not
    ``aud``); the exception name is kept for continuity with the OAuth
    audience concept.
    """


class InvalidIssuer(AuthError):
    """``iss`` does not match the configured Cognito user pool."""


class JWKSUnavailable(AuthError):
    """JWKS cannot be fetched or parsed from Cognito."""
