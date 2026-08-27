"""D3 acceptance — manifest operation → MCP tool descriptor conversion.

Round-trips representative operations from the products manifest to MCP tool
descriptors and asserts both the generated input schema and the annotation
block, plus the type mapping for the full ``schema_type`` vocabulary.
"""

from pathlib import Path
from typing import Any

import pytest

from autods_mcp_server.manifests import build_registry
from autods_mcp_server.manifests.schema import ManifestOperation
from autods_mcp_server.tools import (
    BodySchemaError,
    BusinessErrorsError,
    build_input_model,
    build_tools,
    to_tool,
)


def test_readonly_with_path_params(bundled_manifest_dir: Path) -> None:
    registry = build_registry(bundled_manifest_dir)
    tool = to_tool(registry.get("get_bulk_action_items"))

    assert tool.name == "get_bulk_action_items"
    props = tool.input_schema["properties"]
    # Both path params are required; the JSON body is optional.
    assert {"store_ids", "bulk_action_id"}.issubset(props)
    assert set(tool.input_schema["required"]) == {"store_ids", "bulk_action_id"}
    assert tool.annotations.title
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False


def test_post_with_required_body(bundled_manifest_dir: Path) -> None:
    registry = build_registry(bundled_manifest_dir)
    tool = to_tool(registry.get("upload_products"))

    # A required path param plus the required JSON body.
    assert tool.input_schema["properties"].keys() == {"store_ids", "body"}
    assert set(tool.input_schema["required"]) == {"store_ids", "body"}
    # The body now carries its typed schema: required integer enums, modelled
    # as integers (never strings) so a string value is invalid by construction.
    body = tool.input_schema["properties"]["body"]
    assert body["type"] == "object"
    assert set(body["required"]) == {"region", "status", "buy_site_id"}
    assert body["properties"]["status"]["type"] == "integer"
    assert body["properties"]["status"]["enum"] == [1, 2, 3, 4, 5, 6]
    assert tool.annotations.read_only_hint is False


def test_body_schema_is_emitted_verbatim() -> None:
    """When an operation declares a body_schema, the tool's body property is
    exactly that schema, and ``body`` stays required per request_body_required."""
    body_schema = {
        "type": "object",
        "properties": {"product_status": {"type": "integer", "enum": [1, 2]}},
        "required": ["product_status"],
    }
    operation = ManifestOperation.model_validate(
        {
            "operation_id": "schema_op",
            "method": "POST",
            "path": "/x",
            "has_json_body": True,
            "request_body_required": True,
            "body_schema": body_schema,
            "annotations": {"title": "Schema Op", "readOnlyHint": True},
        }
    )
    tool = to_tool(operation)

    assert tool.input_schema["properties"]["body"] == body_schema
    assert "body" in tool.input_schema["required"]


def test_body_stays_open_without_body_schema() -> None:
    """Regression: an operation with a JSON body but no body_schema keeps the
    open-object body (unchanged pre-RD-58 behaviour)."""
    operation = ManifestOperation.model_validate(
        {
            "operation_id": "open_body_op",
            "method": "POST",
            "path": "/x",
            "has_json_body": True,
            "request_body_required": False,
            "annotations": {"title": "Open", "readOnlyHint": True},
        }
    )
    tool = to_tool(operation)
    body = tool.input_schema["properties"]["body"]

    # Optional open body: not required, with no constraining keys.
    assert "body" not in tool.input_schema.get("required", [])
    assert "enum" not in body and "required" not in body


def test_list_products_status_is_integer_enum(bundled_manifest_dir: Path) -> None:
    registry = build_registry(bundled_manifest_dir)
    tool = to_tool(registry.get("list_products"))
    status = tool.input_schema["properties"]["body"]["properties"]["product_status"]

    assert status["type"] == "integer"
    assert status["enum"] == [1, 2, 3, 4, 5, 6]


def test_build_tools_rejects_string_typed_enum_field() -> None:
    """The boot lint refuses a body_schema that types a known enum field as a
    string — the exact string-vs-integer drift the carrier exists to prevent."""
    operation = ManifestOperation.model_validate(
        {
            "operation_id": "bad_enum_op",
            "method": "POST",
            "path": "/x",
            "base_url_key": "autods_api",
            "has_json_body": True,
            "request_body_required": True,
            "body_schema": {
                "type": "object",
                "properties": {"product_status": {"type": "string"}},
            },
            "annotations": {"title": "Bad", "readOnlyHint": False, "destructiveHint": False},
        }
    )
    with pytest.raises(BodySchemaError, match="product_status"):
        build_tools([operation])


def _business_errors_op(**overrides: Any) -> ManifestOperation:
    payload: dict[str, Any] = {
        "operation_id": "scan_offer",
        "method": "POST",
        "path": "/scan",
        "notes": "`ok` is a transport-level signal only: a rejected scan still answers 200.",
        "business_errors": {"paths": ["scraper_error.errorCode"], "codes": {"PRODUCT_OOS": "hint"}},
        "annotations": {"title": "Scan Offer", "readOnlyHint": True},
        # Set explicitly: these operations bypass the registry (which resolves
        # the manifest-level default), and ``build_tools`` requires exactly one
        # of ``base_url_key`` / ``handler`` (RD-100).
        "base_url_key": "autods_api",
    }
    payload.update(overrides)
    return ManifestOperation.model_validate(payload)


def test_business_errors_block_with_notes_and_paths_passes_the_lint() -> None:
    assert build_tools([_business_errors_op()])


def test_build_tools_rejects_business_errors_without_paths() -> None:
    """A block with no ``paths`` can never match — ``detect_business_errors``
    returns early — so it reads as protection while doing nothing."""
    operation = _business_errors_op(business_errors={"codes": {"PRODUCT_OOS": "hint"}})

    with pytest.raises(BusinessErrorsError, match="no 'paths'"):
        build_tools([operation])


def test_build_tools_rejects_business_errors_the_notes_never_mention() -> None:
    """RD-90's own lesson, applied to its own feature: the block populates a
    ``business_error`` field, and the only place the model is told not to read
    ``ok`` as success is the tool it is holding. Silent if forgotten."""
    operation = _business_errors_op(notes="Read-only POST. Returns the scan result.")

    with pytest.raises(BusinessErrorsError, match="never mention 'ok'"):
        build_tools([operation])


def test_the_ok_warning_is_not_satisfied_by_a_substring() -> None:
    """``ok`` must appear as a word — "token"/"look" must not pass the guard."""
    operation = _business_errors_op(notes="Forwards the caller's token; look at the result.")

    with pytest.raises(BusinessErrorsError, match="never mention 'ok'"):
        build_tools([operation])


def test_operations_without_the_block_are_untouched_by_the_lint() -> None:
    operation = _business_errors_op(business_errors=None, notes="Read-only POST.")

    assert build_tools([operation])


def test_delete_is_destructive() -> None:
    # The products manifest no longer carries a destructive operation, so this
    # round-trips a synthetic DELETE to keep the destructiveHint conversion covered.
    operation = ManifestOperation.model_validate(
        {
            "operation_id": "delete_thing",
            "method": "DELETE",
            "path": "/things/{store_id}/{thing_id}",
            "parameters": [
                {"name": "store_id", "in": "path", "required": True, "schema_type": "str"},
                {"name": "thing_id", "in": "path", "required": True, "schema_type": "str"},
            ],
            "annotations": {"title": "Delete Thing", "readOnlyHint": False, "destructiveHint": True},
        }
    )
    tool = to_tool(operation)

    assert tool.annotations.destructive_hint is True
    assert {"thing_id", "store_id"} == set(tool.input_schema["required"])


def test_schema_type_mapping_covers_all_types() -> None:
    operation = ManifestOperation.model_validate(
        {
            "operation_id": "typed_op",
            "method": "POST",
            "path": "/typed/{p_str}",
            "parameters": [
                {"name": "p_str", "in": "path", "required": True, "schema_type": "str"},
                {"name": "q_int", "in": "query", "required": False, "schema_type": "int"},
                {"name": "q_float", "in": "query", "required": False, "schema_type": "float"},
                {"name": "q_bool", "in": "query", "required": False, "schema_type": "bool"},
                {"name": "q_list", "in": "query", "required": False, "schema_type": "list"},
                {"name": "q_dict", "in": "query", "required": False, "schema_type": "dict"},
            ],
            "has_json_body": True,
            "request_body_required": False,
            "annotations": {"title": "Typed", "readOnlyHint": False, "destructiveHint": False},
        }
    )
    schema = build_input_model(operation).model_json_schema()
    props = schema["properties"]

    assert schema["required"] == ["p_str"]
    assert props["p_str"]["type"] == "string"
    # Optional fields are nullable (anyOf [<type>, null]); assert the type appears.
    assert any(sub.get("type") == "integer" for sub in props["q_int"]["anyOf"])
    assert any(sub.get("type") == "number" for sub in props["q_float"]["anyOf"])
    assert any(sub.get("type") == "boolean" for sub in props["q_bool"]["anyOf"])
    assert any(sub.get("type") == "array" for sub in props["q_list"]["anyOf"])
    assert any(sub.get("type") == "object" for sub in props["q_dict"]["anyOf"])
    # Optional body present but not required.
    assert "body" in props
    assert "body" not in schema.get("required", [])
