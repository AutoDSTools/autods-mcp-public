"""Manifest loader and operation registry.

``load_manifests`` reads every ``*.json`` file in a directory into typed
:class:`Manifest` models. ``ManifestRegistry`` flattens them into a flat,
``operation_id``-keyed index and resolves each operation's effective
``base_url_key`` (operation-level override falling back to the manifest-level
default). The registry is the single lookup surface the tool converter (D3) and
the dispatcher (D4/D6) both read from.

A missing or empty directory yields an empty registry (zero tools) rather than
an error — that's the Phase D default before any manifests are wired in.
"""

from pathlib import Path

from autods_mcp_server.manifests.schema import Manifest, ManifestOperation


class DuplicateOperationError(ValueError):
    """Two manifests declare the same ``operation_id``.

    MCP tool names must be unique across the whole server, so a collision is a
    packaging error we fail loudly on rather than silently shadowing one op.
    """


def load_manifests(directory: Path | str) -> list[Manifest]:
    """Parse every ``*.json`` manifest in ``directory`` (non-recursive).

    Files are read in sorted filename order so the registry's operation
    ordering — and therefore the advertised tool list — is deterministic.
    """
    path = Path(directory)
    if not path.is_dir():
        return []
    manifests: list[Manifest] = []
    for file in sorted(path.glob("*.json")):
        manifests.append(Manifest.model_validate_json(file.read_text(encoding="utf-8")))
    return manifests


class ManifestRegistry:
    """Flat, ``operation_id``-keyed view over a set of manifests."""

    def __init__(self, manifests: list[Manifest]) -> None:
        self._operations: dict[str, ManifestOperation] = {}
        for manifest in manifests:
            for operation in manifest.operations:
                if operation.operation_id in self._operations:
                    raise DuplicateOperationError(
                        f"Duplicate operation_id '{operation.operation_id}' across manifests."
                    )
                # Resolve the effective upstream now so every consumer reads a
                # concrete key and never has to know about manifest-level
                # inheritance.
                operation.base_url_key = operation.base_url_key or manifest.base_url_key
                self._operations[operation.operation_id] = operation

    def list_operations(self) -> list[ManifestOperation]:
        """All operations, in deterministic (load) order."""
        return list(self._operations.values())

    def get(self, operation_id: str) -> ManifestOperation | None:
        return self._operations.get(operation_id)

    def __len__(self) -> int:
        return len(self._operations)


def build_registry(directory: Path | str) -> ManifestRegistry:
    """Convenience: load a directory and wrap it in a registry."""
    return ManifestRegistry(load_manifests(directory))
