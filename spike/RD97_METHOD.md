# RD-97 — running the click-to-action probe

Question: **can an MCP Apps widget invoke a tool?** RD-92's offer picker is
designed around the assumption that it can. RD-82 never tested it.

Deploy `develop` to staging, then work through this in **claude.ai web** — it is
the only client RD-82 found that renders a `ui://` widget from a *remote*
connector (Desktop over a remote connector showed raw `structuredContent`).

## Before you start

* `GET https://mcp-staging.autods.com/probe/action/widget.html` — confirms which
  build is actually deployed. If the `probe-build` meta tag is not `rd97-1`, the
  rollout has not landed and everything below will measure the old bytes.
* `GET https://mcp-staging.autods.com/probe/action/calls` — should be an empty
  list. This is the evidence endpoint; keep it open in a second tab.
* Server log lines are `event=rd97_probe_action`, one per recorded call.

Render the widget by asking the model: *"call probe_action_widget"*.

## The five buttons

Each button records its own request, the host's response, and — the case that
matters — **NO RESPONSE after 8s**. All six RD-82 bugs failed silently, so
silence is a result, not a blank.

| | Button | Answers |
|---|---|---|
| A | `tools/call` → `probe_action` | Q1 does it arrive, Q2 is there a consent step |
| B | `tools/call` → `probe_action_app_only` | Q3 can the app call a model-hidden tool |
| C | `ui/message` | Q5 the model-mediated path |
| D | `ui/update-model-context` | the mitigation for Q4, measured not assumed |
| E | `tools/list` | Q3 from the app side — what the host shows the *app* |

## The measurement discipline that makes Q4 answerable

Every call mints a fresh `marker` (`RD97-XXXXXX`) that exists nowhere in the
conversation until the tool returns it. So, after pressing A:

1. Watch for an approval prompt. **Screenshot it** — acceptance criterion 2. If
   none appears, that absence is the finding; say so explicitly.
2. Check `/probe/action/calls`. A new entry with `path: "direct"` is the proof
   the call reached the server (criterion 1). Note `user_sub` — a widget call
   arriving on the user's own authenticated session is part of the write-safety
   story.
3. Ask the model, in the same conversation: *"What just happened? Did any tool
   run? If so, what did it return?"* — **without pasting the marker.** If it can
   produce `RD97-XXXXXX`, the result entered its context. If it cannot, the
   widget mutated state behind the model's back, which is the highest-risk
   finding in this ticket. Record the answer **verbatim** (criterion 4).

Repeat 1–3 for C, and for D (ask it to repeat the `RD97-CTX-…` marker).

For B and E, also ask the model directly: *"list every tool you can see whose
name starts with probe_"*. If `probe_action_app_only` is absent from the model's
list but present in the app's `tools/list` (button E), `visibility: ["app"]` is
honoured. Then ask it to call `probe_action_app_only` anyway and record what
happens (criterion 3).

## If button A fails outright

Stop — that is the timebox rule on the ticket. Record the host's response (or
the silence) and report. RD-92's offer picker then needs a different interaction
design, which is a design decision, not more debugging.

## Wire formats

Taken from the ext-apps spec (2026-01-26), not guessed:

* app → host tool call: `{"method": "tools/call", "params": {"name": …, "arguments": {…}}}`
* app → host message: `{"method": "ui/message", "params": {"role": "user", "content": {"type": "text", "text": …}}}`
* tool visibility: `_meta.ui.visibility`, values `["model"]` / `["app"]` /
  both (default both) — **not** a top-level `visibility` field.
* model context: `{"method": "ui/update-model-context", "params": {"content": [ContentBlock]}}`

## On completion

1. Write the answers up as an RD-82-style comment on RD-97 — the code does not
   survive, so the ticket is the deliverable. Include the one-paragraph
   recommendation to RD-92: which path the offer picker uses, and the
   write-safety story.
2. Revert the spike stack (the restore commit and the RD-97 commit), exactly as
   RD-82 did. Move `__version__` forward, never backwards.
