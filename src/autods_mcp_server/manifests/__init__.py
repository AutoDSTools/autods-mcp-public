"""Operation manifests — typed schema, loader, and registry.

Phase D of the Public MCP epic (RD-54). Manifests describe the upstream
operations this server re-exposes as MCP tools; they are vendored from the
autods-mcp generator and augmented with MCP annotations + upstream routing
keys (see ``schema``).
"""

from autods_mcp_server.manifests.instructions import (
    INSTRUCTIONS_HARD_LIMIT,
    INSTRUCTIONS_TARGET,
    InstructionsTooLargeError,
    assert_instructions_within_limit,
    build_instructions,
)
from autods_mcp_server.manifests.loader import (
    DuplicateOperationError,
    ManifestRegistry,
    build_registry,
    load_manifests,
)
from autods_mcp_server.manifests.playbooks import (
    DuplicatePlaybookError,
    Playbook,
    PlaybookError,
    PlaybookRegistry,
    PlaybookStep,
    assert_playbooks_valid,
    build_playbook_index,
    build_playbook_registry,
    load_playbooks,
)
from autods_mcp_server.manifests.schema import (
    BusinessErrors,
    Manifest,
    ManifestOperation,
    ManifestParameter,
    SchemaType,
    ToolAnnotations,
)

__all__ = [
    "INSTRUCTIONS_HARD_LIMIT",
    "INSTRUCTIONS_TARGET",
    "BusinessErrors",
    "DuplicateOperationError",
    "DuplicatePlaybookError",
    "InstructionsTooLargeError",
    "Manifest",
    "ManifestOperation",
    "ManifestParameter",
    "ManifestRegistry",
    "Playbook",
    "PlaybookError",
    "PlaybookRegistry",
    "PlaybookStep",
    "SchemaType",
    "ToolAnnotations",
    "assert_instructions_within_limit",
    "assert_playbooks_valid",
    "build_instructions",
    "build_playbook_index",
    "build_playbook_registry",
    "build_registry",
    "load_manifests",
    "load_playbooks",
]
