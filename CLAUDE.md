# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Public MCP (Model Context Protocol) server for AutoDS. It exposes curated AutoDSApi /
ProductsResearch operations to MCP clients (Claude, Cursor, MCP Inspector) over a
Streamable HTTP transport at `/mcp`, acting as an OAuth resource server in front of
Cognito. The server holds no privileged credentials — it forwards each caller's own
bearer token upstream.

## Commands

Python **3.12 only** (`>=3.12,<3.13`). Dependencies are managed with **`uv`** — never use
bare `pip`/`venv`/`python`.

```bash
make install   # uv sync (installs dev + test groups)
make run       # uv run uvicorn --factory autods_mcp_server.app:create_app --reload
make test      # uv run pytest
make lint      # ruff check . && ruff format --check .
make fmt       # ruff format . && ruff check --fix .
```

Run a single test:

```bash
uv run pytest tests/mcp_server/test_dispatch.py::test_name
```

Coverage (line + branch):

```bash
uv run --with pytest-cov pytest --cov=src/autods_mcp_server --cov-report=term-missing --cov-branch
```

Health check: `curl http://localhost:8000/health` → `{"status":"ok"}`.

## Architecture

`create_app()` in `src/autods_mcp_server/app.py` is the FastAPI factory. At boot it loads
settings, builds the MCP runtime (so the manifest lint runs early), wires middlewares
(`RequestContextMiddleware` outermost, then `OriginAllowlistMiddleware`), and mounts
`/health`, the OAuth routes, and the MCP transport. App lifespan closes the upstream HTTP
client and Redis client on exit.

Module map (`src/autods_mcp_server/`):

- `settings.py` — all config via env vars (pydantic-settings); validators enforce
  startup invariants. Non-local refuses to boot without `FORCE_HTTPS=true`,
  `PUBLIC_HOSTNAME`, and `REDIS_URL`.
- `auth/` — Cognito JWT verification. `dependency.get_current_user` guards protected
  routes and returns a `UserContext`. Cognito access tokens carry `client_id` (not
  `aud`), so `client_id` is checked manually against `ALLOWED_COGNITO_CLIENT_IDS`.
  Auth failure → 401 + RFC 6750 `WWW-Authenticate` challenge; Cognito unreachable → 503.
- `oauth/` — three unauthenticated discovery routes (PRM RFC 9728, AS metadata RFC 8414,
  and a DCR shim `POST /oauth/register` RFC 7591 that hands back the pre-created
  `COGNITO_PUBLIC_CLIENT_ID` because Cognito doesn't speak DCR).
- `manifests/` — `schema.py` Pydantic models + `loader.py` (`load_manifests`,
  `ManifestRegistry`).
- `tools.py` — converts manifest operations to MCP `Tool` descriptors and runs the boot
  lint (D5).
- `dispatch.py` — `OperationDispatcher` resolves the upstream base URL, substitutes path
  params, attaches query/header params + JSON body, forwards the caller's bearer token,
  and returns a `{ operation_id, status, ok, data }` envelope.
- `mcp_transport.py` — builds the runtime and mounts the **stateless** Streamable HTTP
  transport behind the auth dependency; the `call_tool` handler applies rate limiting and
  emits the audit log.
- `errors.py` — MCP tool error construction + upstream error mapping.
- `ratelimit.py` (+ `ratelimit.lua`) — per-user token buckets; `redis_client.py`,
  `middleware.py`, `urls.py`.

## Tools are data, not code

Tools are defined by JSON manifests under `manifests/` (`MCP_MANIFEST_DIR`), maintained by
hand. To add a tool, add a JSON operation entry — do **not** write a Python function.
Each operation needs `operation_id`, `method`, `path`, `parameters`,
`has_json_body`/`request_body_required`, `base_url_key` (`autods_api` or
`products_research`), and `annotations`.

Two boot-time lints (D5) refuse to start the server, so a malformed manifest can't reach a
client:

- Every operation must have an `annotations.title` **and** at least one hint
  (`readOnlyHint` or `destructiveHint`).
- Integer enum fields in `body_schema` (e.g. `product_status`, `status`, `region`,
  `site_id`, `buy_site_id`, `inventory_status`) **must** be typed as integers, never
  strings.

### Keep descriptions implementation-agnostic

`instructions`, `notes`, `summary`, and `description` strings ship to MCP clients as the
text the model reads — so they are public. Describe the **observable contract** (sync vs
async, what to poll, input format, output shape), never **how AutoDS is built**.

Do not name internal frameworks, datastores, services, or symbols. In particular:

- ❌ "fires a Celery task" / the task function name → ✅ "starts an asynchronous bulk job"
- ❌ "queries MongoDB" / "ProductsResearch service (Elasticsearch + MongoDB)" → ✅ "queries
  products by filter" / "the product-research catalog"
- ❌ "MongoDB ObjectId" / "`id` maps to `_id` upstream and casts to ObjectId" → ✅ "24-character
  hex id string" / "filter by `id` with value_type `object_id`"

Keep the parts the caller genuinely needs (id format, how to filter, async-then-poll
semantics); drop only the implementation framing. The `value_type: "objectId"` enum value
in a `body_schema` is part of the wire contract — that stays. See `users.json` for the same
instinct applied to a response payload (internal fields told to clients as "do not surface").

## Python conventions

- Do **not** use `from __future__ import annotations`.
- Keep imports at module level. Function-level (local) imports are tolerated
  only when required by code logic (e.g. breaking a circular import, or an
  optional/heavy dependency that must be lazily loaded).

## Versioning

The `__version__` lives in `src/autods_mcp_server/__init__.py`. Bump it on
every commit:

- **Patch** (`x.y.Z`) — fixes, logging, analytics, and other technical changes.
- **Minor** (`x.Y.0`) — new business logic, or new endpoints added in manifests.

Amending a commit that already bumped the version does **not** require a
further bump.

## Commit message format

Commits are multiline: a subject line, a blank line, then a body.

**Subject line:**

```
<JIRA-KEY> :: <Short description> :: [Task URL](https://autods.atlassian.net/browse/<JIRA-KEY>)
```

- `<JIRA-KEY>` is the ticket, e.g. `RD-55`. Segments are separated by ` :: `
  (space-colon-colon-space), and the line ends with ` ::` or the Task URL.
- The `[Task URL](...)` may sit on the subject line, or be moved to the first
  line of the body instead (in which case the subject just ends with ` ::`).

**Body:**

- Explains *what* changed and *why*. A short commit gets a sentence; otherwise
  use a plain bullet-point list of the changes.
- End with the `Co-Authored-By:` trailer when an agent contributed.

Example:

```
RD-50 :: Logging cleanup ::

[Task URL](https://autods.atlassian.net/browse/RD-50)
* Including cognito username into log entries.
* Excluding log entries from third-party libraries.
* Suppressing "/health" log calls.
```

> Note: some early commits group the body into labelled sections (e.g.
> `F0 — ...`, `E2 — ...`). That was specific to the initial phased
> implementation tickets; for a normal ticket, just use a bullet-point list.

## Conventions

- **Error mapping** (`errors.py`): upstream `401 → unauthenticated`, `403 → forbidden`,
  other `4xx → upstream_client_error` (detail sanitized for leak markers), `5xx →
  upstream_error` (generic message to the client; full detail logged server-side only).
- **Audit logging**: each tool call emits exactly one structured `tool_call` line
  (`request_id`, `user_sub`, `tool_name`, `op_id`, `upstream_url`, `upstream_status`,
  `latency_ms`, `error_type`). Never log request/response bodies.
- **Rate limiting**: two per-`user.sub` token buckets (60/min, 1000/hour by default) in
  `call_tool`; Redis-backed via an atomic Lua script that mirrors `evaluate_buckets()`,
  fails open on Redis outage, falls back to in-process locally.
- **Transport is stateless** (`stateless=True`) by design — production runs many
  replicas × workers, so no MCP session is pinned to a worker. Don't reintroduce
  session state.

## Testing

pytest + pytest-asyncio in auto mode (`asyncio_mode = "auto"`); tests live under `tests/`,
mirroring the source tree. Operations are defined inline in fixtures (see
`tests/mcp_server/conftest.py` `mcp_registry`/`mcp_settings`), and upstream calls are
mocked with `httpx.MockTransport`. `tests/conftest.py` snapshots/restores env vars and
resets the settings + JWKS caches around every test.

See `README.md` for the full env-var catalog and phase/RFC background.
