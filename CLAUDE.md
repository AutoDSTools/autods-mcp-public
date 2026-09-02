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
bare `pip`/`venv`/`python` — and not `uvx` either (see the mcp gotcha).

```bash
make install           # uv sync (installs dev + test groups)
make run               # uv run uvicorn --factory autods_mcp_server.app:create_app --reload
make test              # uv run pytest
make lint              # ruff check . && ruff format --check .
make fmt               # ruff format . && ruff check --fix .
make release-checks    # docs/release-checks.md section S against a deployed host (no credentials)
make release-checks-c  # section C: the handshake payload, diffed against this checkout's manifests
```

`release-checks-c` needs a token (`MCP_TOKEN`, or the `E2E_COGNITO_*` password
grant) and must be run **from the released commit** — the diff has two sides, so
from any other commit it reports the checkout's own manifests as drift.

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
- `manifests/` — `schema.py` Pydantic models, `loader.py` (`load_manifests`,
  `ManifestRegistry`), `instructions.py` (concatenates the per-manifest
  `instructions` and enforces the size cap), and `playbooks.py` (RD-100: the
  multi-tool chains from `manifests/playbooks/`, their registry, their boot
  lints, and every string rendered from them).
- `tools.py` — converts manifest operations to MCP `Tool` descriptors and runs the boot
  lint (D5).
- `dispatch.py` — `OperationDispatcher` resolves the upstream base URL, substitutes path
  params, attaches query/header params + JSON body, forwards the caller's bearer token,
  and returns a `{ operation_id, status, ok, data }` envelope.
- `mcp_transport.py` — builds the runtime and mounts the **stateless** Streamable HTTP
  transport behind the auth dependency; the `call_tool` handler applies rate limiting,
  emits the audit log, branches to the local-handler seam (RD-100), and attaches the
  playbook hints. Also serves the playbook resource mirror (`resources/list`+`read`).
- `errors.py` — MCP tool error construction + upstream error mapping.
- `business_errors.py` — detects a business rejection reported *inside* an HTTP
  200 payload (per-operation config is manifest data) and renders it as the
  `business_error` envelope field.
- `payload_paths.py` — the shared dotted-path-with-`*`-wildcard resolver used to
  address a place inside an upstream payload from manifest data.
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
`products_research`), and `annotations`. The exception is a **locally-handled**
operation (RD-100), which declares `handler` instead of `base_url_key` and carries no
`method`/`path` at all — see **Playbooks** below.

Boot-time lints refuse to start the server, so a malformed manifest can't reach a
client:

- Every operation must have an `annotations.title` **and** at least one hint
  (`readOnlyHint` or `destructiveHint`) — D5.
- Integer enum fields in `body_schema` (e.g. `product_status`, `status`, `region`,
  `site_id`, `buy_site_id`, `inventory_status`) **must** be typed as integers, never
  strings — D5.
- The **concatenated** `instructions` across all manifests must be ≤ 6000 chars
  (RD-90). See the tiers below.
- A `business_errors` block must carry at least one `paths` entry, and the operation's
  `notes` must mention `ok` (RD-90). Both failures are otherwise silent: a block with no
  paths can never match, and a block nobody documented populates a field the model was
  never told to read.
- Exactly one of `handler` / `base_url_key` per operation; a forwarding operation needs
  `method` + `path`; `handler` must name a handler the closed local registry serves
  (RD-100).
- The six playbook lints (RD-100), below.

### Where text goes: the four tiers

Manifest text reaches the model through four channels with very different cost and
reliability, so text is placed by how reliably it must arrive — not by which field is
convenient to edit:

| Tier | Where | What belongs there |
|---|---|---|
| 1 | `inputSchema` (parameter `description`, `enum`) | anything needed to form *this* call — enum values, `min-max` syntax, id formats |
| 2 | tool `description` / `notes` | this tool's observable contract: async-then-poll, response shape, the enums needed to *read* a response |
| 3 | playbook `body` (lazy — fetched by `get_playbook`) | multi-tool chains, state machines, polling cadence, recovery matrices |
| 4 | `instructions` | **index only**: where to start, and the two or three server-wide invariants |

Tiers 1–2 arrive attached to the tool. Tier 4 is the expensive one: `instructions` rides in
the client's system prompt on **every** model turn for the life of the conversation and
sits in the cached prefix, so editing it invalidates prompt caching downstream — and it is
also the *least* reliable, since surfacing it at all is client discretion. Hence the size
cap, and hence the rule that a long enum table belongs on the parameter that takes it, not
here.

When an operation takes **no parameters at all** there is no tier-1 slot, and the tier-2
`notes` is where its enum tables go — not tier 4. `get_user_subscription` (RD-89) is the
reference: the add-on-type and credit-type tables it documents are needed to *read* its
response, not to form the call, so `notes` was always their home; the absence of a
parameter just removes the alternative. `get_categories` (RD-108) is the same shape — the
tree's contract (ids are the unique key, labels are not, a parent covers its subtree) is
all about reading the response and feeding it to *another* tool's filter.

The mirror image is `search_products` (RD-107). Its filter `name` forwards straight
upstream as a field name, so the set of usable filters is unguessable and *nothing but*
tier 1 will do: the whole vocabulary — the field list, the `value_type`/`op` each field
takes, the ships-from country codes, the comma separator for `between`/`in` — lives in
the `filters[]` item schema of its `body_schema`, which `tools.py` emits into
`inputSchema` verbatim. It is deliberately **not** an "Agent Reference" block in
`instructions`, and not a playbook either: a filter vocabulary is what an agent needs to
form *this* call, which is the definition of tier 1. Only the response-side contract
(card-shaped results, no total count, the paging ceiling) is tier 2.

Two conventions that section is written against, both load-bearing. **An enum goes in
the `description` unless the wire contract is genuinely closed** — `value_type` and `op`
carry a JSON `enum` because the upstream accepts exactly those tokens, while sortable
fields and filterable field names are listed in prose, because an `enum` narrower than
the API silently rejects valid calls. And **a filter field is only documentable once it
is confirmed against the upstream mapping**, per the phantom-filter gotcha below.

The server-wide index lives in `manifests/_server.json` — a manifest with no operations,
carrying only the tier-4 block. Everything else is per-domain, and an **empty**
`instructions` is the normal case for a domain whose whole contract is tier 1/2
(`users.json`, `stores.json`). There is deliberately no non-empty lint: requiring text per
manifest would push filler into the most expensive channel.

### Business errors inside a 200

Some upstreams report a business rejection as HTTP 200 with an error code in the payload —
`ok` is `true`, `map_upstream_error` never fires, and an agent that branches on `ok`
reports success for a call that did nothing. An operation declares where to look and what
each code means, as data:

```json
"business_errors": {
  "paths": ["scraper_error.errorCode", "data.*.error.errorCode"],
  "codes": { "PRODUCT_OOS": "Offer is out of stock. Choose a different offer." }
}
```

`paths` are dotted paths **into the upstream payload** (i.e. relative to the envelope's
`data`), where `*` fans out over a list's elements or a dict's values. A match publishes a
`business_error` list of `{code, message}` **beside** `data` — never inside it, so the
upstream payload stays verbatim and `dispatch.py` stays a pure forwarder. An operation
carrying this block must also say in its `notes` that `ok` is transport-level only — the
boot lint above enforces that, and that `paths` is non-empty.

This covers a rejection inside a **2xx** only. A business rejection that arrives as a
non-2xx is not deliverable to the model at all (see the Gotcha below), so don't describe
one in `notes` as something the caller will be able to read.

### Polling conventions for async operations (RD-91)

Several upstream operations accept work and finish it later; the web app watches them
over Pusher/SSE and this server has **no push channel**, so each becomes a polling
loop the agent drives — one tool call per attempt, one model turn per call. The
decision is **thin tools plus documented agent-side polling**: no per-operation
dispatcher timeout, no server-side blocking wait, nothing in `dispatch.py`. The
documentation *is* the feature, which makes an undelivered number the whole bug.

The cadence is stated as numbers, never as "poll periodically" (which produces either
one poll or forty): **first poll ~10s** after the write, **then every ~15s**, ceiling
**10 attempts (~2.5 min)**, then report what is unfinished rather than polling on. The
ceiling is the *attempt count* and the duration is derived from it (10 s + 9 × 15 s), so
the two move together — a second, independent time bound is either wrong or redundant,
and `test_no_channel_ships_a_superseded_ceiling` keeps the retired one out. For
the scraper tools, `full_scrape=true` on the **first attempt only** — which is exactly
what the frontend hook does. The browser's own 3 s / 120 s cadence is retired in
documentation: a tool round-trip is already seconds, so a 3 s loop spends the
conversation re-reading the same unfinished job.

By tier: the numbers belong in the polling tool's `notes` (tier 2) and the playbook
`body` (tier 3), with **one** clause in `instructions` (tier 4) and nothing in tier 1.
`get_bulk_action_items` + `product_import` are the reference implementation; a new
polling tool should read like them, and
`tests/mcp_server/test_polling_conventions.py` asserts the numbers arrive at a client
on all three channels (and that no channel disagrees).

`docs/polling-conventions.md` is the single source for the cadence, the two completion
state machines (bulk action; store quote `new → in_progress → ready → linked` /
`cannot_be_sourced`), the scrapers' three-state read, and the rate-limit arithmetic
behind them. Write against that file rather than re-deriving the numbers.

### Playbooks: multi-tool chains (RD-100)

Some goals need several tools in order, and the chain is invisible from any single tool
descriptor — an agent that stops after step 2 of 3 leaves work that looks done. A
**playbook** is one such chain, declared as data in `manifests/playbooks/*.json` and
loaded by its own loader (`manifests/playbooks.py`). `load_manifests` globs `*.json`
**non-recursively**, so the subdirectory is skipped for free; a sibling
`manifests/playbooks.json` would instead be parsed as a manifest and rejected on the
missing `server_name`.

"Playbook", not "workflow": *workflow* implies the server executes something, and it
collides with MCP's experimental tasks primitive.

**Four things are derived, never authored** — each of them would otherwise drift the
moment a chain is renumbered or an operation joins a second chain:

- `step`/`of` come from list position (the models have no such fields);
- the default successor is the next step — author `then` only at a branch;
- the `operation_id → [step]` index is built by the registry, so one operation can
  participate in several playbooks;
- every string a client sees is rendered from the same file.

**`on_failure` is chain consequence, not an error catalogue.** Transport errors
(`errors.py`), in-payload business errors (`business_errors`) and async end-states
(`notes`) are each already delivered at the moment they happen by machinery that owns
them; restating any of it in a playbook creates a second source that drifts and arrives
at the wrong time. What nothing else owns is "did the write land, and is retrying safe?"
— which is why the fields are only ones that hold regardless of *which* error arrived
(`idempotent`, `left_behind`, `verify_with`, `then`, `ask_user`). Don't add a per-step
list of possible errors. `ask_user` is a rare flag, not a default: hosts already gate a
`destructiveHint` tool behind their own prompt, so reserve it for steps where the *retry*
is the risk.

Six boot lints, all fatal, all with a rejection test in `tests/mcp_server/test_playbooks.py`:

1. every `operation_id` / `entry_operation` / `requires[].from_operation` / `then[]` /
   `on_failure.verify_with.operation_id` resolves to a registered operation;
2. `requires[].param` resolves to a declared parameter or `body_schema` property of its
   own step's operation (which is why `requires` is structured, not prose — prose can't
   be linted);
3. the chain's own graph: playbook names unique; no operation twice within one playbook
   (steps are addressed by `operation_id`, so a repeat has no identity to point at — it
   belongs in a second playbook or behind a schema change, not in the same list);
   **`entry_operation` and every `then[]` entry name a step of this playbook**; every
   step reachable from the entry;
4. a non-final step whose operation is `destructiveHint: true` must carry
   `incomplete_alone`;
5. a `destructiveHint` step with `on_failure.idempotent: false` must declare
   `verify_with`, and that operation must be `readOnlyHint: true`. Note the *obvious*
   verification tool is often the wrong one: polling a bulk job needs an id a failed
   write never returned, so verification goes through a list/read operation;
6. every rendered string fits its channel — `body` ≤ 6000, envelope hint ≤ 200
   (serialized, compact), failure tail ≤ 320, description tail ≤ 120. The specific
   rendering is checked per file; the description tail and the merged failure tail are
   checked over the *merged index*, since an operation joining a second chain changes
   them without either file mentioning the other. There is deliberately no merged
   envelope-hint check: the tail carries the same names under a tighter cap, so it always
   overruns first (pinned by `test_the_tail_cap_subsumes_the_merged_hint_cap`).

`then[]` appears in both lint 1 and lint 3 on purpose, because the references a playbook
makes do not share one domain. `requires[].from_operation` and `on_failure.verify_with`
point *outside* the chain by design — the pilot's step 1 takes `store_ids` from
`list_stores_api`, which everything uses and which is deliberately not a step — so all
that can be asked of them is that the operation is registered (lint 1). `entry_operation`
and `then[]` must resolve *within* the playbook, and lint 3 is where that is enforced.
A `then` naming a registered non-step used to pass everything: the step stopped counting
as final, so the client got a hint whose `next` recommended a tool the chain never
contains (a destructive publish, in the case that surfaced it) and which read "step 3 of
3, next: …". Only one shape was caught, by accident — a dangling successor that *replaces*
a valid one orphans the chain and trips reachability, while one added *beside* a valid one
was invisible.

### An operation in several chains gets a vaguer hint, never a guessed one

One operation can be a step of several playbooks — `upload_products` is step 1 of the
plain import and a middle step of the sourcing chain. Nothing in a request says which
chain the caller is following, and the transport is stateless, so there is no session
that could have remembered. Both result-carried hints therefore give the specific step
**only** when the operation belongs to exactly one chain:

- **success hint** — with several candidates it drops to `{in: [names],
  step_depends_on_chain: true, runbook: …}` and asserts nothing else. Only chains with
  work left are named.
- **failure tail** — merged rather than dropped, because it is the half that prevents a
  duplicated write. Every clause that holds in *all* candidates survives (and the
  cautious reading wins: "not idempotent" if any chain says so, `ask_user` if any chain
  sets it); `then` is chain-scoped and becomes a `get_playbook` pointer. A candidate that
  declares no `on_failure` contributes nothing — a missing declaration is not evidence
  that retrying is safe — but is still named.

Taking `steps_for(op)[0]` and stating its step number and `incomplete_alone` as fact is
the tempting alternative and it is a confident lie: an agent in the sourcing chain would
be told "nothing is in the store yet" when the truth for its chain is "listed at cost
with no supplier attached" — the exact outcome playbooks exist to prevent. A pointer
costs one extra call.

**Delivery is split across three channels by when the text is needed**, and that split
is the design, not an optimisation:

| Channel | When it arrives | What it carries |
|---|---|---|
| `get_playbook` tool | on request | the whole runbook — zero tokens until an agent enters the flow |
| result envelope / `isError` text | per call | the per-step nudge, in context right after step N |
| tool `description` tail | every turn | one bounded pointer, ≤ 120 chars, no step bodies |
| `instructions` | every turn | one generated index line per chain |

`get_playbook` is a **tool**, not only a resource: tools are the one primitive every host
exposes to the model, and its `name` **enum is the index** — `inputSchema` is the most
reliably delivered channel there is, so the list of chains reaches the model even in a
client that drops `instructions` entirely. Playbooks are *also* mirrored as
`autods://playbook/<name>` resources (`text/markdown`); that mirror is what declares the
`resources` capability, so RD-92 registers more URIs rather than declaring it again.

### The local-handler seam

`get_playbook` is the first operation this server answers itself. An operation declares
`handler: "playbook"` instead of `base_url_key`; `call_tool` branches to a small **closed**
handler registry *after* the rate limiter, the analytics event and argument validation, and
*before* `dispatch` — so a local operation is metered, tracked, validated and audited
exactly like a forwarded one, and returns the same `{operation_id, status, ok, data}`
envelope. Keep it that way: moving the branch earlier would create an unmetered path, and a
bare markdown `TextContent` instead of the envelope would make a local tool observably
different from every other tool for no gain (a model reads `\n`-escaped markdown fine).

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

The `__version__` lives in `src/autods_mcp_server/__init__.py` and is the only
place it is edited: `pyproject.toml` declares `dynamic = ["version"]` and
hatchling reads that literal (`[tool.hatch.version]`), so the package metadata,
the FastAPI `version=`, and the Sentry release tag all move together. Don't add
a `version =` back to `pyproject.toml` — that's what drifted before.

Bump it on every commit:

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
| adds or removes a manifest **operation** | all three hand-maintained tool inventories — `tests/mcp_server/test_loader.py`, `tests/mcp_server/test_transport.py`, and `tests/e2e/test_staging_smoke.py` (opt-in, so nothing fails if you skip it) — see the `operations_count` gotcha. **C3** of `docs/release-checks.md` needs no edit: `tests/e2e/test_release_checks_c.py` derives that set from the manifests |
| changes a command, workflow, or convention (lint/test/run, commit format, Python rules) | the corresponding section here |
| fixes a bug or incident whose root cause was non-obvious, or adds a guard/workaround that looks removable but isn't | a **Gotchas & hard-won lessons** bullet here (and a **Troubleshooting** entry in `README.md` if an operator/client would hit the symptom) |
| adds a tool, changes what a client observably gets back, or fixes a bug that reached a released build | a check in `docs/release-checks.md` (the post-release agent-driven checklist) phrased as the symptom a *user* would see |
| adds a tool that has to be polled, or changes the cadence / a completion state machine | `docs/polling-conventions.md` (the numbers live there **once**), plus the `notes` and playbook `body` that state them, plus the delivery test |
| adds a fixture the checklist needs (a store, an entitlement, a supplier id) or a step that makes an agent stop and ask mid-run | a check in section `P` of `docs/release-checks.md`, or a rule that lets the run continue with a `skipped` |
| adds an upstream, a stateful dependency, a deployed process or an exposed API surface — or changes deployment / ownership metadata | `catalog-info.yaml`, per the trigger list in **Service Descriptor** below (most changes are *not* triggers — read the list, don't guess) |

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

## Service Descriptor (`catalog-info.yaml`)

Backstage is the org's software catalog and developer portal, at
[backstage.autods.com](https://backstage.autods.com). It builds this service's entity —
[`component:autods-mcp-public`](https://backstage.autods.com/catalog/default/component/autods-mcp-public):
its owner, its links, its Kubernetes tab, and every edge to the services and
infrastructure it reaches — from `catalog-info.yaml` at this repo root, and from nothing
else. That file is the whole of the descriptor; discovery addresses only that path.
Nothing breaks at runtime when it drifts. The graph just quietly starts lying.

- The conventions it follows are org-wide and live in
  [`AutoDSTools/backstage` → `CONVENTIONS.md`](https://github.com/AutoDSTools/backstage/blob/main/CONVENTIONS.md);
  the `§` numbers in the descriptor's comments point there. Read the rule at the source
  rather than paraphrasing it into this repo — a local copy drifts from the original and
  doubles the work of changing it.
- The file's header comments carry the current model and the reasoning behind each
  boundary, **including what is deliberately absent** — no System, no API entity for the
  MCP surface, no `kind: Resource` of our own. Read them before editing: they say *what*
  to edit, the triggers below say *when*.
- Update the descriptor as part of the same change that invalidates it, never left to a
  follow-up.
- Declare only what this repository owns. Other AutoDS services and shared infrastructure
  — Cognito, the Redis cluster, every upstream this server forwards to — are declared
  once, centrally, in
  [`catalog/shared-infrastructure.yaml`](https://github.com/AutoDSTools/backstage/blob/main/catalog/shared-infrastructure.yaml)
  and
  [`catalog/pending-services.yaml`](https://github.com/AutoDSTools/backstage/blob/main/catalog/pending-services.yaml),
  or in the owning repository's own descriptor. Entity names are globally unique, so a
  second declaration produces a `conflicting entityRef` and one copy is silently ignored.
  Third-party APIs are the one exception, namespaced per caller (§ 2).
- The descriptor describes the service, not its deployment (§ 5). Hostnames and ports,
  env-var and helm-values names, URL paths, replica counts, cron schedules, logical
  database numbers, and anything that differs between staging and prod stay out of it —
  out of the descriptions *and* out of the comments. Each of those is owned by a file that
  changes on its own schedule, so a copy in the descriptor rots and nothing reports it.
  That detail belongs here, in `README.md`, or in the values files that own it.
- **Two branch facts, both counter-intuitive here.** Backstage reads `main` or `master`
  and never a development trunk (§ 10), while this repository's default branch is
  `develop`. So (a) `backstage.io/source-location` names `master` — do not "correct" it to
  the default branch; and (b) a descriptor edit reaches the catalog only once a release
  carries it to `master`, so the entity page keeps showing the old model until then. That
  lag is not a broken descriptor.

### When an update is required

- **A new upstream service.** A new `base_url_key` in `settings.py`, with the manifests
  that forward to it — a second AutoDS service this server calls. It gets a `consumesApis`
  entry for that service's API entity and a `dependsOn` entry for its Component. A new
  *operation* against an upstream already declared is **not** a trigger, however many you
  add and however new the manifest file holding them is: the edge already exists, and the
  graph models services, not tools. The boundary both ways — the upstream is the trigger,
  the operation on it is not.
- **A new stateful dependency.** A store, cache, broker or identity provider this server
  opens a connection to. Resolve which kind it is before declaring anything: *shared* goes
  in the central file under devops and is referenced from `dependsOn`; *owned outright by
  this service* is a `kind: Resource` here. A hostname in the deploy repo's values proves
  nothing until you resolve it and grep the other repos' `values-prod.yaml` for the same
  address — every store this server reaches turned out to be shared, and descriptors
  across the org got this wrong in the same direction. The boundary both ways: the Redis
  *cluster* is a dependency; the logical database, key prefix or bucket path this server
  holds on it is not, and never appears in the file at all (§ 3, § 5). A `pyproject.toml`
  package is not a dependency either.
- **A new deployed process.** A second entrypoint with its own Deployment, CronJob or helm
  release in `AutoDSTools/autods-mcp-deploy`. The test is failure isolation: can it fail,
  scale or roll back on its own? Then it earns a Component of its own — and a second
  Component means the descriptor also needs the System that § 4 has so far said no to. The
  nginx sidecar does not qualify: it is a second container in the same pod. Neither does
  new code inside the process that already runs — a route, a middleware, a background task.
- **A new API surface exposed.** A second protocol or server process that others call.
  That gets a `kind: API` in `providesApis`, and it reopens the "no API entity" decision
  recorded in the descriptor's header. Adding a tool, a playbook, a resource URI or an
  OAuth discovery route does not: all of them are the same surface the header already
  reasons about.
- **A direct call to a third-party vendor** — code here that calls a vendor's own HTTP API,
  as distinct from forwarding a caller's token to an AutoDS upstream. That gets its own
  `kind: API` entity named `autods-mcp-public-<vendor>-api` plus a `consumesApis` entry
  (§ 2: the prefix is per calling repository, and another repo's unprefixed vendor entity
  is theirs, never something to reference). `autods-mcp-public-mixpanel-api` is the worked
  example in the descriptor — copy its shape, including the stub `spec.definition`.
  Self-hosted Sentry does *not* qualify: it is our own infrastructure, not a vendor API,
  and the descriptor says why.
- **Deployment or ownership metadata changes.** A changed namespace, a chart renamed in
  `autods-mcp-deploy`, a different Sentry project, a change of owning squad → the
  `backstage.io/*` and `sentry.io/*` annotations and `spec.owner`. Note
  `backstage.io/kubernetes-id` is the *chart* name (`autods-mcp`), not the entity name.
- **Not triggers, and churn is as harmful as drift** — editing the descriptor for a change
  that does not alter the graph teaches reviewers to skim it: a new manifest operation,
  manifest file or playbook against an upstream already declared; tool `description` /
  `notes` / `instructions` text; a new env var or setting; a `pyproject.toml` / `uv.lock`
  bump; replica counts, HPA bounds or resource limits; a Redis key prefix or logical
  database change; a new test; the version bump every commit carries. The descriptor models
  none of them.

### Easy to get wrong

- **Nothing in this repository validates the descriptor — not even its syntax.** ruff is
  Python-only, pytest never opens the file, and the manifest boot lints do not know it
  exists. So resolve every entity name in it against its source, as a step rather than a
  caution: the catalog files in `AutoDSTools/backstage`, or the live instance through the
  Backstage MCP server — never from memory. Report the result on the pull request (§ 9).
  That same lookup answers § 3's owned-vs-shared question before a Resource is declared
  twice under two names.
- **Dangling references never fail.** Ingestion succeeds, the entity materialises, and the
  edge draws to nothing behind a soft warning, so a typo or a name nobody declares survives
  indefinitely. This is exactly how the descriptor went the whole life of the entity with
  the ProductsResearch edge missing and a comment promising to add it, with nothing
  anywhere reporting either (RD-111).
- **A kind outside the allow-list is discarded in silence** (§ 10) — no error in the pull
  request, in the file or on the entity page. Check the instance's `catalog.rules` before
  introducing a kind this catalog does not already use.
- **The entity name is not the chart name and must not be "fixed" to match.** The entity is
  `autods-mcp-public`; the chart, the image and every workload it renders are `autods-mcp`.
  Renaming the entity breaks every consumer's edge silently.
- **The org code-review checklist reads `AGENTS.md` § "Service Descriptor".** This
  repository has no `AGENTS.md` and carries that section here instead — point a reviewer at
  this section when the checklist comes up empty, rather than concluding no rule applies.
- A broken descriptor surfaces on Backstage's next refresh, under **Settings → Locations**.
  Discovery runs roughly every half hour, so it is neither immediate nor precisely
  predictable — and for this repository it only runs against `master` at all.

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
  fails open on Redis outage, falls back to in-process locally. **The buckets stay
  shared across all operations** (RD-91): a `readOnlyHint` exemption was considered and
  rejected, because polls are precisely the calls a runaway loop makes, so exempting
  them removes the only limit that would ever notice. Reviewed against the documented
  cadence — one flow draws 4 polls/min, ~12–14 calls end to end, so 60/min binds at
  ~10 concurrent flows and 1000/hour at ~70 flows — see `docs/polling-conventions.md`.
  Revisit only on a real `error_type=rate_limited` for a legitimate session.
- **Manifest text is tiered by required reliability, not by convenience** (RD-90): tier 1
  `inputSchema` → tier 2 `description`/`notes` → tier 3 playbook → tier 4 `instructions`
  (see **Tools are data**). `instructions` is an index with a hard 6000-char boot lint
  because it rides the client's system prompt on every turn and sits in the cached prefix;
  moving a long enum table back into it is a regression even though nothing about the call
  breaks. Empty per-manifest `instructions` is legitimate — don't add a non-empty lint.
- **Transport is stateless** (`stateless=True`) by design — production runs many
  replicas × workers, so no MCP session is pinned to a worker. Don't reintroduce
  session state. This is also why the playbook hint has no "show it once" dedup: there
  is no session to remember in. A Redis "once per user per hour" gate is possible and
  is deliberately not worth it — if a hint needs deduping, it is too long.
- **A playbook hint rides beside `data`, never inside it** (RD-100) — the same rule as
  `business_error`, for the same reason: `data` is the upstream payload verbatim and
  `dispatch.py` stays a pure forwarder. The hint is emitted only on a successful call of
  a non-final chain step, so a non-playbook envelope is byte-identical to pre-RD-100
  (`test_success_result_shape_matches_the_1x_wire_format` pins that).
- **Both hint renderers take *every* candidate step, and never the first one** — they are
  handed `playbooks.steps_for(name)` whole, and decide what is honest to say for the set
  (see **An operation in several chains…** above). `steps_for(name)[0]` looks equivalent
  while there is one playbook and starts lying the day a second one shares an operation,
  which is the day it matters. Don't narrow the call site back to a single ref.
- **The success path and the failure path are different code paths, and the failure one
  is the half that pays off** (RD-100). `error_result` returns a flat `TextContent` with
  no `data` and no `structuredContent`, so `on_failure` guidance can only be *appended to
  the error text* — it cannot ride on the dict. It is attached on the two ambiguous
  failures (upstream unreachable, mapped upstream error) and nowhere else: a rate-limit or
  `invalid_arguments` rejection sent nothing upstream, so there is no chain consequence to
  report. A step with no `on_failure` produces an error byte-identical to today's.
- **The failure tail's budget (320) is larger than the envelope hint's (200) on purpose**
  (RD-100). The envelope hint is serialized twice per call and repeats on *every* poll of
  a polling step; the failure tail fires once per failure, rides in `content` only, and is
  what prevents a duplicated write — so it can afford to name the verification tool and
  how to use it. Both are boot lints, not runtime truncation: half a sentence about a
  duplicated write is worse than a deploy that refuses to start.
- **OpenTelemetry stays inert** (RD-99): mcp 2.x hard-depends on `opentelemetry-api`
  and instruments its request path, but with no `opentelemetry-sdk` installed the API
  hands back non-recording spans — nothing is collected or exported, and Sentry remains
  the only tracing surface. That was a deliberate choice, not an oversight: adding the
  SDK is a real decision (exporter, endpoint, sampling) and carries the same
  never-let-the-bearer-token-out rule as the Sentry scopes. Don't install
  `opentelemetry-sdk` casually — installing it alone silently turns tracing on.
- **Sentry** (`sentry.py`, self-hosted `sentry.autods.com`): no-op unless `SENTRY_URL`
  is set (so local/test send nothing); the release comes from `__version__`, the
  environment tag defaults to `MCP_ENV`. Handled failures are returned as
  `CallToolResult(is_error=True)` envelopes, so they're captured **explicitly** —
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
- **Two upstream red herrings when verifying a filter enum against AutoDSApi.** (a) The
  `value_type` in `api/resource/products/schema.py:products_schema_fields` is **not** the
  contract — that tuple only feeds the `OneOf` allowlist for a filter's `name`. The cast
  comes from the `value_type` *the caller sends* (`helper/convert_queries.py:convert_value`),
  so what a filter value must be is decided by the **stored field type** in
  `dal/model/item.py`. `variations.active_buy_item.region` is annotated `string` there and
  is an `IntEnumField` in Mongo — it takes the integer. (b) `EnumField(Region,
  use_name=True, …)` does **not** mean "send the name": `use_name` is popped into
  `metadata` for the API docs, and the deserializer stays value-based unless
  `serialize_with_name=True` is passed (`helper/custom_webargs_fields.py`). `region`,
  `status` and `buy_site_id` on the upload body really are integers, which is what the
  integer-enum boot lint asserts. Check the model field, not the schema annotation.
- **Filter *fields* go phantom the same way tools do** (RD-90). `products.json` documented
  a top-level `region` filter; `products_schema_fields` has no such entry — the item's
  region is only `variations.active_buy_item.region`. Nothing catches this: the phantom-tool
  test can only see tool-shaped tokens, and a bad filter `name` fails upstream with a
  validation error the model can't act on. When you add a filterable field to manifest
  text, confirm it against that tuple.
- **A parsed manifest field that nothing reads is invisible, not harmless** (RD-90).
  `Manifest.instructions` was parsed from day one and never passed to
  `Server(instructions=…)`, so for the whole life of the server every hand-written enum
  table, filter rule and "see the … table in server instructions" pointer reached exactly
  nobody — and drifted freely, ending up documenting an `update_product` PUT workflow for
  a tool that was never registered. Nothing failed, no test noticed, and the text looked
  authoritative in review. When you add a manifest field, add the test that asserts it
  arrives at a *client*, not just that it parses; `uv run python scripts/mcp_call.py
  instructions` prints what a real handshake actually carried.
- **A business rejection that arrives as a non-2xx cannot reach the model, however the
  manifest describes it** (RD-90). `map_upstream_error` deliberately hands the client a
  generic typed error and keeps the upstream detail server-side (`log_full`) — and for a
  3xx the message is only "unexpected redirect", because `follow_redirects=False` means a
  3xx signals a misconfigured upstream far more often than a business answer. So
  `get_product_by_id`'s `notes` calling its 307 "a documented business response" promised
  the model text it never receives: the caller sees an opaque `upstream_error` and reports
  a fault instead of a missing add-on. `business_errors` closes this only for a 200. When
  an upstream signals entitlement or policy through a status code, the *observable*
  behaviour (which typed error arrives, and what it actually means) is what the `notes`
  must describe — not the upstream's intent. Don't widen the error mapping to echo 3xx/4xx
  bodies instead: those carry internal hostnames and are sanitized for that reason.
- **The scrapers' error path is snake_case on the wire, and a mis-cased
  `business_errors` path never matches** (RD-91). `ScraperErrorAPI` is
  `error_code` / `error_msg` / `retries`; the `scraper_error.errorCode` spelling in the
  illustrative block above (and in `README.md`) is the *frontend's* camelised view — its
  request helper renames the keys. A path that doesn't match produces no error anywhere:
  the boot lint only checks that `paths` is non-empty, so the operation ships looking
  protected and the `business_error` field simply never appears. Confirm the path against
  a live response. Same class of trap in the two scan endpoints: `/offers/scan` reports a
  freshly queued id in `not_in_db`, `/products/scan` files it under `no_info` and leaves
  `not_in_db` empty — documenting one shape for both makes "queued" unobservable for
  products. Details in `docs/polling-conventions.md`.
- **A failed async sourcing request has no error status — the record disappears**
  (RD-91). `alibaba_1688_request` rolls back by *deleting* the store quote and reports
  the error only over SSE, which this server cannot see. So a poller must treat "the
  quote that was there is gone" (or never appeared) as failure; waiting for
  `cannot_be_sourced` waits forever. Any tool that documents that chain has to say so —
  and note `{"status": "ok"}` from the trigger is the task being *queued*, nothing more.
- **Registering `on_list_resources` is what declares the `resources` capability** —
  `Server.get_capabilities` derives the whole capability block from which handlers exist,
  so adding a resource handler changes the handshake for every client, not just the ones
  that ask for a resource. That is fine and intended here (RD-100 landed the capability;
  RD-92 adds URIs), but it means you cannot add a resource handler "just to try it" on a
  branch that ships. Relatedly, `ReadResourceResult` contents need an explicit
  `mime_type`: a bare string advertises `text/plain` and the markdown is lost.
- **A locally-handled operation must not inherit the manifest-level `base_url_key`**
  (RD-100). The registry resolves the manifest default onto every operation that doesn't
  set one; left unguarded, a `handler` operation would silently acquire an upstream and
  the "exactly one of the two" lint could never fire. The `if operation.handler is None`
  guard in `loader.py` is what keeps that lint meaningful — it looks removable and isn't.
- **`operations_count` in a manifest is cosmetic** — the model uses `extra="ignore"` and
  drops it; it's never validated and silently drifts. The real count guarantee is the
  hand-maintained tool inventories, and there are **three** of them —
  adding or removing an operation means editing all three in the same commit:
  `tests/mcp_server/test_loader.py` (operation count), `tests/mcp_server/test_transport.py`
  (advertised-tool count), and `tests/e2e/test_staging_smoke.py` (`AUTODS_OPS` /
  `PRODUCTS_RESEARCH_OPS` / `LOCAL_OPS`, which `test_tools_list_exposes_all_registered_ops`
  asserts `tools/list` equals **exactly**). Only the first two run in CI. The e2e file is
  opt-in (`RUN_STAGING_E2E=1`), so forgetting it fails nothing locally and the staleness
  only surfaces the next time someone runs the staging smoke — which is how it silently
  missed both `get_current_user` (RD-68) and `get_playbook` (RD-100) and sat broken until
  RD-89. Grep for the previous tool count before assuming you've found every site.
  There used to be a **fourth**, and it was the worst: the tool count + list written out
  in **C3** of `docs/release-checks.md`, which nothing executed at all, so a stale count
  surfaced only when a human or an agent ran the checklist and then had to work out which
  side was wrong. RD-89 updated all three test inventories and still left C3 reading
  "exactly the **12** tools" against a 13-tool handshake — which is how a run gets told a
  healthy release is broken. `tests/e2e/test_release_checks_c.py` now *derives* that set
  from the manifests and diffs it against the live handshake, so C3 has no list to keep in
  step and this class of staleness cannot come back. Don't reintroduce a literal count
  there.
- **New read tools must mirror `products_research.json` conventions**, which are
  load-bearing, not stylistic: enum-valued query params list allowed values in the
  `description` (not a JSON `enum`), `"min-max"` range filters are typed `str`, and
  `product_id`/`internal_id` stay distinct params. Verify enum value sets and example
  ranges against *live* upstream data — an enum narrower than the API silently rejects
  valid calls (e.g. a percentage field mistakenly documented as a 0–1 fraction).
- **`search_products` answers four different malformed calls badly, each in its own way —
  two 500s, a 400 and an empty 200 — and one of them is the call our own text used to ask
  for** (RD-107). Omitting `filters` entirely **500s** (`filters: []` is fine) — and the
  manifest said "Omit for an unfiltered listing", so the first call an exploring agent
  makes was the one that failed. `between` with the `min-max` dash form that
  `get_winning_products` documents **500s** too (the range operand is unpacked from a bare
  comma split upstream), so the two tools' range syntaxes are *not* interchangeable.
  `order_by.name` accepts any string and then rejects a text field with a **400** — which
  reaches the client as `upstream_client_error`, not `upstream_error`, so the two shapes
  are diagnosed differently; sortable is `created_at`, `product_details.min_price`,
  `product_details.min_shipping_time`, `spv_param`, `view_count`. And `condition` was
  advertised for years while the search path never read it: every filter is ANDed, so
  `"or"` came back **200 with zero results** for two mutually exclusive terms — no error at
  all, which is why it is gone from the `body_schema` rather than documented.
- **A 500 the upstream owns is still ours to keep an agent away from, and the schema is
  the lever** (RD-107). The upstream status codes belong to another repository and another
  team's release, so waiting for them leaves the bad shape reachable in the meantime.
  Where a malformed body is *statically* recognisable, make the `body_schema` refuse it:
  `filters` is `required` on `search_products`, so the omitted form is a typed
  `invalid_arguments` naming the field, from the boot-compiled validator, before anything
  is sent — which is strictly better for the agent than an opaque `upstream_error` it
  cannot act on. The `maximum` on `offset` (9999) is the same lever on a 400 rather than a
  500, and it changes what the tool can answer: an `offset` past the 10,000-result window
  is now `invalid_arguments`, so the only ceiling behaviour still reachable through this
  tool is the *silent short page* below. Say that in one place — a description that
  promises an upstream refusal the schema now intercepts is worse than no description.
  Two boundaries on this. It only works for what a schema can express: a
  wrong-shaped *value* like the dash-form `between` operand is documented on the
  parameter, not linted. And the guard must not be widened past the failure — a
  `minItems: 1` on `filters` would also refuse `filters: []`, which is the documented
  unfiltered listing and works perfectly well; `test_search_products_without_filters_is_refused_locally`
  pins both halves for that reason.
- **Filtering and reading are different field sets on `search_products`** (RD-107). The
  response is card-shaped — 12 fields — while the filterable surface is the whole indexed
  document, so `rating`, `description`, `brand`, `url` and the per-variation details can
  be filtered on and are never returned; `get_product_by_id` is the follow-up. Two
  consequences that look like bugs and aren't: `rating`/`rating_count` are unset on every
  Marketplace product, so the UI's "best sellers" combination plus `site_name =
  private_suppliers` matches nothing at all; and paging ends at 10,000 results, where the
  boundary page comes back **silently short** rather than empty — a short page is exactly
  how an agent infers "end of results", so it concludes the catalog is exhausted instead
  of reporting the ceiling. `projection` is not a way out: the frontend sends it, no
  request schema accepts it, and it is dropped at parse time — don't add it to the
  manifest.
- **The category tree hides two traps, and neither produces an error** (RD-108). (1) Its
  **labels repeat** — `Clothing`, `Pants`, `Shorts`, `Shoes` and `Accessories` each exist
  under `Women`, `Men`, `Girls` *and* `Boys`, and `Mirrors` under two unrelated parents —
  so a caller resolving a category by label match picks one of several unrelated subtrees
  and gets a confident wrong answer. Only `value` is unique; position in the tree is what
  disambiguates a label. That is why the `notes` say so twice, and why the phrasing is
  asserted by a test rather than left to review. (2) Upstream fetches the tree in **one
  page of 1000 documents with no scroll**, and rebuilds it from `parent_id is None`
  downwards — so past that cap a node whose parent fell outside it vanishes *with its
  whole subtree*, silently, with a 200 and a well-formed response. The measured tree is
  **270 nodes**, and that margin is the only reason this tool is trustworthy; if it ever
  approaches 1000, the upstream fetch has to be fixed **before** anything here changes.
- **`/subscriptions/user-subscription/` renders three enums three different ways, and
  only a live call tells you which** (RD-89). In one response body: `addon_type` is a
  name (`"product_hub"`), add-on and package `status` is an **integer** (`1` active, `2`
  canceled, `3` expired), and `credit_type` is a numeric code delivered as a **string**
  (`"1"`/`"2"`/`"3"`). The string is explicable from the source — the repo builds the
  entry with `CreditType.<x>.value` (an int) into a field declared `fields.String`, so
  marshmallow stringifies it — and it breaks the server-wide "enums are integers"
  invariant, so an agent matching on `1` finds nothing. `status` is the one that caught
  us out: with the versions AutoDSApi currently pins, `marshmallow_sqlalchemy` maps a
  `Column(Enum(IntEnum))` to `fields.Enum`, which dumps **by name** — so reading the
  schema predicts `"canceled"` while staging actually returns `2`. Deployed AutoDSApi is
  evidently on an older mapping, which also means the rendering can flip on an
  AutoDSApi dependency bump with nothing here failing. Hence the tool's `notes` document
  the numeric codes *and* tell the caller to accept the name form, and release-check P4
  asks for the raw pairs and flags a name as a finding. Verify a response-side enum
  against a live call, never against the schema — same lesson as the `EnumField(...,
  use_name=True)` red herring above.
- **An absent credit entry is not a zero balance** (RD-89). The auto-order entry is
  appended only under `if autods_user.auto_order_credits:` — falsy — and the attribute
  defaults to `None` and is *left* at `None` when the balance source raises inside its
  5 s timeout. So a **zero** balance and an **unavailable** balance both render as a
  missing entry, indistinguishably; the staging account that verified RD-89 has
  `orders_processor` active and no `"1"` entry at all, so this is the common case, not
  the edge. Reading "no entry" as "0 credits" reports a confirmed empty wallet for what
  may be a timed-out upstream — the `notes` and release-check P8 both say "no spendable
  balance established" instead. Same class of trap as the mis-cased `business_errors`
  path: nothing fails, the field is just quietly absent.
- **The dispatcher is a pure forwarder** — `dispatch._parse_response` returns
  `response.json()` verbatim. You cannot trim or reshape a response via manifest text; that
  needs an upstream change. Don't add per-operation response logic — it breaks "tools are
  data".
- **`uvicorn.access`, `httpx`, and `mcp` INFO lines duplicate our structured
  `request`/`tool_call` logs**, so `configure_logging` raises them to WARNING — guarded so
  `LOG_LEVEL=debug` still gets the firehose. Don't undo it; emit the audit line, not the
  library's.
- **The server runs on mcp 2.x, and `uvx` ignores the pin entirely.** `uvx --with mcp
  python …` resolves whatever is newest regardless of `uv.lock` (and picks Python 3.13
  for a 3.12-only project) — always `uv run`. The 1.x → 2.x port landed in RD-99; the
  three v2 behaviours that bite silently, with nothing raising anywhere, are below.
- **v2 `model_dump()` emits snake_case, so serializing an MCP type ourselves needs
  `by_alias=True`.** In 1.x the model fields *were* the wire names (`inputSchema`,
  `isError`), so `model_dump()` happened to produce wire format. In 2.x the fields are
  snake_case with camelCase aliases, so the same call yields `input_schema` — a
  silently wrong shape, not an error. The SDK's own outbound path dumps `by_alias=True,
  mode="json"`; anywhere we dump an MCP model by hand (`scripts/mcp_call.py`, tests)
  must do the same. Constructor kwargs still accept either spelling, so
  `types.Tool(inputSchema=…)` keeps working and hides the switch.
- **The success-path `CallToolResult` is now built by hand, and its shape is a
  contract.** Through 1.x the `call_tool` decorator wrapped a returned dict into
  `structuredContent` + one `text` block of `json.dumps(payload, indent=2)`; v2 removed
  that wrapping. `_success_result` in `mcp_transport.py` reproduces it exactly, and
  `test_success_result_shape_matches_the_1x_wire_format` pins the bytes (the literal
  was captured from a 1.29.0 run). A "cleanup" here — a compact `json.dumps`, a dropped
  `structuredContent` — changes every successful response with no test-free signal.
- **v2 does not turn handler exceptions into `isError` results.** 1.x's decorator
  caught everything and returned `CallToolResult(isError=True)`, so the model saw the
  error and could self-correct; v2 lets it escape as a top-level JSON-RPC error the LLM
  never sees. `on_call_tool` therefore ends in a deliberate catch-all that returns the
  typed `internal_error` envelope (and emits the audit line if nothing else did). Don't
  remove it as dead code — it only fires on paths no `except` clause names. Relatedly,
  v2 does no argument validation at all (`validate_input` is gone), so
  `_build_validators` / `_validate_arguments` are the only schema gate left.
- **`httpx` and `httpx2` are not interchangeable, and mixing them fails quietly.** The
  SDK's *client* transports run on httpx2 since 2.x: passing an `httpx.AsyncClient` as
  `streamable_http_client(http_client=…)` degrades silently rather than raising, and an
  `except httpx.ConnectError:` around an SDK call still imports fine while never
  matching again. Only the MCP client moved (tests, `scripts/mcp_call.py`); the
  upstream dispatcher and its `pytest-httpx` mocks stay on `httpx`. Note httpx2 uses the
  OS trust store via `truststore`, not certifi — irrelevant in prod (the server never
  uses the SDK's HTTP client) but it can bite in a minimal container image;
  `SSL_CERT_FILE` / `SSL_CERT_DIR` are honoured first.
- **Extra keys on an MCP model are dropped without a word (and `_meta` is the field
  that used to be the trap).** Under 1.x the models were `extra="allow"` without
  `populate_by_name`, so `types.Tool(meta={...})` silently created a junk extra field
  serialised as `"meta"` and the client never saw the metadata; the fix was
  `types.Tool(**{"_meta": {...}})`. mcp 2.x sets `populate_by_name=True`, so `meta=` now
  populates the real field — but `extra="allow"` is gone, so any *other* non-schema key
  you stuff into an MCP model is accepted at construction and then discarded. Same
  failure shape as before (no error, no data), different cause: relevant to the RD-82 /
  RD-97 widget probes, which rode extra fields.
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
