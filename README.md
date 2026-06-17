# autods-mcp-server

Public MCP (Model Context Protocol) server for AutoDS. Exposes selected
AutoDSApi and ProductsResearch tooling to MCP-compatible clients
(Claude, Cursor, etc.).

This repo is the foundation for the Public MCP epic
([RD-50](https://autods.atlassian.net/browse/RD-50)). Phase A
([RD-51](https://autods.atlassian.net/browse/RD-51)) set up the repo
skeleton, the FastAPI app, structured logging, origin / HTTPS-only
middlewares and the local Docker workflow. Phase B
([RD-52](https://autods.atlassian.net/browse/RD-52)) adds the Cognito
JWT verification stack. Phase C
([RD-53](https://autods.atlassian.net/browse/RD-53)) adds the MCP-spec
OAuth discovery endpoints (PRM, AS metadata) and the Dynamic Client
Registration shim. Phase D
([RD-54](https://autods.atlassian.net/browse/RD-54)) mounts the MCP
Streamable HTTP transport at `/mcp`, loads tool manifests, converts them
to MCP tool descriptors, and dispatches each tool call to the right
upstream service. Phase E
([RD-55](https://autods.atlassian.net/browse/RD-55)) finalizes the
launch endpoint set — the AutoDSApi manifests plus the hand-authored
ProductsResearch read endpoints (`manifests/products_research.json`) —
and adds the opt-in staging end-to-end smoke suite. Phase F
([RD-56](https://autods.atlassian.net/browse/RD-56)) hardens the server
for production: a stateless transport, Redis-backed per-user rate
limiting, audit logging, upstream error mapping, and graceful shutdown.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/) (dependency / virtualenv manager)
- Docker + docker-compose (for the local container workflow)

## Local development

First, copy the env template and fill in real values:

```bash
cp .env.example .env
# edit .env with your Cognito pool/region/client IDs
```

Then either:

```bash
uv sync
uv run uvicorn --factory autods_mcp_server.app:create_app --reload
curl http://localhost:8000/health   # {"status":"ok"}
```

### Via Docker

```bash
docker compose up
curl http://localhost:8000/health
```

`docker-compose.yml` reads `.env` via `env_file`, so the same values
used by `uv run` are picked up by the container. Source is bind-mounted
into the container, so edits hot-reload.

## Configuration

All settings come from environment variables. See
`src/autods_mcp_server/settings.py` for the full schema. Key knobs:

| Variable | Required | Notes |
|---|---|---|
| `MCP_ENV` | yes | `local` / `staging` / `prod` |
| `COGNITO_USER_POOL_ID` | yes | Cognito user pool that mints tokens |
| `COGNITO_REGION` | no (default `us-west-2`) | Used to compute the JWKS URL and `iss` claim |
| `ALLOWED_COGNITO_CLIENT_IDS` | yes (JSON list) | Access tokens are accepted only if their `client_id` is in this list |
| `COGNITO_PUBLIC_CLIENT_ID` | yes | client_id the DCR shim hands back to MCP clients; must also be in `ALLOWED_COGNITO_CLIENT_IDS` |
| `COGNITO_DOMAIN` | yes | Cognito Hosted UI domain (bare hostname or full URL); used to build authorize / token endpoints |
| `MCP_OAUTH_SCOPES` | no (default `email openid phone profile`) | JSON list of scopes published in PRM + AS metadata |
| `MCP_REGISTRATION_REDIRECT_URIS` | yes for `/oauth/register` | JSON list of redirect URIs the DCR shim will echo back. Must mirror the URIs pre-registered on the Cognito client |
| `FORCE_HTTPS` | yes in non-local | Set to `true` to acknowledge ALB-terminated TLS |
| `PUBLIC_HOSTNAME` | yes in non-local | Pins the PRM URL host (defends against `Host` / `X-Forwarded-Host` injection) |
| `MCP_MANIFEST_DIR` | no (default bundled `manifests/`) | Directory the MCP runtime loads tool manifests from. Point at an empty dir to serve zero tools |
| `LOG_LEVEL` | no (default `INFO`) | |
| `REDIS_URL` | yes in non-local | Shared Redis backing the per-user rate limiter (`redis://` / `rediss://`). Unset in local falls back to an in-process limiter |
| `RATE_LIMIT_PER_MINUTE` | no (default `60`) | Per-user token-bucket ceiling; `0` disables this bucket |
| `RATE_LIMIT_PER_HOUR` | no (default `1000`) | Per-user token-bucket ceiling; `0` disables this bucket |
| `MIXPANEL_TOKEN` | no | Mixpanel project token for the tool-call event. Unset → analytics disabled (the local default) |
| `COGNITO_ATTR_NEGATIVE_CACHE_TTL_SECONDS` | no (default `21600`) | TTL for *negative* identity-cache entries (6h) |
| `COGNITO_ATTR_POSITIVE_CACHE_TTL_SECONDS` | no (default `86400`) | TTL for *positive* identity-cache entries (24h); the id is immutable but the cached `email`/`name` can change, so positives expire and refresh |

Non-local environments enforce HTTPS via `X-Forwarded-Proto` and refuse
to boot without `FORCE_HTTPS=true`, `PUBLIC_HOSTNAME`, and `REDIS_URL` set.

Product analytics (RD-63): on each tool call the server emits a **MCP Call
Received** Mixpanel event, keyed by the stable `autods_user_id`. The identity is
resolved from AutoDSApi (the `get_current_user` lookup — see *Self-identity*
below) and cached in-process + Redis; no AWS/Cognito-admin credentials are
needed. Tracking is fire-and-forget and fails open, and the event is skipped
when the identity is unresolved (never keyed on the Cognito `sub`). Logs
(`request` access line + `tool_call` audit line) carry `autods_user_id` +
`email` alongside `cognito_username`.

## Authentication (Phase B)

The server is an OAuth resource server. Protected routes depend on
`autods_mcp_server.auth.get_current_user`, which:

1. Extracts `Authorization: Bearer <token>`.
2. Verifies the JWT against the JWKS document published by the
   configured Cognito user pool — signature (RS256), `iss`, `exp` are
   checked by PyJWT; `client_id` is checked manually against
   `ALLOWED_COGNITO_CLIENT_IDS` because Cognito access tokens carry
   `client_id` instead of `aud`.
3. Returns a `UserContext` with `sub`, `email`, `groups`, and the raw
   token wrapped in `pydantic.SecretStr`.

On failure, the response is HTTP 401 with an RFC 6750-compliant
`WWW-Authenticate: Bearer resource_metadata="<url>"` challenge that
points clients at the (future, Phase C/C2) protected-resource metadata
document.

If Cognito itself is unreachable or returns a malformed JWKS, the
response is HTTP 503 instead — clients should retry rather than
re-authenticate.

The `JWKSClient` caches keys for `ttl_seconds` (default 24h), refreshes
on unknown-`kid`, and rate-limits all refresh attempts (success or
failure) to one per `min_refresh_interval` (default 30s) to bound both
unknown-kid amplification and Cognito-outage amplification.

## OAuth discovery + DCR (Phase C)

The server exposes three unauthenticated routes that let MCP clients
auto-bootstrap the OAuth flow:

| Route | RFC | Returns |
|---|---|---|
| `GET /.well-known/oauth-protected-resource` | 9728 | `resource`, `authorization_servers`, supported scopes |
| `GET /.well-known/oauth-authorization-server` | 8414 | Proxy AS metadata — `authorization_endpoint` and `token_endpoint` point at Cognito Hosted UI; `registration_endpoint` points back at us |
| `POST /oauth/register` | 7591 | DCR shim — returns the pre-created `COGNITO_PUBLIC_CLIENT_ID` and echoes back validated `redirect_uris` |

The DCR shim is a thin proxy because Cognito itself doesn't speak DCR.
MCP clients (Claude, Cursor, MCP Inspector) refuse to start the OAuth
flow without a `registration_endpoint`, so we hand them back the fixed
Cognito public client they would have used anyway, after validating each
requested redirect URI against `MCP_REGISTRATION_REDIRECT_URIS` (exact
match — globs would mask Cognito-side rejections).

Token verification still happens against Cognito's issuer
(`COGNITO_USER_POOL_ID` / `COGNITO_REGION`); the `issuer` field in our
AS metadata document is our own resource URL (so clients discover this
proxy via `/.well-known/oauth-authorization-server`).

The 401 `WWW-Authenticate` challenge wired in Phase B advertises the
PRM URL, completing the discovery loop:

1. Client GETs a protected MCP route → 401 with `WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"`.
2. Client fetches PRM, then AS metadata, then `POST /oauth/register`.
3. Client redirects the user to Cognito's `authorization_endpoint` and
   exchanges the code at Cognito's `token_endpoint`.
4. Client sends the resulting access token in `Authorization: Bearer …`.

### Manual end-to-end test (C6)

The MCP Inspector at `http://localhost:6274` can be pointed at a local
server (`uv run uvicorn …`) with OAuth enabled. With the Phase D `/mcp`
transport live, the Inspector completes the OAuth flow and then lists the
manifest-defined tools.

## MCP runtime (Phase D)

The MCP Streamable HTTP transport is mounted at `/mcp` (both `POST` and
`GET` for the SSE stream, per the MCP spec) behind the Phase B auth
dependency — an unauthenticated request gets the same RFC 6750 `401 +
WWW-Authenticate` challenge, which is what MCP clients follow to discover
the OAuth flow. On success the verified `UserContext` is carried through
to each tool call, and the dispatcher forwards the caller's own bearer
token upstream (the server never holds privileged credentials).

### Manifests

Tools are defined by JSON manifests under `MCP_MANIFEST_DIR` (default: the
bundled `manifests/`). The format mirrors
`autods-mcp/generated/servers/<server>/operations.json`, extended with two
fields the public server needs:

- `annotations` — `{ title, readOnlyHint, destructiveHint }` per operation.
  The server **refuses to boot** if any tool lacks a `title` or lacks both
  hint flags (D5).
- `base_url_key` — which upstream serves the operation (`autods_api` →
  `AUTODS_API_BASE_URL`, `products_research` → `PRODUCTS_RESEARCH_BASE_URL`).
  Set per-operation or once at the manifest level; one running server can
  route different tools to different upstreams.

Each operation's path/query/header parameters (plus a free-form JSON `body`
when present) are converted into a pydantic model whose JSON schema becomes
the tool's `inputSchema`.

The manifests under `manifests/` are maintained by hand: add a new operation
as a JSON entry with its `parameters`, `has_json_body`/`request_body_required`
flags, `annotations` (`title` + at least one hint), and `base_url_key`. The
server's D5 startup lint refuses to boot if any operation is missing its
annotations, so a malformed manifest can't reach a client.

### Self-identity (RD-68)

The caller's own AutoDS identity (`id`, `name`, `email`) is resolved by the
`get_current_user` tool (`manifests/users.json`, AutoDSApi `GET /users/list/` —
which returns just the authenticated user). `SelfIdentityResolver`
(`identity.py`) dispatches this operation with the caller's already-forwarded
bearer token — no privileged credentials — and is exposed on `app.state` for
downstream consumers (e.g. log/analytics enrichment). It **fails open**: any
dispatch error, non-2xx, or unparseable payload resolves to `None` and never
breaks auth or a tool call.

### Manifest → upstream call flow

1. Client lists tools via `tools/list`; each descriptor carries the
   manifest annotations.
2. Client calls a tool; the SDK validates arguments against `inputSchema`.
3. The dispatcher looks up the operation, resolves its upstream base URL
   from `base_url_key`, substitutes path params, attaches query/header
   params and the JSON body, forwards `Authorization: Bearer …`, and
   returns a structured `{ operation_id, status, ok, data }` envelope.

## Hardening (Phase F)

Production runs 2–10 replicas × 5 uvicorn workers, which shapes every
choice here:

- **Stateless transport.** The `StreamableHTTPSessionManager` runs
  `stateless=True`, so no MCP session is retained between requests. A
  stateful session is a live coroutine pinned to one worker; stateless
  lets any worker on any replica serve any request (no `Session not
  found` 404s) and removes unbounded per-worker session growth. The
  trade-off — the server→client GET SSE / resumability stream — is
  unused by this synchronous tool-forwarding server.
- **Per-user rate limiting.** Two token buckets keyed by `user.sub`
  (`60/min` and `1000/hour` by default) enforced in `call_tool`. State
  lives in Redis via an atomic Lua script so the limit holds
  cluster-wide; on a Redis outage the limiter *fails open*. Local dev
  with no `REDIS_URL` falls back to an in-process limiter. On exceed,
  the tool returns a `rate_limited` error with a retry-after hint.
- **Audit logging.** Every tool call emits one structured `tool_call`
  line: `request_id`, `user_sub`, `tool_name`, `op_id`, `upstream_url`,
  `upstream_status`, `latency_ms`, and `error_type` on failure. Never a
  request/response body.
- **Upstream error mapping.** Upstream `401 → unauthenticated`,
  `403 → forbidden`, other `4xx → upstream_client_error` (sanitized
  detail), `5xx → upstream_error` (generic to the user, full detail
  logged server-side).
- **Graceful shutdown.** uvicorn runs with
  `--timeout-graceful-shutdown 30`; on `SIGTERM` it stops accepting new
  connections and drains in-flight tool calls within the window
  (≤ Kubernetes `terminationGracePeriodSeconds`). The app lifespan
  closes the upstream HTTP and Redis clients on exit.

## Lint / format / test

```bash
uv run ruff check .
uv run ruff format .
uv run pytest
```

Coverage (line + branch, with missing-line report):

```bash
uv run --with pytest-cov pytest --cov=src/autods_mcp_server --cov-report=term-missing --cov-branch
```

### Staging end-to-end smoke (E3)

`tests/e2e/` drives the real server (real Cognito JWT verification + real
upstream calls) against staging and asserts every registered tool returns
a 2xx or a documented business error. It is **opt-in** and skipped unless
`RUN_STAGING_E2E=1` and the staging env vars are set:

```bash
RUN_STAGING_E2E=1 \
  E2E_COGNITO_USERNAME=… E2E_COGNITO_PASSWORD=… \
  E2E_COGNITO_CLIENT_ID=… E2E_COGNITO_USER_POOL_ID=… E2E_COGNITO_DOMAIN=… \
  E2E_STORE_IDS=… \
  uv run pytest tests/e2e
```

The app client in `E2E_COGNITO_CLIENT_ID` must have `USER_PASSWORD_AUTH`
enabled. The write ops (`upload_products`, `publish_drafts_to_marketplace`)
are skipped unless `E2E_INCLUDE_WRITES=1`, so a default run never mutates
staging data. See `tests/e2e/conftest.py` for the full env-var contract.
