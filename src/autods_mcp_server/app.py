"""FastAPI application factory.

Phase A — exposes /health and wires the foundational middlewares
(request context, Origin allowlist); HTTPS is enforced by a settings
validator plus a request-level X-Forwarded-Proto guard. Phase C mounts the
OAuth discovery + DCR endpoints (PRM, AS metadata, /oauth/register).
Phase D mounts the MCP Streamable HTTP transport at /mcp (behind the
Phase B auth dependency) and the runtime that serves manifest-defined
tools.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from autods_mcp_server import __version__
from autods_mcp_server.logging import configure_logging, resolve_level
from autods_mcp_server.mcp_transport import build_runtime, mcp_lifespan, mount_mcp
from autods_mcp_server.middleware import OriginAllowlistMiddleware, RequestContextMiddleware
from autods_mcp_server.oauth import router as oauth_router
from autods_mcp_server.sentry import init_sentry
from autods_mcp_server.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)
    init_sentry(settings)

    # Build the MCP runtime up front: loading manifests runs the D5 annotation
    # lint, so a mis-annotated manifest fails create_app() — i.e. boot — rather
    # than surfacing on the first tool call.
    runtime = build_runtime(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp_lifespan(runtime):
            yield

    application = FastAPI(
        title="autods-mcp-server",
        version=__version__,
        docs_url="/docs" if settings.is_local else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.is_local else None,
        lifespan=lifespan,
    )

    # Starlette wraps add_middleware calls innermost-first, so the LAST
    # call becomes the outermost. We want RequestContext outermost so the
    # request_id is bound before any security middleware short-circuits.
    # /health is hit on every load-balancer/liveness probe; its access log is
    # noise. Suppress it unless explicitly debugging (LOG_LEVEL=debug).
    quiet_paths = () if resolve_level(settings) <= logging.DEBUG else ("/health",)
    application.add_middleware(OriginAllowlistMiddleware, settings=settings)
    application.add_middleware(RequestContextMiddleware, quiet_paths=quiet_paths)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    application.include_router(oauth_router)
    mount_mcp(application, runtime)

    return application
