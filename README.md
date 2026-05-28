# autods-mcp-server

Public MCP (Model Context Protocol) server for AutoDS. Exposes selected
AutoDSApi and ProductsResearch tooling to MCP-compatible clients
(Claude, Cursor, etc.).

This repo is the foundation for the Public MCP epic
([RD-50](https://autods.atlassian.net/browse/RD-50)). Phase A (this
ticket — [RD-51](https://autods.atlassian.net/browse/RD-51)) sets up
the repo skeleton, the FastAPI app, structured logging, origin /
HTTPS-only middlewares and the local Docker workflow. MCP/OAuth
runtime lands in later phases.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/) (dependency / virtualenv manager)
- Docker + docker-compose (for the local container workflow)

## Local development

```bash
uv sync
uv run uvicorn autods_mcp_server.app:app --reload
curl http://localhost:8000/health   # {"status":"ok"}
```

### Via Docker

```bash
docker compose up
curl http://localhost:8000/health
```

Source is bind-mounted into the container, so edits hot-reload.

## Configuration

All settings come from environment variables. See
`src/autods_mcp_server/settings.py` for the full schema. The key knob
is `MCP_ENV` — one of `local`, `staging`, `prod`. Non-local
environments enforce HTTPS via `X-Forwarded-Proto` and require
`FORCE_HTTPS=true` at boot.

## Lint / format / test

```bash
uv run ruff check .
uv run ruff format .
uv run pytest
```
