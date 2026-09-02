"""Section C of ``docs/release-checks.md``, automated: the handshake payload.

What reaches a client at ``initialize`` is the whole of this server's contract
that no unit test can see — a manifest field that parses but never ships, a
description baked into a stale image, an annotation hint a host gates its
confirmation prompt on. Section C is where that gets checked, and it was checked
by hand five times before it was checked by a test once.

**The design: one diff, no hand-maintained expectations.** Every assertion below
compares the *deployed* payload against the payload this checkout's manifests
build, using the same loaders, lints and ``build_tools`` call that
``build_runtime`` uses. Nothing here lists the tools, restates a description or
tabulates the annotation hints, because every such list is a fifth copy that
rots — the ``operations_count`` gotcha in ``CLAUDE.md`` is about exactly that.
The diff therefore covers C3 (tool set + instructions), C4 (descriptions), C5
(annotations) and R13's descriptor half (the ``search_products`` filter
vocabulary) at once, and keeps covering them when a tool is added.

**This suite is coupled to the checkout, unlike section S.** Run it from the
commit that was released. Run from ``develop`` against a lagging environment it
will report your own unreleased manifests as drift — which is why the version
check runs first and says so in its failure message.

What is deliberately *not* here:

* **C1 / C2** — a fresh browser authorization and a reconnect. C1 is the one
  step a human must drive (its recipe removes and re-adds the server), and no
  test can hold a browser session. The fixture connecting at all is the only
  part of C2 that automates.
* **C7 (tails) and C9 (cadence numbers)** are implied by description equality:
  if the deployed description matches local byte-for-byte, the tail and the
  numbers arrived. The *local* side of both is already guarded in-process by
  ``tests/mcp_server/test_playbooks.py`` and
  ``tests/mcp_server/test_polling_conventions.py``. One live cross-channel
  assertion is kept below anyway, because a channel disagreement is the
  regression the checklist calls user-visible.
"""

import json

import pytest

from tests.e2e.conftest import Handshake, LocalBuild
from tests.mcp_server.test_polling_conventions import (
    CADENCE_TOKENS,
    FINAL_STATUSES,
    NON_FINAL_STATUSES,
)

# The tool whose ``notes`` carry the polling cadence, and the invariant clause in
# ``instructions`` that has to agree with them.
_POLLING_TOOL = "get_bulk_action_items"

# The first line of the tier-4 index. Asserted as an anchor, not as prose: if the
# instructions arrive truncated or replaced, this is what goes missing first.
_INSTRUCTIONS_HEADER = "## AutoDS MCP — start here"

# Fields that make up a tool descriptor on the wire. ``name`` is the key, so the
# set difference is reported separately.
_DESCRIPTOR_FIELDS = ("description", "inputSchema", "annotations")


def _first_difference(local: str, live: str) -> str:
    """Where two descriptions start to differ, for a readable assertion message."""
    for index, (left, right) in enumerate(zip(local, live, strict=False)):
        if left != right:
            return (
                f"first differ at char {index}: "
                f"local {local[index : index + 70]!r} vs live {live[index : index + 70]!r}"
            )
    longer, label = (local, "local") if len(local) > len(live) else (live, "live")
    return f"one is a prefix of the other; {label} has {longer[min(len(local), len(live)) :][:200]!r} extra"


def test_c4_deployed_build_is_the_one_this_checkout_describes(
    deployed_handshake: Handshake, local_build: LocalBuild
) -> None:
    """The ``serverInfo`` version stamp pins the build every other check grades.

    mcp 2.x attaches ``serverInfo`` to every result's ``_meta`` and it rides the
    same single-sourced ``__version__`` as the Sentry release tag, so this is the
    cheapest possible answer to "which build am I actually testing" — no manifest
    diffing and no Sentry event required.

    A mismatch is not necessarily a bad release: far more often it means this
    checkout is ahead of (or behind) the deployment. Either way every assertion
    below is comparing two different builds, so read this failure first.
    """
    assert deployed_handshake.server_version == local_build.server_version, (
        f"the deployed server reports version {deployed_handshake.server_version!r} but this checkout is "
        f"{local_build.server_version!r}. Every other check in this module diffs the deployed payload against "
        f"this checkout's manifests, so they are now comparing two different builds. Check out the released "
        f"commit before reading anything into the failures below."
    )


def test_c3_the_deployed_tool_set_is_exactly_what_the_manifests_register(
    deployed_handshake: Handshake, local_build: LocalBuild
) -> None:
    """C3's tool count and list — derived, so it can never go stale.

    This is the assertion that retires the fourth hand-maintained inventory. The
    other three (``test_loader``, ``test_transport``, ``test_staging_smoke``) are
    executed by something; C3's count lived only in prose and was left reading
    "12" against a 13-tool handshake after RD-89, which is how a run gets told a
    healthy release is broken.
    """
    missing = sorted(set(local_build.tools) - set(deployed_handshake.tools))
    unexpected = sorted(set(deployed_handshake.tools) - set(local_build.tools))
    assert not missing and not unexpected, (
        f"tools/list does not match the manifests: registered but not advertised={missing}, "
        f"advertised but not registered={unexpected} "
        f"(deployed {len(deployed_handshake.tools)} tools, manifests build {len(local_build.tools)})"
    )


def test_c3_instructions_arrive_and_match_the_manifests(deployed_handshake: Handshake, local_build: LocalBuild) -> None:
    """The tier-4 block reached the client, and is the one this checkout builds.

    A parsed manifest field that nothing forwards is invisible rather than
    harmless — ``Manifest.instructions`` was parsed from day one and passed to
    nobody for the whole life of the server (RD-90). This asserts arrival, not
    just that the text exists locally.
    """
    assert deployed_handshake.instructions, (
        "the deployed server advertised no instructions at all — the tier-4 block reached no client"
    )
    assert deployed_handshake.instructions.startswith(_INSTRUCTIONS_HEADER), (
        f"instructions do not start with {_INSTRUCTIONS_HEADER!r}; got {deployed_handshake.instructions[:80]!r}"
    )
    assert deployed_handshake.instructions == local_build.instructions, (
        "the deployed instructions differ from what this checkout builds: "
        f"{_first_difference(local_build.instructions, deployed_handshake.instructions)}"
    )


@pytest.mark.parametrize("field", _DESCRIPTOR_FIELDS)
def test_c4_c5_every_descriptor_field_matches_the_manifests(
    deployed_handshake: Handshake, local_build: LocalBuild, field: str
) -> None:
    """C4 (descriptions), C5 (annotations) and R13's descriptor half, in one diff.

    Parametrised per field so a description drift and an ``inputSchema`` drift
    are separate failures rather than one wall of JSON.

    ``inputSchema`` is where the ``search_products`` filter vocabulary lives
    (RD-107) and where ``get_playbook``'s ``name`` enum indexes the chains
    (RD-100) — both unguessable, so their absence silently degrades an agent
    rather than erroring. ``annotations`` is what hosts gate confirmation
    prompts on, so a lost ``destructiveHint`` is a safety regression that no
    call would ever reveal.
    """
    shared = sorted(set(local_build.tools) & set(deployed_handshake.tools))
    drifted = []
    for name in shared:
        local_value = local_build.tools[name].get(field)
        live_value = deployed_handshake.tools[name].get(field)
        if local_value == live_value:
            continue
        if field == "description":
            detail = _first_difference(str(local_value or ""), str(live_value or ""))
        else:
            detail = (
                f"local={json.dumps(local_value, sort_keys=True)[:400]} "
                f"live={json.dumps(live_value, sort_keys=True)[:400]}"
            )
        drifted.append(f"  {name}.{field}: {detail}")
    assert not drifted, (
        f"{len(drifted)} tool(s) serve a different {field} than this checkout builds — the pod is running "
        "another build (a cached image tag is the usual cause):\n" + "\n".join(drifted)
    )


def test_c6_c8_playbook_resources_are_advertised_and_shaped(
    deployed_handshake: Handshake, local_build: LocalBuild
) -> None:
    """C8 — the playbook resource mirror, and the capability it declares.

    Registering ``on_list_resources`` is what puts ``resources`` in the
    handshake's capability block, so an error here means the server stopped
    declaring the capability — a regression, not a client quirk. The explicit
    ``mimeType`` matters too: a bare string advertises ``text/plain`` and the
    markdown body is lost.
    """
    assert deployed_handshake.resources is not None, (
        f"resources/list failed on the deployed server ({deployed_handshake.resources_error}) — the server "
        "is no longer declaring the resources capability"
    )
    by_uri = {r["uri"]: r for r in deployed_handshake.resources}
    for expected in local_build.resources:
        live = by_uri.get(expected["uri"])
        assert live is not None, f"{expected['uri']} is registered locally but not advertised; got {sorted(by_uri)}"
        assert live.get("mimeType") == expected["mimeType"], (
            f"{expected['uri']} advertises mimeType {live.get('mimeType')!r}, expected {expected['mimeType']!r} "
            "— a bare string loses the markdown"
        )


def test_c6_get_playbook_enum_indexes_every_registered_playbook(
    deployed_handshake: Handshake, local_build: LocalBuild
) -> None:
    """C6 — the ``name`` enum *is* the playbook index, so it must not be empty.

    ``inputSchema`` is the most reliably delivered channel there is, which is why
    the chain index rides it rather than ``instructions``. An empty enum means
    the playbook files did not ship in the image: every other tool still works,
    and the chain guidance is silently absent for every user. Called out
    separately from the schema diff so that failure names its own cause.
    """
    enum = deployed_handshake.tools["get_playbook"]["inputSchema"]["properties"]["name"].get("enum")
    expected = sorted(r["name"] for r in local_build.resources)
    assert enum, "get_playbook advertises no playbook enum — the playbook files did not ship in the image"
    assert sorted(enum) == expected, (
        f"get_playbook enum {sorted(enum)} does not index the registered playbooks {expected}"
    )


def test_c9_the_polling_cadence_agrees_across_both_live_channels(deployed_handshake: Handshake) -> None:
    """C9 — the cadence numbers arrive, and the two channels do not disagree.

    The numbers come from ``test_polling_conventions`` rather than being retyped:
    the cadence lives in ``docs/polling-conventions.md`` once, and a fifth copy
    of "10 attempts" in a test file is the drift that documentation is trying to
    avoid. This is the only assertion here that is not a local/live diff, because
    the failure it catches — the two channels stating *different* numbers — is a
    disagreement between two halves of the deployed payload.
    """
    notes = deployed_handshake.tools[_POLLING_TOOL]["description"]
    instructions = deployed_handshake.instructions
    for token in CADENCE_TOKENS:
        assert token in notes, f"{_POLLING_TOOL} no longer states {token!r}; an agent gets one poll or forty"
    for token in (*NON_FINAL_STATUSES, *FINAL_STATUSES):
        assert token in notes, f"{_POLLING_TOOL} no longer states {token!r} — an agent cannot tell when to stop"
    missing = [token for token in CADENCE_TOKENS if token not in instructions]
    assert not missing, (
        f"the instructions' async invariant is missing {missing} while {_POLLING_TOOL} states them — "
        "two channels disagreeing about the cadence is the regression, not a cosmetic drift"
    )
