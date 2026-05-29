"""FastAPI application factory.

Phase A — exposes /health and wires the foundational middlewares
(request context, Origin allowlist, HTTPS-only). Phase C mounts the
OAuth discovery + DCR endpoints (PRM, AS metadata, /oauth/register).
MCP transport and tool manifests are added in later phases.
"""

from fastapi import FastAPI

from autods_mcp_server import __version__
from autods_mcp_server.logging import configure_logging
from autods_mcp_server.middleware import (
    HttpsOnlyMiddleware,
    OriginAllowlistMiddleware,
    RequestContextMiddleware,
)
from autods_mcp_server.oauth import router as oauth_router
from autods_mcp_server.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    application = FastAPI(
        title="autods-mcp-server",
        version=__version__,
        docs_url="/docs" if settings.is_local else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.is_local else None,
    )

    # Starlette wraps add_middleware calls innermost-first, so the LAST
    # call becomes the outermost. We want RequestContext outermost so the
    # request_id is bound before any security middleware short-circuits.
    application.add_middleware(OriginAllowlistMiddleware, settings=settings)
    application.add_middleware(HttpsOnlyMiddleware, settings=settings)
    application.add_middleware(RequestContextMiddleware)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    application.include_router(oauth_router)

    return application
