# autods-mcp-server

Public MCP (Model Context Protocol) server for AutoDS. Exposes selected
AutoDSApi and ProductsResearch tooling to MCP-compatible clients
(Claude, Cursor, etc.).

This repo is the foundation for the Public MCP epic
([RD-50](https://autods.atlassian.net/browse/RD-50)). Phase A
([RD-51](https://autods.atlassian.net/browse/RD-51)) set up the repo
skeleton, the FastAPI app, structured logging, the Origin-allowlist
middleware, HTTPS enforcement (a startup settings validator plus a
request-level `X-Forwarded-Proto` guard) and the local Docker workflow. Phase B
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

## Connecting a client

The three deployed environments each expose the MCP Streamable HTTP
transport at `/mcp`:

| Environment | MCP endpoint |
|---|---|
| Local | `http://localhost:8000/mcp` |
| Staging | `https://mcp-staging.autods.com/mcp` |
| Production | `https://mcp.autods.com/mcp` |

Every environment is an OAuth resource server (see *Authentication* /
*OAuth discovery + DCR* below), so the client drives a browser sign-in
against Cognito the first time it connects. Cognito **exact-matches**
redirect URIs — no random loopback ports, no path wildcards — so each
client must use a callback that is pre-registered on the Cognito app
client *and* listed in that environment's `MCP_REGISTRATION_REDIRECT_URIS`
(local: `.env`; staging/prod: the helm `values-*.yaml` in
`autods-mcp-deploy`). The recipes below use callbacks that are already
registered.

### Claude Code

Claude Code needs the fixed OAuth callback port `2048` (its
`http://localhost:2048/callback` redirect is pre-registered in all three
environments). Add the server, then run `/mcp` and complete the browser
sign-in:

```bash
# Local (server running via `make run`)
claude mcp add --transport http --callback-port 2048 autods-public-local http://localhost:8000/mcp

# Staging
claude mcp add --transport http --callback-port 2048 autods-public-staging https://mcp-staging.autods.com/mcp

# Production
claude mcp add --transport http --callback-port 2048 autods-public-prod https://mcp.autods.com/mcp
```

Use exactly these names. Tools are namespaced by alias
(`mcp__autods-public-staging__list_stores_api`), so a consistent name is what tells
you — and an agent — which environment a call lands in; `docs/release-checks.md`
relies on it before it will run anything that writes.

Add `-s user` to register the server for every project instead of just
the current one (the default scope is `local`).

### Claude (web & desktop)

The claude.ai web app and the Claude desktop app authenticate through the
hosted `https://claude.ai/api/mcp/auth_callback` /
`https://claude.com/api/mcp/auth_callback` redirects (both pre-registered),
so there's nothing to configure by hand — two custom connectors are
already published:

- **AutoDS** → staging
- **AutoDS Prod** → production

Enable them from **Settings → Customize → Connectors** (the *Connectors*
menu), then authorize when prompted.

Note these two do **not** follow the `autods-public-*` alias convention above, and
"AutoDS" pointing at *staging* while "AutoDS Prod" points at production is exactly
the ambiguity that convention exists to remove. For a `docs/release-checks.md` run,
add the CLI servers by their canonical names rather than relying on these.

### Codex

Codex's default OAuth callback uses a random port and path, which can
never match Cognito's exact-match allowlist. Each environment therefore
pre-registers a **static** callback on port `2048` with a fixed,
per-environment path. Point Codex at it in `~/.codex/config.toml`:

```toml
# Staging
[mcp_servers.autods-public-staging]
url = "https://mcp-staging.autods.com/mcp"
mcp_oauth_callback_port = 2048
mcp_oauth_callback_url = "http://localhost:2048/callback/j1OexN-34suD"

# Production
[mcp_servers.autods-public-prod]
url = "https://mcp.autods.com/mcp"
mcp_oauth_callback_port = 2048
mcp_oauth_callback_url = "http://localhost:2048/callback/sSA0buWJ4hMg"
```

The callback path differs per environment (`j1OexN-34suD` for staging,
`sSA0buWJ4hMg` for prod) — it must match the URI registered in that
environment's `MCP_REGISTRATION_REDIRECT_URIS` exactly. Loosening the
server-side allowlist does not help; Cognito still rejects a mismatch.

### MCP Inspector (local debugging)

The MCP Inspector at `http://localhost:6274` points at a local server
(`make run`) and completes the OAuth flow via its
`http://localhost:6274/oauth/callback` redirect (registered locally and in
staging), then lists the manifest-defined tools. See *Manual end-to-end
test (C6)* below.

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
| `SENTRY_URL` | no | Self-hosted Sentry DSN (`https://<key>@sentry.autods.com/<id>`). Unset (or `MCP_ENV=local`) makes Sentry init a no-op. Delivered via External Secrets in staging/prod |
| `SENTRY_ENVIRONMENT` | no (default `MCP_ENV`) | Sentry environment tag. The release is derived from `__version__` in code, not an env var |
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

Error / performance reporting (RD-66): when `SENTRY_URL` is set (staging/prod,
via External Secrets) the server reports to the self-hosted
`sentry.autods.com`. Init is a **no-op** locally or with `SENTRY_URL` unset, so
dev/test runs send no events. The handled upstream/internal failures the server
returns as `CallToolResult(isError=True)` envelopes (see *Hardening* below) are
captured explicitly, since Sentry's automatic exception capture never sees them.
The bearer token is never passed into a Sentry scope, `send_default_pii` stays
off, and a substring-matching event scrubber (`sentry.py`) over-redacts
compound secret keys (`access_token`, `api_secret`, `user_password`, …); the
user is still identified by stable id + email via `set_user`. Traces are sampled
at 1%.

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
`autods-mcp/generated/servers/<server>/operations.json`, extended with the
fields the public server needs — two of them per operation:

- `annotations` — `{ title, readOnlyHint, destructiveHint }` per operation.
  The server **refuses to boot** if any tool lacks a `title` or lacks both
  hint flags (D5).
- `base_url_key` — which upstream serves the operation (`autods_api` →
  `AUTODS_API_BASE_URL`, `products_research` → `PRODUCTS_RESEARCH_BASE_URL`).
  Set per-operation or once at the manifest level; one running server can
  route different tools to different upstreams. An operation served *by this
  server* declares `handler` instead — see **Playbooks** below.

Each operation's path/query/header parameters (plus a free-form JSON `body`
when present) are converted into a pydantic model whose JSON schema becomes
the tool's `inputSchema`.

The manifests under `manifests/` are maintained by hand: add a new operation
as a JSON entry with its `parameters`, `has_json_body`/`request_body_required`
flags, `annotations` (`title` + at least one hint), and `base_url_key`. The
server runs its startup lints and refuses to boot if any fails, so a
malformed manifest can't reach a client: (1) every operation must have an
`annotations.title` and at least one hint; (2) integer enum fields in a
`body_schema` (e.g. `product_status`, `status`, `region`, `site_id`,
`buy_site_id`, `inventory_status`) must be typed as integers, never strings;
(3) the concatenated `instructions` (below) must be at most 6000 characters;
(4) a `business_errors` block (below) must declare at least one path, and its
operation's `notes` must mention `ok`; (5) each operation declares exactly one
of `handler` / `base_url_key`; (6) the six playbook lints (below).

#### Server instructions

Each manifest may carry an `instructions` string. They are concatenated in
sorted-filename order and passed to the MCP `Server`, which puts them in
`InitializeResult.instructions` — the text a client surfaces in the model's
system prompt (Claude Code shows it under an "MCP Server Instructions"
heading). To see exactly what a live server advertises:

```bash
uv run python scripts/mcp_call.py instructions
```

Because that text rides in the system prompt on every model turn and sits in
the client's cached prefix, it is kept to an **index**: where to start, and the
handful of invariants that hold across every tool (enums are integers,
`store_ids` is a path parameter, `ok` is a transport-level signal only). It
lives in `manifests/_server.json`, a manifest with no operations. Anything
needed to form a specific call belongs in that tool's `inputSchema`, and
anything describing one tool's contract in its `description`/`notes` — both
arrive attached to the tool, which is cheaper and more reliable. An empty
`instructions` is normal for a domain that has nothing to add. The 6000-char
boot lint is what keeps the channel from growing back.

#### Polling conventions (RD-91)

Some upstream operations accept work and finish it later — `upload_products` returns a
`bulk_action` before anything is in the store. The web app watches those over
Pusher/SSE; this server has no push channel, so an MCP client polls instead, and the
cadence is part of the manifest text rather than something the server enforces (no
per-operation timeout, no blocking wait).

The documented cadence, stated numerically because vague guidance produces either one
poll or forty: **first poll ~10 s** after the write, **then every ~15 s**, stopping
after **10 attempts** (~2.5 min at that cadence) — then report what is still unfinished.
The attempt count is the ceiling; the duration follows from it. It replaces the
browser's 3 s / 120 s loop, which is right for a page and wrong for an agent (a tool
round-trip is already seconds, and each attempt costs a model turn).

It is delivered on three channels at once — the polling tool's `notes`, the playbook
runbook, and one clause of the server `instructions` — and
`tests/mcp_server/test_polling_conventions.py` asserts all three carry the same
numbers and that they arrive at a real client.
`docs/polling-conventions.md` holds the whole convention: the cadence, the completion
state machines, the scrapers' three-state read, and the rate-limit budget behind it.

#### Business errors inside a 200

Some upstreams answer a request they refused with HTTP 200 and an error code
inside the payload, which makes the envelope's `ok` misleading on its own. An
operation can declare where those codes live and what each one means:

```json
"business_errors": {
  "paths": ["scraper_error.errorCode", "data.*.error.errorCode"],
  "codes": { "PRODUCT_OOS": "Offer is out of stock. Choose a different offer." }
}
```

`paths` are dotted paths into the upstream payload (`*` matches every element
of a list or value of a dict). On a match the envelope gains a
`business_error` list of `{code, message}` **next to** `data`; `data` itself is
always the upstream payload verbatim, and an operation without the block gets
an untouched envelope.

This applies to a rejection inside an HTTP 200 only. A refusal the upstream
reports with a non-2xx status is mapped to a generic typed error whose detail is
logged server-side and never echoed to the caller, so it can't be surfaced as a
recovery hint this way.

#### Playbooks (RD-100)

Some goals need several tools called in order, and nothing in a single tool
descriptor says so — an agent that stops after step 2 of 3 leaves work that
looks done and isn't. A **playbook** declares one such chain as data, in
`manifests/playbooks/*.json`:

```json
{
  "name": "product_import",
  "title": "Import products into a store and confirm they landed",
  "when_to_use": "The user wants supplier products added to one of their stores…",
  "entry_operation": "upload_products",
  "steps": [
    {
      "operation_id": "upload_products",
      "goal": "Submit the chosen supplier products to the store…",
      "requires": [{"param": "store_ids", "from_operation": "list_stores_api", "field": "id"}],
      "then": ["get_bulk_action_items"],
      "incomplete_alone": "Nothing is in the store until the bulk job finishes.",
      "on_failure": {
        "idempotent": false,
        "left_behind": "The bulk job may have started even though the call errored.",
        "verify_with": {"operation_id": "list_products", "how": "product_status 1, filtered on the uploaded title"},
        "then": "If the drafts exist, poll them; otherwise resubmit unchanged.",
        "ask_user": true
      }
    }
  ],
  "done_when": "list_products returns the submitted products…",
  "body": "…the markdown runbook…"
}
```

Step numbers, the default successor (the next step in the list) and the
`operation_id → steps` index are all **derived**, never authored, so renumbering
a chain or adding an operation to a second chain can't desynchronise them. Six
boot lints cover the rest: every referenced operation must be registered, every
`requires[].param` must be an input its own step accepts, names must be unique
and every step reachable from `entry_operation`, a non-final destructive step
must say what stopping costs (`incomplete_alone`), a non-idempotent destructive
step must name a **read-only** operation that establishes whether the write
landed (`verify_with`), and every rendered string must fit its channel.

A client meets a playbook on four channels, by when the text is needed:

- **`get_playbook`** — one tool, `name` an enum of the registered playbooks,
  returning the whole runbook. The runbook therefore costs nothing until an
  agent enters the flow, and the enum doubles as the index of what exists.
  It is answered by this server (no upstream call) and is otherwise an ordinary
  tool: rate-limited, audited, tracked, and returning the usual envelope.
- **the result envelope** — a successful call of a non-final chain step gains a
  `playbook` field *beside* `data` (`{name, step, next, incomplete_alone,
  runbook}`, at most 200 serialized characters), so the nudge is in context
  exactly when the agent has just finished that step.
- **the error text** — a failing chain step whose `on_failure` is declared gets
  the chain consequence appended to the `isError` message ("this step is not
  idempotent … verify with `list_products` … before retrying"). An error result
  carries no `structuredContent`, so this is the only way to deliver it, and it
  is the half that prevents a duplicated write.
- **the tool `description` and `instructions`** — one bounded pointer each
  (`Step 1 of 3 in playbook "product_import" — call get_playbook for the full
  chain.`) and one generated index line per chain. No step bodies in either.

One tool can be a step of several chains, and nothing in a request says which one
the caller is following (the transport is stateless, so there is no session that
could have remembered). Where that happens, both result-carried hints say less
rather than guessing: the envelope hint names the candidate chains and points at
`get_playbook` instead of stating a step number, and the error text keeps only the
clauses every candidate agrees on — resolving the disagreements towards caution,
so "not idempotent" wins over "idempotent". A confident wrong answer about what an
unfinished chain left behind is worse than one extra call.

Playbooks are also mirrored as `autods://playbook/<name>` resources with
`mimeType: text/markdown`, for hosts that let a user attach a resource:

```bash
uv run python scripts/mcp_call.py resources
uv run python scripts/mcp_call.py get_playbook '{"name":"product_import"}'
```

### Self-identity (RD-68)

The caller's own AutoDS identity (`id`, `name`, `email`) is resolved by the
`get_current_user` tool (`manifests/users.json`, AutoDSApi `GET /users/list/` —
which returns just the authenticated user). `SelfIdentityResolver`
(`identity.py`) dispatches this operation with the caller's already-forwarded
bearer token — no privileged credentials — and is exposed on `app.state` for
downstream consumers (e.g. log/analytics enrichment). It **fails open**: any
dispatch error, non-2xx, or unparseable payload resolves to `None` and never
breaks auth or a tool call.

### Entitlements and credit balances (RD-89)

`get_current_user` deliberately exposes only `id`/`name`/`email`, so it cannot
answer "may this account do X". `get_user_subscription` (`manifests/users.json`,
AutoDSApi `GET /subscriptions/user-subscription/`) is the read that can: one
parameterless call returns `user_addons[]` (which add-ons the account holds),
`user_credits[]` (the balances it can spend) and `user_packages[]` (plan
context). It is the pre-flight call for an entitlement-gated flow, and the first
call to make when another tool answers `forbidden` — it is permitted for every
account status, so it still answers when the others refuse.

Three things about the response are documented in the tool's `notes` because a
caller gets them wrong otherwise:

- **Active means present and not canceled.** `expired` (`3`) still counts as
  active — this mirrors how the platform itself decides — and a missing entry
  means the account never held the add-on.
- **The enum renderings in one response are not uniform.** `addon_type` is a
  name (`"product_hub"`), add-on/package `status` is an integer (`1`/`2`/`3`),
  and `credit_type` is a numeric code delivered as a *string* (`"1"` =
  auto-order credits, `"2"` = product-finder, `"3"` = AI-rewrite) — the one
  place an AutoDS enum arrives neither as an integer nor as a name.
- **An absent credit entry is not a confirmed zero.** The auto-order entry is
  omitted both when the balance is zero and when the upstream balance source
  didn't answer inside its timeout, and the response can't distinguish them.

Answering the call reconciles the account against that separate balance source
under a bounded wait, so its latency is less predictable than a plain read
(0.35–1.3 s measured against staging, with a few seconds still in contract).
Call it once per task and keep the answer — it is not a polling tool.

### Manifest → upstream call flow

0. Client connects; the `initialize` response carries the concatenated
   manifest `instructions`.
1. Client lists tools via `tools/list`; each descriptor carries the
   manifest annotations.
2. Client calls a tool; the server validates the arguments against that tool's
   `inputSchema` (a jsonschema validator compiled at boot — since mcp 2.x the
   SDK performs no validation of its own) and rejects a bad body as a typed
   `invalid_arguments` error before any upstream call.
3. The dispatcher looks up the operation, resolves its upstream base URL
   from `base_url_key`, substitutes path params, attaches query/header
   params and the JSON body, forwards `Authorization: Bearer …`, and
   returns a structured `{ operation_id, status, ok, data }` envelope — plus a
   `business_error` sibling when the operation declares one and the payload
   matches, and a `playbook` sibling when the tool is a non-final step of a
   chain. An operation that declares a `handler` instead of a `base_url_key`
   skips step 3 entirely: it is answered locally, after the same rate limiting,
   validation, analytics and audit, and returns the same envelope shape.

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
  (`60/min` and `1000/hour` by default) enforced in `on_call_tool`. State
  lives in Redis via an atomic Lua script so the limit holds
  cluster-wide; on a Redis outage the limiter *fails open*. Local dev
  with no `REDIS_URL` falls back to an in-process limiter. On exceed,
  the tool returns a `rate_limited` error with a retry-after hint. Both
  buckets cover every operation, reads included: at the documented
  polling cadence one flow costs ~12–14 calls and draws 4 polls/min, so
  the minute bucket binds at roughly ten concurrent flows and the hour
  bucket at seventy flows per hour (RD-91 review in
  `docs/polling-conventions.md`).
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

## Troubleshooting

- **Client shows a connection error right after authorizing (but retry works).** The first
  authenticated call (`initialize`) resolves the caller's identity synchronously via a
  blocking upstream call. On a cold cache with a slow upstream this can exceed the MCP
  client's connect timeout (~10s for Claude) even though the server returns 200 and warms
  the cache — the client's automatic retry then connects. Treat it as upstream latency, not
  an auth/config failure; watch upstream response times.
- **OAuth fails at the Cognito sign-in step** (`oauth_error=invalid_request`,
  `oauth_error_subtype=provider_redirect`, etc.). The authorize/token exchange happens
  directly between the MCP client and Cognito Hosted UI — it bypasses this server, so the
  real error is in Cognito / CloudWatch, not the MCP logs. Triage by scope: *one* user
  failing is usually a Cognito identity-linking collision (a native account and a federated
  `Google_<sub>` identity sharing one email that Cognito won't auto-merge); *all* users
  failing points at global config (scopes, redirect-URI allowlist, or `client_id`).
- **Connecting a new MCP client.** See *Connecting a client* above for the per-environment
  endpoints and the ready-made Claude Code / Claude web+desktop / Codex recipes. The root
  cause any new client hits: Cognito exact-matches redirect URIs — no random loopback ports,
  no path wildcards — so the client must use a stable callback that is both pre-registered on
  the Cognito app client and listed in `MCP_REGISTRATION_REDIRECT_URIS`. Claude Code does this
  with `--callback-port 2048`; Codex needs `mcp_oauth_callback_port` / `mcp_oauth_callback_url`
  set (its default random port+path can never match). Loosening the server-side allowlist does
  not help — Cognito still rejects it.
- **A burst of ~60s `500`s on `POST /mcp`.** These surface as `ClientDisconnect` and are
  usually benign client-side timeouts, but a *flood* means either the transport is hanging
  (see the Sentry request-body gotcha in `CLAUDE.md`) or genuine slow upstream tool calls —
  worth checking upstream latency, which the disconnect noise can otherwise mask.

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

**"Every registered tool" is a hand-maintained list, not a discovered one.** The
op-name sets at the top of `tests/e2e/test_staging_smoke.py` are what
`test_tools_list_exposes_all_registered_ops` asserts `tools/list` equals exactly,
so **adding a manifest operation means editing that file too** — alongside the
tool-count assertions in `tests/mcp_server/test_loader.py` and
`test_transport.py`. Because this suite is opt-in, omitting it breaks nothing in
CI: the list simply goes stale and the next staging run fails on a tool that has
been shipping for months.

### Post-release checks

`docs/release-checks.md` is the checklist for a *deployed* build — the things the
automated suites can't see: the OAuth sign-in a real client drives, the manifest
text reaching a client, analytics still firing. Run it against staging (and the
read-only sections against production) after every release, and extend it in the
same commit as any new tool, user-visible behaviour, or shipped bug fix. A new
tool means one more edit that is easy to miss: the tool count and list in its
**C3** check, which nothing lints and nothing executes.

It is written to be **driven by an agent**: ask Claude to run the release checks
and it asks one round of questions, hands you the browser sign-in (the only step
it must not do itself), then runs sections S, P, C, R, W and O unattended and
reports. Two things need a person — the fresh browser authorization, and naming
the store W may write to (a description like "the Shopify store" is enough; the
agent resolves it against your store list).

The connected MCP servers must be named `autods-public-staging` and
`autods-public-prod` — the checklist keys the environment off the alias, so the
tool namespace itself says which environment a call lands in.

Observability (Sentry, Mixpanel, logs) is automated too, given access:
`scripts/fetch_logs.py` reads the deployed server's structured logs out of the
cluster log archive on S3, so verifying the audit trail needs AWS credentials
rather than cluster access.

```bash
uv run python scripts/fetch_logs.py --env staging --since 30m
uv run python scripts/fetch_logs.py --env staging --since 2h --event request --status 500
uv run python scripts/fetch_logs.py --env staging \
  --since 2026-08-27T08:25 --until 2026-08-27T09:00   # reproducible window
```

It prints every structured event by default (`--event all`) and echoes the filters
in force, because the earlier `tool_call` default silently hid `request` lines and
turned a 500-flood check into a false pass. A relative `--since` is resolved at
process start and a wide scan takes minutes, so pass absolute bounds whenever the
window has to be reproducible — for example when joining log lines to a recorded
run. When nothing matches, the footer says whether a filter excluded the events or
the window genuinely held none (the archive lags a few minutes).

Its section **S** (deployment identity + the unauthenticated surface) is
automated as `tests/e2e/test_release_checks_s.py`, which probes a *deployed*
host over the network rather than standing the app up in-process:

```bash
make release-checks                                        # staging

RUN_RELEASE_CHECKS=1 MCP_RELEASE_BASE_URL=https://mcp.autods.com \
  uv run pytest tests/e2e/test_release_checks_s.py -v -rs  # production
```

Every check is read-only and safe anywhere, and none needs credentials — section
S is the unauthenticated surface. Per-environment expectations (Cognito Hosted UI
domain, a registered redirect URI) live in `KNOWN_ENVIRONMENTS` in
`tests/e2e/conftest.py`. Sections C, R, W and O remain manual/agent-driven.
