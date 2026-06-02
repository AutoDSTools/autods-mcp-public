"""Operation manifests — typed schema, loader, and registry.

Phase D of the Public MCP epic (RD-54). Manifests describe the upstream
operations this server re-exposes as MCP tools; they are vendored from the
autods-mcp generator and augmented with MCP annotations + upstream routing
keys (see ``schema``).
"""

from autods_mcp_server.manifests.loader import (
    DuplicateOperationError,
    ManifestRegistry,
    build_registry,
    load_manifests,
)
from autods_mcp_server.manifests.schema import (
    Manifest,
    ManifestOperation,
    ManifestParameter,
    SchemaType,
    ToolAnnotations,
)

__all__ = [
    "DuplicateOperationError",
    "Manifest",
    "ManifestOperation",
    "ManifestParameter",
    "ManifestRegistry",
    "SchemaType",
    "ToolAnnotations",
    "build_registry",
    "load_manifests",
]
