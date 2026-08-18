# RD-97 — can an MCP Apps widget invoke a tool?

Spike findings, August 2026. Measured against `mcp-staging.autods.com` from
claude.ai web over a **remote connector**, with Claude Code as the control.
Every row is an observation, not an inference, unless marked otherwise.

Nine recorded calls back this up (`GET /probe/action/calls`, `source: redis`),
covering every path; all nine carry a resolved `user_sub` and `autods_user_id`.

## Recommendation for RD-92

**Render the grid with the widget, use the direct `tools/call` path for reads
only, and put the sourcing request — a write — through `ui/message`.**

On claude.ai web `ui/message` stages an **editable** prompt in the composer
behind an explicit "use caution" warning and waits for the user to press send.
That is precisely the UI-gated confirmation a write wants: it produces a visible
conversation turn, and because the *model* makes the call it holds the result, so
the next turn cannot contradict reality.

The direct path is faster (673ms–3.4s) and needs no ceremony, but it is invisible
on every axis that matters for a write: no consent, no conversation trail, and
the model will actively tell the user the action did not happen. Its one
available mitigation, `ui/update-model-context`, is a **silent no-op**. The
concrete hazard is **duplicate writes**: the model's failure mode is a false
negative ("nothing ran"), not a fabricated confirmation, so a user who asks "did
that work?" is told no and may click again.

If a direct-path write is wanted anyway, accountability has to come from the
server — idempotency keyed on the widget's nonce — not from the protocol. And
note the composer prefill is claude.ai-web behaviour, **not a security
boundary**: another host may auto-send, so write authorization must be enforced
server-side regardless.

## Answers

| # | Question | Answer |
|---|---|---|
| Q1 | Does a widget-issued `tools/call` reach the server and execute? | **Yes.** 6 direct calls executed, 673ms–3.4s, each returning a server-minted marker to the app and recorded server-side. |
| Q2 | Is there a consent step, and who sees it? | **Direct path: none.** No prompt, no turn, no collapsed tool block — nothing renders at all. **`ui/message`: yes** — editable composer prefill + a security warning, user must send. |
| Q3 | Does `visibility: ["app"]` hide the tool from the model? | **Yes, on claude.ai web.** The model listed five `probe_*` tools; `probe_action_app_only` appeared in neither that list nor its tool search — yet the app called it successfully. Claude Code (no MCP Apps support) both listed and called it, so the server offers it unconditionally: the filtering is the host's. |
| Q4 | What does the model learn afterwards? | **Nothing, and it asserts the opposite.** After 2 executed calls it insisted `probe_action` "has not been invoked … by me or by the widget". `ui/update-model-context` does **not** fix this (see below). |
| Q5 | Same four points for `ui/message` | Arrives ✓ (recorded `path: "ui-message"`), consent ✓ (explicit, editable, warned), visible turn ✓, model awareness ✓ — it reported marker `RD97-8A6D04` verbatim because it made the call itself. |

## `ui/update-model-context` is a silent no-op

Button F issued the direct call and then chained a context update carrying the
marker the server actually minted, with unambiguous wording:

> "The user completed an action in the click-to-action widget. The tool
> probe_action ran and returned marker RD97-5C39EB for nonce caea8400. This work
> is already done; do not repeat it."

Both requests returned `{}` in 23–32ms. Run twice (`RD97-5C39EB`,
`RD97-6FD339`). Asked afterwards, the model said *"probe_action still hasn't
run, so nothing has been minted for me to report"* and, to "can you name both
markers?", *"There are no markers — not one, and not two."*

An earlier run on build rd97-1 looked like a positive: cued with the exact prefix
`RD97-CTX`, the model produced `RD97-CTX-5FB958E6` while saying *"not from
anything I'd seen before you asked … to answer you I read the widget's own
context."* That should **not** be scored as the mitigation working — it rests on
the model's own account of its retrieval, it needed the caller to already know
the string, and the properly-worded test (F) is a clear negative. Because nothing
was delivered, whether a second update **overwrites** the first is not
measurable.

## The model does not fabricate — the failure is a false negative

Worth recording, because it changes the risk. Asked for a marker that did not
reach it, the model refused to invent one, and unprompted named the trap:

> "A marker is a one-off string with no derivable structure, so anything I
> produced here would be fabrication dressed up as a result … a plausible-looking
> fake marker is worse than an empty answer."

and on the plural ask:

> "the shift from 'the marker' to 'both markers' makes the answer easier to get
> wrong … a plural ask invites me to produce a matched pair."

So the direct path does not produce false confirmations; it produces confident
denials of work that did happen. For a write, that means retries and duplicates,
not phantom successes.

## Protocol gotchas (add to RD-82's six)

7. **`ui/message`'s `params.content` must be an array.** The published spec
   example shows a bare `{type, text}` object; claude.ai rejects it with
   `-32603 … "expected array, received object"`. Trust the host over the example.
8. **`tools/list` is not available to an app** — `-32601 Method not found`, even
   though `hostCapabilities.serverTools` is advertised. `serverTools` means the
   app may *call* tools, not enumerate them: a widget must know its tool names up
   front.
9. **`ui/update-model-context` returns success and does nothing** (see above).
   Never treat its `{}` as evidence the model was informed.
10. **`ui/message` is a composer prefill on claude.ai web, not an autonomous
    send.** The app cannot force a turn; it can only propose one. Do not design a
    flow that assumes the message was sent.

## Client matrix (RD-97 axes)

| | widget renders | app→`tools/call` | honours `visibility:["app"]` |
|---|---|---|---|
| claude.ai web (remote connector) | ✅ | ✅ | ✅ hidden from model, callable by app |
| Claude Code | n/a | n/a | ❌ lists **and** calls it — the control |
| Claude Desktop (remote connector) | ❌ per RD-82 (raw `structuredContent`) | not reached | not reached |

## Risks carried into RD-92

- **Duplicate writes on the direct path.** The model denies completed work, so
  the user retries. Idempotency on a widget-supplied key is mandatory if the
  direct path is used for anything that mutates.
- **The model may drop optional arguments.** Two model-issued calls arrived with
  `nonce: null`, and the `ui/message` call dropped `note`. Anything the write
  depends on must not be optional, and must not rely on the model relaying it.
- **Consent UX is host-specific and is not a control.** The prefill + warning is
  claude.ai web. Enforce authorization server-side.
- **An app cannot discover tools**, so widget and tool names are coupled at build
  time — version skew between widget and tool schema is a real failure mode.
- **No fallback coverage.** Desktop over a remote connector does not render the
  widget at all (RD-82), so the text/`structuredContent` path must stay correct
  on its own.

## Cleanup

Probe stack is `MCP_ENV=staging`-only and spans four commits (`aade79b`,
`bf99044`, `cafe51b`, `ccde66a`). Revert all four once this note is on the
ticket. `__version__` moves forward, never backwards.

Cost: two staging deploys (rd97-1, rd97-2) rather than the one the timebox
allowed — the second was needed because `ui/message` had to be re-shaped and
because the D result turned out to measure delivery rather than belief.
