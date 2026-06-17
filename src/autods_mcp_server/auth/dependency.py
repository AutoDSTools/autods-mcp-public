"""FastAPI auth dependency.

Extracts ``Authorization: Bearer ...``, calls ``verify_token``, and
returns a ``UserContext`` to the route handler. Every failure mode
short-circuits with HTTP 401 + a spec-compliant ``WWW-Authenticate``
challenge so MCP clients can discover the protected-resource metadata
document and start the OAuth flow.

The PRM URL host is pinned to ``settings.public_hostname`` whenever
it is configured (i.e. always in staging/prod). That keeps a hostile
``Host`` / ``X-Forwarded-Host`` value from steering MCP clients to
attacker-controlled metadata. Local dev (where ``public_hostname`` may
be unset) falls back to request-derived host. The PRM endpoint itself
lands in Phase C / C2; until then the URL is a stable placeholder.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr

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
from autods_mcp_server.auth.jwks import JWKSClient, get_jwks_client
from autods_mcp_server.auth.verify import verify_token

# ``PRM_PATH`` is re-exported here (auth/__init__ surfaces it); its
# canonical definition lives in ``urls`` so the 401 challenge URL and the
# served route can't drift apart.
from autods_mcp_server.settings import Settings, get_settings
from autods_mcp_server.urls import PRM_PATH, effective_base_url


class UserContext(BaseModel):
    """The slice of token claims downstream handlers actually need."""

    sub: str
    email: str | None = None
    groups: list[str] = Field(default_factory=list)
    raw_token: SecretStr
    # Resolved server-side from AutoDSApi (RD-63/RD-68); ``None`` when unresolved
    # (lookup failed, account not resolvable, or the resolver is disabled).
    autods_user_id: str | None = None


# Error codes follow RFC 6750 §3.1.
_ERROR_INVALID_REQUEST = "invalid_request"
_ERROR_INVALID_TOKEN = "invalid_token"


def build_www_authenticate(
    resource_metadata_url: str,
    *,
    error: str | None = None,
    error_description: str | None = None,
) -> str:
    """Build the ``WWW-Authenticate: Bearer ...`` challenge value."""

    def _sanitize(value: str) -> str:
        # Strip CR/LF/quotes so neither a hostile claim payload (description)
        # nor a hostile Host header (URL, in the local-dev fallback path) can
        # smuggle header injection through a quoted-string value.
        return value.replace('"', "'").replace("\r", " ").replace("\n", " ")

    parts = [f'Bearer resource_metadata="{_sanitize(resource_metadata_url)}"']
    if error:
        parts.append(f'error="{_sanitize(error)}"')
    if error_description:
        parts.append(f'error_description="{_sanitize(error_description)}"')
    return ", ".join(parts)


def _build_unauthorized_exception(
    request: Request,
    settings: Settings,
    error: str,
    description: str,
) -> HTTPException:
    prm_url = f"{effective_base_url(request, settings)}{PRM_PATH}"
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": error, "error_description": description},
        headers={
            "WWW-Authenticate": build_www_authenticate(
                prm_url,
                error=error,
                error_description=description,
            ),
        },
    )


def _build_service_unavailable_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "service_unavailable", "error_description": "Error accessing AWS Cognito."},
    )


def _extract_bearer(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    return parts[1]


def settings_dependency() -> Settings:
    """FastAPI dep that resolves to the current Settings singleton.

    Public so tests (and future routes) can target it via
    ``app.dependency_overrides``.
    """
    return get_settings()


def jwks_dependency(
    settings: Annotated[Settings, Depends(settings_dependency)],
) -> JWKSClient:
    """FastAPI dep that resolves to the JWKS client for the current settings."""
    return get_jwks_client(settings)


async def get_current_user(
    request: Request,
    settings: Annotated[Settings, Depends(settings_dependency)],
    jwks: Annotated[JWKSClient, Depends(jwks_dependency)],
) -> UserContext:
    token = _extract_bearer(request)
    if token is None:
        raise _build_unauthorized_exception(
            request=request,
            settings=settings,
            error=_ERROR_INVALID_REQUEST,
            description="Missing or malformed Authorization header (expected 'Bearer <token>').",
        )

    try:
        claims = await verify_token(token, jwks=jwks, settings=settings)
    except TokenExpired as exc:
        raise _build_unauthorized_exception(
            request=request,
            settings=settings,
            error=_ERROR_INVALID_TOKEN,
            description="Token expired.",
        ) from exc
    except InvalidAudience as exc:
        raise _build_unauthorized_exception(
            request=request,
            settings=settings,
            error=_ERROR_INVALID_TOKEN,
            description="Token client_id is not allowed.",
        ) from exc
    except InvalidIssuer as exc:
        raise _build_unauthorized_exception(
            request=request,
            settings=settings,
            error=_ERROR_INVALID_TOKEN,
            description="Token issuer is not allowed.",
        ) from exc
    except (InvalidSignature, UnknownKid, MalformedToken) as exc:
        raise _build_unauthorized_exception(
            request=request,
            settings=settings,
            error=_ERROR_INVALID_TOKEN,
            description="Token could not be validated.",
        ) from exc
    except JWKSUnavailable as exc:
        raise _build_service_unavailable_exception() from exc
    except AuthError as exc:
        raise _build_unauthorized_exception(
            request=request,
            settings=settings,
            error=_ERROR_INVALID_TOKEN,
            description="Token validation failed.",
        ) from exc

    # The verified context. The identity resolver needs the forwarded token — it
    # resolves the caller's own AutoDS identity by calling AutoDSApi's
    # ``get_current_user`` on the caller's behalf (no privileged credentials).
    user_context = UserContext(
        sub=claims.sub,
        email=claims.email,
        groups=list(claims.groups),
        raw_token=SecretStr(token),
    )

    # Resolve the stable AutoDS identity (autods_user_id + email) from AutoDSApi,
    # cached (RD-63/RD-68). Fails open: a lookup failure leaves autods_user_id
    # None and never blocks auth. The resolver is stashed on app.state by
    # mount_mcp; absent (e.g. bare auth-only test apps) → identity unresolved.
    resolver = getattr(request.app.state, "identity_resolver", None)
    identity = await resolver.resolve(user_context) if resolver is not None else None
    if identity is not None:
        user_context.autods_user_id = identity.user_id
        # The cached email (when present) supersedes any email claim.
        if identity.email:
            user_context.email = identity.email

    # Surface the authenticated identity to RequestContextMiddleware, which runs
    # upstream of auth and reads it back off request.state (the shared ASGI
    # scope) after the response is produced, to tag the access log line.
    request.state.cognito_username = claims.sub
    request.state.autods_user_id = user_context.autods_user_id
    request.state.email = user_context.email

    return user_context
