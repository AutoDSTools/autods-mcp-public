"""Manifest operation -> MCP tool descriptor conversion (D3) and the D5 lint.

Each manifest operation becomes one MCP ``Tool``. We build a pydantic v2 model
describing the tool's inputs — one field per path/query/header parameter, plus a
free-form ``body`` object when the operation carries a JSON body — and hand its
``model_json_schema()`` to the SDK as the tool ``inputSchema``. The annotation
block from the manifest is emitted verbatim on every descriptor.

When an operation carries a ``body_schema``, that JSON Schema is emitted verbatim
as the ``body`` field's schema. Otherwise ``body`` is modelled as an open object
(matching the autods-mcp TS runtime's ``z.record(z.any())``) — the generator only
records *that* a body exists, not its shape, so un-modelled bodies stay open.
"""

from typing import Any

from mcp import types
from pydantic import BaseModel, Field, create_model

from autods_mcp_server.manifests.schema import ManifestOperation, SchemaType

# autods-mcp ``schema_type`` -> Python type used for the generated pydantic field.
# Mirrors the mapping in the autods-mcp TS runtime (int/float/bool/list/dict/str).
_SCHEMA_TYPE_TO_PY: dict[SchemaType, Any] = {
    "int": int,
    "float": float,
    "bool": bool,
    "list": list[Any],
    "dict": dict[str, Any],
    "str": str,
}

# The MCP tool name grammar (``^[a-zA-Z0-9_-]{1,128}$``); operation_ids that
# violate it would be silently warned-and-kept by the SDK, so we surface it.
_MAX_TOOL_NAME_LENGTH = 128

# Body fields whose AutoDS values are integer enums (1=draft, 2=active, …). A
# ``body_schema`` that types one of these as a string reintroduces the exact
# string-vs-integer bug this carrier exists to prevent, so the boot lint rejects
# it. Matched by property name anywhere in the body schema.
_INTEGER_ENUM_BODY_FIELDS = frozenset(
    {"product_status", "status", "region", "site_id", "buy_site_id", "inventory_status"}
)


class ToolAnnotationError(ValueError):
    """A registered operation is missing a required MCP annotation (D5)."""


class BodySchemaError(ValueError):
    """An operation's ``body_schema`` types a known integer-enum field as a string."""


def build_input_model(operation: ManifestOperation) -> type[BaseModel]:
    """Build a pydantic model describing one operation's tool input.

    Required parameters become required fields; optional ones default to
    ``None``. A JSON body (when present) is an open ``body`` object, required iff
    the operation marks it required.
    """
    fields: dict[str, Any] = {}
    for parameter in operation.parameters:
        py_type = _SCHEMA_TYPE_TO_PY.get(parameter.schema_type, str)
        if parameter.required:
            fields[parameter.name] = (py_type, Field(description=parameter.description or None))
        else:
            fields[parameter.name] = (py_type | None, Field(default=None, description=parameter.description or None))

    if operation.has_json_body:
        body_type: Any = dict[str, Any]
        if operation.request_body_required:
            fields["body"] = (body_type, Field(description="JSON request body."))
        else:
            fields["body"] = (body_type | None, Field(default=None, description="JSON request body."))

    # A stable, unique model name keeps pydantic's schema ``title`` readable.
    return create_model(f"{operation.operation_id}_Input", **fields)


def _build_description(operation: ManifestOperation) -> str:
    """Compose a human/LLM-facing description from the manifest text fields.

    ``notes`` carry the most actionable guidance the generator produced (enum
    meanings, body shape, side effects), so they're appended when present.
    """
    parts = [operation.summary.strip(), operation.description.strip()]
    if operation.notes:
        parts.append(operation.notes.strip())
    description = " ".join(part for part in parts if part)
    return description or operation.operation_id


def _build_input_schema(operation: ManifestOperation) -> dict[str, Any]:
    """The tool ``inputSchema``: the param model's JSON schema, with the
    ``body`` property replaced by ``operation.body_schema`` when present.

    The pydantic model already places ``body`` in ``required`` iff the body is
    required, so swapping only the property's subschema preserves required-ness:
    a required body must now match the schema; an optional one must match *when
    present*.
    """
    schema = build_input_model(operation).model_json_schema()
    if operation.has_json_body and operation.body_schema is not None:
        schema.setdefault("properties", {})["body"] = dict(operation.body_schema)
    return schema


def to_tool(operation: ManifestOperation) -> types.Tool:
    """Convert a manifest operation into an MCP ``Tool`` descriptor."""
    annotations = operation.annotations
    return types.Tool(
        name=operation.operation_id,
        description=_build_description(operation),
        inputSchema=_build_input_schema(operation),
        annotations=types.ToolAnnotations(
            title=annotations.title,
            readOnlyHint=annotations.read_only_hint,
            destructiveHint=annotations.destructive_hint,
        ),
    )


def assert_valid_annotations(operations: list[ManifestOperation]) -> None:
    """D5 startup lint: every operation needs a title and at least one hint.

    Raises:
        ToolAnnotationError: if any operation lacks a ``title``, or lacks *both*
            ``readOnlyHint`` and ``destructiveHint``. Raised at boot so a
            mis-annotated manifest can never reach a client.
    """
    for operation in operations:
        annotations = operation.annotations
        if not annotations.title:
            raise ToolAnnotationError(f"Operation '{operation.operation_id}' is missing annotation 'title'.")
        if annotations.read_only_hint is None and annotations.destructive_hint is None:
            raise ToolAnnotationError(
                f"Operation '{operation.operation_id}' must set 'readOnlyHint' or 'destructiveHint'."
            )
        if len(operation.operation_id) > _MAX_TOOL_NAME_LENGTH:
            raise ToolAnnotationError(
                f"Operation '{operation.operation_id}' exceeds the {_MAX_TOOL_NAME_LENGTH}-char MCP tool-name limit."
            )


def _assert_integer_enum_fields(operation: ManifestOperation) -> None:
    """Reject a ``body_schema`` that types a known integer-enum field as a string.

    Walks the schema recursively, so an enum field nested inside ``properties``
    of an object or the ``items`` of an array is checked too. Raised at boot so a
    string-typed ``product_status`` can never reach a client.
    """
    if operation.body_schema is None:
        return

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        for name, subschema in (node.get("properties") or {}).items():
            if name in _INTEGER_ENUM_BODY_FIELDS and isinstance(subschema, dict) and subschema.get("type") == "string":
                raise BodySchemaError(
                    f"Operation '{operation.operation_id}' body_schema types enum field "
                    f"'{name}' as 'string'; AutoDS enum fields take integer values."
                )
        for child in (node.get("properties") or {}).values():
            walk(child)
        walk(node.get("items"))

    walk(operation.body_schema)


def build_tools(operations: list[ManifestOperation]) -> list[types.Tool]:
    """Lint, then convert every operation to an MCP tool descriptor."""
    assert_valid_annotations(operations)
    for operation in operations:
        _assert_integer_enum_fields(operation)
    return [to_tool(operation) for operation in operations]
