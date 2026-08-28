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
CADENCE_TOKENS = ("~10s", "~15s", "10 attempts", "~3 min")

# The frontend interval and ceiling (``useSourcingProducts.jsx``: timeoutLimit
# 3000, REQUEST_TIMEOUT 120000), retired in documentation by RD-91.
RETIRED_FRONTEND_CADENCE = ("every 3 s", "every 3 seconds", "3000", "120 s", "120 seconds")

# The bulk-action state machine. 1/2 are non-final, 3/4/99 final — the distinction
# an agent needs in order to stop, and the one an author is tempted to compress
# into "poll until finished".
NON_FINAL_STATUSES = ("1=created", "2=in_progress")
FINAL_STATUSES = ("3=finished", "4=canceled", "99=error")


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
