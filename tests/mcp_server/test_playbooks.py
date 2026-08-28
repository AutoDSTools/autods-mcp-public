"""RD-100 — playbooks: chain metadata as data, delivered lazily.

Four things are under test here, and they map onto the four channels a chain
reaches a model through:

* **the files** — a dedicated loader reads ``manifests/playbooks/``, and nothing
  a playbook file contains may leak into the operation registry;
* **the derivation** — step numbers, successors and the ``operation_id -> steps``
  index come from list position, never from an authored number, so renumbering a
  chain or adding an operation to a second chain cannot desynchronise them;
* **the boot lints** — every one of them refuses to boot, because each failure
  they catch is silent at runtime (a chain naming a tool that doesn't exist, a
  destructive step whose retry duplicates a write, text that blows the budget of
  the channel it rides on);
* **the delivery** — the ``get_playbook`` tool and its ``name`` enum, the
  envelope hint on the success path, the chain-consequence tail on the failure
  path, and the resource mirror.

The delivery tests drive the real transport, because the thing being asserted is
what a *client* receives — the same lesson RD-90 paid for when a manifest field
was parsed for a year and never reached anyone.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
from fastapi import FastAPI
from structlog.testing import capture_logs

from autods_mcp_server import mcp_transport
from autods_mcp_server.analytics import MixpanelClient
from autods_mcp_server.auth.dependency import jwks_dependency
from autods_mcp_server.manifests import build_registry
from autods_mcp_server.manifests.loader import load_manifests
from autods_mcp_server.manifests.playbooks import (
    DESCRIPTION_TAIL_MAX_CHARS,
    ENVELOPE_HINT_MAX_CHARS,
    FAILURE_HINT_MAX_CHARS,
    DuplicatePlaybookError,
    Playbook,
    PlaybookError,
    PlaybookRegistry,
    PlaybookStepRef,
    assert_playbooks_valid,
    build_playbook_registry,
    hint_size,
    load_playbooks,
    render_description_tail,
    render_failure_hint,
    render_success_hint,
)
from autods_mcp_server.mcp_transport import build_runtime, mount_mcp
from autods_mcp_server.ratelimit import BucketSpec, InMemoryRateLimiter
from autods_mcp_server.tools import OperationHandlerError, build_tools
from tests.mcp_server.conftest import mcp_client_session

_VALID_UPLOAD_BODY = {"region": 1, "status": 1, "buy_site_id": 1, "new_products": [{"asin": "B0TEST123"}]}


def _ok_upstream(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"task_id": "abc"})


# --- synthetic environments -------------------------------------------------
#
# The committed playbook is the pilot chain and deliberately passes every lint,
# so the rejection tests need their own tiny world: one destructive write, one
# read, and whatever playbook the test is trying to get past the lint.


def _write_env(
    tmp_path: Path,
    playbooks: dict[str, dict[str, Any]],
    *,
    operations: list[dict[str, Any]] | None = None,
    with_tool: bool = False,
) -> Path:
    """A manifest dir plus a ``playbooks/`` subdirectory, on disk."""
    directory = tmp_path / "env"
    directory.mkdir(exist_ok=True)
    ops = operations if operations is not None else _default_operations()
    if with_tool:
        ops = [*ops, _playbook_tool_operation()]
    (directory / "ops.json").write_text(
        json.dumps({"server_name": "test-ops", "instructions": "", "operations": ops}),
        encoding="utf-8",
    )
    subdirectory = directory / "playbooks"
    subdirectory.mkdir(exist_ok=True)
    for filename, playbook in playbooks.items():
        (subdirectory / filename).write_text(json.dumps(playbook), encoding="utf-8")
    return directory


def _default_operations() -> list[dict[str, Any]]:
    return [
        {
            "operation_id": "write_thing",
            "method": "POST",
            "path": "/things/{store_id}",
            "parameters": [{"name": "store_id", "in": "path", "required": True, "schema_type": "str"}],
            "has_json_body": True,
            "body_schema": {"type": "object", "properties": {"title": {"type": "string"}}},
            "annotations": {"title": "Write Thing", "readOnlyHint": False, "destructiveHint": True},
        },
        {
            "operation_id": "read_things",
            "method": "POST",
            "path": "/things/{store_id}/list",
            "parameters": [{"name": "store_id", "in": "path", "required": True, "schema_type": "str"}],
            "annotations": {"title": "Read Things", "readOnlyHint": True, "destructiveHint": False},
        },
        {
            "operation_id": "write_other",
            "method": "POST",
            "path": "/others",
            "annotations": {"title": "Write Other", "readOnlyHint": False, "destructiveHint": False},
        },
    ]


def _playbook_tool_operation() -> dict[str, Any]:
    return {
        "operation_id": "get_playbook",
        "handler": "playbook",
        "summary": "Get the runbook for one playbook.",
        "parameters": [{"name": "name", "in": "query", "required": True, "schema_type": "str"}],
        "annotations": {"title": "Get Playbook", "readOnlyHint": True, "destructiveHint": False},
    }


def _chain(**overrides: Any) -> dict[str, Any]:
    """A minimal two-step chain that passes every lint."""
    playbook: dict[str, Any] = {
        "name": "demo",
        "title": "Demo chain",
        "when_to_use": "The user wants a thing written and then confirmed.",
        "entry_operation": "write_thing",
        "done_when": "read_things shows the thing.",
        "steps": [
            {
                "operation_id": "write_thing",
                "goal": "Write the thing.",
                "incomplete_alone": "The thing is unconfirmed until it is read back.",
                "on_failure": {
                    "idempotent": False,
                    "left_behind": "The write may have landed.",
                    "verify_with": {"operation_id": "read_things", "how": "filter on the title"},
                    "then": "If it is there, stop; otherwise write again.",
                },
            },
            {"operation_id": "read_things", "goal": "Read the thing back."},
        ],
        "body": "# Demo\n",
    }
    playbook.update(overrides)
    return playbook


def _lint(tmp_path: Path, playbooks: dict[str, dict[str, Any]], **kwargs: Any) -> None:
    """Load a synthetic environment and run the playbook boot lints on it."""
    directory = _write_env(tmp_path, playbooks, **kwargs)
    registry = build_registry(directory)
    assert_playbooks_valid(build_playbook_registry(directory), registry)


# --- the files: two loaders, one directory ----------------------------------


def test_playbook_files_never_enter_the_manifest_registry(bundled_manifest_dir: Path) -> None:
    """``load_manifests`` globs ``*.json`` non-recursively, so the subdirectory
    is skipped for free — which is exactly why the files live in a subdirectory
    rather than as a sibling ``playbooks.json`` (that would be parsed as a
    manifest and rejected on the missing ``server_name``)."""
    manifests = load_manifests(bundled_manifest_dir)
    registry = build_registry(bundled_manifest_dir)

    assert all(manifest.server_name != "product_import" for manifest in manifests)
    assert registry.get("product_import") is None
    # …and no manifest picked up a playbook's fields.
    assert all(not hasattr(manifest, "steps") for manifest in manifests)


def test_the_dedicated_loader_reads_the_subdirectory(bundled_manifest_dir: Path) -> None:
    playbooks = build_playbook_registry(bundled_manifest_dir)

    assert playbooks.names() == ["product_import"]
    assert playbooks.get("product_import").entry_operation == "upload_products"


def test_a_missing_playbook_directory_is_not_an_error(tmp_path: Path) -> None:
    """A deployment that ships manifests and no chains is legitimate."""
    assert load_playbooks(tmp_path / "nope") == []
    assert len(PlaybookRegistry([])) == 0


def test_an_unknown_field_in_a_playbook_file_is_rejected(tmp_path: Path) -> None:
    """``extra="forbid"``, unlike the vendored manifest models: a mistyped
    ``incomplete_alone`` that parses to nothing is the invisible-field failure
    the manifests already got bitten by."""
    directory = _write_env(
        tmp_path,
        {"demo.json": _chain(steps=[{"operation_id": "read_things", "goal": "x", "incompleet_alone": "typo"}])},
    )

    with pytest.raises(PlaybookError, match="demo.json"):
        load_playbooks(directory / "playbooks")


# --- derivation: positions, successors, and the reverse index ---------------


def test_step_numbers_are_derived_from_list_order(bundled_manifest_dir: Path) -> None:
    playbook = build_playbook_registry(bundled_manifest_dir).get("product_import")

    labels = [PlaybookStepRef(playbook, i).label for i in range(len(playbook.steps))]

    assert labels == ["1/3", "2/3", "3/3"]


def test_no_step_number_is_authored_anywhere(bundled_manifest_dir: Path) -> None:
    """A hand-written ``"step": 2, "of": 7`` breaks the moment a chain is
    renumbered — it touches every file — and cannot express one operation
    belonging to two chains. The model forbids the fields; this asserts nobody
    reintroduced them by another name."""
    for file in sorted((bundled_manifest_dir / "playbooks").glob("*.json")):
        raw = json.loads(file.read_text(encoding="utf-8"))
        for step in raw["steps"]:
            assert "step" not in step and "of" not in step


def test_the_default_successor_is_the_next_step(bundled_manifest_dir: Path) -> None:
    """Step 2 of the committed chain authors no ``then``, so its successor comes
    from list position."""
    playbook = build_playbook_registry(bundled_manifest_dir).get("product_import")

    assert playbook.steps[1].then == []
    assert PlaybookStepRef(playbook, 1).next_operations == ["list_products"]
    assert PlaybookStepRef(playbook, 2).next_operations == []
    assert PlaybookStepRef(playbook, 2).is_final is True


def test_an_authored_then_overrides_the_default(tmp_path: Path) -> None:
    playbook = _chain(
        steps=[
            {"operation_id": "write_thing", "goal": "w", "then": ["read_things"], "incomplete_alone": "x"},
            {"operation_id": "write_other", "goal": "o"},
            {"operation_id": "read_things", "goal": "r"},
        ]
    )
    # write_other is only reachable as the *list* successor of write_thing,
    # which the authored ``then`` replaces — so the lint catches it.
    with pytest.raises(PlaybookError, match="unreachable"):
        _lint(tmp_path, {"demo.json": playbook})


def test_one_operation_can_belong_to_two_playbooks(tmp_path: Path) -> None:
    """The reverse index is a list, not a single ref — which is the whole reason
    step numbers are positional rather than authored."""
    second = _chain(name="other", steps=[{"operation_id": "read_things", "goal": "r"}], entry_operation="read_things")
    directory = _write_env(tmp_path, {"a_demo.json": _chain(), "b_other.json": second})

    playbooks = build_playbook_registry(directory)
    refs = playbooks.steps_for("read_things")

    assert [(ref.playbook.name, ref.label) for ref in refs] == [("demo", "2/2"), ("other", "1/1")]
    # The description tail names both chains rather than picking one: which
    # chain the agent is in is not knowable from a tool descriptor.
    tail = render_description_tail(refs)
    assert '"demo"' in tail and '"other"' in tail
    # Each chain named exactly once. Guaranteed by lint 3 rejecting a repeated
    # operation within one playbook, which is what keeps the rendering honest
    # without a dedupe pass that could never fire.
    assert tail.count('"demo"') == 1 and tail.count('"other"') == 1
    assert len(tail) <= DESCRIPTION_TAIL_MAX_CHARS


# --- ambiguity: an operation in several chains -------------------------------
#
# The server is stateless and nothing in a request says which chain the caller is
# following, so for a shared operation the honest hint is a vaguer one. The
# alternative — state the first chain's step number and ``incomplete_alone`` as
# fact — is a confident lie about what an unfinished chain left broken.


def _shared_write_env(tmp_path: Path) -> Path:
    """Two chains that both start with the same destructive write, disagreeing on
    every chain-scoped field: what stopping costs, what the write leaves behind,
    what to do next, and whether to ask first."""
    first = _chain(
        name="import_only",
        steps=[
            {
                "operation_id": "write_thing",
                "goal": "Write the thing.",
                "incomplete_alone": "Nothing is stored until it is read back.",
                "on_failure": {
                    "idempotent": False,
                    "left_behind": "The write may have landed.",
                    "verify_with": {"operation_id": "read_things", "how": "filter on the title"},
                    "then": "If it is there, continue; otherwise write again.",
                    "ask_user": True,
                },
            },
            {"operation_id": "read_things", "goal": "Read it back."},
        ],
        entry_operation="write_thing",
    )
    second = _chain(
        name="sourced_import",
        steps=[
            {
                "operation_id": "write_thing",
                "goal": "Write the thing as part of the longer flow.",
                "incomplete_alone": "The thing is live at cost with no supplier attached.",
                "on_failure": {
                    "idempotent": False,
                    "left_behind": "A partial listing may exist at the wrong price.",
                    "verify_with": {"operation_id": "read_things", "how": "filter on the title"},
                    "then": "Do not write again until the supplier link is checked.",
                },
            },
            {"operation_id": "write_other", "goal": "Attach the supplier."},
            {"operation_id": "read_things", "goal": "Confirm."},
        ],
        entry_operation="write_thing",
    )
    return _write_env(tmp_path, {"a_import_only.json": first, "b_sourced_import.json": second})


def test_a_shared_step_gets_the_candidates_not_the_first_chain(tmp_path: Path) -> None:
    """The success hint names the chains it could be and stops there. The step
    number, the successors and ``incomplete_alone`` all differ between the two,
    so asserting any of them would be right half the time at best."""
    playbooks = build_playbook_registry(_shared_write_env(tmp_path))

    hint = render_success_hint(playbooks.steps_for("write_thing"))

    assert hint == {
        "in": ["import_only", "sourced_import"],
        "step_depends_on_chain": True,
        "runbook": "get_playbook(<the chain you are in, from `in`>)",
    }
    # Specifically: neither chain's own warning is presented as the truth.
    assert "Nothing is stored" not in json.dumps(hint)
    assert "live at cost" not in json.dumps(hint)
    assert hint_size(hint) <= ENVELOPE_HINT_MAX_CHARS


def test_a_shared_step_that_is_final_in_every_chain_gets_no_hint(tmp_path: Path) -> None:
    """A chain that is over is not a candidate — there is nothing to nudge
    towards. Both chains end on ``read_things``, so it stays absent entirely."""
    playbooks = build_playbook_registry(_shared_write_env(tmp_path))

    assert render_success_hint(playbooks.steps_for("read_things")) is None


def test_the_shared_chains_are_a_shippable_configuration(tmp_path: Path) -> None:
    """The ambiguity above is not a synthetic impossibility — the two chains pass
    every boot lint, which is why the renderers have to cope with it at all."""
    directory = _shared_write_env(tmp_path)

    assert_playbooks_valid(build_playbook_registry(directory), build_registry(directory))


def test_a_shared_step_merges_only_what_the_chains_agree_on(tmp_path: Path) -> None:
    """The failure tail is the half that prevents a duplicated write, so it is
    merged rather than dropped: every clause that holds in both chains survives,
    the chain-scoped ``then`` becomes a pointer, and the cautious reading wins."""
    playbooks = build_playbook_registry(_shared_write_env(tmp_path))

    tail = render_failure_hint(playbooks.steps_for("write_thing"))

    assert tail == (
        'Playbook step in "import_only", "sourced_import": this step is not idempotent. '
        "Verify with read_things (filter on the title). "
        "Recovery differs by chain — call get_playbook for yours. "
        "Ask the user before retrying."
    )
    # The verification tool is what stops the duplicate write, and it survives
    # because it is a property of the write rather than of the recipe.
    assert "read_things" in tail
    # left_behind disagrees, so neither version is asserted…
    assert "may have landed" not in tail and "wrong price" not in tail
    # …and neither is either chain's next move.
    assert "continue; otherwise" not in tail and "supplier link is checked" not in tail
    # ask_user is set in one chain only: the cautious reading wins.
    assert tail.endswith("Ask the user before retrying.")
    assert len(tail) <= FAILURE_HINT_MAX_CHARS


def test_a_shared_step_is_called_not_idempotent_if_any_chain_says_so(tmp_path: Path) -> None:
    """Telling an agent a blind retry is safe when one chain says it is not is
    the single mistake that costs the user a duplicate listing."""
    idempotent_everywhere = _chain(
        name="cautious",
        steps=[
            {
                "operation_id": "write_thing",
                "goal": "w",
                "incomplete_alone": "x",
                "on_failure": {"idempotent": True, "then": "Just retry."},
            },
            {"operation_id": "read_things", "goal": "r"},
        ],
        entry_operation="write_thing",
    )
    directory = _write_env(tmp_path, {"a.json": _chain(), "b.json": idempotent_everywhere})
    playbooks = build_playbook_registry(directory)

    tail = render_failure_hint(playbooks.steps_for("write_thing"))

    assert "this step is not idempotent." in tail
    assert "this step is idempotent." not in tail


def test_a_shared_step_with_one_undeclared_on_failure_still_merges(tmp_path: Path) -> None:
    """A chain that declares no ``on_failure`` contributes nothing — a missing
    declaration is not evidence that retrying is safe — but it is still named, so
    a caller in that chain can see itself in the list."""
    silent = _chain(
        name="silent",
        steps=[
            {"operation_id": "write_thing", "goal": "w", "incomplete_alone": "x"},
            {"operation_id": "read_things", "goal": "r"},
        ],
        entry_operation="write_thing",
    )
    directory = _write_env(tmp_path, {"a.json": _chain(), "b_silent.json": silent})
    playbooks = build_playbook_registry(directory)

    tail = render_failure_hint(playbooks.steps_for("write_thing"))

    assert '"demo", "silent"' in tail
    assert "this step is not idempotent." in tail
    assert "Recovery differs by chain" in tail


def test_an_unshared_operation_is_unaffected(tmp_path: Path) -> None:
    """The specific rendering is what a single-chain deployment has always sent,
    and it must not change shape just because the renderers now take a list."""
    playbooks = build_playbook_registry(_write_env(tmp_path, {"demo.json": _chain()}))
    refs = playbooks.steps_for("write_thing")

    assert render_success_hint(refs) == {
        "name": "demo",
        "step": "1/2",
        "next": ["read_things"],
        "incomplete_alone": "The thing is unconfirmed until it is read back.",
        "runbook": 'get_playbook("demo")',
    }
    assert render_failure_hint(refs).startswith('Playbook "demo" step 1/2: this step is not idempotent.')


# --- the boot lints ---------------------------------------------------------


def test_lint_rejects_a_step_naming_an_unregistered_operation(tmp_path: Path) -> None:
    playbook = _chain(steps=[{"operation_id": "ghost_op", "goal": "g"}], entry_operation="ghost_op")

    with pytest.raises(PlaybookError, match="ghost_op"):
        _lint(tmp_path, {"demo.json": playbook})


def test_lint_rejects_an_unregistered_verify_with(tmp_path: Path) -> None:
    playbook = _chain()
    playbook["steps"][0]["on_failure"]["verify_with"] = {"operation_id": "ghost_read", "how": "x"}

    with pytest.raises(PlaybookError, match="ghost_read"):
        _lint(tmp_path, {"demo.json": playbook})


def test_lint_rejects_an_unregistered_requires_source(tmp_path: Path) -> None:
    playbook = _chain()
    playbook["steps"][0]["requires"] = [{"param": "store_id", "from_operation": "ghost_op", "field": "id"}]

    with pytest.raises(PlaybookError, match="ghost_op"):
        _lint(tmp_path, {"demo.json": playbook})


def test_lint_accepts_a_requires_param_that_resolves(tmp_path: Path) -> None:
    """A body path resolves against the ``body_schema`` property names — the
    check that catches an upstream rename."""
    playbook = _chain()
    playbook["steps"][0]["requires"] = [
        {"param": "store_id", "from_operation": "read_things", "field": "id"},
        {"param": "body.title", "from_operation": "read_things", "field": "title"},
    ]

    _lint(tmp_path, {"demo.json": playbook})


def test_lint_rejects_a_requires_param_the_operation_does_not_accept(tmp_path: Path) -> None:
    playbook = _chain()
    playbook["steps"][0]["requires"] = [{"param": "body.invented", "from_operation": "read_things", "field": "x"}]

    with pytest.raises(PlaybookError, match="invented"):
        _lint(tmp_path, {"demo.json": playbook})


def test_lint_rejects_duplicate_playbook_names(tmp_path: Path) -> None:
    """The name is the enum value clients call ``get_playbook`` with."""
    directory = _write_env(tmp_path, {"a.json": _chain(), "b.json": _chain(title="Another")})

    with pytest.raises(DuplicatePlaybookError, match="demo"):
        build_playbook_registry(directory)


def test_lint_rejects_an_entry_operation_that_is_not_a_step(tmp_path: Path) -> None:
    with pytest.raises(PlaybookError, match="entry_operation"):
        _lint(tmp_path, {"demo.json": _chain(entry_operation="write_other")})


def test_lint_rejects_a_then_naming_an_operation_that_is_not_a_step(tmp_path: Path) -> None:
    """``then`` is the next step in *this* chain, so a registered operation outside
    it is not a successor. Unchecked, this reached clients: the step stopped
    counting as final, so it emitted a hint whose ``next`` recommended a tool the
    chain never contains, reading as "step 2 of 2, next: write_other"."""
    playbook = _chain()
    playbook["steps"][1]["then"] = ["write_other"]

    with pytest.raises(PlaybookError, match="not a step of this playbook"):
        _lint(tmp_path, {"demo.json": playbook})


def test_lint_rejects_a_dangling_then_beside_a_valid_one(tmp_path: Path) -> None:
    """The shape nothing used to catch. A dangling successor that *replaces* a
    valid one orphans the rest of the chain and trips the reachability check; one
    added *beside* a valid one leaves the graph intact and rode straight into the
    client's ``next``. It was rejected only when the extra name happened to push
    the rendered hint past its size cap, which is luck, not a check — so this
    asserts the successor error specifically, not merely that something raised."""
    playbook = _chain()
    playbook["steps"][0]["then"] = ["read_things", "write_other"]

    with pytest.raises(PlaybookError, match="not a step of this playbook"):
        _lint(tmp_path, {"demo.json": playbook})


def test_lint_rejects_a_playbook_with_no_steps(tmp_path: Path) -> None:
    with pytest.raises(PlaybookError, match="no steps"):
        _lint(tmp_path, {"demo.json": _chain(steps=[], entry_operation="read_things")})


def test_lint_rejects_the_same_operation_twice_in_one_playbook(tmp_path: Path) -> None:
    """One operation may belong to several playbooks but not appear twice in one:
    ``then`` / ``entry_operation`` / ``next_operations`` all address a step by
    ``operation_id``, so a second occurrence cannot be pointed at. Without this
    lint it surfaced as a confusing *unreachable* error (the reverse index keeps
    only the first position) and rendered the tail as ``playbooks "x", "x"``."""
    playbook = _chain(
        steps=[
            {"operation_id": "write_thing", "goal": "w", "incomplete_alone": "x"},
            {"operation_id": "read_things", "goal": "r"},
            {"operation_id": "read_things", "goal": "confirm again"},
        ]
    )

    with pytest.raises(PlaybookError, match="twice"):
        _lint(tmp_path, {"demo.json": playbook})


def test_lint_rejects_an_unreachable_step(tmp_path: Path) -> None:
    """An unreachable step renders into ``get_playbook``'s payload and its
    operation's description tail, and nothing ever tells an agent to get
    there."""
    playbook = _chain(
        steps=[
            {"operation_id": "write_thing", "goal": "w", "then": [], "incomplete_alone": "x"},
            {"operation_id": "read_things", "goal": "r"},
            {"operation_id": "write_other", "goal": "o"},
        ]
    )
    playbook["steps"][1]["then"] = []
    # read_things is the list successor of write_thing and final by position, so
    # write_other is reachable; break that by making read_things terminal.
    playbook["steps"][1]["then"] = ["read_things"]

    with pytest.raises(PlaybookError, match="unreachable"):
        _lint(tmp_path, {"demo.json": playbook})


def test_lint_rejects_a_destructive_non_final_step_without_incomplete_alone(tmp_path: Path) -> None:
    """If an agent can stop there and leave damage, the file has to say what the
    damage is."""
    playbook = _chain()
    del playbook["steps"][0]["incomplete_alone"]

    with pytest.raises(PlaybookError, match="incomplete_alone"):
        _lint(tmp_path, {"demo.json": playbook})


def test_lint_rejects_a_non_idempotent_destructive_step_without_verify_with(tmp_path: Path) -> None:
    """ "Retry on failure" without a way to check for partial success duplicates
    the write."""
    playbook = _chain()
    del playbook["steps"][0]["on_failure"]["verify_with"]

    with pytest.raises(PlaybookError, match="verify_with"):
        _lint(tmp_path, {"demo.json": playbook})


def test_lint_rejects_a_verify_with_that_is_not_read_only(tmp_path: Path) -> None:
    """Verifying a failed write must not be able to write again."""
    playbook = _chain()
    playbook["steps"][0]["on_failure"]["verify_with"] = {"operation_id": "write_other", "how": "x"}

    with pytest.raises(PlaybookError, match="readOnlyHint"):
        _lint(tmp_path, {"demo.json": playbook})


def test_lint_rejects_an_oversized_body(tmp_path: Path) -> None:
    with pytest.raises(PlaybookError, match="body is"):
        _lint(tmp_path, {"demo.json": _chain(body="x" * 6001)})


def test_lint_rejects_an_oversized_envelope_hint(tmp_path: Path) -> None:
    """The one channel that repeats: serialized twice per call, and on a polling
    step it fires on every poll."""
    playbook = _chain()
    playbook["steps"][0]["incomplete_alone"] = "y" * 250

    with pytest.raises(PlaybookError, match="envelope hint"):
        _lint(tmp_path, {"demo.json": playbook})


def test_lint_rejects_an_oversized_failure_hint(tmp_path: Path) -> None:
    playbook = _chain()
    playbook["steps"][0]["on_failure"]["then"] = "z" * 400

    with pytest.raises(PlaybookError, match="failure hint"):
        _lint(tmp_path, {"demo.json": playbook})


def test_lint_rejects_an_oversized_description_tail(tmp_path: Path) -> None:
    """The tail grows with the number of chains an operation joins, so this is
    the one cap that can be blown by a file that never mentions the operation's
    other chains — which is why it is checked over the merged index."""
    files = {
        f"{index}.json": _chain(
            name=f"chain_number_{index}_with_a_longish_name",
            entry_operation="read_things",
            steps=[{"operation_id": "read_things", "goal": "r"}],
        )
        for index in range(3)
    }

    with pytest.raises(PlaybookError, match="description tail"):
        _lint(tmp_path, files)


def test_lint_rejects_an_oversized_merged_failure_tail(tmp_path: Path) -> None:
    """The merged tail replaces each chain's short ``then`` with a fixed pointer
    and prefixes every chain name, so it can overrun 320 while each chain's own
    rendering fits comfortably. Like the description tail, no single file is at
    fault, so it is checked over the merged index."""
    shared_failure = {
        "idempotent": False,
        "left_behind": "A partial listing may exist at the wrong price, with no supplier attached to it yet. " * 2,
        "verify_with": {"operation_id": "read_things", "how": "filter on the submitted title"},
        "then": "Retry.",
    }
    files = {
        f"{index}.json": _chain(
            name=f"chain_number_{index}",
            entry_operation="write_thing",
            steps=[
                {"operation_id": "write_thing", "goal": "w", "incomplete_alone": "x", "on_failure": shared_failure},
                {"operation_id": "read_things", "goal": "r"},
            ],
        )
        for index in range(2)
    }
    # Each chain on its own is inside the budget — only the merge overruns.
    single = render_failure_hint([PlaybookStepRef(Playbook.model_validate(files["0.json"]), 0)])
    assert len(single) <= FAILURE_HINT_MAX_CHARS

    with pytest.raises(PlaybookError, match="merged failure tail"):
        _lint(tmp_path, files)


def test_the_tail_cap_subsumes_the_merged_hint_cap() -> None:
    """Why there is no merged-envelope-hint lint: the description tail carries the
    same chain names in a longer sentence under a tighter cap (120 vs 200), so it
    always overruns first. This pins that headroom at the worst case — two chains
    with the longest names the tail cap allows. If it fails, the merged hint's
    fixed text grew and the lint in ``assert_playbooks_valid`` must come back."""
    name = "n" * 26  # the longest that keeps a two-chain tail inside its cap
    refs = [
        PlaybookStepRef(
            Playbook.model_validate(
                _chain(
                    name=f"{name}{index}",
                    entry_operation="write_thing",
                    steps=[
                        {"operation_id": "write_thing", "goal": "w", "incomplete_alone": "x"},
                        {"operation_id": "read_things", "goal": "r"},
                    ],
                )
            ),
            0,
        )
        for index in range(2)
    ]

    assert len(render_description_tail(refs)) <= DESCRIPTION_TAIL_MAX_CHARS
    assert hint_size(render_success_hint(refs)) < ENVELOPE_HINT_MAX_CHARS


def test_the_committed_playbooks_pass_every_lint(bundled_manifest_dir: Path) -> None:
    assert_playbooks_valid(build_playbook_registry(bundled_manifest_dir), build_registry(bundled_manifest_dir))


# --- the handler lint -------------------------------------------------------


def _operation(**overrides: Any) -> Any:
    from autods_mcp_server.manifests.schema import ManifestOperation

    payload: dict[str, Any] = {
        "operation_id": "op",
        "method": "GET",
        "path": "/x",
        "base_url_key": "autods_api",
        "annotations": {"title": "Op", "readOnlyHint": True},
    }
    payload.update(overrides)
    return ManifestOperation.model_validate(payload)


def test_handler_lint_rejects_an_operation_with_both_handler_and_upstream() -> None:
    with pytest.raises(OperationHandlerError, match="both handler"):
        build_tools([_operation(handler="playbook")], PlaybookRegistry([Playbook.model_validate(_chain())]))


def test_handler_lint_rejects_an_operation_with_neither() -> None:
    with pytest.raises(OperationHandlerError, match="neither"):
        build_tools([_operation(base_url_key=None)])


def test_handler_lint_rejects_a_forwarding_operation_without_method_or_path() -> None:
    """``method``/``path`` became optional so a local operation needn't invent
    them; a forwarded operation still has to declare both, or it blows up
    resolving an empty URL on its first call."""
    with pytest.raises(OperationHandlerError, match="no 'method'/'path'"):
        build_tools([_operation(path="")])


def test_handler_lint_rejects_an_unknown_handler() -> None:
    with pytest.raises(OperationHandlerError, match="unknown handler"):
        build_tools([_operation(base_url_key=None, handler="shell")])


def test_handler_lint_rejects_the_playbook_tool_with_no_playbooks_registered() -> None:
    """Its ``name`` enum would be empty, so every call would fail validation —
    a dead end that still costs a tool definition."""
    with pytest.raises(OperationHandlerError, match="none are registered"):
        build_tools([_operation(base_url_key=None, handler="playbook")], PlaybookRegistry([]))


def test_a_locally_handled_operation_does_not_inherit_the_manifest_upstream(tmp_path: Path) -> None:
    """Left to inherit, the "exactly one of the two" lint could never fire."""
    directory = _write_env(tmp_path, {"demo.json": _chain()}, with_tool=True)

    operation = build_registry(directory).get("get_playbook")

    assert operation.handler == "playbook"
    assert operation.base_url_key is None


# --- delivery: the get_playbook tool ----------------------------------------


async def test_get_playbook_advertises_the_registered_names_as_an_enum(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The enum *is* the index. It lives in ``inputSchema`` — the most reliably
    delivered channel there is — so the list of chains reaches the model even in
    a client that drops ``instructions`` entirely."""
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        tools = await session.list_tools()

    tool = next(t for t in tools.tools if t.name == "get_playbook")

    assert tool.input_schema["properties"]["name"]["enum"] == ["product_import"]
    assert tool.annotations.read_only_hint is True


async def test_get_playbook_returns_the_standard_envelope(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """A uniform envelope is worth more than unescaped newlines: a client cannot
    tell a locally-served operation from a forwarded one."""
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("get_playbook", {"name": "product_import"})

    payload = result.structured_content
    assert result.is_error is False
    assert payload["operation_id"] == "get_playbook"
    assert payload["status"] == 200
    assert payload["ok"] is True
    data = payload["data"]
    assert data["name"] == "product_import"
    assert data["entry_operation"] == "upload_products"
    assert [step["operation_id"] for step in data["steps"]] == [
        "upload_products",
        "get_bulk_action_items",
        "list_products",
    ]
    # Derived positions and successors are materialised, so the client reads the
    # same numbers the hints do.
    assert [(step["step"], step["of"]) for step in data["steps"]] == [(1, 3), (2, 3), (3, 3)]
    assert data["steps"][1]["next"] == ["list_products"]
    assert data["body"].startswith("# Import products into a store")
    # A locally-served operation carries no chain hint of its own.
    assert "playbook" not in payload


async def test_get_playbook_rejects_an_unregistered_name(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("get_playbook", {"name": "no_such_chain"})

    assert result.is_error is True
    assert result.content[0].text.startswith("invalid_arguments: ")


async def test_get_playbook_is_rate_limited_like_any_other_call(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The local-handler branch sits *after* the rate limiter, so a local
    operation can't become an unmetered path."""
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    limiter = InMemoryRateLimiter([BucketSpec("minute", capacity=1, refill_rate=1 / 60)])
    app, runtime = make_mcp_app(settings, rate_limiter=limiter)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        first = await session.call_tool("get_playbook", {"name": "product_import"})
        second = await session.call_tool("get_playbook", {"name": "product_import"})

    assert first.is_error is False
    assert second.is_error is True
    assert second.content[0].text.startswith("rate_limited: ")


class _FakeTracker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def track(self, distinct_id: str, event_name: str, properties: dict[str, Any] | None = None) -> None:
        self.calls.append((distinct_id, event_name, properties or {}))


async def test_get_playbook_emits_the_audit_line_and_the_analytics_event(
    mcp_settings, bundled_manifest_dir: Path, jwks_client, access_token: str, monkeypatch
) -> None:
    """Everything a forwarded call is observed by, a local one is observed by
    too — with a templated endpoint that names the handler instead of inventing
    a URL."""

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/list/"):
            return httpx.Response(200, json=[{"id": 999, "name": "Alice", "email": "alice@example.com"}])
        return httpx.Response(200, json={})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    tracker = _FakeTracker()
    mixpanel = MixpanelClient(tracker)
    runtime = build_runtime(
        settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        mixpanel=mixpanel,
    )
    app = FastAPI()
    mount_mcp(app, runtime)
    app.dependency_overrides[jwks_dependency] = lambda: jwks_client

    with capture_logs() as logs:
        monkeypatch.setattr(mcp_transport, "_audit_logger", structlog.get_logger("audit-playbook"))
        async with mcp_client_session(app, runtime, token=access_token) as session:
            await session.call_tool("get_playbook", {"name": "product_import"})
    await mixpanel.drain()

    events = [props for _distinct, name, props in tracker.calls if name == "MCP Call Received"]
    assert events and events[0]["Remote Endpoint"] == "local playbook get_playbook"

    audit = [line for line in logs if line.get("event") == "tool_call" and line["tool_name"] == "get_playbook"]
    assert len(audit) == 1
    assert audit[0]["upstream_url"] is None
    assert audit[0]["upstream_status"] == 200
    assert "error_type" not in audit[0]


# --- delivery: the success-path envelope hint -------------------------------


async def test_the_envelope_hint_is_absent_on_a_final_step(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """A chain that is over has nothing to nudge."""
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=_ok_upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("list_products", {"store_ids": "s1", "body": {"product_status": 1}})

    assert result.is_error is False
    assert "playbook" not in result.structured_content


async def test_the_envelope_hint_is_absent_on_a_non_chain_tool(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=_ok_upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("list_stores_api", {})

    assert result.is_error is False
    assert "playbook" not in result.structured_content


async def test_the_envelope_hint_rides_beside_data_not_inside_it(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """Same placement rule as ``business_error``: ``data`` stays the upstream
    payload verbatim."""
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=_ok_upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("upload_products", {"store_ids": "s1", "body": _VALID_UPLOAD_BODY})

    payload = result.structured_content
    assert payload["data"] == {"task_id": "abc"}
    assert payload["playbook"]["step"] == "1/3"
    assert payload["playbook"]["next"] == ["get_bulk_action_items"]
    assert payload["playbook"]["runbook"] == 'get_playbook("product_import")'
    # …and it is in the text block too, since that is a dump of the same dict.
    assert '"playbook"' in result.content[0].text


async def test_the_ambiguous_hint_reaches_a_real_client(
    mcp_settings, make_mcp_app, tmp_path: Path, access_token
) -> None:
    """The RD-90 lesson applied to the ambiguous case: assert the vaguer hint
    arrives over a real session, not just that the renderer produces it. This is
    the wiring that changed — the transport now hands the renderers every
    candidate step instead of the first one."""
    settings = mcp_settings(manifest_dir=_shared_write_env(tmp_path))
    app, runtime = make_mcp_app(settings, upstream_handler=_ok_upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("write_thing", {"store_id": "s1", "body": {"title": "t"}})

    hint = result.structured_content["playbook"]
    assert hint["in"] == ["import_only", "sourced_import"]
    assert "step" not in hint and "incomplete_alone" not in hint
    # The claim a first-match pick would have shipped, delivered to nobody.
    assert "live at cost" not in result.content[0].text


def test_every_rendered_hint_fits_its_channel(bundled_manifest_dir: Path) -> None:
    """The caps, asserted on what a client actually receives — which is the
    rendering over *every* chain the operation is in, not the one its own file
    would produce alone."""
    playbooks = build_playbook_registry(bundled_manifest_dir)
    for playbook in playbooks.list_playbooks():
        for index in range(len(playbook.steps)):
            ref = PlaybookStepRef(playbook, index)
            refs = playbooks.steps_for(ref.step.operation_id)
            hint = render_success_hint(refs)
            if hint is not None:
                assert hint_size(hint) <= ENVELOPE_HINT_MAX_CHARS
            failure = render_failure_hint(refs)
            if failure is not None:
                assert len(failure) <= FAILURE_HINT_MAX_CHARS
            assert len(render_description_tail(refs)) <= DESCRIPTION_TAIL_MAX_CHARS


async def test_a_chain_tool_description_carries_the_bounded_pointer(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        tools = await session.list_tools()

    by_name = {tool.name: tool for tool in tools.tools}
    assert by_name["upload_products"].description.endswith(
        'Step 1 of 3 in playbook "product_import" — call get_playbook for the full chain.'
    )
    # No step bodies in a description: the pointer, and nothing else.
    assert "incomplete_alone" not in by_name["upload_products"].description
    assert "playbook" not in by_name["list_stores_api"].description


# --- delivery: the failure-path tail ----------------------------------------


async def test_a_failing_chain_step_carries_its_chain_consequence(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The half that actually pays off. An error result is a flat text block
    with no ``structuredContent``, so the guidance has to ride on the text."""

    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("upload_products", {"store_ids": "s1", "body": _VALID_UPLOAD_BODY})

    text = result.content[0].text
    assert result.is_error is True
    assert text.startswith("upstream_error: ")
    assert 'Playbook "product_import" step 1/3: this step is not idempotent.' in text
    assert "Verify with list_products" in text
    assert "Ask the user before retrying." in text
    # The upstream detail is still not echoed.
    assert "boom" not in text


async def test_an_unreachable_upstream_carries_it_too(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The genuinely ambiguous failure: the async job may have started and
    nothing in the error says which."""

    def upstream(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("upload_products", {"store_ids": "s1", "body": _VALID_UPLOAD_BODY})

    text = result.content[0].text
    assert result.is_error is True
    assert text.startswith("upstream_unreachable: ")
    assert 'Playbook "product_import" step 1/3' in text


async def test_a_step_without_on_failure_produces_an_unchanged_error(
    mcp_settings, make_mcp_app, tmp_path: Path, access_token
) -> None:
    """Byte-identical to what the client saw before playbooks existed."""
    playbook = _chain(
        steps=[
            {"operation_id": "write_thing", "goal": "w", "incomplete_alone": "x"},
            {"operation_id": "read_things", "goal": "r"},
        ]
    )
    directory = _write_env(tmp_path, {"demo.json": playbook}, with_tool=True)

    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    settings = mcp_settings(manifest_dir=directory)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("write_thing", {"store_id": "s1"})

    assert result.is_error is True
    assert result.content[0].text == (
        "upstream_error: The upstream service encountered an error (HTTP 500). Please try again later."
    )


async def test_a_non_chain_tool_error_is_unchanged(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("list_stores_api", {})

    assert result.is_error is True
    assert result.content[0].text == ("forbidden: You don't have permission to perform this operation (HTTP 403).")


# --- delivery: the resource mirror ------------------------------------------


async def test_playbooks_are_mirrored_as_markdown_resources(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """Nearly free, and it is what declares the ``resources`` capability — RD-92
    registers more URIs rather than declaring it again. A mirror, not the
    delivery mechanism: ``resources/`` is host-mediated and uneven across
    clients, which is why the runbook ships as a tool."""
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        listed = await session.list_resources()
        read = await session.read_resource("autods://playbook/product_import")

    uris = {str(resource.uri): resource for resource in listed.resources}
    assert "autods://playbook/product_import" in uris
    assert uris["autods://playbook/product_import"].mime_type == "text/markdown"

    contents = read.contents[0]
    # A bare string would silently become text/plain and lose the markdown.
    assert contents.mime_type == "text/markdown"
    assert contents.text.startswith("# Import products into a store")


# --- the committed text -----------------------------------------------------


def test_the_committed_playbook_text_is_implementation_agnostic(bundled_manifest_dir: Path) -> None:
    """It ships to clients like every other manifest string, so it describes the
    observable contract and never how AutoDS is built."""
    blob = " ".join(pb.model_dump_json() for pb in build_playbook_registry(bundled_manifest_dir).list_playbooks())

    for forbidden in ("celery", "mongo", "elasticsearch", "redis", "postgres", "django", "flask", "kafka"):
        assert forbidden not in blob.lower(), f"playbook text names an internal technology: {forbidden}"
