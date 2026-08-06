# RD-82 — Showing product images in Claude MCP clients

Spike findings, August 2026. Measured against `mcp-staging.autods.com`, Claude Desktop
(Linux beta), Claude Code v2.1.223, and claude.ai web. Every row below is an observation,
not an inference, unless marked otherwise.

## Recommendation

**Build the MCP Apps widget, scoped first to the research/marketplace surface.** It is the
only path that shows a product image to a *user* inline, and it costs **zero vision
tokens** because images travel as URLs in `structuredContent` and are fetched by the
browser. Supplier-CDN images load inside the Claude sandbox — verified.

**Add base64 thumbnails as a second, opt-in path** for cases where the *model* must judge
an image (bad hero shots, watermarks, collages). Cap server-side at 20 images of 252px.

**Defer `list_products`** until `autods-scraper-images` has a thumbnail source. That is an
infra ticket in another repo, not MCP work.

## Answers

| # | Question | Answer |
|---|---|---|
| Q0 | Thumbnails by CDN URL rewrite? | **Research surface 100%.** In-store 37% full + 9% partial (eBay 400px only). Measured over 3.4k real URLs from 77 hosts, from AWS staging egress. |
| Q1/Q2 | Do clients render MCP `ImageContent`? | Yes — web, Desktop, Claude Code. Always inside the **collapsed tool block**, never inline in the prose. |
| Q3 | Real image block or base64-as-text? | **Native everywhere.** Measured: 1200px image cost +2,100 tokens against a control; base64-as-text would have been ~20,000. claude-code#31208 does not reproduce. |
| Q4 | Does `annotations.audience: ["user"]` hide an image from the model? | **No — ignored in every client.** There is no display-only path. |
| Q5 | Tool-result ceiling | **Token-based, not byte-based** (17× byte increase at identical dimensions changed nothing). Client-specific: Desktop truncates at ~25k visual tokens (16 × 1200px); web delivered 20 × 1200px = 36,980. |
| Q6 | Does claude.ai render a `ui://` widget from a remote connector? | **Yes**, web and Desktop-over-stdio. Desktop over a *remote* connector showed raw `structuredContent` instead. |
| Q7 | Do supplier CDN images load inside the widget? | **Yes** — Amazon, Shopify, `data:`, own origin `<img>`, and `fetch`→blob all pass, with zero CSP violations, once the CSP is declared correctly (see Gotchas). |

## Numbers that drive the design

Claude bills `ceil(w/28) × ceil(h/28)` visual tokens per image.

| 20 products | visual tokens | ≈ cost/call (Opus 5) |
|---|---|---|
| widget (URLs in `structuredContent`) | **0** | **$0** |
| base64 @ 252px | 1,620 | $0.01 |
| base64 @ 1200px | 36,980 | $0.23, and truncates |

MCP has no display-size field: the pixels you send are the pixels you pay for.

## Where the in-store images actually are

51% of `list_products` images sit in `autods-scraper-images` — a flat bucket of
`{md5}.png` objects, 200 KB–1 MB each, **no thumbnails, no CDN in front, and
`Content-Type: image/jpeg` on genuine PNG bytes**. Scrapers-API's `/images/s3_urls`
returns exactly one URL per original and rejects images for being *too small*; it is a
large-image gatekeeper, not a thumbnailer. Confirmed by contract, code, and an
authenticated bucket listing.

The gap concentrates by lifecycle, not by CDN: **drafts are 80% AutoDS-hosted** (87–100%
on every sell site except Amazon-sourced), and among active products the platforms that
never re-host — Etsy 100%, TikTok 92%, Facebook 91%, WooCommerce 70% — stay AutoDS-hosted
for good.

## Client capability matrix

| | ImageContent | `ui://` widget | CSP honoured |
|---|---|---|---|
| claude.ai web | ✅ collapsed block | ✅ | ✅ (both `_meta` locations) |
| Claude Desktop (local stdio) | ✅ collapsed block | ✅ | ❌ external images blocked |
| Claude Desktop (remote connector) | ✅ | ❌ raw JSON | n/a |
| Claude Code | ✅ | n/a | n/a |

## Gotchas for whoever implements this

Six protocol bugs cost most of the spike's time. Each failed **silently**.

1. **`_meta.ui.csp` must be on the `resources/read` response, not only `resources/list`.**
   The host renders from the read. Declared only on the list, the sandbox comes back as
   `{}` and every external image is blocked by the default policy. This looked like
   non-deterministic host behaviour for three runs.
2. **`types.Tool(meta=…)` silently produces the wrong wire format.** The field is `meta`,
   its alias is `_meta`, and the models are `extra="allow"` without `populate_by_name`, so
   `meta=` creates a junk extra field serialised as `"meta"`. Construct via
   `**{"_meta": …}`. Same trap on `types.Resource`.
3. **`read_resource` returning a bare `str` defaults the mime type to `text/plain`**, and
   the host then does not treat the resource as an MCP App. Return
   `[ReadResourceContents(content=…, mime_type="text/html;profile=mcp-app")]`.
4. **A widget must send `ui/notifications/size-changed`** or the host leaves it at a 150px
   default and it reads as a blank region. `hostContext.containerDimensions` showed
   `maxHeight: 5000` — the host was never capping us.
5. **`ui/notifications/initialized` must follow the `ui/initialize` *response*.** Sent
   early, claude.ai ignores every later notification, sizing included. Desktop tolerates it.
6. **The approved CSP is at `result.hostCapabilities.sandbox`,** not `result.sandbox`.

Also: `uvx --with mcp` resolves mcp 2.0.0, whose low-level `Server` has no `list_tools`.
Use `uv run` so the project's pinned 1.27.2 is used. mcp 2.0.0 is a breaking change for
this codebase generally.

## Risks carried into implementation

- **Silent truncation.** Clients truncate over-budget results without erroring, and the
  model then misreports what it received — one run claimed "19 of 20" while holding 16,
  and described a half-decoded JPEG as a different, smaller image. Cap server-side; never
  let the client truncate.
- **Signed URLs expire.** TikTok marketplace URLs carry `t`/`ps`/`shp`/`shcp` params. In a
  widget the browser fetches them whenever the user scrolls back, so a persisted
  conversation will show broken images. Proxy them or accept decay.
- **WAF-hostile hosts.** Walmart blocks parameterised requests from some networks; a
  server-side proxy will hit the same wall. Degrade to no image, never to a full-size one.
- **No fallback coverage.** Claude Code, Cursor and MCP Inspector do not render widgets,
  so the text/`structuredContent` path must stay correct on its own.
- **Architectural cost.** A widget means a `resources/` capability the server does not have
  today, HTML/JS assets and a build step in a Python repo, and widget/tool-schema version
  skew. Name it in the implementation ticket rather than discovering it in review.

## Cleanup

The probe lives in `spike/` and is active on `MCP_ENV=staging` only. Revert the whole
stack — six commits, `e7cde21` through `ab916e0` — including the Pillow dependency and the
Dockerfile `COPY spike` / `PYTHONPATH` lines, once this note is accepted.
