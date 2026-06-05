"""Typed models for the operation manifests.

The on-disk manifest format mirrors
``autods-mcp/generated/servers/<server>/operations.json`` — the generator in
that repo is the source of truth for the shape of an operation. Phase D extends
it with two fields the public server needs and the generated file does not carry:

* ``annotations`` — the MCP tool-annotation block (``title``, ``readOnlyHint``,
  ``destructiveHint``). The MCP spec asks every tool descriptor to advertise
  these so clients can render/safelist tools sensibly; D5 refuses to boot if any
  operation is missing them.
* ``base_url_key`` — which upstream service serves the operation. The dispatcher
  resolves it to a concrete base URL from ``Settings`` (D6), so one running
  server can route different operations to AutoDSApi vs ProductsResearch.

The committed files under ``manifests/`` are maintained by hand — new
operations are added manually with both fields populated. The format still
mirrors the autods-mcp ``operations.json`` shape, so anything we don't model is
dropped (``extra="ignore"``); leftover generator bookkeeping fields
(``is_top_typed_tool``, ``generated_at``, …) the runtime has no use for are
simply ignored.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# The parameter ``schema_type`` vocabulary the autods-mcp generator emits. Kept
# here as the single source of truth shared by the converter's type mapping.
SchemaType = Literal["int", "float", "bool", "list", "dict", "str"]


class ToolAnnotations(BaseModel):
    """MCP tool annotations (the subset Phase D populates).

    Field aliases match the camelCase keys both the manifest JSON and the MCP
    wire format use; ``populate_by_name`` lets internal code use snake_case.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    title: str | None = None
    read_only_hint: bool | None = Field(default=None, alias="readOnlyHint")
    destructive_hint: bool | None = Field(default=None, alias="destructiveHint")


class ManifestParameter(BaseModel):
    """A single path/query/header parameter of an operation."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str
    location: Literal["path", "query", "header"] = Field(alias="in")
    required: bool = False
    schema_type: SchemaType = "str"
    description: str | None = None


class ManifestOperation(BaseModel):
    """One upstream operation, exposed as one MCP tool."""

    model_config = ConfigDict(extra="ignore")

    operation_id: str
    method: str
    path: str
    summary: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    parameters: list[ManifestParameter] = Field(default_factory=list)
    has_json_body: bool = False
    request_body_required: bool = False
    # Optional JSON Schema describing the request body. When set, the converter
    # emits it verbatim as the ``body`` field's schema in the tool ``inputSchema``
    # (instead of an open object), so clients/models see field names, types,
    # integer ``enum`` constraints and required fields — and the SDK validates
    # the body against it before the call reaches us. Omitted for operations
    # whose body shape isn't modelled yet; those keep the open-object behaviour.
    body_schema: dict[str, Any] | None = None
    notes: str | None = None
    # Whether the operation is side-effect-free is advertised to clients via
    # ``annotations.read_only_hint`` (the MCP-canonical signal), so the
    # generator's separate ``safe`` flag is intentionally not modelled here —
    # it would only duplicate the hint and risk drifting out of sync with it.
    annotations: ToolAnnotations = Field(default_factory=ToolAnnotations)
    # Which upstream serves this op. ``None`` means "inherit the manifest-level
    # default"; the registry resolves it to a concrete value at load time.
    base_url_key: str | None = None


class Manifest(BaseModel):
    """A whole manifest file — one upstream domain's worth of operations."""

    model_config = ConfigDict(extra="ignore")

    server_name: str
    domain: str = ""
    instructions: str = ""
    # Manifest-level fallback applied to operations that don't set their own
    # ``base_url_key``. Defaults to AutoDSApi, the historical single upstream.
    base_url_key: str = "autods_api"
    operations: list[ManifestOperation] = Field(default_factory=list)
