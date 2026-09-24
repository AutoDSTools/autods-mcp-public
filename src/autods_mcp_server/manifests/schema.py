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


class BusinessErrors(BaseModel):
    """Declarative mapping of in-payload business rejections to recovery hints.

    Some upstreams report a business rejection as HTTP 200 with an error code
    inside the payload: the envelope's ``ok`` is ``True``, nothing in
    ``errors.map_upstream_error`` fires, and an agent that branches on ``ok``
    concludes the call succeeded. This block tells the transport where to look
    and what to say when it finds something.

    ``paths`` are dotted payload paths into the upstream ``data``, where ``*``
    matches every element of a list / every value of a dict (see
    ``payload_paths``). ``codes`` maps an upstream error code to the recovery
    hint the model should read. Matching produces a ``business_error`` field
    *beside* ``data`` in the envelope — never inside it, so the upstream
    contract stays byte-identical and the dispatcher stays a pure forwarder.
    """

    model_config = ConfigDict(extra="ignore")

    paths: list[str] = Field(default_factory=list)
    codes: dict[str, str] = Field(default_factory=dict)


class LinkBlock(BaseModel):
    """Where a rendered product links to in the AutoDS web app (RD-92).

    A grid the user picks a product from is a dead end without this: they
    choose by eye and then have to find the same product again by title. So
    each item carries a ``link`` beside its image URLs, and the widget renders
    it as an anchor.

    ``route`` names a row of the **closed** route table in ``product_links``,
    never a path. The paths belong to another repository's router and the host
    belongs to the environment, so composing either from manifest text would
    put half a URL in a file that cannot be linted; a name can be, and the boot
    lint rejects one the table does not serve.

    ``when_body_field`` / ``when_body_equals`` gate the link on a field of the
    *request* body, which is needed because one tool can return more than one
    kind of product: ``list_products`` returns drafts or active products
    according to ``product_status``, and the response carries nothing that
    tells them apart. Both must be set together, or neither — a gate with only
    a field name would match every call, which is the opposite of what someone
    writing one intends.

    An operation declares a **list** of these and the first whose gate matches
    wins, the same rule as ``image_paths``. A tool that answers for one product
    type declares one ungated entry; ``list_products`` declares one per status
    it links, and a status with no entry gets no link at all.
    """

    model_config = ConfigDict(extra="ignore")

    route: str
    when_body_field: str = ""
    when_body_equals: int | None = None


class ImagesBlock(BaseModel):
    """Where the product images live inside one operation's response (RD-92).

    Product photos arrive as plain CDN URLs buried in the upstream payload, so
    neither the model nor the user ever sees a picture. This block tells the
    transport how to *address* them — as data, because every operation buries
    them somewhere slightly different — and the transport publishes what it
    finds as an ``images`` field beside ``data`` (never inside it, the same rule
    as ``business_error`` and ``playbook``).

    ``item_path`` and ``image_paths`` are dotted paths into the upstream payload
    resolved by ``payload_paths``, so ``*`` — never ``[]`` — fans out over a
    list's elements or a dict's values. One notation across every manifest
    block was worth more than matching RD-92's illustrative ``images[].url``
    spelling, and ``assert_images_usable`` rejects the bracket form outright so
    the decision cannot quietly rot.

    * ``item_path`` addresses the list of items (``"results"``). Omitted ⇒ the
      root payload is the item, which is what a by-id read returns — as a bare
      object, or as a list, since several AutoDS reads answer the same request
      either way.
    * ``image_paths`` are tried **in order per item, first match wins**: a
      product carries several plausible image fields and they are not equally
      good. ``original_image_url`` is deliberately never listed — it is the
      un-edited original and would misrepresent what is actually listed.
    * ``per_item`` × ``max`` is bounded at 20 by a boot lint, because 20 × 252 px
      is the measured base64 ceiling below the point where the client truncates
      silently (RD-82: a run claimed "19 of 20" while holding 16, and described
      a half-decoded JPEG as a different product).
    * ``label_path`` / ``id_path`` are per-item paths for the caption and the id
      a widget cell needs. Both optional; a cell with no label renders the
      image alone.
    * ``widget`` names the ``ui://`` widget this operation's result should
      render in, and must be one the widget registry serves. Omitted ⇒ the URLs
      are published in ``structuredContent`` and nothing is rendered.
    * ``links`` give each item a URL into the AutoDS web app, so a rendered
      grid is something the user can act on rather than only look at. Tried in
      order, first matching gate wins. Empty ⇒ no item carries a link, which is
      also what happens when no gate matches or the item has no id.
    * ``base64`` decides whether the synthetic ``include_images`` parameter is
      offered at all, and what it defaults to. Three values because the
      surfaces genuinely need three: ``"opt_in"`` (offered, default off) is the
      normal case; ``"off"`` withholds the parameter entirely, which is where
      ``list_products`` sits until the scraper bucket has a thumbnail source —
      that is the one surface where the missing small variant actually bites,
      because a full-size original is what would get encoded; and
      ``"default_on"`` is for the one shape where the *model* must judge the
      picture rather than the user choosing from a set (an image-search offer
      list, RD-95), which is the only thing that justifies paying vision
      tokens.
    """

    model_config = ConfigDict(extra="ignore")

    item_path: str = ""
    image_paths: list[str] = Field(default_factory=list)
    per_item: int = 1
    max: int = 20
    label_path: str = ""
    id_path: str = ""
    widget: str | None = None
    base64: Literal["off", "opt_in", "default_on"] = "opt_in"
    links: list[LinkBlock] = Field(default_factory=list)


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
    # Empty for a locally-handled operation, which never builds an upstream
    # request. The lint in ``tools.py`` requires both on a forwarding operation.
    method: str = ""
    path: str = ""
    summary: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    parameters: list[ManifestParameter] = Field(default_factory=list)
    # RD-95: query values sent on every call and never offered to the model.
    # For an upstream that serves several stores behind one endpoint, where this
    # tool is only ever about one of them (``store=offers_1688``): a parameter
    # the model must fill with a constant is a parameter it can fill wrong. It
    # cannot live in ``path`` either, because httpx *replaces* a URL's query
    # string when ``params`` is passed. A boot lint forbids a key that is also a
    # declared parameter, so neither side can silently override the other.
    fixed_query: dict[str, str] = Field(default_factory=dict)
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
    # Optional declarative "the upstream said 200 but rejected the request"
    # config. ``None`` means the operation has no in-payload business errors to
    # look for, which is the case for every operation whose failures arrive as
    # a non-2xx status.
    business_errors: BusinessErrors | None = None
    # RD-92: where this operation's response carries product images, and which
    # widget (if any) renders them. ``None`` — the common case — means the
    # operation has no image surface: no ``images`` envelope field, no
    # ``_meta.ui`` on the descriptor, and no ``include_images`` parameter.
    images: ImagesBlock | None = None
    # Whether the operation is side-effect-free is advertised to clients via
    # ``annotations.read_only_hint`` (the MCP-canonical signal), so the
    # generator's separate ``safe`` flag is intentionally not modelled here —
    # it would only duplicate the hint and risk drifting out of sync with it.
    annotations: ToolAnnotations = Field(default_factory=ToolAnnotations)
    # Which upstream serves this op. ``None`` means "inherit the manifest-level
    # default"; the registry resolves it to a concrete value at load time.
    # Left ``None`` (and *not* filled in from the manifest default) when
    # ``handler`` is set — such an operation has no upstream.
    base_url_key: str | None = None
    # RD-100: the local handler that serves this operation instead of the
    # upstream dispatcher. ``None`` — the overwhelmingly common case — means the
    # operation is forwarded. The set of valid values is a closed registry in
    # ``mcp_transport``, and a boot lint requires exactly one of ``handler`` /
    # ``base_url_key`` per operation: a "local" operation that also names an
    # upstream, or a forwarded one that names neither, is a packaging error.
    handler: str | None = None


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
