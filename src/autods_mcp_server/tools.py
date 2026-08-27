"""Manifest operation -> MCP tool descriptor conversion (D3) and the boot lints.

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

import re
from typing import Any

from mcp import types
from pydantic import BaseModel, Field, create_model

from autods_mcp_server.manifests.playbooks import (
    HANDLER_PLAYBOOK,
    PlaybookRegistry,
    render_description_tail,
)
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

# A standalone mention of ``ok`` in an operation's ``notes`` — backticked, bare
# or capitalised. Used as the (deliberately loose) proxy for "this operation
# warns the model that ``ok`` is transport-level only"; no lint can check that
# the sentence around it says the right thing, but it does catch the block being
# added with the warning forgotten entirely.
_OK_MENTION = re.compile(r"\bok\b", re.IGNORECASE)


class ToolAnnotationError(ValueError):
    """A registered operation is missing a required MCP annotation (D5)."""


class BodySchemaError(ValueError):
    """An operation's ``body_schema`` types a known integer-enum field as a string."""


class BusinessErrorsError(ValueError):
    """An operation's ``business_errors`` block can never fire, or is undocumented."""


class OperationHandlerError(ValueError):
    """An operation names both a local handler and an upstream, or neither."""


# The parameter a ``handler: "playbook"`` operation takes. Its ``enum`` is the
# registered playbook names, injected at boot — which is what makes the enum the
# index: ``inputSchema`` is the most reliably delivered channel there is, so the
# list of playbooks reaches the model even in a client that drops ``instructions``
# entirely.
_PLAYBOOK_NAME_PARAM = "name"


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


def _build_description(operation: ManifestOperation, playbooks: PlaybookRegistry | None = None) -> str:
    """Compose a human/LLM-facing description from the manifest text fields.

    ``notes`` carry the most actionable guidance the generator produced (enum
    meanings, body shape, side effects), so they're appended when present.

    When the operation is a step of a playbook (RD-100), a single bounded line
    is appended pointing at ``get_playbook``. Bounded on purpose: this text
    rides in the tool definitions on every turn, many turns before the situation
    it describes arises, so it is a pointer and never a step body. The chain
    itself is delivered lazily by ``get_playbook``, and the per-step nudge rides
    on the call result, where it lands exactly when it is relevant.
    """
    parts = [operation.summary.strip(), operation.description.strip()]
    if operation.notes:
        parts.append(operation.notes.strip())
    if playbooks is not None:
        tail = render_description_tail(playbooks.steps_for(operation.operation_id))
        if tail:
            parts.append(tail)
    description = " ".join(part for part in parts if part)
    return description or operation.operation_id


def _build_input_schema(operation: ManifestOperation, playbooks: PlaybookRegistry | None = None) -> dict[str, Any]:
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
    if operation.handler == HANDLER_PLAYBOOK and playbooks is not None:
        # The registered names are runtime data, so they can't be authored in
        # the manifest; injecting them here is what keeps the enum and the
        # committed playbook files from drifting apart.
        name_schema = schema.setdefault("properties", {}).get(_PLAYBOOK_NAME_PARAM)
        if isinstance(name_schema, dict):
            name_schema["enum"] = playbooks.names()
    return schema


def to_tool(operation: ManifestOperation, playbooks: PlaybookRegistry | None = None) -> types.Tool:
    """Convert a manifest operation into an MCP ``Tool`` descriptor."""
    annotations = operation.annotations
    return types.Tool(
        name=operation.operation_id,
        description=_build_description(operation, playbooks),
        input_schema=_build_input_schema(operation, playbooks),
        annotations=types.ToolAnnotations(
            title=annotations.title,
            read_only_hint=annotations.read_only_hint,
            destructive_hint=annotations.destructive_hint,
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


def _assert_business_errors_usable(operation: ManifestOperation) -> None:
    """Reject a ``business_errors`` block that can't fire, or that ships silently.

    Two failure modes, both of which look fine in review and produce no runtime
    signal whatsoever — the same shape as the bug RD-90 itself fixed:

    * **No ``paths``.** ``detect_business_errors`` returns ``None`` without a
      path to look at, so the block is dead config that reads as protection.
    * **``notes`` that never mention ``ok``.** The whole point of the block is
      that an agent branching on ``ok`` must not read a 2xx as success. That
      warning belongs on the tool the agent is actually holding (tier 2); left
      out, the block populates a field nothing told the model to read.

    Raises:
        BusinessErrorsError: on either, at boot, like the other manifest lints.
    """
    config = operation.business_errors
    if config is None:
        return
    if not config.paths:
        raise BusinessErrorsError(
            f"Operation '{operation.operation_id}' declares 'business_errors' with no 'paths'; "
            f"the block can never match and is dead config."
        )
    if not _OK_MENTION.search(operation.notes or ""):
        raise BusinessErrorsError(
            f"Operation '{operation.operation_id}' declares 'business_errors' but its 'notes' never "
            f"mention 'ok'; an operation that can reject a request inside a 2xx must say so on the "
            f"tool itself, so the model knows not to read 'ok' as success."
        )


def _assert_handler_or_upstream(operation: ManifestOperation, playbooks: PlaybookRegistry | None) -> None:
    """Exactly one of ``handler`` / ``base_url_key``, and a served handler at that.

    Both halves fail silently otherwise. An operation with neither reaches the
    dispatcher and blows up resolving an empty upstream key on the first call; an
    operation with both looks routable and is not; a ``handler`` value nothing
    serves would be dispatched upstream to a path that doesn't exist. The
    ``handler: "playbook"`` case additionally needs at least one registered
    playbook: with none, the tool's ``name`` enum is empty, so every call fails
    validation and the tool is a dead end that still costs a tool definition.

    Raises:
        OperationHandlerError: at boot, like the other manifest lints.
    """
    handler = operation.handler
    if handler is None:
        if not operation.base_url_key:
            raise OperationHandlerError(
                f"Operation '{operation.operation_id}' declares neither 'handler' nor 'base_url_key'; "
                f"an operation is either served locally or forwarded to an upstream."
            )
        if not operation.method or not operation.path:
            raise OperationHandlerError(
                f"Operation '{operation.operation_id}' is forwarded upstream but declares no 'method'/'path'."
            )
        return
    if operation.base_url_key:
        raise OperationHandlerError(
            f"Operation '{operation.operation_id}' declares both handler '{handler}' and base_url_key "
            f"'{operation.base_url_key}'; exactly one of the two is served."
        )
    if handler != HANDLER_PLAYBOOK:
        raise OperationHandlerError(
            f"Operation '{operation.operation_id}' names unknown handler '{handler}'; the local-handler "
            f"registry is closed."
        )
    if playbooks is not None and not len(playbooks):
        raise OperationHandlerError(
            f"Operation '{operation.operation_id}' serves playbooks, but none are registered; its 'name' "
            f"enum would be empty and every call would fail validation."
        )


def build_tools(operations: list[ManifestOperation], playbooks: PlaybookRegistry | None = None) -> list[types.Tool]:
    """Lint, then convert every operation to an MCP tool descriptor."""
    assert_valid_annotations(operations)
    for operation in operations:
        _assert_integer_enum_fields(operation)
        _assert_business_errors_usable(operation)
        _assert_handler_or_upstream(operation, playbooks)
    return [to_tool(operation, playbooks) for operation in operations]
