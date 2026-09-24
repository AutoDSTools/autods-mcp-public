# Polling conventions for asynchronous operations (RD-91)

Several AutoDS operations accept work and finish it later. The web app watches both
halves over Pusher/SSE; **this server has no push channel**, so every asynchronous
operation an MCP tool exposes becomes a polling loop driven by the agent — one tool
call per attempt, one model turn per tool call.

The decision taken in RD-91 is **thin tools plus documented agent-side polling**: no
per-operation dispatcher timeout, no server-side blocking wait, no change to
`dispatch.py` or `create_http_client`. The server keeps forwarding one request per
call; what an agent must do *between* calls is manifest text.

This file is the single source for the numbers and the state machines. It is internal
(nothing here ships to a client), so it may name upstream internals — the text that
*does* ship must not, per **Keep descriptions implementation-agnostic** in
`CLAUDE.md`.

## The cadence

Stated as concrete numbers, never as "poll periodically" — vague guidance produces
either one poll or forty:

| | |
| --- | --- |
| first poll | **~10 s** after the call that started the work |
| subsequent polls | every **~15 s** |
| ceiling | **10 attempts** — **~2.5 min** at this cadence |
| on hitting the ceiling | report what is unfinished and its last status; stop |

The ceiling is the **attempt count**; the duration is its consequence, not a second
independent bound. 10 s + 9 × 15 s = 145 s, so the tenth attempt lands at ~2.4 min and a
little later once the round-trips are counted — which is why it is written as *~2.5 min*
and why the two numbers must move together. An earlier draft said "10 attempts or ~3 min,
whichever comes first"; the minutes could never bind, and the checklist that quoted them
told an operator to record an elapsed time the run had not reached.
| `full_scrape` (scraper tools only) | `true` on the **first** attempt only |

These numbers deliberately **retire the frontend cadence in documentation.**
`v2-frontend/src/routes/SourcingRequest/hooks/useSourcingProducts.jsx` polls every
3 s up to 120 s (`timeoutLimit = 3000`, `REQUEST_TIMEOUT = 120000`). That is right
for a browser and wrong for an agent: a tool round-trip is already seconds, and each
attempt costs a turn, so a 3 s loop spends the conversation re-reading the same
unfinished job. Do not copy the frontend's interval into manifest text.

`full_scrape=true` on the first attempt only preserves the existing frontend
semantics exactly: the hook passes `fullScrape: timerRef.current < timeoutLimit`, and
`timerRef.current` is `null` on the first call and `>= 3000` on every later one — so
the first attempt forces a scrape and the rest read what the scrape produced.

**Where the numbers go** (see the four tiers in `CLAUDE.md`): tier 2 (the polling
tool's own `notes`) and tier 3 (the playbook `body`) are where they belong, because
they are that tool's observable contract and that chain's runbook respectively. Tier 4
(`instructions`) carries the cadence once, as one clause of the "writes are
asynchronous" invariant, and nothing more — it rides in the system prompt on every
turn. Tier 1 is for forming *this* call and has nothing to say about cadence.

## Reference implementation: `get_bulk_action_items`

The bulk-action chain is the one already in production, so it is the reference — a new
polling tool should read like it. See `manifests/bulk_actions.json` (tier 2) and
`manifests/playbooks/product_import.json` (tier 3). `list_store_quotes`
(`manifests/store_quotes.json`, RD-93) is the second one and was written against it.

`upload_products` returns `{status, bulk_action}` immediately; the created product's
id only exists once the bulk-action item finishes.

| `status` | `BulkStatus` | polling |
| --- | --- | --- |
| 1 | `created` | not final — keep polling |
| 2 | `in_progress` | not final — keep polling |
| 3 | `finished` | final; the item landed |
| 4 | `canceled` | final; it will not land |
| 99 | `error` | final; read `error_reason` / `error_code` |

- The job is over when **no item is left at 1 or 2** — not when the call returns 2xx.
- Take the created product's id from a finished item's `autods_product_id`
  (`AutoDSApi/dal/model/bulk_action_item.py`).
- A mixed batch (some 3, some 99) is the normal outcome, not a failed call.
- Reaching the ceiling is a third answer, distinct from finished and from failed:
  report the unfinished items and their last status.

## Store quotes: `new → in_progress → ready → linked`

Read by `list_store_quotes` (RD-93), filtered on `product_id`. That one tool is both
the "has this product been sourced already?" pre-check and the completion poll — the
same call read before and after the write — which is also why the two readings of an
empty `results` are part of its `notes`. It is the second polling tool after
`get_bulk_action_items`, and it copies that one's phrasing rather than restating the
numbers differently.

The write side landed with RD-94 as `create_1688_sourcing_request`. The async
endpoint
`POST /store_quotes/{store_id}/product/{autods_product_id}/alibaba-1688-request-async`
returns `{"status": "ok"}` **before any work happens** — it only confirms the Celery
task was queued. Completion is observable solely as the store quote reaching
`linked`.

That write is also the one place where a *failed* call is more dangerous than a
slow one. There is no idempotency key upstream, so a transport-level failure
(`UpstreamRequestError`, no response body) leaves the agent unable to tell an
accepted request from a rejected one, while the AO credit may already be spent.
The rule, carried in the tool's `notes` and — once RD-110 lands the playbook —
in its step's `on_failure`: **on any failure or timeout, read
`list_store_quotes` filtered on `product_id` before retrying.** A returned quote
means the request landed; continue the completion poll and never resubmit blind.
A blind second attempt risks a second charge, and it is **not** refused: the
trigger answers `{"status": "ok"}` whatever the state, and every check runs in
the background. Against an existing 1688 quote the second attempt runs again
without charging and may re-link the variations. Against an existing quote from
any other supplier it fails, and the rollback then **deletes that existing quote**
(`alibaba_1688_request` rolls back whatever quote the product has, not only one it
created) — an upstream bug, recorded in the tool's `notes` until it is fixed. The
same background checks are why a missing Order Processing add-on, a product with
no variations or too few credits never surface as an error: each looks like a
quote that never appeared.

`request_manual_sourcing` (`POST /store_quotes/{store_id}/product/{product_id}/request`)
is **not** an async trigger — it answers with the store quote itself — but it
routes into the same `create_and_request_store_quote`, so it charges the same AO
credit and creates the same one-request-per-product record. A Honestfulphilment
quote is worked by people within about 48 h, which is two orders of magnitude
past the ceiling above: it is reported as submitted, not polled to a ceiling.

`StoreQuoteStatus` (`AutoDSApi/dal/model/quote.py`), string values:

| value | polling |
| --- | --- |
| `new` | not final — keep polling |
| `in_progress` | not final — keep polling |
| `ready` | quoted, not yet linked — keep polling |
| `linked` | final; the request is finished (see below — this is not proof of a link) |
| `cannot_be_sourced` | final; this product cannot be sourced this way |

**The failure mode has no status.** If `tasks/quote_tasks.py:alibaba_1688_request`
raises, it calls `rollback_store_quote_on_error`, which **deletes** the store quote,
and reports the error only through an SSE event — a channel this server cannot see.
So a failed request does not surface as `cannot_be_sourced`; it surfaces as the quote
**not being there**. Any tool documenting this chain must say so: a quote that was
present and is now absent, or one that has still not appeared when the poll reaches
its ceiling, is a failure and not "still working".

An empty result on an **early** poll is not that failure yet. The task creates the
record only after it has read the product, counted its orders and charged the
credit, and it waits in the queue before it starts, so the first poll can simply
come too early. Only two readings are a failure: *seen, then gone*, and *never seen
by the ceiling*. Treating the first empty poll as a failure reports a working request
as a failed one — and invites a resubmit, which is the double-charge risk above.

**`linked` does not prove the link.** `link_product` sets `LINKED` whatever the
pairing matched (`AutoDSApi/bl/manager/quote_manager.py`). `_set_buy_items`
(`AutoDSApi/bl/manager/product_manager.py`) skips a variation whose supplier variation
id is not in the offer, and an active product keeps that variation on its old
supplier. So a wrong `item_id_on_site` gives `linked` with nothing linked. The status
says the request finished. Only the product says whether its variations now carry
the offer's variation ids — read it with `list_products`, which is what release check
W8 does.

**The credit is charged before the record exists, and a rollback does not refund it.**
`_charge_ao_credits` runs before `create_store_quote`, and `rollback_store_quote_on_error`
only deletes the record. So a request that disappears may have cost a credit. Whether
it did cannot be read from the store quote; only the auto-order balance shows it. This is the concrete reason the notes must say *check the status,
don't assume success* — `{"status": "ok"}` on the trigger means nothing about the
outcome.

`TryAgainLaterError` is retried by the task itself (up to 10 times, with its own
countdown), so a slow quote is normal within the ceiling.

Two more reads hang off the same record and are **not** poll targets, because they
answer an error rather than an empty result while the quote is still working
(`AutoDSApi/bl/manager/quote_manager.py`):

- `get_store_quote_versions` reads `store_quote.quote.item_id_on_site`, and
  `quote_id` is null until an offer is attached — so on `new` / `in_progress` it
  raises and the caller sees `upstream_error`. It also filters the versions to
  `QuoteStatus.QUOTED`, so the other two `QuoteStatus` values never appear in its
  answer however the enum is documented elsewhere.
- `list_store_quote_shipping_options` refuses anything outside `READY` / `LINKED`
  with a `QuoteValidationError` (400 → `upstream_client_error`).

Both tools' `notes` say "check the status first" for that reason, and the staging
e2e skips them unless the store's first quote is already at `ready` or `linked`.

## Scrapers: the three-state read

Landed with RD-95 as `search_1688_offers_by_image` (`/offers/scan`) and
`get_1688_product_details` (`/products/scan`), in `manifests/suppliers.json`. Both are
poll-until-ready: the call queues a scrape and simultaneously returns whatever is in
the store right now. The poll is therefore the *same* call repeated, with
`full_scrape=true` on the first attempt only.

Measured on staging (2026-09-24, through the gateway): an image search answered
`not_in_db` on the first call and offers on the second, ~12 s later; a details read of
an offer that search had just found answered in full on the first call; a
well-formed id for an offer that does not exist sat in `no_info` for one poll, then
came back with `error.error_code: PRODUCT_404`. So the ceiling is rarely reached on a
healthy input, and the loop mostly ends on data or on an error. An agent has to be able to tell **still working** from **done**
from **failed**, and the fields that carry that are not the same on the two
endpoints.

Common shape (`autods_models.scraper_api.responses`): `data`, `no_info`,
`not_in_db`, and — on offers — `scraper_error`.

**`/offers/scan` → `OffersResponse`** (`data` is a *list* of offers):

- `not_in_db` non-empty → the id was just queued; nothing scraped yet.
- `no_info` non-empty with `data` empty → the id is in the scan queue; scraping in
  progress. (The view sets `no_info = in_db` precisely when `data` is empty.)
- `data` non-empty → done.
- `scraper_error` present → **stop, do not re-poll.**

**`/products/scan` → `ProductsResponse` / `ShortProductsResponse`** (`data` is a
*dict* keyed by the requested asin):

- asin in `no_info` and absent from `data` → queued or scraping in progress.
- `data[asin]` present with no `error` → done.
- `data[asin].error` present → **stop, do not re-poll** for that asin.
- `not_in_db` is effectively **always empty here** — unlike the offers view, the
  products view returns the read's own lists, and the base parser files an id with no
  stored data under `no_info`, never `not_in_db`
  (`Scrapers-API/scrapers_api/products/parsers/base.py:_get_products`). Do not
  document `not_in_db` as the "queued" signal for products.

Errors are `ScraperErrorAPI`: `error_code`, `error_msg`, `retries` — **snake_case on
the wire.** Every `ScraperErrorCode` value (`UNEXPECTED`, `NO_OFFERS`, `PRODUCT_404`,
`PRODUCT_OOS`, `SHIPPING_UNAVAILABLE`, `VIRTUAL_PRODUCT`, `CUSTOMIZABLE_PRODUCT`,
`BLACKLISTED_PRODUCT`, `TOO_MANY_RETRIES`, `INVALID_INPUT`) is terminal for that
input — `TOO_MANY_RETRIES` means the scraper already exhausted its own retries. None
of them means "come back later", so an error always ends the poll loop. Re-queuing
with `full_scrape=true` is a *fresh scrape decision*, not a continuation of the loop,
and it needs a reason to believe the input will scrape this time.

**A `business_errors` block for these must use the wire spelling.** The ScrapersAPI
response is `scraper_error.error_code` (the frontend's `scraperError.errorCode` is its
own request helper camelising keys, and was the spelling of this repo's illustrative
blocks until RD-95). A mis-cased path never matches, the boot lint cannot see it, and
the operation ships looking protected. RD-95 confirmed both paths against live
answers — `scraper_error.error_code` on offers, `data.*.error.error_code` on products —
and `tests/mcp_server/test_suppliers.py` runs them against those recorded answers.

**`PRODUCT_OOS` on `/products/scan` is also the answer to a malformed id.** An id that
fails the 1688 id format is never scraped: the view builds a placeholder entry (title
"Out of stock", price `133.133`) with `PRODUCT_OOS` and answers at once. The tool's
notes say to check the id before reporting an offer as sold out.

Also note the asymmetry when writing the notes: `/products/scan` has **no
`scraper_error`** field, so for a product a failed scrape that never produced a
`data[asin]` entry is indistinguishable from a slow one. There, the ceiling is the
only stop condition.

## Rate limits (reviewed, unchanged)

Two token buckets keyed by `user.sub`, shared across all operations: **60/min** and
**1000/hour** (`RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_PER_HOUR`, `ratelimit.py`).
Checked against the cadence above:

- One flow costs ~12–14 calls (1 write + ≤10 polls + a confirming read + discovery).
  At a 15 s interval a single flow draws **4 polls/min**, so the minute bucket holds
  ~10 concurrent flows before it binds, and the hour bucket ~70 flows/hour. Head-room
  of roughly 3.5× on the hour bucket for a 20-product sourcing session.
- The buckets are per user, not per session, so two clients signed in as the same
  account (Claude desktop and Cursor, say) share them. At 4 polls/min each that is
  still comfortable.
- **The cadence is what makes this hold.** At the frontend's 3 s interval one flow
  draws 20 polls/min and three concurrent flows trip 60/min. The documented ceiling
  is the backstop against a runaway loop, which is also the argument against a
  separate laxer bucket for `readOnlyHint: true` operations: polls are exactly the
  calls a runaway loop makes, and exempting them would remove the only limit that
  ever notices. It would also add a second Redis key namespace and a per-operation
  class lookup on the hot path for no measured need.

**Decision: no bucket change.** Revisit if `error_type=rate_limited` actually appears
in the audit log for a legitimate session (`scripts/fetch_logs.py`), and land any
change with the `settings.py` + `README.md` update the ticket asks for.

## Checklist for a new polling tool

1. Cadence and ceiling in the tool's `notes`, numerically (~10 s, ~15 s, 10 attempts,
   ~2.5 min) — copy the phrasing from `get_bulk_action_items` rather than inventing new
   numbers.
2. The state machine in the `notes`: which states are non-final, which are final, and
   which field carries the result or the error.
3. "A 2xx means the read worked, not that the work succeeded" — say it on the tool.
4. What reaching the ceiling means, so it is not reported as success.
5. If the chain spans tools, the same numbers in the playbook `body`, and nowhere a
   second set of numbers.
6. A test that the numbers arrive at a client, not just that the manifest parses
   (`tests/mcp_server/test_polling_conventions.py`) — the RD-90 lesson.
