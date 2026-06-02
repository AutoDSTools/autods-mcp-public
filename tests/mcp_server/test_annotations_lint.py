"""D5 acceptance — startup annotation lint.

The lint must refuse a manifest whose operation lacks a ``title`` or lacks both
hint flags, and accept one that satisfies the rule. ``build_runtime`` surfaces
the same error so a mis-annotated manifest fails boot.
"""

import pytest

from autods_mcp_server.manifests.schema import ManifestOperation
from autods_mcp_server.mcp_transport import build_runtime
from autods_mcp_server.tools import ToolAnnotationError, assert_valid_annotations


def _op(operation_id: str, annotations: dict) -> ManifestOperation:
    return ManifestOperation.model_validate(
        {"operation_id": operation_id, "method": "GET", "path": "/x", "annotations": annotations}
    )


def test_passes_with_title_and_a_hint() -> None:
    assert_valid_annotations([_op("ok", {"title": "OK", "readOnlyHint": True})])


def test_missing_title_raises() -> None:
    with pytest.raises(ToolAnnotationError, match="title"):
        assert_valid_annotations([_op("no_title", {"readOnlyHint": True})])


def test_missing_both_hints_raises() -> None:
    with pytest.raises(ToolAnnotationError, match="readOnlyHint"):
        assert_valid_annotations([_op("no_hints", {"title": "T"})])


def test_destructive_hint_alone_is_sufficient() -> None:
    assert_valid_annotations([_op("del", {"title": "Del", "destructiveHint": True})])


def test_build_runtime_refuses_mis_annotated_manifest(mcp_settings, write_manifest) -> None:
    directory = write_manifest(
        {
            "server_name": "bad",
            "operations": [
                {"operation_id": "untitled", "method": "GET", "path": "/x", "annotations": {"readOnlyHint": True}}
            ],
        }
    )
    settings = mcp_settings(manifest_dir=directory)

    with pytest.raises(ToolAnnotationError):
        build_runtime(settings)
