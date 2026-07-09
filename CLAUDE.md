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
- `analytics.py` — Mixpanel "MCP Call Received" event per tool call, keyed by the
  stable `autods_user_id`; fire-and-forget, fails open, no-op without `MIXPANEL_TOKEN`.
- `identity.py` — `SelfIdentityResolver` resolves the caller's AutoDS identity via the
  `get_current_user` operation with the caller's forwarded token; fails open to `None`.
- `sentry.py` — self-hosted Sentry init + context/capture helpers (see the Sentry
  convention below); no-op without `SENTRY_URL`.
- `logging.py` — structured logging setup + `get_logger`.
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

## Keeping docs in sync

`CLAUDE.md` (this file) and `README.md` are hand-maintained and drift silently —
nothing lints them. Treat them as part of the change, not an afterthought: a
feature isn't done until the docs describe it. Before finishing a change, run
this checklist and update whatever it touches **in the same commit**:

| If the change… | Update |
|---|---|
| adds/renames/removes a top-level module in `src/autods_mcp_server/` | the **Architecture → Module map** in this file |
| adds a new env var (new `validation_alias` in `settings.py`) | the **Configuration** table *and* narrative in `README.md`, and `.env.example` |
| adds or changes a user-facing feature (analytics, Sentry, a new transport behavior, …) | the relevant `README.md` section (narrative) *and* a **Conventions** bullet here if it carries an invariant a future editor must not break |
| adds/changes a manifest tool, `base_url_key`, or a boot-time lint (D5) | the **Tools are data** section here *and* the **Manifests** section in `README.md` |
| changes a command, workflow, or convention (lint/test/run, commit format, Python rules) | the corresponding section here |
| fixes a bug or incident whose root cause was non-obvious, or adds a guard/workaround that looks removable but isn't | a **Gotchas & hard-won lessons** bullet here (and a **Troubleshooting** entry in `README.md` if an operator/client would hit the symptom) |

Rule of thumb: if you added an invariant a reviewer would flag if broken (a
secret that must not leak, a fail-open path, a stateless-transport assumption),
it belongs in **Conventions** here so it survives the next edit. If you added
something an *operator* or *client* needs to know (an env var, a feature, an
endpoint), it belongs in `README.md`. Most features touch both. When one file
points at the other (e.g. "see `README.md` for the env-var reference"), keep
that pointer honest — don't let it promise information the target doesn't hold.

Expand **Gotchas & hard-won lessons** whenever a change cost real debugging time
or came from an incident — a fix whose root cause was surprising, or a guard that
a future editor would plausibly "clean up" and thereby reintroduce the bug (the
Sentry `max_request_body_size="never"` line is the canonical example). Conventions
states the rule to follow; a Gotcha explains the *failure mode* and why the guard
exists, so the two complement each other — a load-bearing guard often deserves
both. Write the bullet so it names the symptom, the root cause, and the
consequence of undoing the guard.

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
- **Sentry** (`sentry.py`, self-hosted `sentry.autods.com`): no-op unless `SENTRY_URL`
  is set (so local/test send nothing); the release comes from `__version__`, the
  environment tag defaults to `MCP_ENV`. Handled failures are returned as
  `CallToolResult(isError=True)` envelopes, so they're captured **explicitly** —
  automatic exception capture never sees them. **The bearer token must never reach
  Sentry:** never pass `raw_token` / the `Authorization` header into a scope, keep
  `send_default_pii` off, and preserve the substring `_SensitiveDataScrubber` (it
  over-redacts compound secret keys on purpose, and is shared as both the SDK
  `event_scrubber` and the scrubber for the custom `contexts` we attach — the base
  `EventScrubber` never walks custom contexts). Identify users via `set_user` only.
  `init_sentry` also passes **`max_request_body_size="never"`** — this is load-bearing,
  see the transport-hang gotcha below.

## Gotchas & hard-won lessons

Non-obvious failure modes learned the hard way. Each cost real debugging time or a
production incident; don't undo the guard without understanding why it's there.

- **Sentry integrations will hang the whole transport if they read the request body**
  (RD-71). The Starlette/FastAPI integrations' request-body extractor runs *before* the
  route and drains the ASGI receive stream, so the stateless transport's own
  `request.body()` blocks until the client gives up (~60s) — a total `/mcp` outage
  (once observed as 1157/1157 POSTs failing over 6h). `max_request_body_size="never"` in
  `init_sentry` is the fix; never remove it or add config/middleware that reads the body.
  A flood of `ClientDisconnect` 500s on `/mcp` is the *symptom* of this hang, not client
  noise — an earlier `before_send` filter that suppressed those disconnects only masked
  the outage and was removed once the root cause was fixed. Don't reintroduce it.
- **`init_sentry` is a no-op locally, so nothing exercises Sentry + the real transport
  together unless a test forces it** — which is exactly why the hang above shipped
  unseen. Keep the regression test that inits Sentry against the real transport and
  bounds the drive with `anyio.fail_after(...)`, so a reintroduced hang fails CI loudly
  instead of just stalling it.
- **Cognito *access* tokens carry neither `email` nor custom attributes — only ID tokens
  do — and this server verifies the access token.** Resolve any extra identity by calling
  upstream with the caller's forwarded token (`get_current_user` / `/users/list/`), never
  from a token claim, and never via boto3 `AdminGetUser` (that would hand the server
  privileged AWS credentials, breaking the no-privileged-credentials posture — it was
  deliberately rejected). Note the log/`request.state` field named `cognito_username` is
  actually `claims.sub` (the immutable UUID); key caches on `sub`, which is always present.
- **`GET /users/list/` returns only the authenticated caller** (it's effectively
  `/users/me/`, despite the name). Keep the response parsing tolerant — a single-element
  list *or* a bare object, with `id == 0` valid — and don't "fix" the resolver to paginate.
- **Identity resolution is synchronous on the auth path and runs on the first call
  (`initialize`).** A cold cache + slow upstream can push `initialize` past the MCP
  client's connect timeout (~10s for Claude) even though the server returns 200 and warms
  the cache — the client's retry then connects, leaving a confusing "authorized but the
  connection errored" state. It's upstream latency, not an auth/config bug.
- **The fail-open resolver fails *silently and completely*:** if identity can't resolve
  (missing permission, upstream error, unset token) every lookup degrades to `None` and,
  because analytics keys off the resolved identity, *no events fire at all* — with a valid
  token and no startup warning. Verify analytics actually emit after a deploy.
- **Never `@lru_cache` an async lookup** — it caches the coroutine object, not the awaited
  value. The identity cache uses a plain dict for this reason.
- **The Mixpanel SDK's default `Consumer` has no HTTP timeout and 4 in-thread retries**, so
  a hung Mixpanel would pin the `asyncio.to_thread` worker and stall shutdown. Keep the
  bounded `Consumer(request_timeout=3s, retry_limit=1)`, the `_MAX_PENDING=256`
  drop-on-overflow, and the time-bounded drain. Analytics is best-effort — shed events,
  never block a request. `distinct_id` must be a *truthy* AutoDS user id: a blank/falsy id
  makes Mixpanel file the event anonymously under a throwaway `$device:` id, so skip the
  event on any falsy id (and never key on the Cognito `sub`).
- **The integer-enum boot lint only inspects `body_schema`.** Enum-valued *query* params
  (e.g. `product_status`) are not type-checked, so a string-vs-int contract mismatch on a
  query enum ships silently with no test catching it — verify query-param enums against the
  upstream controller by hand. Boot *does* fail on duplicate `operation_id`s across
  manifests and on tool names over 128 chars (`_MAX_TOOL_NAME_LENGTH`).
- **`operations_count` in a manifest is cosmetic** — the model uses `extra="ignore"` and
  drops it; it's never validated and silently drifts. The real count guarantee is the
  tool-count assertions in the tests; update those by hand when you add/remove an operation.
- **New read tools must mirror `products_research.json` conventions**, which are
  load-bearing, not stylistic: enum-valued query params list allowed values in the
  `description` (not a JSON `enum`), `"min-max"` range filters are typed `str`, and
  `product_id`/`internal_id` stay distinct params. Verify enum value sets and example
  ranges against *live* upstream data — an enum narrower than the API silently rejects
  valid calls (e.g. a percentage field mistakenly documented as a 0–1 fraction).
- **The dispatcher is a pure forwarder** — `dispatch._parse_response` returns
  `response.json()` verbatim. You cannot trim or reshape a response via manifest text; that
  needs an upstream change. Don't add per-operation response logic — it breaks "tools are
  data".
- **`uvicorn.access`, `httpx`, and `mcp` INFO lines duplicate our structured
  `request`/`tool_call` logs**, so `configure_logging` raises them to WARNING — guarded so
  `LOG_LEVEL=debug` still gets the firehose. Don't undo it; emit the audit line, not the
  library's.
- **OAuth metadata URL fields are typed `str`, not `AnyUrl`/`HttpUrl`** — `AnyUrl` appends a
  trailing slash and breaks the byte-identity RFC 8414/9728 require between `issuer`/
  `resource` and the discovery URL. Don't "clean up" the types. Relatedly, the advertised
  host must come from `PUBLIC_HOSTNAME` (non-local), never `Host`/`X-Forwarded-Host`, and
  `"` is stripped from host- and claim-derived strings so nothing smuggles a quoted-string
  header injection into `WWW-Authenticate`/metadata.

## Local dev & debugging

- **PyCharm's "Debug" crashes the server** (while "Run" works): `pydevd` monkeypatches
  `asyncio.run` with a pre-3.12 signature, so uvicorn ≥0.30's `Server.run()` — which passes
  `loop_factory=...` — raises `TypeError`. Use `debug_server.py` (it `await`s
  `server.serve()` inside a plain `asyncio.run`), or upgrade to PyCharm 2025.1+.
- **Debug on the host as a single process with `--reload` OFF.** Reload runs the app in a
  child process the debugger never attaches to; the working directory must be the repo root
  so pydantic-settings loads `.env` and the default `manifests/` path resolves. No Redis is
  needed locally — the rate limiter falls back to an in-process bucket when `REDIS_URL` is
  unset.

## Testing

pytest + pytest-asyncio in auto mode (`asyncio_mode = "auto"`); tests live under `tests/`,
mirroring the source tree. Operations are defined inline in fixtures (see
`tests/mcp_server/conftest.py` `mcp_registry`/`mcp_settings`), and upstream calls are
mocked with `httpx.MockTransport`. `tests/conftest.py` snapshots/restores env vars and
resets the settings + JWKS caches around every test.

See `README.md` for the env-var reference (key knobs) and phase/RFC background, and
`settings.py` for the full schema.
