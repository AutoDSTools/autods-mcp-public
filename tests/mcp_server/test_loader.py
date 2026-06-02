"""D2 acceptance — manifest loader + registry.

Parses the vendored products manifest and exercises the registry surface
(``list_operations`` returning typed models, lookup, base_url_key resolution,
duplicate detection, empty-dir → empty registry).
"""

import json
from pathlib import Path
from typing import Any

import pytest

from autods_mcp_server.manifests import (
    DuplicateOperationError,
    ManifestOperation,
    build_registry,
    load_manifests,
)


def _operation(operation_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "operation_id": operation_id,
        "method": "GET",
        "path": f"/{operation_id}",
        "annotations": {"title": operation_id, "readOnlyHint": True},
    }
    base.update(overrides)
    return base


def test_loads_vendored_products_manifest(bundled_manifest_dir: Path) -> None:
    registry = build_registry(bundled_manifest_dir)

    operations = registry.list_operations()
    assert len(operations) == 5
    assert all(isinstance(op, ManifestOperation) for op in operations)

    op = registry.get("upload_products")
    assert op is not None
    assert op.method == "POST"
    assert op.has_json_body is True
    assert op.request_body_required is True


def test_base_url_key_inherits_manifest_default(write_manifest, tmp_path: Path) -> None:
    directory = write_manifest(
        {
            "server_name": "demo",
            "base_url_key": "products_research",
            "operations": [
                _operation("inherits"),  # no per-op key
                _operation("overrides", base_url_key="autods_api"),
            ],
        }
    )
    registry = build_registry(directory)

    assert registry.get("inherits").base_url_key == "products_research"
    assert registry.get("overrides").base_url_key == "autods_api"


def test_missing_directory_is_empty_registry(tmp_path: Path) -> None:
    registry = build_registry(tmp_path / "does-not-exist")
    assert len(registry) == 0
    assert registry.list_operations() == []


def test_empty_directory_is_empty_registry(empty_manifest_dir: Path) -> None:
    assert len(build_registry(empty_manifest_dir)) == 0


def test_duplicate_operation_id_across_manifests_raises(tmp_path: Path) -> None:
    directory = tmp_path / "manifests"
    directory.mkdir()
    for name in ("a.json", "b.json"):
        manifest = {"server_name": name, "operations": [_operation("dup")]}
        (directory / name).write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DuplicateOperationError):
        build_registry(directory)


def test_load_manifests_is_deterministically_ordered(write_manifest) -> None:
    directory = write_manifest(
        {
            "server_name": "demo",
            "operations": [_operation("first"), _operation("second"), _operation("third")],
        }
    )
    manifests = load_manifests(directory)
    assert [op.operation_id for op in manifests[0].operations] == ["first", "second", "third"]
