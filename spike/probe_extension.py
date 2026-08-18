"""Probe plumbing bolted onto the real server — RD-82 (images) + RD-97 (actions).

Throwaway, and on ``develop`` only because staging deploys from it. **Revert the
whole spike stack once RD-97's measurements are on the ticket.**

Everything here is inert unless ``MCP_ENV=staging``. That gate is the reason no
deploy-repo change is needed (``values-staging.yaml`` already sets it) and why
prod and local behave exactly as before — the production path never registers a
probe tool and never imports Pillow. The hooks are two guarded blocks, in
``mcp_transport._build_server`` and ``app.create_app``.

RD-82 (images, answered — see FINDINGS.md):

* ``ui://autods/probe`` — MCP App resource serving ``widget_probe.html``, with
  ``_meta.ui.csp.resourceDomains`` declaring the supplier CDNs we care about.
* ``probe_widget`` — carries ``_meta.ui.resourceUri`` so the host renders the
  resource; returns ``structuredContent`` rows (URLs only, the zero-token path).
* ``probe_image`` / ``probe_control`` — the Phase 1 image/no-image pair.
* ``GET /probe/img.jpg`` — same-origin image with permissive CORS.

RD-97 (click-to-action, open) lives in ``action_probe`` and is merged in here so
both spikes share one gate, one resource handler, and one in-process dispatch:
``probe_action``, ``probe_action_app_only``, ``probe_action_widget``, the
``ui://autods/action-probe`` resource, and the ``/probe/action/*`` readback
routes.

Both widgets are subject to the six silent protocol bugs recorded in
FINDINGS.md — in particular the ``_meta`` alias trap below, and the rule that
the CSP must be declared on the ``resources/read`` response, not only the list.
"""

import base64
import json
import pathlib
from typing import Any

from fastapi import FastAPI, Request, Response
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents

from spike import action_probe, fixtures

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

SIZES = fixtures.SIZES
_CACHE: dict[tuple[int, int], str] = {}


def _fixture_b64(size: int, index: int) -> str:
    key = (size, index)
    if key not in _CACHE:
        _CACHE[key] = base64.b64encode(fixtures.build(size, index)).decode()
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
            "Each image carries a 4-digit verification code rendered in large digits. "
            "When asked, report the code and the background colour exactly as you see them; do not guess."
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

# RD-97 rides along on the same staging gate and the same in-process dispatch.
PROBE_TOOL_NAMES = frozenset(t.name for t in _PROBE_TOOLS) | action_probe.ACTION_TOOL_NAMES


async def handle_probe_call_async(
    name: str,
    arguments: dict[str, Any],
    *,
    user_sub: str | None,
    autods_user_id: str | None,
) -> types.CallToolResult:
    """Single entry point for the transport. RD-97's handler is async (it writes
    the call record to Redis) and needs the caller identity; RD-82's is neither,
    so the split stays here rather than in ``mcp_transport``."""
    if name in action_probe.ACTION_TOOL_NAMES:
        return await action_probe.handle_action_call(name, arguments, user_sub=user_sub, autods_user_id=autods_user_id)
    return handle_probe_call(name, arguments)


def handle_probe_call(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    """Serve an RD-82 probe tool. Callers must check ``name in PROBE_TOOL_NAMES`` first."""
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
            ),
            action_probe.action_resource(),  # RD-97
        ]

    @server.read_resource()
    async def read_resource(uri: Any) -> list[ReadResourceContents]:
        # RD-97's widget, same contract: mime type set explicitly and the CSP
        # repeated on the read response, because the host renders from the read.
        if str(uri) == action_probe.ACTION_WIDGET_URI:
            return [
                ReadResourceContents(
                    content=action_probe.action_widget_html(),
                    mime_type="text/html;profile=mcp-app",
                    meta=action_probe.action_read_meta(),
                )
            ]
        if str(uri) != WIDGET_URI:
            raise ValueError(f"Unknown resource '{uri}'")
        # Returning a bare str is deprecated AND defaults the mime type to
        # text/plain, which would stop the host treating this as an MCP App.
        # The CSP goes on BOTH the list entry and the read response. The host
        # fetches the resource to render it; if it only reads _meta from the
        # contents, a declaration that lives solely on the list entry is
        # invisible and the sandbox comes back with no approved domains.
        return [
            ReadResourceContents(
                content=_WIDGET_HTML,
                mime_type="text/html;profile=mcp-app",
                meta={"ui": {"csp": {"resourceDomains": _RESOURCE_DOMAINS, "connectDomains": _RESOURCE_DOMAINS}}},
            )
        ]

    return [*tools, *_PROBE_TOOLS, *action_probe.action_tools()]


# Last report the widget POSTed. Redis-backed so it survives the replica the
# GET happens to land on; the in-process copy is the local/no-Redis fallback.
_LAST_REPORT: dict[str, Any] = {}
_REPORT_KEY = "rd82:probe:last_report"


def mount_probe_routes(app: FastAPI) -> None:
    """Same-origin image for widget rows 4 and 5. Unauthenticated by design —
    it serves a generated fixture, never user data, and exists only under the flag."""

    async def _redis():  # noqa: ANN202 - spike helper
        from autods_mcp_server.redis_client import create_redis  # noqa: PLC0415
        from autods_mcp_server.settings import get_settings  # noqa: PLC0415

        return create_redis(get_settings())

    @app.get("/probe/widget.html", include_in_schema=False)
    async def probe_widget_html() -> Response:
        """The exact widget bytes the ui:// resource serves, over plain HTTP, so the
        deployed build can be identified without going through an MCP client."""
        return Response(content=_WIDGET_HTML, media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.post("/probe/report", include_in_schema=False)
    async def probe_report_post(request: Request) -> dict[str, str]:
        """The widget POSTs its own results here, so they can be read back without
        a screenshot. Public and unauthenticated: it holds probe output only."""
        body = await request.body()
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {"parse_error": body.decode("utf-8", "replace")[:2000]}
        _LAST_REPORT.clear()
        _LAST_REPORT.update(payload)
        redis = await _redis()
        if redis is not None:
            try:
                await redis.set(_REPORT_KEY, json.dumps(payload), ex=86400)
            finally:
                await redis.aclose()
        return {"stored": "ok"}

    @app.get("/probe/report", include_in_schema=False)
    async def probe_report_get() -> Response:
        redis = await _redis()
        if redis is not None:
            try:
                raw = await redis.get(_REPORT_KEY)
            finally:
                await redis.aclose()
            if raw:
                return Response(content=raw, media_type="application/json", headers={"Cache-Control": "no-store"})
        return Response(
            content=json.dumps(_LAST_REPORT or {"empty": True}, indent=2),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    action_probe.mount_action_routes(app)  # RD-97 readback + report endpoints.

    @app.get("/probe/img.jpg", include_in_schema=False)
    async def probe_img() -> Response:
        return Response(
            content=fixtures.build(256, 0),
            media_type="image/jpeg",
            headers={
                # The widget's fetch() runs from Claude's per-server iframe origin,
                # sha256(mcp_url)[:32] + ".claudemcpcontent.com". Wildcard here
                # because the probe serves no credentials and no user data.
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
        )
