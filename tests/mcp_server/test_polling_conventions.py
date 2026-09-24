"""RD-91 — the polling cadence, on the channels a client actually receives.

Two steps of the sourcing flow are asynchronous and the web app watches both over
Pusher/SSE. This server has no push channel, so each becomes a polling loop the
agent drives: one tool call per attempt, one turn per tool call. The decision was
thin tools plus *documented* agent-side polling — which makes the documentation the
feature, and an undelivered number the whole bug.

So these tests do what the RD-90 lesson says: assert the numbers arrive at a
client, not merely that the manifest parses. The cadence is delivered on three
channels at once (tool ``notes``, playbook ``body``, server ``instructions``), and
the failure mode of drift between them is an agent that polls at whichever number
it read last.

The retired frontend cadence is asserted *absent* for the same reason: 3 s / 120 s
is correct for a browser and wrong here, and it is the number an author is most
likely to copy.

Canonical source for all of it: ``docs/polling-conventions.md``.
"""

import json
from pathlib import Path

import pytest

from autods_mcp_server.manifests import (
    build_instructions,
    build_playbook_index,
    build_playbook_registry,
    build_registry,
    load_manifests,
)
from autods_mcp_server.tools import to_tool
from tests.mcp_server.conftest import mcp_client_session

# The cadence, as tokens that must appear in each channel that carries it. Not a
# wording assertion: the four numbers are the contract, the sentences around them
# differ per channel by design (tier 2 states a tool's contract, tier 3 a runbook
# step, tier 4 a one-clause invariant).
CADENCE_TOKENS = ("~10s", "~15s", "10 attempts", "~2.5 min")

# The frontend interval and ceiling (``useSourcingProducts.jsx``: timeoutLimit
# 3000, REQUEST_TIMEOUT 120000), retired in documentation by RD-91.
RETIRED_FRONTEND_CADENCE = ("every 3 s", "every 3 seconds", "3000", "120 s", "120 seconds")

# Numbers that once stated the ceiling and no longer do. Asserted *absent*, not
# merely "the right one is present": the tokens above cannot catch a stale
# duration sitting next to a correct one, and two ceilings in one channel is the
# same undelivered-number bug as no ceiling at all. ``~3 min`` was the original
# ceiling — 10 attempts at this cadence is ~2.4 min, so the minutes could never
# bind and the checklist quoting them told an operator to record an elapsed time
# the run had not reached.
SUPERSEDED_CEILINGS = ("~3 min", "3 minutes", "whichever comes first")

# The bulk-action state machine. 1/2 are non-final, 3/4/99 final — the distinction
# an agent needs in order to stop, and the one an author is tempted to compress
# into "poll until finished".
NON_FINAL_STATUSES = ("1=created", "2=in_progress")
FINAL_STATUSES = ("3=finished", "4=canceled", "99=error")

# The store-quote state machine (RD-93). Strings on the wire, not integers —
# the one place the server-wide "enums are integers" invariant does not hold —
# and `ready` is the trap: the offer has arrived but the product is not attached
# to it yet, so it reads like an end state and is not one.
STORE_QUOTE_STATUSES = ("`new`", "`in_progress`", "`ready`", "`linked`", "`cannot_be_sourced`")


def _tool_text(manifest_dir: Path, operation_id: str) -> str:
    """Everything one tool descriptor ships: description (summary + notes + the
    playbook tail) and inputSchema."""
    playbooks = build_playbook_registry(manifest_dir)
    tool = to_tool(build_registry(manifest_dir).get(operation_id), playbooks)
    return tool.description + json.dumps(tool.input_schema)


# --- tier 2: the polling tool's own notes ------------------------------------


def test_the_polling_tool_carries_the_cadence_itself(bundled_manifest_dir: Path) -> None:
    """``get_bulk_action_items`` is the reference implementation (RD-91), and it
    is the descriptor an agent is holding at the moment it decides when to poll
    again. A cadence reachable only from a *different* tool's text, or only from
    the playbook, is not delivered where the decision is made."""
    text = _tool_text(bundled_manifest_dir, "get_bulk_action_items")

    for token in CADENCE_TOKENS:
        assert token in text, f"get_bulk_action_items no longer states {token!r}"


def test_the_polling_tool_distinguishes_final_from_non_final(bundled_manifest_dir: Path) -> None:
    """ "Keep polling while 1 or 2" is the whole state machine; without the final
    states named, an agent cannot tell a finished job from a canceled one."""
    text = _tool_text(bundled_manifest_dir, "get_bulk_action_items")

    for token in NON_FINAL_STATUSES + FINAL_STATUSES:
        assert token in text, f"get_bulk_action_items no longer names status {token!r}"


def test_the_polling_tool_warns_that_a_successful_read_is_not_success(bundled_manifest_dir: Path) -> None:
    """A 2xx from the poll says the progress query worked. Reading it as "the
    import worked" is the classic false success this chain produces, and the
    warning has to sit on the tool that returns the 2xx."""
    text = _tool_text(bundled_manifest_dir, "get_bulk_action_items").lower()

    assert "autods_product_id" in text, "the id a finished item yields is what the next step needs"
    assert "no item is left at 1 or 2" in text


# --- tier 2: the sourcing-request poll (RD-93) -------------------------------


def test_the_sourcing_poll_carries_the_cadence_itself(bundled_manifest_dir: Path) -> None:
    """``list_store_quotes`` is the second polling tool, and the same rule
    applies: the cadence has to be on the descriptor the agent is holding when
    it decides whether to call again, not only in the runbook."""
    text = _tool_text(bundled_manifest_dir, "list_store_quotes")

    for token in CADENCE_TOKENS:
        assert token in text, f"list_store_quotes no longer states {token!r}"


def test_the_sourcing_poll_names_every_state(bundled_manifest_dir: Path) -> None:
    """All five, or the agent cannot tell "keep going" from "stop". ``ready`` is
    the one that costs something when it is missing: it looks like an end state
    and the flow is not finished until ``linked``."""
    text = _tool_text(bundled_manifest_dir, "list_store_quotes")

    for token in STORE_QUOTE_STATUSES:
        assert token in text, f"list_store_quotes no longer names status {token}"


def test_the_sourcing_poll_says_a_vanished_request_is_a_failure(bundled_manifest_dir: Path) -> None:
    """The correction that matters most (RD-91, RD-93). A failed sourcing
    request is *removed* rather than marked failed, so a loop that waits for a
    final status waits for ever — and an empty ``results`` means different
    things before and after the write. A poll specified only as "wait for a
    terminal status" is a bug, and this is the assertion that keeps the two
    readings in the text."""
    text = _tool_text(bundled_manifest_dir, "list_store_quotes").lower()

    assert "disappears" in text, "the fourth outcome is not named"
    assert "still working" in text, 'the "gone means failed, not still working" reading is missing'
    assert "ambiguous" in text, "an empty results reads two ways and the notes must say which is which"
    assert "before the write" in text and "after the write" in text


def test_the_sourcing_poll_warns_that_a_successful_read_is_not_success(bundled_manifest_dir: Path) -> None:
    """The trigger answers ``{"status": "ok"}`` before any work happens and this
    read answers 2xx whatever the request is doing, so ``ok`` is true on both
    while nothing has been sourced. The warning belongs on the tool holding the
    status."""
    text = _tool_text(bundled_manifest_dir, "list_store_quotes").lower()

    assert "transport-level" in text
    assert "the status is the only evidence" in text


# --- tier 2: the sourcing write (RD-94) --------------------------------------


def test_the_sourcing_write_says_its_answer_is_not_the_outcome(bundled_manifest_dir: Path) -> None:
    """``create_1688_sourcing_request`` answers ``{"status": "ok"}`` before any
    sourcing has happened, and nothing else ever reports the outcome. The tool
    returning that answer is the one that has to say what it does not mean —
    a warning reachable only from the *poll* arrives after the agent has already
    decided the work is done."""
    text = _tool_text(bundled_manifest_dir, "create_1688_sourcing_request")

    assert "accepted for processing" in text
    assert "transport-level" in text
    for token in CADENCE_TOKENS:
        assert token in text, f"create_1688_sourcing_request no longer states {token!r}"


def test_the_sourcing_write_says_a_vanished_request_is_a_failure(bundled_manifest_dir: Path) -> None:
    """The write is where an agent learns what it is waiting for, and the failure
    mode has no status of its own: the record is removed. Stated only on the
    poll, it arrives to an agent that has already written the loop."""
    text = _tool_text(bundled_manifest_dir, "create_1688_sourcing_request").lower()

    assert "removed" in text
    assert "cannot_be_sourced" in text
    assert "never appears" in text


def test_the_sourcing_write_carries_the_poll_before_retry_recovery(bundled_manifest_dir: Path) -> None:
    """The one that prevents a duplicated charge. There is no idempotency key
    upstream, and a transport failure carries no answer at all — so "it failed,
    try again" is exactly the wrong reflex, and the tool has to say which read
    establishes whether the first attempt landed.

    RD-110's ``on_failure`` block delivers the same thing on the failure path,
    where it arrives at the moment of the decision. Both, deliberately: the
    contract is tier 2 and the moment-of-failure nudge is tier 3."""
    text = _tool_text(bundled_manifest_dir, "create_1688_sourcing_request").lower()

    assert "do not submit it again" in text
    assert "list_store_quotes" in text
    assert "credit already charged" in text


def test_both_credit_spending_writes_state_the_price(bundled_manifest_dir: Path) -> None:
    """Both sourcing requests charge an auto-order credit the moment they are
    accepted, and neither can be cancelled afterwards. ``request_manual_sourcing``
    is the one that *reads* as free — it takes no body and submits in one call —
    so the price has to be on its own descriptor rather than inferred from the
    tool beside it. The destructive hint is the other half and is asserted in
    ``test_transport``; a host prompt says "this tool is destructive", never what
    it costs.

    Checked on the first paragraph of ``notes`` only, not on the whole text: a
    model reads the opening of a description far more reliably than its middle,
    and a price that drifted down the notes would still pass a whole-text check."""
    registry = build_registry(bundled_manifest_dir)
    for operation_id in ("create_1688_sourcing_request", "request_manual_sourcing"):
        text = registry.get(operation_id).notes.split("\n\n")[0].lower()

        assert "1 auto-order credit" in text, f"{operation_id} no longer states the credit cost"
        assert "not refundable" in text, f"{operation_id} no longer states the charge is not refundable"
        assert "cannot be cancelled" in text, f"{operation_id} no longer states the request cannot be cancelled"


def test_the_sourcing_writes_send_the_quote_lifecycle_to_the_web_app(bundled_manifest_dir: Path) -> None:
    """Three endpoints manage a manual quote afterwards and none of them is
    exposed here, so an agent that submits one and then looks for the tool to
    re-request it finds none. ``get_store_quote_versions`` makes that worse by
    showing versions nothing here can act on — hence the pointer at the web app
    on the tools that create the situation."""
    for operation_id in ("create_1688_sourcing_request", "request_manual_sourcing"):
        text = _tool_text(bundled_manifest_dir, operation_id).lower()

        assert "web app" in text, f"{operation_id} no longer says where the quote lifecycle is handled"


def test_the_sourcing_write_offers_the_failure_exit(bundled_manifest_dir: Path) -> None:
    """Sourcing needs an *active* product, so a chain that stops half way leaves
    a live listing priced at supplier cost. The runbook (RD-110) owns the full
    exit; the tool that creates the exposure still has to name it and point at
    the tool that undoes it."""
    for operation_id in ("create_1688_sourcing_request", "request_manual_sourcing"):
        text = _tool_text(bundled_manifest_dir, operation_id)

        assert "delete_product" in text, f"{operation_id} no longer names the cleanup tool"


# --- tier 2: the supplier scans (RD-95) --------------------------------------

SUPPLIER_SCAN_OPS = ("search_1688_offers_by_image", "get_1688_product_details")


@pytest.mark.parametrize("operation_id", SUPPLIER_SCAN_OPS)
def test_the_supplier_scans_carry_the_cadence_themselves(bundled_manifest_dir: Path, operation_id: str) -> None:
    """Both scans are polled — the same call, repeated — so each is the
    descriptor the agent holds when it decides whether to call again."""
    text = _tool_text(bundled_manifest_dir, operation_id)

    for token in CADENCE_TOKENS:
        assert token in text, f"{operation_id} no longer states {token!r}"


@pytest.mark.parametrize("operation_id", SUPPLIER_SCAN_OPS)
def test_the_supplier_scans_force_a_scrape_on_the_first_attempt_only(
    bundled_manifest_dir: Path, operation_id: str
) -> None:
    """``full_scrape`` true on every poll restarts the scan each time, so it
    never finishes. The rule has to be on the parameter (tier 1, where the value
    is chosen) and in the notes (tier 2, where the loop is described)."""
    registry = build_registry(bundled_manifest_dir)
    operation = registry.get(operation_id)
    parameter = next(p for p in operation.parameters if p.name == "full_scrape")

    assert "FIRST call" in (parameter.description or "")
    assert "`full_scrape: true` on the first call only" in (operation.notes or "")


@pytest.mark.parametrize("operation_id", SUPPLIER_SCAN_OPS)
def test_the_supplier_scans_say_what_the_ceiling_means(bundled_manifest_dir: Path, operation_id: str) -> None:
    """A ceiling with nothing found is neither "found nothing" nor "done"."""
    notes = build_registry(bundled_manifest_dir).get(operation_id).notes or ""

    assert "On reaching the ceiling" in notes
    assert "do not keep polling" in notes


# --- tier 3: the playbook runbook --------------------------------------------


def test_the_playbook_body_states_the_same_numbers(bundled_manifest_dir: Path) -> None:
    """The runbook is where an agent that entered the chain reads the cadence, so
    it repeats the numbers rather than pointing at the tool — but it must repeat
    *these* numbers. Two channels disagreeing is worse than one being silent."""
    playbook = build_playbook_registry(bundled_manifest_dir).get("product_import")

    for token in CADENCE_TOKENS:
        assert token in playbook.body, f"the product_import runbook no longer states {token!r}"


def test_the_playbook_says_what_hitting_the_ceiling_means(bundled_manifest_dir: Path) -> None:
    """A bounded loop needs a third answer beside finished and failed. Left
    unsaid, an agent that exhausts the attempts reports the import as done."""
    playbook = build_playbook_registry(bundled_manifest_dir).get("product_import")
    step = next(s for s in playbook.steps if s.operation_id == "get_bulk_action_items")

    assert "ceiling" in playbook.body
    assert "ceiling" in (step.incomplete_alone or "")


# --- tier 4: the server-wide invariant --------------------------------------


def test_the_server_index_carries_the_cadence_once(bundled_manifest_dir: Path) -> None:
    """The cadence is server-wide, so it earns one clause of the "writes are
    asynchronous" invariant — and no more than that. ``instructions`` rides in the
    client's system prompt on every turn and sits in the cached prefix."""
    instructions = build_instructions(
        load_manifests(bundled_manifest_dir),
        playbook_index=build_playbook_index(build_playbook_registry(bundled_manifest_dir)),
    )

    for token in CADENCE_TOKENS:
        assert token in instructions, f"the server index no longer states {token!r}"


# --- what must not be there --------------------------------------------------


def _all_shipped_text(manifest_dir: Path) -> str:
    """Every string the manifests and playbooks ship to a client."""
    parts: list[str] = []
    for manifest in load_manifests(manifest_dir):
        parts.append(manifest.instructions)
        for operation in manifest.operations:
            parts.extend([operation.summary, operation.description, operation.notes or ""])
            parts.append(json.dumps(operation.body_schema or {}))
            parts.extend(parameter.description or "" for parameter in operation.parameters)
    for playbook in build_playbook_registry(manifest_dir).list_playbooks():
        parts.append(playbook.model_dump_json())
    return "\n".join(parts)


@pytest.mark.parametrize("retired", RETIRED_FRONTEND_CADENCE)
def test_no_channel_ships_the_retired_frontend_cadence(bundled_manifest_dir: Path, retired: str) -> None:
    """The frontend polls the scrapers every 3 s with a 120 s ceiling. For an
    agent a tool round-trip is already seconds and every attempt costs a turn, so
    that loop spends the conversation re-reading the same unfinished job. It is
    also the number an author is most likely to copy from the web app."""
    assert retired not in _all_shipped_text(bundled_manifest_dir)


@pytest.mark.parametrize("superseded", SUPERSEDED_CEILINGS)
def test_no_channel_ships_a_superseded_ceiling(bundled_manifest_dir: Path, superseded: str) -> None:
    """A stale duration beside a correct one is invisible to the presence
    assertions above — every channel would still contain ``10 attempts`` and
    ``~2.5 min`` and the test would pass while the text stated two ceilings. The
    ceiling is the attempt count and the duration is derived from it, so a second
    number is always either wrong or redundant."""
    assert superseded not in _all_shipped_text(bundled_manifest_dir)


def test_the_cadence_is_stated_numerically_not_vaguely(bundled_manifest_dir: Path) -> None:
    """ "Poll periodically" produces either one poll or forty, which is the reason
    RD-91 exists at all. Guard the two phrasings the manifests actually used."""
    text = _all_shipped_text(bundled_manifest_dir).lower()

    for vague in ("poll periodically", "fifteen to thirty seconds", "a few minutes of no movement"):
        assert vague not in text, f"manifest text fell back to vague cadence guidance: {vague!r}"


# --- delivery ----------------------------------------------------------------


async def test_the_cadence_reaches_a_real_client(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The acceptance test: a real handshake plus ``tools/list``, asserting the
    numbers are in what the wire actually carried. Everything above reads the
    manifests through the loader; this reads what a client receives."""
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        instructions = session.instructions
        tools = await session.list_tools()

    poller = next(tool for tool in tools.tools if tool.name == "get_bulk_action_items")

    for token in CADENCE_TOKENS:
        assert token in poller.description, f"the delivered tool descriptor lacks {token!r}"
        assert token in (instructions or ""), f"the delivered instructions lack {token!r}"


async def test_the_sourcing_state_machine_reaches_a_real_client(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The same acceptance test for the second polling tool (RD-93). The
    cadence *and* the disappearing-request reading are checked on the wire: a
    manifest field nothing reads is invisible rather than harmless, and this is
    the one channel that proves the text left the server."""
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        tools = await session.list_tools()

    poller = next(tool for tool in tools.tools if tool.name == "list_store_quotes")

    for token in CADENCE_TOKENS:
        assert token in poller.description, f"the delivered tool descriptor lacks {token!r}"
    for token in STORE_QUOTE_STATUSES:
        assert token in poller.description, f"the delivered tool descriptor lacks status {token}"
    assert "disappears" in poller.description.lower()


async def test_the_sourcing_write_contract_reaches_a_real_client(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """RD-94. The three things that cost the user real money if they do not
    arrive — the credit price, the cadence the agent waits on, and the
    poll-before-retry rule that stops a second charge — checked on the wire
    rather than through the loader. A manifest field nothing reads is invisible
    rather than harmless."""
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        tools = await session.list_tools()

    for scan in SUPPLIER_SCAN_OPS:
        delivered = next(tool for tool in tools.tools if tool.name == scan)
        for token in CADENCE_TOKENS:
            assert token in delivered.description, f"the delivered {scan} descriptor lacks {token!r}"

    writer = next(tool for tool in tools.tools if tool.name == "create_1688_sourcing_request")
    description = writer.description.lower()

    assert "1 auto-order credit" in description
    assert "do not submit it again" in description
    for token in CADENCE_TOKENS:
        assert token in writer.description, f"the delivered tool descriptor lacks {token!r}"
    # The annotation is the half a host reads: without it no client asks the
    # user anything before spending the credit.
    assert writer.annotations.destructive_hint is True
    assert writer.annotations.read_only_hint is False
