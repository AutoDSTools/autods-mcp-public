"""Debug-friendly entrypoint for the MCP server.

Exists only to work around PyCharm/pydevd: its debugger monkeypatches
``asyncio.run`` with a signature that predates Python 3.12's ``loop_factory``,
so uvicorn's ``Server.run()`` (which passes ``loop_factory=...``) crashes under
the debugger. We sidestep it by awaiting ``server.serve()`` inside a plain
``asyncio.run(main())`` — which the patched runner accepts.

Run/debug this file directly (working directory = repo root, so ``.env`` and
the bundled ``manifests/`` resolve). Equivalent to:
    uvicorn --factory autods_mcp_server.app:create_app --host 127.0.0.1 --port 2049
"""

import asyncio

import uvicorn


async def main() -> None:
    config = uvicorn.Config(
        "autods_mcp_server.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=2049,
        # no reload: the reloader spawns a child process the debugger can't see.
    )
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(main())
