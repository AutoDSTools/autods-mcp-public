"""RD-97 — can an MCP Apps widget invoke a tool?

Throwaway, like the RD-82 stack it bolts onto. Active on ``MCP_ENV=staging``
only (see the gate in ``mcp_transport._build_server``); **revert with the
restore commit once the measurements are on the ticket.**

RD-82 proved a widget renders and can talk to the host. It never issued a
``tools/call`` from inside the widget, so click-to-action — the interaction
RD-92's offer picker is designed around — is still an assumption. This module
is the server half of the test; ``widget_action.html`` is the client half.

What it adds:

* ``probe_action`` — performs no real work. Records the call server-side (a
  structured ``rd97_probe_action`` log line **and** a Redis-backed ring buffer
  readable at ``GET /probe/action/calls``) and returns a distinctive marker.
  The ``path`` argument says which mechanism produced the call, so one log
  stream separates the direct and model-mediated paths.
* ``probe_action_app_only`` — identical, but declared
  ``_meta.ui.visibility: ["app"]``. Question 3 is whether claude.ai actually
  hides it from the model; the server always lists it, so anything the model
  cannot see is the *host* filtering.
* ``probe_action_widget`` — carries ``_meta.ui.resourceUri`` so the host
  renders ``ui://autods/action-probe``.

The marker is the instrument for question 4. Each call mints a fresh codeword
that exists nowhere in the conversation until the tool returns it, so asking
the model afterwards "what did that call return?" distinguishes *the result
entered the model's context* from *the model is guessing plausibly*. Ask
without pasting the codeword into the chat, or the measurement is worthless.

Wire formats are per the ext-apps spec (2026-01-26), not guessed:

* app → host tool call: ``{"method": "tools/call",
  "params": {"name": ..., "arguments": {...}}}``
* app → host chat message: ``{"method": "ui/message",
  "params": {"role": "user", "content": {"type": "text", "text": ...}}}``
* tool visibility lives at ``_meta.ui.visibility``, values ``["model"]``,
  ``["app"]`` or both (default both) — **not** a top-level ``visibility`` field.
"""

import json
import pathlib
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request, Response
from mcp import types

from autods_mcp_server.logging import get_logger

logger = get_logger(__name__)

ACTION_WIDGET_URI = "ui://autods/action-probe"
_ACTION_WIDGET_HTML = (pathlib.Path(__file__).parent / "widget_action.html").read_text(encoding="utf-8")

# The widget loads no external assets and needs exactly one connect target: our
# own origin, for the report POST. Deliberately narrower than RD-82's CDN list —
# nothing here is testing image loading, and a short list makes an unexpected
# CSP denial obvious. Declared on BOTH resources/list and resources/read: the
# host renders from the read, and a declaration only on the list comes back as
# an empty sandbox (RD-82 bug 1, which nearly produced the wrong recommendation).
ACTION_CSP_DOMAINS = [
    "https://mcp-staging.autods.com",
    "https://mcp.autods.com",
]
_ACTION_CSP = {"ui": {"csp": {"connectDomains": ACTION_CSP_DOMAINS, "resourceDomains": ACTION_CSP_DOMAINS}}}

# Redis ring buffer so a call recorded by whichever replica served it can be read
# back from any other. Capped and TTL'd — this is probe output, not data.
_CALLS_KEY = "rd97:probe:calls"
_CALLS_MAX = 200
_REPORT_KEY = "rd97:probe:last_report"
_TTL_SECONDS = 86400

# Local fallbacks for the no-Redis path (local runs). Never authoritative on staging.
_LOCAL_CALLS: list[dict[str, Any]] = []
_LOCAL_REPORT: dict[str, Any] = {}

_ACTION_DESCRIPTION = (
    "RD-97 probe. Performs no real work: it records that it was called and returns a one-off marker. "
    "Call it when asked to, passing through the `nonce` and `path` values you were given verbatim. "
    "After calling it, report the `marker` from the result exactly as it came back; never invent one."
)

_ACTION_TOOLS = [
    # NOTE (RD-82 bug 2): the field is ``meta``, its alias is ``_meta``, and these
    # models are ``extra="allow"`` without ``populate_by_name`` — so ``meta=...``
    # silently creates a junk extra field serialised as "meta" and the host never
    # sees the UI metadata. Always construct through the alias.
    types.Tool(
        **{
            "name": "probe_action_widget",
            "description": (
                "RD-97 probe. Renders the click-to-action probe widget. "
                "Use this when asked to test whether a widget can invoke a tool."
            ),
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": types.ToolAnnotations(title="Click-to-action probe widget", readOnlyHint=True),
            "_meta": {"ui": {"resourceUri": ACTION_WIDGET_URI}},
        }
    ),
    types.Tool(
        name="probe_action",
        description=_ACTION_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "nonce": {"type": "string", "description": "Correlation id minted by the caller."},
                "path": {
                    "type": "string",
                    "description": (
                        "Which mechanism produced this call: 'direct' (widget issued tools/call), "
                        "'ui-message' (widget asked via ui/message and the model called it), "
                        "or 'model-direct' (the user asked the model directly)."
                    ),
                },
                "note": {"type": "string", "description": "Free-text label for the run."},
            },
            # Nothing is required on purpose: a validation error would tell us about
            # our schema, not about whether the call mechanism works.
        },
        annotations=types.ToolAnnotations(title="Action probe", readOnlyHint=True),
    ),
    types.Tool(
        **{
            "name": "probe_action_app_only",
            "description": (
                "RD-97 probe, declared app-only. Identical to probe_action. "
                "If you can see this tool, say so and call it when asked — that is itself the measurement."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "nonce": {"type": "string"},
                    "path": {"type": "string"},
                    "note": {"type": "string"},
                },
            },
            "annotations": types.ToolAnnotations(title="Action probe (app-only)", readOnlyHint=True),
            # Question 3. The server lists this unconditionally; if the model cannot
            # see or call it, that is claude.ai honouring the declaration.
            "_meta": {"ui": {"visibility": ["app"]}},
        }
    ),
]

ACTION_TOOL_NAMES = frozenset(t.name for t in _ACTION_TOOLS)


async def _redis():  # noqa: ANN202 - spike helper
    from autods_mcp_server.redis_client import create_redis  # noqa: PLC0415
    from autods_mcp_server.settings import get_settings  # noqa: PLC0415

    return create_redis(get_settings())


async def _redis_op(fn, *, what: str):  # noqa: ANN001, ANN202 - spike helper
    """Run a Redis operation, failing open like the rest of this server.

    Load-bearing for the measurement, not just for uptime: the question RD-97
    asks is whether a widget-issued call *arrives*. If a Redis hiccup raised
    out of the tool handler, the call would arrive and still surface in the
    widget as an error — which reads exactly like "the mechanism doesn't work"
    and would send the spike chasing the wrong answer. The structured log line
    is the real evidence; Redis is only the convenient readback.
    """
    redis = None
    try:
        redis = await _redis()
        if redis is None:
            return None
        return await fn(redis)
    except Exception as exc:  # noqa: BLE001 - deliberate fail-open
        logger.warning("rd97_probe_redis_unavailable", what=what, error=str(exc))
        return None
    finally:
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:  # noqa: BLE001
                pass


async def handle_action_call(
    name: str,
    arguments: dict[str, Any],
    *,
    user_sub: str | None,
    autods_user_id: str | None,
) -> types.CallToolResult:
    """Serve an RD-97 probe tool. Callers must check ``name in ACTION_TOOL_NAMES``."""
    if name == "probe_action_widget":
        return types.CallToolResult(
            content=[types.TextContent(type="text", text='{"ok": true, "widget": "action-probe"}')],
            structuredContent={
                "widget": "action-probe",
                "instructions": (
                    "Press the buttons in the widget. Each one records a call server-side; "
                    "read them back at GET /probe/action/calls."
                ),
            },
        )

    # Minted per call, so it cannot be in the conversation before the result is.
    marker = f"RD97-{secrets.token_hex(3).upper()}"
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "tool": name,
        "marker": marker,
        "nonce": arguments.get("nonce"),
        "path": arguments.get("path"),
        "note": arguments.get("note"),
        "arguments": arguments,
        # Whether a widget-issued call arrives on the user's authenticated session
        # is part of the write-safety story, so record who the server thinks called.
        "user_sub": user_sub,
        "autods_user_id": autods_user_id,
    }

    # Evidence half 1: the structured log line the acceptance criteria ask for.
    logger.info("rd97_probe_action", **record)

    # Evidence half 2: readable without k8s log access, which matters because the
    # thing being measured is whether a call arrives at all.
    _LOCAL_CALLS.insert(0, record)
    del _LOCAL_CALLS[_CALLS_MAX:]

    async def _store(redis):  # noqa: ANN001, ANN202
        await redis.lpush(_CALLS_KEY, json.dumps(record))
        await redis.ltrim(_CALLS_KEY, 0, _CALLS_MAX - 1)
        await redis.expire(_CALLS_KEY, _TTL_SECONDS)

    await _redis_op(_store, what="record-call")

    payload = {
        "ok": True,
        "marker": marker,
        "tool": name,
        "nonce": arguments.get("nonce"),
        "path": arguments.get("path"),
        "recorded": True,
    }
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))],
        structuredContent=payload,
    )


def action_tools() -> list[types.Tool]:
    return list(_ACTION_TOOLS)


def action_resource() -> types.Resource:
    return types.Resource(
        **{
            "uri": ACTION_WIDGET_URI,
            "name": "AutoDS click-to-action probe",
            "mimeType": "text/html;profile=mcp-app",
            # Alias, not field name — see the _PROBE_TOOLS note in probe_extension.
            "_meta": _ACTION_CSP,
        }
    )


def action_widget_html() -> str:
    return _ACTION_WIDGET_HTML


def action_read_meta() -> dict[str, Any]:
    return dict(_ACTION_CSP)


def mount_action_routes(app: FastAPI) -> None:
    """Readback + report endpoints. Unauthenticated by design — they hold probe
    output only (markers and echoed probe arguments), never user data, and exist
    only on staging under the MCP_ENV gate."""

    @app.get("/probe/action/widget.html", include_in_schema=False)
    async def action_widget_over_http() -> Response:
        """The exact bytes the ui:// resource serves, so the deployed build can be
        identified without going through an MCP client."""
        return Response(
            content=_ACTION_WIDGET_HTML,
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/probe/action/calls", include_in_schema=False)
    async def action_calls() -> Response:
        """Every recorded probe_action call, newest first. This is the evidence for
        acceptance criterion 1."""
        raw = await _redis_op(lambda r: r.lrange(_CALLS_KEY, 0, _CALLS_MAX - 1), what="list-calls")
        if raw:
            calls = [json.loads(item) for item in raw]
            return Response(
                content=json.dumps({"count": len(calls), "calls": calls, "source": "redis"}, indent=2),
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )
        return Response(
            content=json.dumps({"count": len(_LOCAL_CALLS), "calls": _LOCAL_CALLS, "source": "in-process"}, indent=2),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/probe/action/report", include_in_schema=False)
    async def action_report_post(request: Request) -> dict[str, str]:
        """The widget POSTs its own transcript here — every request it sent and every
        response the host returned — so a run can be read without a screenshot."""
        body = await request.body()
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {"parse_error": body.decode("utf-8", "replace")[:4000]}
        _LOCAL_REPORT.clear()
        _LOCAL_REPORT.update(payload)
        await _redis_op(lambda r: r.set(_REPORT_KEY, json.dumps(payload), ex=_TTL_SECONDS), what="store-report")
        return {"stored": "ok"}

    @app.get("/probe/action/report", include_in_schema=False)
    async def action_report_get() -> Response:
        raw = await _redis_op(lambda r: r.get(_REPORT_KEY), what="read-report")
        if raw:
            return Response(content=raw, media_type="application/json", headers={"Cache-Control": "no-store"})
        return Response(
            content=json.dumps(_LOCAL_REPORT or {"empty": True}, indent=2),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
