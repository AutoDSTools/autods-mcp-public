# Post-release checks

Instructions for **Claude** (or a human) to verify a released build of the AutoDS
public MCP server still does the things users connect it for: authorize, see their
stores, list and upload products, search the research catalog, track a bulk job.

Written to be driven by an agent: one hand-off for the browser sign-in, one round of
questions, then an unattended run ending in a report. See *How to run this*.

The unit suite covers the code in isolation and `tests/e2e/` covers the transport
with a password-grant token. Neither covers what breaks *after* a release: a stale
image, a manifest that never reached the client, an upstream that moved, an OAuth
redirect that no longer matches, analytics that silently stopped firing. That's
what this file is for.

## How to run this

A run is designed to be **one human hand-off, then unattended**. The user says
"run the release checks against staging"; Claude asks a single round of questions,
the user does the one thing only a human can do (authorize a browser sign-in), and
Claude drives everything else to a report without asking again.

| | |
|---|---|
| Staging | `https://mcp-staging.autods.com/mcp` |
| Production | `https://mcp.autods.com/mcp` |

### Who does what

| | Human | Claude |
|---|---|---|
| **C1** — fresh browser authorization | ✅ only they can | ❌ never |
| **W** — go-ahead + *which* store | ✅ describes it | ❌ never chooses one |
| Everything else — S, P, C2–C5, R, W execution, O, the report | | ✅ |

Only those two need a person. Observability (O) is automatable given the
connectors and AWS credentials listed in that section — check they're present,
don't assume.

### The opening round (ask once, then stop asking)

Claude asks these **together, in one message**, and nothing else for the rest of
the run:

1. **Which environment** — staging or production. That fixes the alias too (see
   *Pinning the target*); don't ask for the alias separately.
2. **Writes** — run section W or not. If yes, **which store**: the human describes
   it however they like ("the Shopify store", a store name, a URL). They do *not*
   need to know the id — P5 resolves the description against their store list.
   Staging only.
3. **Is the connection already authorized?** If not, hand them C1 and wait.

If the user already answered any of these in their request, don't re-ask it.

After this round: no more questions. A missing fixture is a `skipped` line in the
report, not a prompt. If something genuinely cannot proceed, finish every other
section and say what was left out and why.

### Pinning the target

A run that believes it is on staging while holding production tools is the one way
this checklist can do damage, so the alias is **fixed by convention**, never guessed:

| Environment | Required alias | URL |
|---|---|---|
| Staging | `autods-public-staging` | `https://mcp-staging.autods.com/mcp` |
| Production | `autods-public-prod` | `https://mcp.autods.com/mcp` |

Tools therefore arrive as `mcp__autods-public-staging__<tool>` /
`mcp__autods-public-prod__<tool>`, and the tool namespace alone tells you which
environment a call lands in. Before the first tool call:

1. **Check the alias for the chosen environment exists** (`claude mcp get <alias>`,
   or look at the tool namespace). If it doesn't, hand the user the command and
   wait — don't substitute a differently-named server that happens to point at the
   same URL, because the next run would face the same ambiguity:

   > Aliases are **project-scoped**, and `claude mcp get` answers for the workspace
   > it runs in: from the wrong directory it reports `No MCP server named …` for an
   > alias that is present and whose tools you are already holding. Run it from the
   > workspace root that defines the alias, or just read the tool namespace — which
   > is the cheaper check and the safer one. **Never dump the raw MCP server config
   > to read its URL:** those entries carry access tokens.

   ```bash
   claude mcp add --transport http --callback-port 2048 \
     autods-public-staging https://mcp-staging.autods.com/mcp
   claude mcp add --transport http --callback-port 2048 \
     autods-public-prod https://mcp.autods.com/mcp
   ```

2. **Never call a tool from the other environment's alias**, and never fall back to
   an alias whose target you cannot name. If only a non-conforming alias is
   available: reads may proceed with the mismatch stated in the report, and section
   W is **skipped outright**, no exceptions.
3. **Cross-check after P1**: the account `get_current_user` returns must be the one
   the human expects for that environment (staging and production are different
   Cognito pools over different data). A surprise identity aborts the run.

### Order of the run

| Phase | Section | Driver | Blocks |
|---|---|---|---|
| 1 | **S** — unauthenticated surface | `make release-checks` (shell) | nothing; S needs no connection |
| 2 | **C1** — authorization | human, at a browser | everything below |
| 3 | **P** — account readiness | Claude, MCP | which of R/W can run at all |
| 4 | **C2–C5** — handshake payload | Claude, MCP | nothing |
| 5 | **R** — reads | Claude, MCP | W |
| 6 | **W** — writes | Claude, MCP, gated | nothing |
| 7 | **O** — observability | mixed | nothing |

S is independent of the rest — run it first (it is fast and needs nothing), but a
failure there does not stop the connected sections.

### Rules for the run

- **Never run section W against production, and never without an explicit go-ahead
  naming the store id.** Every other section is read-only and safe anywhere.
- **Never run `claude mcp remove` / `add` for the server under test.** C1's recipe
  does exactly that, which is why C1 belongs to the human: executing it would sever
  Claude's own tool access mid-run.
- Record for each check: `pass` / `fail` / `skipped (why)`. A check you couldn't run
  is not a pass — say so.
- An `upstream_*` error means the MCP server did its job and the upstream refused;
  an `internal_error`, `invalid_arguments`, or a transport failure means **this
  server** regressed. Classify every failure that way before reporting it.
- A **P** failure is an account or environment gap, not a release blocker. Keep the
  two apart in the report — "this account has no store" is not "the release is bad".
- On a failure, capture the tool name, the exact arguments, and the full error text.
  `x-request-id` from an HTTP probe (or the `request_id` in the server log) is what
  ties a report to the server-side line.
- **Pace the calls.** The limiter allows 60/min and 1000/hour per user; a full run is
  roughly 25 calls. Run them sequentially — never fan out — so a `rate_limited` error
  is a real finding rather than self-inflicted.
- Don't retry a failing check more than once, and never "fix" arguments to make a
  check pass. The arguments in this file *are* the contract under test.

---

## S — Deployment identity and the unauthenticated surface

No auth needed; plain `curl`. Run these first — they fail fastest, and they need
no credentials.

> **This section is automated.** `tests/e2e/test_release_checks_s.py` implements
> S1–S5 against a deployed host:
>
> ```bash
> make release-checks                                        # staging
> RUN_RELEASE_CHECKS=1 MCP_RELEASE_BASE_URL=https://mcp.autods.com \
>   uv run pytest tests/e2e/test_release_checks_s.py -v -rs  # production
> ```
>
> Every check is read-only and safe against production. Per-environment
> expectations (Cognito Hosted UI domain, a registered redirect URI) live in
> `KNOWN_ENVIRONMENTS` in `tests/e2e/conftest.py`; override them with
> `E2E_EXPECTED_COGNITO_DOMAIN` / `E2E_REGISTERED_REDIRECT_URI` for a host that
> table doesn't know. The `curl` recipes below stay here as the manual fallback
> and as the definition of what each check means.

**S1 — Health.**
`curl -s https://<host>/health` → `{"status":"ok"}` with HTTP 200.

**S2 — Protected-resource metadata (RFC 9728).**
`curl -s https://<host>/.well-known/oauth-protected-resource` → `resource` is exactly
`https://<host>/mcp`, `authorization_servers` is `["https://<host>"]`, `scopes_supported`
is non-empty. The `resource` string must be byte-identical to the endpoint clients
call — a trailing slash here breaks discovery for every new client.

**S3 — AS metadata (RFC 8414).**
`curl -s https://<host>/.well-known/oauth-authorization-server` → `issuer` equals
`https://<host>` byte-for-byte, `authorization_endpoint`/`token_endpoint` point at
the Cognito Hosted UI domain for this environment, `registration_endpoint` points
back at `https://<host>/oauth/register`. Also fetch the advertised `jwks_uri`: it
must return keys. A wrong `COGNITO_USER_POOL_ID` in the rollout looks perfectly
well-formed in this document and only surfaces when every sign-in fails.

**S4 — The 401 challenge.**
`curl -s -i -X POST https://<host>/mcp -H 'Content-Type: application/json' -d '{}'`
→ HTTP 401 with
`WWW-Authenticate: Bearer resource_metadata="https://<host>/.well-known/oauth-protected-resource"`.
This header is the whole discovery loop; without it no client can bootstrap OAuth.

Repeat with `-H 'Authorization: Bearer not-a-real-token'` → also 401. A **503**
here means the pod can't reach Cognito's JWKS at all, so no token can be verified
and every sign-in fails — invisible from S1–S3, and release-blocking.

**S5 — DCR shim.** POST a registration with a redirect URI that *is* in this
environment's `MCP_REGISTRATION_REDIRECT_URIS` → **201** (RFC 7591 §3.2.1)
echoing that URI back plus the pre-created `client_id`. POST one that isn't →
400 with `error: "invalid_redirect_uri"`, and a list mixing a registered URI with
an unregistered one must be refused too. Cognito exact-matches redirect URIs, so
a shim that accepts anything just moves the failure to sign-in.

This proves only that the shim matches *its own* allowlist. That the allowlist
mirrors what is registered on the Cognito app client is C1's job, and no script
can drive it.

---

## C — Client connection and authorization

**C1 — Fresh authorization (human, at a browser). The hand-off point.** This is the
one step Claude must never execute: the recipe below removes and re-adds the very
server Claude is calling through, which would sever its own tools mid-run. It is
also the single most release-sensitive path, and nothing else on this list
exercises it.

Claude's job here is to *hand the user this block, verbatim, and wait* — then
resume at **P** once they confirm. If the connection is already authorized and the
user does not want to re-authorize, record C1 as `skipped (connection already
authorized; fresh sign-in not re-tested)` and carry on. That is a real gap in the
run, not a pass.

> Remove the server from your client, re-add it, and authorize from scratch:
> ```
> claude mcp remove autods-public-staging
> claude mcp add --transport http --callback-port 2048 autods-public-staging https://mcp-staging.autods.com/mcp
> /mcp        # then complete the browser sign-in
> ```
> Expected: the browser opens Cognito Hosted UI, sign-in succeeds, the client reports
> connected, and `/mcp` lists the tools. Try it once with a **federated (Google)**
> account and once with a **native** account — identity-linking collisions only show
> up on one of them.
>
> Best run in a **separate** Claude session (or before starting one), so the agent
> driving the checklist never loses its connection.

If sign-in itself fails (`oauth_error=invalid_request`, `provider_redirect`, …), the
exchange happened directly between the client and Cognito and never touched this
server — triage in Cognito/CloudWatch. One user failing is usually an identity-linking
collision; all users failing is global config (scopes, redirect allowlist, client_id).

**C2 — Reconnect on an existing token.** Re-run `/mcp` (or restart the client) with
the cached token. Expected: connects without a browser round-trip.

A connection error *immediately after* authorizing that succeeds on retry is the
known cold-cache case: `initialize` resolves identity synchronously upstream and can
exceed the client's ~10s connect timeout. Note it as upstream latency, not an auth
failure — but if it reproduces on **every** connect, it is a regression.

**C3 — Handshake payload.** In the connected session, list the tools.
Expected: exactly the **14** tools below, and the server instructions arrive with them.

Verify the instructions with the script, not with the client's UI:

```bash
MCP_URL=https://mcp-staging.autods.com/mcp uv run python scripts/mcp_call.py instructions
MCP_URL=https://mcp-staging.autods.com/mcp uv run python scripts/mcp_call.py list
```

`mcp_call.py` defaults to `http://localhost:2049/mcp`, so **without `MCP_URL` it
probes localhost** and fails with a connection traceback that looks like a server
outage. It reuses a cached token and only opens a browser once that token expires.

> **`MCP_URL` repoints the call, not the sign-in — so the two lines above are
> staging-only.** The endpoint comes from `MCP_URL`; the token comes from
> `Settings`, i.e. from the repo's `.env`, whose `COGNITO_DOMAIN` is
> `auth-staging.autods.com`. Pointing `MCP_URL` at production therefore opens a
> **staging** Cognito authorization link — and, in the silent case that actually
> costs you the run, reuses a cached *staging* token and sends it to production,
> which 401s and reads exactly like a broken release. For production, hand the
> script a token and skip the OAuth flow entirely:
>
> ```bash
> MCP_TOKEN=<a production access token> MCP_URL=https://mcp.autods.com/mcp \
>   uv run python scripts/mcp_call.py instructions
> ```
>
> `MCP_TOKEN` short-circuits `get_token()`, so nothing reads `.env`. Overriding
> the Cognito settings for the run instead (`COGNITO_DOMAIN`,
> `COGNITO_PUBLIC_CLIENT_ID`, `MCP_REGISTRATION_REDIRECT_URIS`) works too, but it
> costs a second browser sign-in mid-run, which the opening-round rule forbids.

The client's own UI is evidence in **one direction only**, and that direction is
usable. If the client *renders* the instructions and the tool descriptors (Claude
Code has an *MCP Server Instructions* section), the server sent them — grade C3 and
C4 from that and skip the script. What proves nothing is their **absence**: when a
client loads tools lazily, that section can be missing for a server whose handshake
carried the text perfectly. So absence is a client-side display question and the
script is what settles it; presence settles it on its own.

```
get_current_user  get_user_subscription  list_stores_api  list_products
upload_products  publish_drafts_to_marketplace  get_bulk_action_items
search_products  get_winning_products  get_product_by_id
get_similar_products  get_recommended_products  get_categories
get_playbook
```

Check the instructions text starts with `## AutoDS MCP — start here` and matches
`manifests/_server.json` at the released commit. A manifest field that parses but
never reaches a client is the exact failure RD-90 was about — verify it *arrived*,
don't assume.

**C4 — Descriptions match the released manifests.** Spot-check two or three tool
descriptions in the client against `manifests/*.json` at the released commit. They
are baked into the image, so a drift here means the pod is running an older build
than the release claims (e.g. a cached image tag) — this and C3 are what actually
pin the build under test.

**C5 — Annotations.** Every read tool advertises `readOnlyHint: true`;
`publish_drafts_to_marketplace` advertises `destructiveHint: true`. Clients gate
confirmation prompts on these.

**C6 — Playbooks are advertised and fetchable.** In the client, check that
`get_playbook`'s `name` parameter offers an **enum** of playbook names (not a free
string), then call `get_playbook '{"name": "product_import"}'`.
→ an `ok: true` envelope whose `data.steps` carries three steps with derived
`step`/`of` numbers and a non-empty `data.body`. An empty enum, or a `get_playbook`
that answers `invalid_arguments` for a name the enum itself offered, means the
playbook files didn't ship in the image — the chain guidance is then silently absent
for every user even though every other tool works.

**C7 — The chain hint reaches the model.** The `upload_products` description must end
with `Step 1 of 3 in playbook "product_import" — call get_playbook for the full
chain.`, and `list_stores_api`'s must carry no such line.
→ if the tails are missing, agents get no signal that upload is step 1 of 3 and will
stop after it. If a tail appears on a tool that is in no chain, the index is wrong.

Once a **second** playbook ships, add the shared-step half: for a tool that is a step of
two chains, the description tail names both, and a successful call's `playbook` envelope
field carries `in: [both names]` with **no** `step` and **no** `incomplete_alone`. A step
number or a single chain's warning there means the server went back to picking the first
match, which reads perfectly and is wrong for whichever chain the caller is actually in —
grade it `fail`, not cosmetic. Until then this half is `skipped` (one chain exists, so
there is nothing to be ambiguous about).

**C8 — Resources.** `uv run python scripts/mcp_call.py resources`
→ lists `autods://playbook/<name>` with `text/markdown` for each playbook. An error
about an unsupported method means the server stopped declaring the `resources`
capability — a regression, not a client quirk.

**C9 — The polling cadence is delivered.** `get_bulk_action_items`' description must
state the numbers (first poll ~10s, then every ~15s, stop after 10 attempts / ~2.5 min)
and the 1/2 → 3/4/99 state machine; the instructions from C3 must carry the same
cadence in the "writes are asynchronous" invariant.
→ this is the only thing standing between an agent and either one poll or forty (see
`docs/polling-conventions.md`). Losing it is user-visible — an import reported as done
while it is still running — even though every call still returns 200. If the two
channels state *different* numbers, that is the regression, not a cosmetic drift.

---

## P — Account readiness (run before R; decides what R and W can do)

Half of this checklist needs *fixtures* — a store, a product id, an entitlement —
and the authorized account may simply not have them. Establish that first, so a
later `skipped` is a known gap rather than a surprise, and so a missing entitlement
is never misreported as a broken release.

**P is not a release gate.** Every check here answers "does this account have what
the checks need", not "did the build regress". Report P failures separately and keep
going: the run continues with whatever fixtures resolved.

Claude runs all of P through the connected server, no user input.

**P1 — Identity resolves.** `get_current_user` `{}` → a numeric `id` and the `email`
of the account the human said they authorized.
*Fixture:* `autods_user_id` (needed by O2).
*If it fails:* stop. Nothing below works, and analytics are silently dead everywhere
(see O2). This is the one hard stop in the run.
*If the identity is not who the human expected:* stop and say so — the alias is
probably pointing at the other environment.

**P2 — At least one store, and what kind.** `list_stores_api` `{}` → for each entry
record `store.id`, `store.name`, `store.site`.
*Fixture:* a `store_id` for R3/R10, and the site (which marketplace) for W3.
*If the list is empty:* R3, R10 and all of W are `skipped (account has no store)`.
Everything else still runs.
*Note it if* the account has only live/production-looking stores — that constrains W
even on staging.

**P3 — The research catalog answers, and yields a product id.** `search_products`
with the R5 body → a non-empty `data.results`.
*Fixture:* one `results[]._id` (24-char hex) for R7–R9.
*If empty or failing:* R7, R8, R9 are `skipped (no product id available)`.

**P4 — Entitlements, read rather than inferred.** `get_user_subscription` `{}` → a
`user_addons[]` list. For each of `product_hub` (the Product Finding Hub, which also
gates TikTok Analytics) and `orders_processor` (sourcing / Fulfilled-by-AutoDS),
record active / not active by the tool's own rule: **an entry is present and its
`status` is not the canceled code** (`1` = active, `2` = canceled, `3` = expired, so
`expired` still counts as active; no entry at all means the account never held it).
Record the raw `addon_type`/`status` pairs too, so the grading below can be re-derived
from the report — and flag it as a finding if `status` arrives as a *name* rather than a
number, because that is an upstream rendering change the tool's `notes` tolerate but
callers may not.
*Fixture:* the two entitlement flags (needed by R6, R7 and P8).
*Why it matters:* without the Product Finding Hub, `get_winning_products` returns a
limited free set and `get_product_by_id` on a *winning* product answers with an
`upstream_error` about an unexpected redirect. Both are correct behaviour. Decide this
**here**, once, so R6 and R7 are graded against the right expectation instead of being
filed as faults.
*Record it either way* — "no add-on" changes what R6/R7 mean, it doesn't skip them.
*Its latency is less predictable than the other reads* (it reconciles against a separate
balance source under a bounded wait — measured 0.35–1.3 s on staging). Anything up to a
few seconds is within contract; only a timeout is a finding.
*If this check fails:* fall back to inferring the Product Finding Hub from
`get_winning_products` `{"offset": 0, "limit": 1, "sort": "-created_at"}` — a limited
result set means no add-on — and record P4 as `fail` with the inference noted. All
three arguments are required there; omitting `sort` gets an `invalid_arguments`
rejection, which is the schema gate working, not an entitlement answer. Note that
`get_user_subscription` is permitted for every account status, so a `forbidden` here
is a real finding rather than an entitlement answer.

**P5 — Resolve the write target** (only when the human authorized writes). Match
their description — "the Shopify store", a name, a URL — against the P2 list on
`store.name` and `store.site`, case-insensitively.
*Fixture:* exactly one `store.id`.
*It must resolve to exactly one store.* Zero matches or more than one → all of W is
`skipped (store description matched N stores)`, naming the candidates so the human
can be precise next time. **Never pick one to break a tie**, and never fall back to
"the only store" when their description didn't match it.
*Echo the resolution* — id, name, site — in the report, so what was written to is on
the record. Also re-assert here that the environment is staging; W does not run
against production whatever the human said.

**P6 — A product to upload** (only if W will run). W1 needs a supplier product, and
any catalog product will do — take one from the P3 `search_products` results and
read the supplier identifier off its document (`get_product_by_id` returns the full
record). The field is **`idOnSite`** (e.g. `"2326363383"`); `url` carries the same
identifier as a full product URL, and `new_products[].asin` accepts either form.
*Fixture:* that supplier id **and** the product's `siteName`, because W1's
`buy_site_id` must name the same supplier site — `1`=amazon, `2`=aliexpress,
`4`=walmart. The two travel together; a supplier id paired with the wrong site is
rejected upstream.
*If the first product has no `idOnSite`,* try the next P3 result; after three,
`skipped (no catalog product exposed a supplier id)` and W1 does not run.

**P7 — Pre-existing drafts** (only if W will run). `list_products` on the P5 store
with `product_status: 1` → record the draft ids that already exist.
*Why:* W3 must publish only the drafts W1 created, and this snapshot is what makes
the diff possible. Without it, W3 cannot run safely.

**P8 — Spendable credits.** Read `user_credits[]` off the **P4 response** — don't call
again. Record `amount_of_credits` for `credit_type` `"1"` (auto-order credits, the
balance a sourcing request spends). The code arrives as a *string*, not an integer;
matching on `1` finds nothing.
*Fixture:* the auto-order balance, for grading any check that spends credits.
*If no `"1"` entry is present:* record `skipped (no spendable auto-order balance
established)` — **not** "zero credits". The entry is omitted both when the balance is
genuinely zero and when the balance source didn't answer inside the timeout, and the
response cannot tell the two apart. Either way, treat a later insufficient-credits
refusal as an account gap, not a release regression.

---

## R — Read paths (the operations users actually run)

Run these **in order** — later checks reuse ids discovered earlier. All are safe in
any environment. Every one must return an envelope with `ok: true` and `status: 200`.

R1, R2 and R5 call the same operations P1, P2 and P3 already called. Don't repeat
the calls — reuse the responses and grade them a second time against a different
question: **P asked whether the account has the data; R asks whether the response
still has the documented shape.** Everything from R3 on uses the fixtures P
resolved, and is `skipped` when P didn't produce one.

`get_user_subscription` has no R check on purpose: P4 and P8 already grade its
documented shape (`user_addons[]` with `addon_type`/`status`, `user_credits[]` with a
string `credit_type`), and it is the one read whose *entitlement* answer the rest of
the run is graded against — so re-asking it in R would only repeat P.

**R1 — `get_current_user` `{}`.**
→ `data` is a single-element list with a numeric `id`, `name`, `email` matching the
authorized account. This is the identity analytics and logs are keyed on; if it fails
here, tracking is silently dead everywhere (see O2).

**R2 — `list_stores_api` `{}`.**
→ `data` is a list of stores; each entry nests the record under `store` with an
integer `store.id`, a `store.name`, and a `store.site`. Use the `store_id` P2
resolved for the rest of this section.

**R3 — `list_products`** `{"store_ids": "<id>", "body": {"product_status": 2, "limit": 2,
"projection": ["title", "variations.price", "total_sold_count"]}}`
→ `data.results` is a list, and each item carries **only** the projected fields plus
ids. If projection is ignored the response balloons past the client's token limit —
that is a user-visible break, not a cosmetic one.

**R4 — Integer-enum contract.** Repeat R3 with `"product_status": "active"`.
→ must be **rejected**, either as `invalid_arguments` (the schema gate caught it) or
as an `upstream_client_error` 422. A success here means the enum contract that every
manifest description promises has quietly changed.

**R5 — `search_products`** `{"body": {"order_by": {"name": "created_at", "direction":
"desc"}, "limit": 2, "filters": [{"name": "search_query", "value": "phone holder",
"value_type": "string", "op": "search"}]}}`
→ `data.results` is a non-empty list; `data.region` and `data.currency` are present.
This is where the `results[]._id` P3 kept comes from.

**R6 — `get_winning_products`** `{"offset": 0, "limit": 2, "sort": "-created_at"}`
→ `data.results` is a list. Without the Product Finding Hub add-on this is the
limited free set — still a pass. Grade it against what P4 established; don't
re-derive the entitlement here.

**R7 — `get_product_by_id`** `{"product_id": "<the P3 id>"}`
→ the full product document, with `id` equal to what you asked for.
An `upstream_error` about an *unexpected redirect* on a **winning** product means the
caller lacks the Product Finding Hub add-on — expected per P4, not a fault, and not a
retry. The same error on the P3 id (a normal catalog product) **is** a failure.

**R8 — `get_similar_products`** `{"product_id": "<the P3 id>"}`
→ `data.results` is a list. Expect a very large payload — this op has no projection
and can exceed a client's response-token cap; note it if it does, that is the user
experience.

**R9 — `get_recommended_products`** `{"product_id": "<the P3 id>", "limit": 1}`
→ `data.results` is a list and `data.totalResults` is present.

**R10 — `get_bulk_action_items`** `{"store_ids": "<id>", "bulk_action_id": 1, "body":
{"limit": 1}}`
→ `{"total_results": …, "results": […]}`. An unknown id legitimately yields
`total_results: 0` or a documented 4xx; both pass. What must **not** happen is
`internal_error` or a transport failure.
On a **2xx** the envelope must also carry a `playbook` sibling of `data` reading
`{"name": "product_import", "step": "2/3", "next": ["list_products"], …}` — this is
step 2 of a chain, and without that field an agent has nothing telling it the import
isn't confirmed until it reads the store back. It must **not** appear on R3's
`list_products` (the final step) or on R1/R2 (not chain tools at all).

**R11 — Bad input is refused cleanly.** Call `get_product_by_id` with
`{"product_id": "not-a-hex-id"}` and `list_products` with no `body`.
→ a typed `invalid_arguments` / `upstream_client_error` with a readable message —
never `internal_error`, never a stack trace, never an internal hostname or upstream
URL in the text.

**R12 — `get_categories` `{}`, then the filter it exists for.**
→ `data.results` is a non-empty list of `{value, label, children}` nodes, each `value`
a 24-character hex id, with `Other Category` last and childless. Then take a top-level
`value` — a root, not a leaf — and repeat R5 with
`"filters": [{"name": "categories.autods_category_id.$id", "value": "<that value>",
"value_type": "objectId", "op": "="}]`.
→ `data.results` is non-empty, and the check passes at the first root that matches.
One empty root is not a failure — nothing promises every top-level category is
stocked, and the order the roots arrive in is not pinned by anything, so work down
the list. **Every** root matching nothing is the failure this check exists for:
supplying ids to that filter is the tool's entire purpose, and its `notes` promise a
parent id covers the whole subtree beneath it — so an empty result hands an agent
that followed the documentation a confident "no products in this category" for a
category full of them. Grade the tree's shape from the same response: a node missing
`children` is a report line, and repeated labels are expected, not a fault — they are
documented as unique only among siblings.

---

## W — Write paths (gated: staging only, explicit go-ahead, test store)

Do not run without a human confirming the environment and the store id. These mutate
data. If you cannot get that confirmation, mark W as `skipped` and say so.

The gate is settled in the opening round and resolved by P5 — **don't stop mid-run
to ask again.** If P5 didn't resolve the human's description to exactly one store, W
is `skipped` and the run continues to O. Both conditions must hold on their own:
authorization given *and* target is staging.

**W1 — `upload_products`** to the store P5 resolved:
`{"store_ids": "<test id>", "body": {"region": 1, "status": 1, "buy_site_id":
<the site P6 resolved>, "new_products": [{"asin": "<the supplier id P6 resolved>"}]}}`
`buy_site_id` is **not** a constant: it must be the supplier site of the product P6
picked (`1`=amazon, `2`=aliexpress, `4`=walmart), since the catalog hands back
whatever site the search matched.
→ returns immediately with `data.bulk_action.id` and an uploading-started status.
Writes are asynchronous — this is the job being *accepted*, not done.

**W2 — Poll it.** Feed `bulk_action.id` into `get_bulk_action_items` with the same
`store_ids` until items reach `status` 3 (finished) or 99 (error).
→ items appear and progress. `ok: true` on W1 with nothing ever landing here is the
classic false-success, and the whole reason `ok` is documented as transport-level only.

Poll on the **documented** cadence, which is also what keeps an unattended run from
spinning: first poll ~10s after W1, then every ~15s, at most 10 attempts (~2.5 min).
Those are the numbers the tool's own `notes` state (RD-91,
`docs/polling-conventions.md`) — polling faster here would test a cadence no agent is
told to use. The bound to count is the **attempts**; record the elapsed time you
actually reached rather than the nominal one. If it hasn't reached 3 or 99 by then,
record `inconclusive (still <status> after N attempts / <elapsed>)` and move on — a slow
job is not the same
finding as a job that never lands, and only W3 depends on the result.

**W3 — `publish_drafts_to_marketplace`** on the drafts W1 created, targeted by id:
`{"store_ids": "<test id>", "body": {"product_status": 1, "filters": [{"name": "id",
"op": "in", "value_type": "list", "value_list": ["<draft ids>"]}]}}`
→ returns a `bulk_action`; poll it the same way (same bounded schedule as W2).
**Never omit `filters`** — that publishes every draft in the store.

The draft ids must be the ones **W1 created**, identified by diffing against the
draft list P7 recorded. If W2 was inconclusive, or you cannot tell the new drafts
from the pre-existing ones, W3 is `skipped (cannot target only the new drafts)` —
publishing the wrong drafts is worse than not running the check.

**W4 — Business error inside a 200.** If a write returns `ok: true` *and* a
`business_error` list beside `data`, confirm each entry has a `code` and a
human-readable `message`. That path is only reachable when an upstream refuses inside
a 200; if you can't trigger it, skip it.

---

## O — Observability (after the release, before you call it green)

The evidence lives in three systems outside the MCP server, each reachable with the
right access. **Verify the access before claiming the check** — a connector pointed
at the wrong instance answers cheerfully and tells you nothing.

| | Needs | Verify it with |
|---|---|---|
| O1 | the `sentry` MCP server, on the **self-hosted** instance | `find_organizations` returns `webUrl: https://sentry.autods.com` |
| O2 | a Mixpanel MCP server, authorized | `claude mcp list` shows it connected |
| O3, O4 | AWS credentials that can read `s3://autods-cluster-logs` | `scripts/fetch_logs.py` returns rows |

Anything whose access isn't there is `skipped (needs <system> access)` — never a
pass, and never inferred from another check.

**Record the UTC time before P1 and after the last W call.** All four checks below
query that window; without it O2 and O3 devolve into scanning.

Run this section **last**: every check here looks for traces of the calls R and W
just made.

**O1 — Sentry.** No new error class since the deploy, and — *if this environment
produced any event at all* — the new `release` tag (from `__version__`) on it.

Take the release half as conditional, because on a healthy run there is nothing to
read it from: `capture_tool_error` fires only on an upstream **5xx or unexpected
3xx** (`mapped.log_full is not None`), and `capture_tool_exception` only on a
transport failure or an `internal_error`. A mapped **4xx** and every
`invalid_arguments` rejection reach Sentry not at all. So a clean environment
emits zero events, `release:autods-mcp-public@<version>` legitimately matches
nothing, and that is a `skipped (no event in this environment to carry the tag)` —
**not** a pass, and not a fault either. Only an environment that *did* error owes
you a release tag, and a tag reading an *older* version there is the finding this
check exists for.

Use the `sentry` MCP server — the one that answers for `sentry.autods.com`. A
connector named "Sentry" that points at `sentry.io` or `mcp.sentry.dev` is the
**hosted** service, a different instance entirely: it will answer, find nothing, and
look like a pass. Confirm the host, then filter issues by `environment` and the
release tag.

Confirm it by asking the server, not by reading config: `find_organizations` returns
the org with its `webUrl`/`regionUrl`, and self-hosted answers
`https://sentry.autods.com`. `claude mcp get sentry` is the wrong instrument here —
it prints scope and status but not the connection arguments, so it cannot tell the
two instances apart, and a *name* proves nothing either way (this workspace carries
both a self-hosted `sentry` and a separate hosted connector). Reading the tool
namespace is also enough to know the server is **present**: holding
`mcp__sentry__*` tools means it is connected, whatever `claude mcp get` says from
the wrong directory — do not record O1 as `skipped` on the strength of that command
alone.

Two things about this self-hosted instance, both of which will otherwise cost you a
wrong reading:

- **The environment tag reads `prod`, not `production`.** It defaults to `MCP_ENV`
  (`sentry.py`), and the deployed values are `prod` and `staging`. `environment:production`
  is not an error and not empty-because-healthy — it matches nothing, in a project with
  thousands of live events, and looks *exactly* like a clean environment. It is the
  cheapest way to file a false pass on this whole section, so confirm the value on a
  real event's tags before trusting any empty result. Same trap as `--env prod` in
  `fetch_logs.py`, which spells it the same way.
- **`search_events` is not available — it answers HTTP 404.** Use `search_issues`
  for the list, and `get_sentry_resource` on an issue for per-event detail.
- **`environment:` in a `search_issues` query does not scope the issue's
  aggregates.** An issue surfaced by `environment:staging` can be almost entirely
  production traffic, occurrence and user counts included; `AUTODS-MCP-PUBLIC-K`
  (4,861 events, 203 users) is prod, and reads as staging here. Confirm the
  environment on the issue's **latest event tags** (`get_sentry_resource` →
  `environment`, `release`, `url`) before attributing anything to this run. Those
  tags are also where you read the release: `release: autods-mcp-public@<version>`.
  `user_context: [Filtered]` and an event with no request body are the scrubber and
  `max_request_body_size="never"` doing their jobs — worth a glance while you're there.
- **"First seen" in the issue *listing* is clamped to the query period, so it reads
  as recent for an issue that is months old.** A 24h search reported both live issues
  as "first seen 23 hours ago"; `get_sentry_resource` dates them to 2026-07-09.
  Never conclude "this error class is new since the deploy" from the listing — that
  is what the `firstSeen:` query below is for, and the issue resource is where the
  real timestamp lives.

For "no new error class since the deploy", `search_issues` with
`environment:<env> firstSeen:-24h` is the query that works — an empty result is the
pass, **provided `<env>` is a value the tag actually takes**:

```bash
# production                      # staging
environment:prod firstSeen:-24h   environment:staging firstSeen:-24h
```

An issue that *is* new in that window still has to be dated before you call it a
regression: read its `release` tag with `get_sentry_resource`. A first-seen inside
24h carrying the **previous** version fired on the old build and is not new since
the deploy — it is the last gasp of what you just replaced.

**O2 — Mixpanel.** *MCP Call Received* events appear for the calls you just made,
keyed by the AutoDS user id from **P1**. Identity resolution fails **open and
silently**: if it breaks, no events fire at all, with a valid token and no warning
anywhere. P1/R1 passing is not proof — check that the events actually arrived.

Query the Mixpanel MCP server for the event name, filtered to `distinct_id` =
the P1 `autods_user_id`, over the recorded run window. Zero events with a green R
section is the exact silent-failure this check exists for — report it as a finding,
not as "no data".

Pinned, so a run doesn't spend calls rediscovering them:

| | |
|---|---|
| Project id | staging **1914445**, production **2576216** (org *AutoDS Main*, 2130892) |
| Breakdown property | **`$distinct_id`** — plain `distinct_id` is not an event property and breaks down to a single `"undefined"` row that looks like missing data |

An `insights` query on *MCP Call Received* with `unit: "hour"` over the last day
gives a per-hour series whose final bucket should equal the number of tool calls the
run made (this run: 22 calls, 22 events); add the `$distinct_id` breakdown to confirm
they are keyed to the P1 account and not filed anonymously under a `$device:` id.

**O3 — Logs.** Each tool call above produced exactly one `tool_call` line with
`request_id`, `cognito_username`, `autods_user_id`, `tool_name`, `op_id`,
`upstream_status`, `upstream_url`, `latency_ms`. No request or response bodies in
the logs.

The caller field is **`cognito_username`** (which holds `claims.sub`, not a
username — see the gotcha in `CLAUDE.md`), alongside `autods_user_id` and `email`.
There is no `user_sub` key; grepping for one finds nothing on a perfectly healthy
line.

The pods ship stdout to `s3://autods-cluster-logs`, so this needs AWS credentials,
not cluster access. `scripts/fetch_logs.py` handles the archive layout:

```bash
# absolute bounds — use the UTC window recorded around P1..W (see above)
uv run python scripts/fetch_logs.py --env staging \
  --since 2026-08-27T08:25 --until 2026-08-27T09:00
uv run python scripts/fetch_logs.py --env staging --since 30m --request-id <id>,<id>
```

Assert three things: **one** `tool_call` line per call the run made (join on the
`x-request-id` values collected during the run), each carrying the documented
fields, and **no request or response bodies anywhere** in the output. The footer
prints a per-event tally (`scanned events: request=9, tool_call=7`), so the
one-line-per-call count is read off it rather than counted by hand. The header and
footer go to **stderr** and the entries to stdout, so redirect them apart
(`>out.txt 2>meta.txt`) — merging with `2>&1` splices the footer onto the middle of
a data line, where `tail` will not find it and it looks like no footer printed.

Three properties of this script decide whether the answer means anything:

* **`--event` defaults to `all`.** It used to default to `tool_call`, which made a
  seemingly unfiltered call hide every `request` line — the reading that turned
  O4 into a false pass. The header now echoes the filters in force, and a run that
  matches nothing says whether a filter or the archive lag is responsible.
* **Prefer absolute `--since`/`--until` for anything that goes in the report.** A
  relative window is resolved when the process starts, and a wide scan takes
  minutes, so two back-to-back `--since 40m` calls cover *different* periods — one
  can even resolve to a window ending in the future, returning zero entries from a
  perfectly healthy archive.
* **`--request-id` matches the *whole* id, but the entries print only its first 8
  characters.** Copying `32b97934` out of the output and passing it back filters
  everything away — `0 entries matched`, exit 2, indistinguishable from a healthy
  empty window. Since this check tells you to join on the ids collected during the
  run, that is the main path, not a corner: take full ids from `--json` (the
  `request_id` field) or from the `x-request-id` response header, never from the
  text display.

The archive lags a few minutes — if the window comes back empty, wait and retry
once before recording anything.

**O4 — No `/mcp` 500 flood.** A burst of ~60s `500`s / `ClientDisconnect` on `POST
/mcp` is the symptom of the transport hanging on a drained request body, not client
noise. A handful is benign; a flood is an outage — check whether anything new reads
the request body (Sentry integrations, middleware) and confirm
`max_request_body_size="never"` is still in `init_sentry`.

```bash
uv run python scripts/fetch_logs.py --env staging \
  --since 2026-08-27T06:30 --until 2026-08-27T09:35 --event request --status 500
```

The 500 status lives on `status_code` for a `request` entry (`upstream_status` is
the tool-call field); `--status` checks both, so the recipe above is right — but if
you filter by hand in `--json` output, read `status_code`.

Widen the window to cover the deploy, not just the run — the flood this catches
happens when traffic arrives, which may be well before the checks. Scanning ~3
hours is ~200 objects and takes several minutes, so run it in the background rather
than under a short `timeout`; a killed scan (exit 143) is not a pass.

An empty result is the pass, and it now distinguishes itself from a mistake: the
footer says whether the window held `request` events that a filter excluded, or
held nothing at all. The run itself is corroborating evidence: ~25 POSTs to `/mcp`
with none hanging ~60s says the hang is not active *now*, which is not the same as
"there was no burst".

---

## Reporting

Report as a table of check id → `pass` / `fail` / `skipped (why)`, then for each
failure: tool + exact arguments, full error text, `request_id`, and whether it is a
**server regression** (`internal_error`, `invalid_arguments` on valid input, transport
failure, missing/wrong tool or instructions) or an **upstream/entitlement condition**
(`upstream_*`, a documented business error). Only the first kind blocks a release.

An unattended run ends with four things, in this order:

1. **The verdict, first line.** Ship / don't ship, and the one reason. A reader who
   stops here must not be misled.
2. **The table** — every check id including the skipped ones, with the environment,
   the alias, and the account identity from P1 stated above it so the run is
   reproducible.
3. **Blockers** — server regressions only, each with arguments and error text.
4. **What wasn't covered, and what would unblock it** — the C1 sign-in if it wasn't
   re-run, every P gap, and every O check that needed access Claude didn't have.
   This list is the point of an unattended run: it is what the human has to do
   *themselves* before the release is actually verified.

When W ran, state the store it wrote to (id, name, site) and what it created. A
write section that reports only `pass` leaves nobody able to clean up.

Never report a `skipped` as a `pass`, and never describe a run as clean when section
O went unchecked — analytics dying silently is precisely the failure O2 exists for.

## Keeping this list current

This file is meant to grow. Extend it in the **same commit** as the change:

- **New tool** → a numbered check in `R` (or `W` if it writes) with concrete arguments
  and the expected response shape, and bump the tool count and list in C3.
- **New behaviour a user can see** (a new envelope field, a new error type, a new
  client recipe) → the section it belongs to.
- **Bug fixed in a released build** → a check that would have caught it, written as
  the *observable* symptom a user would report, not as the internal cause. R4
  (integer enums), R11 (clean refusals), W2 (async writes really land), O2 (analytics
  silently dead) and O4 (transport hang) each exist because that failure shipped once.
- **Anything you had to skip repeatedly** for lack of a fixture (a test store, an
  entitlement, a seeded bulk action) → a check in `P`, so the gap is established up
  front and named in every report instead of being quietly permanent.
- **Anything that forced Claude to stop and ask mid-run** → either a question moved
  into the opening round, or a rule that lets the run continue with a `skipped`. A
  run that needs the human twice is a bug in this file.

### When *not* to touch it

Growth is not the goal — coverage of post-release failure is. The question to ask is
whether a *deployed* build could give a wrong answer while every test still passes.
If not, it does not belong here:

- **An upstream incident that says nothing about this server's contract.** Hand it to
  the upstream owner. The staging products-search 500 (`AttributeError` inside
  `/marketplace/api/products/`) broke the most important read path in a run and
  earned no new check: R5 caught it exactly as written, so a second check would only
  restate what already worked.
- **A finding whose fix hasn't landed.** It belongs in the report and the ticket
  until then. `get_similar_products` answering with ~1.6 MB is a genuine defect, but
  R8 already says to note an oversized payload; it earns an edit when the server
  gains a projection or a cap — i.e. when what a client observably gets back changes.
- **A run result.** Verdicts, store ids, draft ids, bulk-action numbers go in the
  report. This file is the *procedure*; pasting outcomes into it corrupts the next
  run's baseline.
- **An environment- or account-specific value.** Never hardcode a store or product
  id. If a run needs one, add a *resolution rule* to `P` — that is what P5 and P6
  are — so the checklist stays runnable by anyone.
- **Anything a test already proves.** Provable in-process with mocks → `tests/`.
  Provable against a deployed host with no credentials or fixtures → extend
  `tests/e2e/test_release_checks_s.py`, which is why section S is automated. Prose
  here is the last resort, not the first.
- **A check that just failed.** See *Rules for the run*: the arguments in this file
  **are** the contract under test. Relaxing them after a failure destroys the thing
  being measured.
- **Thoroughness for its own sake.** Every check costs wall-clock and rate-limit
  budget (60/min and 1000/hour per user; a full run is roughly 25 calls). A check
  with no crisp pass/fail is worse than no check — it lands an ungradeable line in
  the report and teaches readers to skim.
