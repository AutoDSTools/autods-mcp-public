"""RD-82 Phase 1 — throwaway stdio MCP server that probes image rendering.

NOT part of the app. Never imported by ``create_app``; belongs on the
``spike/RD-82-image-probe`` branch only and must never be merged.

Run it with ``uv run`` — NOT ``uvx``. ``uvx --with mcp`` resolves the newest
mcp (2.0.0), whose low-level ``Server`` dropped the ``list_tools`` decorator,
so the module fails at import with ``'Server' object has no attribute
'list_tools'``; it also picks Python 3.13 for a 3.12-only project. ``uv run``
uses the project's locked mcp 1.27.2.

    uv run --with pillow python spike/probe_server.py

    # Claude Code
    claude mcp add image-probe -- uv run \\
        --directory /home/sergey/AutoDSTools/autods-mcp-public --with pillow \\
        python /home/sergey/AutoDSTools/autods-mcp-public/spike/probe_server.py

    # Claude Desktop — merge into ~/.config/Claude/claude_desktop_config.json
    # (Linux; the file already exists and holds "preferences" — keep them).
    # Quit Claude Desktop first: it rewrites this file on exit and will
    # otherwise clobber the edit.
    {"mcpServers": {"image-probe": {"command": "uv", "args": [
        "run", "--directory", "/home/sergey/AutoDSTools/autods-mcp-public",
        "--with", "pillow", "python",
        "/home/sergey/AutoDSTools/autods-mcp-public/spike/probe_server.py"]}}}

Fixtures come from ``spike/fixtures.py``: each carries a 4-digit code that
appears ONLY in the pixels — not in the tool description, not in the envelope.
Ask the model to read the code back; a correct answer is the only proof the
image reached it. ``python spike/fixtures.py`` prints the expected values.
"""

import base64
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

try:  # importable both as `spike.probe_server` and as a bare script
    from spike import fixtures
except ImportError:  # pragma: no cover
    import fixtures

SIZES = fixtures.SIZES


_CACHE: dict[tuple[int, int, bool], str] = {}


def _b64(size: int, index: int, noisy: bool = False) -> str:
    key = (size, index, noisy)
    if key not in _CACHE:
        _CACHE[key] = base64.b64encode(fixtures.build(size, index, noisy)).decode()
    return _CACHE[key]


server: Server = Server("autods-image-probe")

_TOOLS = [
    types.Tool(
        name="probe_image",
        description=(
            "RD-82 probe. Returns `count` synthetic JPEG images at `size` px plus a text envelope. "
            "Each image contains a 4-digit verification code rendered in large digits. "
            "When asked, report the code and the background colour exactly as you see them; do not guess."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "size": {"type": "integer", "enum": list(SIZES), "description": "Square edge in px."},
                "count": {"type": "integer", "minimum": 1, "maximum": 25},
                "audience": {
                    "type": "string",
                    "enum": ["both", "user"],
                    "description": "'user' sets annotations.audience=['user'] on every image block (Q4).",
                },
                "variant": {
                    "type": "string",
                    "enum": ["flat", "noise"],
                    "description": (
                        "'noise' keeps the same pixel dimensions but ~17x the bytes, "
                        "separating a byte-based result ceiling from a token-based one (Q5)."
                    ),
                },
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


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return _TOOLS


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    if name == "probe_control":
        return types.CallToolResult(content=[types.TextContent(type="text", text='{"ok": true, "images": 0}')])

    size = int(arguments.get("size", 252))
    count = int(arguments.get("count", 1))
    audience = arguments.get("audience", "both")
    noisy = arguments.get("variant") == "noise"
    variant = "noise" if noisy else "flat"
    ann = types.Annotations(audience=["user"]) if audience == "user" else None

    content: list[Any] = [
        types.TextContent(
            type="text",
            text=f'{{"ok": true, "images": {count}, "size": {size}, "audience": "{audience}", "variant": "{variant}"}}',
        )
    ]
    for i in range(count):
        content.append(
            types.ImageContent(type="image", data=_b64(size, i, noisy), mimeType="image/jpeg", annotations=ann)
        )
    return types.CallToolResult(content=content)


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import anyio

    anyio.run(main)
