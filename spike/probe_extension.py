"""RD-82 Phase 1.5/2 — probe plumbing bolted onto the real server.

Throwaway. Lives only on ``spike/RD-82-image-probe``; never merge it. Everything
here is inert unless ``MCP_IMAGE_PROBE=true``, and it is deliberately kept in
``spike/`` so the production diff is three guarded lines (see PATCH below).

What it adds when enabled:

* ``ui://autods/probe`` — the MCP App resource serving ``widget_probe.html``,
  with ``_meta.ui.csp.resourceDomains`` declaring the supplier CDNs we care
  about. Whether claude.ai honours that declaration is exactly Q7; the widget
  reports back the host's own ``sandbox.csp`` either way.
* ``probe_widget`` — a tool carrying ``_meta.ui.resourceUri`` so the host knows
  to render the resource, returning ``structuredContent`` with real product
  rows (URLs only, no bytes — the zero-token path).
* ``probe_image`` / ``probe_control`` — the Phase 1 tools, so claude.ai web can
  be measured on the same fixtures as the local stdio run.
* ``GET /probe/img.jpg`` — a same-origin image with permissive CORS, so the
  widget can test both ``<img src>`` and ``fetch``→blob against our own origin.

PATCH (apply on the spike branch only):

``mcp_transport.py`` — at the end of ``_build_server``, before ``return server``::

    if settings.image_probe_enabled:                       # RD-82 spike
        from spike.probe_extension import register_probe   # noqa: PLC0415
        tools = register_probe(server, tools)

``app.py`` — after ``mount_mcp(application, runtime)``::

    if settings.image_probe_enabled:                       # RD-82 spike
        from spike.probe_extension import mount_probe_routes  # noqa: PLC0415
        mount_probe_routes(application)

``settings.py`` — one field::

    image_probe_enabled: bool = Field(default=False, validation_alias="MCP_IMAGE_PROBE")
"""

import base64
import io
import pathlib
from typing import Any

from fastapi import FastAPI, Response
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from PIL import Image, ImageDraw

WIDGET_URI = "ui://autods/probe"
_WIDGET_HTML = (pathlib.Path(__file__).parent / "widget_probe.html").read_text(encoding="utf-8")

# Declared for the host to approve, deny, or ignore — the point of the probe.
# Wildcards are supported by the spec, which matters for TikTok's p16/p19 shards.
_RESOURCE_DOMAINS = [
    "https://m.media-amazon.com",
    "https://cdn.shopify.com",
    "https://*.ttcdn-us.com",
    "https://ae01.alicdn.com",
    "https://i.ebayimg.com",
    "https://static.wixstatic.com",
    "https://autods-scraper-images.s3-us-west-2.amazonaws.com",
    "https://mcp-staging.autods.com",
]

SIZES = (252, 384, 1200)
_PALETTE = [(220, 70, 70), (60, 140, 220), (80, 180, 110), (230, 170, 50), (150, 90, 200)]
_CACHE: dict[tuple[int, int], str] = {}


def _fixture_bytes(size: int, index: int) -> bytes:
    img = Image.new("RGB", (size, size), _PALETTE[index % len(_PALETTE)])
    draw = ImageDraw.Draw(img)
    draw.rectangle([size * 0.1, size * 0.1, size * 0.9, size * 0.9], outline=(255, 255, 255), width=max(2, size // 40))
    for i in range(index + 1):
        y = size * 0.75 + i * (size * 0.03)
        draw.rectangle([size * 0.2, y, size * 0.8, y + size * 0.015], fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _fixture_b64(size: int, index: int) -> str:
    key = (size, index)
    if key not in _CACHE:
        _CACHE[key] = base64.b64encode(_fixture_bytes(size, index)).decode()
    return _CACHE[key]


# Real rows so the widget renders something representative. URLs only: this is
# the zero-vision-token path, which is the whole argument for the widget.
_SAMPLE_ROWS = [
    {
        "title": "Sample — Amazon CDN",
        "price": "19.99",
        "image_url": "https://m.media-amazon.com/images/I/31PPBbEggBL._SS256_.jpg",
    },
    {
        "title": "Sample — Shopify CDN",
        "price": "24.50",
        "image_url": (
            "https://cdn.shopify.com/s/files/1/0751/4016/9797/files/"
            "da9f68513ac117e50e574c8221a08aff.jpg?v=1785908965&width=256"
        ),
    },
    {
        "title": "Sample — AutoDS S3 (no thumbnail available)",
        "price": "12.00",
        "image_url": "https://autods-scraper-images.s3-us-west-2.amazonaws.com/1866c96ca0131d2a317412579f385395.png",
    },
]

_PROBE_TOOLS = [
    # NOTE: the field is ``meta`` but its alias is ``_meta``, and these models are
    # ``extra="allow"`` without ``populate_by_name``. Passing ``meta=...`` therefore
    # SILENTLY creates a junk extra field that serialises as "meta" — the host never
    # sees the UI metadata and nothing errors. Always construct via the alias.
    types.Tool(
        **{
            "name": "probe_widget",
            "description": (
                "RD-82 probe. Renders the CSP probe widget and returns sample product rows as structuredContent."
            ),
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": types.ToolAnnotations(title="CSP probe widget", readOnlyHint=True),
            "_meta": {"ui": {"resourceUri": WIDGET_URI}},
        }
    ),
    types.Tool(
        name="probe_image",
        description=(
            "RD-82 probe. Returns `count` synthetic JPEGs at `size` px plus a text envelope. "
            "Each image carries (index+1) white bars so you can verify the model actually saw pixels."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "size": {"type": "integer", "enum": list(SIZES)},
                "count": {"type": "integer", "minimum": 1, "maximum": 25},
                "audience": {"type": "string", "enum": ["both", "user"]},
            },
            "required": ["size", "count"],
        },
        annotations=types.ToolAnnotations(title="Image probe", readOnlyHint=True),
    ),
    types.Tool(
        name="probe_control",
        description="RD-82 baseline. Same envelope, zero images. Use to measure the token delta.",
        inputSchema={"type": "object", "properties": {}},
        annotations=types.ToolAnnotations(title="Image probe (control)", readOnlyHint=True),
    ),
]

PROBE_TOOL_NAMES = frozenset(t.name for t in _PROBE_TOOLS)


def handle_probe_call(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    """Serve a probe tool. Callers must check ``name in PROBE_TOOL_NAMES`` first."""
    if name == "probe_control":
        return types.CallToolResult(content=[types.TextContent(type="text", text='{"ok": true, "images": 0}')])

    if name == "probe_widget":
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f'{{"ok": true, "rows": {len(_SAMPLE_ROWS)}}}')],
            structuredContent={"products": _SAMPLE_ROWS},
        )

    size = int(arguments.get("size", 252))
    count = int(arguments.get("count", 1))
    ann = types.Annotations(audience=["user"]) if arguments.get("audience") == "user" else None
    content: list[Any] = [types.TextContent(type="text", text=f'{{"ok": true, "images": {count}, "size": {size}}}')]
    content.extend(
        types.ImageContent(type="image", data=_fixture_b64(size, i), mimeType="image/jpeg", annotations=ann)
        for i in range(count)
    )
    return types.CallToolResult(content=content)


def register_probe(server: Server, tools: list[types.Tool]) -> list[types.Tool]:
    """Attach the ``ui://`` resource handlers and return the extended tool list."""

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return [
            types.Resource(
                **{
                    "uri": WIDGET_URI,
                    "name": "AutoDS CSP probe",
                    "mimeType": "text/html;profile=mcp-app",
                    # Alias, not field name — see the note on _PROBE_TOOLS above.
                    "_meta": {
                        "ui": {"csp": {"resourceDomains": _RESOURCE_DOMAINS, "connectDomains": _RESOURCE_DOMAINS}}
                    },
                }
            )
        ]

    @server.read_resource()
    async def read_resource(uri: Any) -> list[ReadResourceContents]:
        if str(uri) != WIDGET_URI:
            raise ValueError(f"Unknown resource '{uri}'")
        # Returning a bare str is deprecated AND defaults the mime type to
        # text/plain, which would stop the host treating this as an MCP App.
        return [ReadResourceContents(content=_WIDGET_HTML, mime_type="text/html;profile=mcp-app")]

    return [*tools, *_PROBE_TOOLS]


def mount_probe_routes(app: FastAPI) -> None:
    """Same-origin image for widget rows 4 and 5. Unauthenticated by design —
    it serves a generated fixture, never user data, and exists only under the flag."""

    @app.get("/probe/img.jpg", include_in_schema=False)
    async def probe_img() -> Response:
        return Response(
            content=_fixture_bytes(256, 0),
            media_type="image/jpeg",
            headers={
                # The widget's fetch() runs from Claude's per-server iframe origin,
                # sha256(mcp_url)[:32] + ".claudemcpcontent.com". Wildcard here
                # because the probe serves no credentials and no user data.
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
        )
