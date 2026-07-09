"""FastAPI router for the OAuth discovery + DCR endpoints.

Mounted by ``app.create_app`` so it sits behind the Origin allowlist
middleware and the HTTPS guard like every other route. The three endpoints are
deliberately small — all the policy lives in ``metadata`` and
``registration``; this module is just HTTP plumbing.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from autods_mcp_server.auth.dependency import settings_dependency
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
    RegistrationError,
    register_client,
)
from autods_mcp_server.settings import Settings
from autods_mcp_server.urls import effective_base_url

router = APIRouter(tags=["oauth"])


@router.get(PRM_PATH, response_model=ProtectedResourceMetadata)
async def protected_resource_metadata(
    request: Request,
    settings: Annotated[Settings, Depends(settings_dependency)],
) -> ProtectedResourceMetadata:
    base = effective_base_url(request, settings)
    return build_prm(base_url=base, scopes=settings.mcp_oauth_scopes)


@router.get(AS_METADATA_PATH, response_model=AuthorizationServerMetadata)
async def authorization_server_metadata(
    request: Request,
    settings: Annotated[Settings, Depends(settings_dependency)],
) -> AuthorizationServerMetadata:
    # COGNITO_DOMAIN is required in every environment, so the Cognito Hosted
    # UI endpoints always resolve — no placeholder/503 path to guard here.
    base = effective_base_url(request, settings)
    return build_as_metadata(
        base_url=base,
        authorization_endpoint=settings.cognito_authorization_endpoint,
        token_endpoint=settings.cognito_token_endpoint,
        jwks_uri=settings.cognito_jwks_url,
        scopes=settings.mcp_oauth_scopes,
    )


@router.post(
    DCR_PATH,
    response_model=ClientRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    payload: ClientRegistrationRequest,
    settings: Annotated[Settings, Depends(settings_dependency)],
) -> ClientRegistrationResponse:
    try:
        return register_client(payload, settings=settings)
    except RegistrationError as exc:
        # RFC 7591 §3.2.2 says the registration error response is a 400
        # with an ``error`` + ``error_description`` body.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": exc.error, "error_description": exc.description},
        ) from exc
