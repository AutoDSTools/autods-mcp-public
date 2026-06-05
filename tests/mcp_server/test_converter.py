"""D3 acceptance — manifest operation → MCP tool descriptor conversion.

Round-trips representative operations from the products manifest to MCP tool
descriptors and asserts both the generated input schema and the annotation
block, plus the type mapping for the full ``schema_type`` vocabulary.
"""

from pathlib import Path

import pytest

from autods_mcp_server.manifests import build_registry
from autods_mcp_server.manifests.schema import ManifestOperation
from autods_mcp_server.tools import BodySchemaError, build_input_model, build_tools, to_tool


def test_readonly_with_path_params(bundled_manifest_dir: Path) -> None:
    registry = build_registry(bundled_manifest_dir)
    tool = to_tool(registry.get("get_bulk_action_items"))

    assert tool.name == "get_bulk_action_items"
    props = tool.inputSchema["properties"]
    # Both path params are required; the JSON body is optional.
    assert {"store_ids", "bulk_action_id"}.issubset(props)
    assert set(tool.inputSchema["required"]) == {"store_ids", "bulk_action_id"}
    assert tool.annotations.title
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False


def test_post_with_required_body(bundled_manifest_dir: Path) -> None:
    registry = build_registry(bundled_manifest_dir)
    tool = to_tool(registry.get("upload_products"))

    # A required path param plus the required JSON body.
    assert tool.inputSchema["properties"].keys() == {"store_ids", "body"}
    assert set(tool.inputSchema["required"]) == {"store_ids", "body"}
    # The body now carries its typed schema: required integer enums, modelled
    # as integers (never strings) so a string value is invalid by construction.
    body = tool.inputSchema["properties"]["body"]
    assert body["type"] == "object"
    assert set(body["required"]) == {"region", "status", "buy_site_id"}
    assert body["properties"]["status"]["type"] == "integer"
    assert body["properties"]["status"]["enum"] == [1, 2, 3, 4, 5, 6]
    assert tool.annotations.readOnlyHint is False


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

    assert tool.inputSchema["properties"]["body"] == body_schema
    assert "body" in tool.inputSchema["required"]


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
    body = tool.inputSchema["properties"]["body"]

    # Optional open body: not required, with no constraining keys.
    assert "body" not in tool.inputSchema.get("required", [])
    assert "enum" not in body and "required" not in body


def test_list_products_status_is_integer_enum(bundled_manifest_dir: Path) -> None:
    registry = build_registry(bundled_manifest_dir)
    tool = to_tool(registry.get("list_products"))
    status = tool.inputSchema["properties"]["body"]["properties"]["product_status"]

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

    assert tool.annotations.destructiveHint is True
    assert {"thing_id", "store_id"} == set(tool.inputSchema["required"])


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
