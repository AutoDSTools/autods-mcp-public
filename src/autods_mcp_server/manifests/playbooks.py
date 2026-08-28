"""Playbooks: multi-tool chains declared as data, delivered lazily (RD-100).

Some goals need several tools called in order, and the chain is invisible from
any single tool descriptor. An agent that stops after the second call of a
seven-call chain leaves the user with work that looks done and is not — a live
listing priced at cost, an import nobody polled. Nothing in MCP models that:
there is no workflow primitive, no client executes a chain on the server's
behalf, and prose cannot enforce an invariant. A playbook lowers the frequency
of a dropped chain; it does not remove the failure.

**Playbook**, not "workflow": "workflow" implies the server executes something,
and it collides with MCP's experimental tasks primitive.

Chain metadata is authored once, per chain, in ``manifests/playbooks/*.json``
(the subdirectory ``load_manifests`` skips for free, because it globs ``*.json``
non-recursively). Four things are derived here rather than authored, because
each of them would otherwise drift the moment a chain is renumbered or an
operation joins a second chain:

* ``step`` / ``of`` come from list position;
* the default successor is the next step (``then`` is authored only at a branch);
* the ``operation_id -> [step]`` index is built by the registry, so one
  operation can participate in several playbooks;
* every string a client sees (the description tail, the envelope hint, the
  failure tail, the ``instructions`` index) is rendered from the same file.

**The delivery split is the whole design.** The runbook body is fetched by the
``get_playbook`` tool, so it costs nothing until an agent enters the flow; the
per-step nudge rides on the *result*, so it is in context exactly when the agent
has just finished step N; the tool ``description`` carries only a bounded
pointer. Rendering chain blocks into descriptions instead would put the text
many turns before the situation arises — and under lazy tool loading, possibly
not in context at all.

``on_failure`` is deliberately **not** an error catalogue. Transport errors,
in-payload business errors and async end-states are each already delivered at
the moment they happen, by machinery that owns them; restating any of it here
creates a second source that drifts and arrives at the wrong time. What nothing
else owns is the *chain consequence* of a failed step — "did the write land, and
is retrying safe?". That is the failure mode this field exists for: an agent
told merely to retry a non-idempotent, asynchronous write duplicates it.

**An operation in several chains renders a vaguer hint, not a guessed one.** One
operation can be a step of many playbooks — ``upload_products`` is step 1 of the
plain import and a middle step of the sourcing chain — and nothing in a request
says which chain the caller is following. The transport is stateless by design,
so there is no session to have remembered it in either. Both result-carried hints
therefore state the specific step *only* when the operation belongs to exactly
one chain; with several they fall back to naming the candidates and pointing at
``get_playbook``, and the failure tail keeps only the clauses every candidate
agrees on. Picking the first chain and stating its step number and
``incomplete_alone`` as fact reads better and is a confident lie — precisely
about what an unfinished chain has left broken, which is the one thing this
module exists to get right.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from autods_mcp_server.manifests.loader import ManifestRegistry

# Subdirectory of the manifest dir the playbook files live in. A subdirectory
# rather than a sibling ``playbooks.json``, which ``load_manifests`` would try
# to parse as a Manifest and reject on the missing ``server_name``.
PLAYBOOK_SUBDIR = "playbooks"

# ``ManifestOperation.handler`` value that routes a call to the local playbook
# handler instead of the upstream dispatcher.
HANDLER_PLAYBOOK = "playbook"

# Envelope key the per-step hint is published under — a sibling of ``data``,
# like ``business_error``, so ``data`` stays the upstream payload verbatim.
PLAYBOOK_KEY = "playbook"

# --- size budgets -----------------------------------------------------------
#
# All three are boot lints rather than runtime truncation: an author who writes
# a sentence too long gets a loud failure naming the overrun, instead of a
# client silently receiving half a sentence.

# The runbook body. Lazy, but not free once fetched.
BODY_MAX_CHARS = 6000

# The tool ``description`` tail. It rides in the tool definitions on every turn
# (or on fetch, in a lazy-loading client), so it is a pointer, never a step body.
DESCRIPTION_TAIL_MAX_CHARS = 120

# The success-path envelope hint, measured over its compact JSON serialization.
# This is the one channel that *repeats*: it is serialized twice per call
# (``content`` + ``structuredContent``) and fires on every poll of a polling
# step, so ``incomplete_alone`` has to be one short sentence.
ENVELOPE_HINT_MAX_CHARS = 200

# The failure-path tail appended to the ``isError`` text. Larger than the
# envelope budget on purpose: it fires once per failure rather than once per
# poll, rides in ``content`` only (an error result carries no
# ``structuredContent``), and it is the half that actually prevents a duplicate
# write — so it can afford to name the verification tool and how to use it.
FAILURE_HINT_MAX_CHARS = 320


class PlaybookError(ValueError):
    """A playbook file is unloadable, or fails one of the boot lints."""


class DuplicatePlaybookError(PlaybookError):
    """Two playbook files declare the same ``name``."""


class UnknownPlaybookError(KeyError):
    """``get_playbook`` was asked for a name that is not registered."""


class PlaybookRequirement(BaseModel):
    """One input of a step that comes from an earlier call's response.

    Structured rather than prose (``"internal_id from get_tiktok_products"``)
    because only the structured form can be linted: ``param`` is asserted to
    resolve to a declared parameter or ``body_schema`` property of the step's
    own operation, which is the check that catches drift when the upstream
    renames a field.
    """

    model_config = ConfigDict(extra="forbid")

    # A parameter name, or a dotted path into the operation's request body
    # (``body.new_products[].asin``). Linted against the operation's schema.
    param: str
    from_operation: str
    # Where in ``from_operation``'s response the value comes from. Free text:
    # response shapes are not modelled anywhere in this server (the dispatcher
    # is a pure forwarder), so there is nothing to lint it against.
    field: str


class VerifyWith(BaseModel):
    """The read operation that establishes whether a failed step landed."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    how: str


class OnFailure(BaseModel):
    """What a *failed* step means for the chain.

    Only properties that are true regardless of *which* error arrived — so
    nothing here drifts when an upstream changes an error string. Notably there
    is no per-step list of possible errors: that duplicates ``business_errors``
    and rots on every upstream change.
    """

    model_config = ConfigDict(extra="forbid")

    # May this step be repeated blind?
    idempotent: bool
    # What state may exist despite the error.
    left_behind: str | None = None
    # Which read establishes whether it landed, and how.
    verify_with: VerifyWith | None = None
    # What the chain does in each case.
    then: str
    # A rare flag, not a default. Hosts already gate a destructive tool behind
    # their own approval prompt, so a retry re-prompts anyway; reserve this for
    # steps where the retry *itself* is the risk (credit-spending or otherwise
    # non-idempotent writes) rather than building a second consent mechanism.
    ask_user: bool = False


class PlaybookStep(BaseModel):
    """One call in a chain.

    ``extra="forbid"``, unlike the vendored manifest models: these files are
    hand-authored, and a mistyped ``incomplete_alone`` that parses to nothing is
    exactly the invisible-field failure the manifests already got bitten by.
    """

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    goal: str
    requires: list[PlaybookRequirement] = Field(default_factory=list)
    # Successors. Optional: the default is the next step in the list, so author
    # this only where the chain branches.
    then: list[str] = Field(default_factory=list)
    # What is left unfinished if an agent stops here. Required by the boot lint
    # on any non-final destructive step.
    incomplete_alone: str | None = None
    on_failure: OnFailure | None = None


class Playbook(BaseModel):
    """One chain: what it achieves, in what order, and when it is done."""

    model_config = ConfigDict(extra="forbid")

    name: str
    title: str
    when_to_use: str
    # Where an agent enters the chain. Must be one of the steps: numbering is
    # positional, so an entry point outside the list would make "step 1 of N"
    # mean something different from "the first thing you call".
    entry_operation: str
    steps: list[PlaybookStep]
    done_when: str
    # The markdown runbook. Fetched only by ``get_playbook``, so this is where
    # polling cadence, state machines and recovery tables belong.
    body: str = ""


@dataclass(frozen=True)
class PlaybookStepRef:
    """One step, plus the position that gives it a step number.

    Positions are derived, never authored, which is what lets one operation
    belong to several playbooks without any file knowing about the others.
    """

    playbook: Playbook
    index: int

    @property
    def step(self) -> PlaybookStep:
        return self.playbook.steps[self.index]

    @property
    def number(self) -> int:
        return self.index + 1

    @property
    def total(self) -> int:
        return len(self.playbook.steps)

    @property
    def label(self) -> str:
        return f"{self.number}/{self.total}"

    @property
    def next_operations(self) -> list[str]:
        """Successors: the authored ``then``, else the next step in the list."""
        if self.step.then:
            return list(self.step.then)
        if self.index + 1 < self.total:
            return [self.playbook.steps[self.index + 1].operation_id]
        return []

    @property
    def is_final(self) -> bool:
        return not self.next_operations


def load_playbooks(directory: Path | str) -> list[Playbook]:
    """Parse every ``*.json`` playbook in ``directory`` (non-recursive).

    Sorted-filename order, for the same reason ``load_manifests`` uses it: the
    order reaches clients (the ``get_playbook`` enum, the ``instructions``
    index), so it must not depend on which replica answered.

    A missing directory yields no playbooks rather than an error — a deployment
    that ships manifests and no chains is a legitimate configuration.
    """
    path = Path(directory)
    if not path.is_dir():
        return []
    playbooks: list[Playbook] = []
    for file in sorted(path.glob("*.json")):
        try:
            playbooks.append(Playbook.model_validate_json(file.read_text(encoding="utf-8")))
        except ValueError as exc:
            raise PlaybookError(f"Playbook file '{file.name}' is invalid: {exc}") from exc
    return playbooks


class PlaybookRegistry:
    """Name-keyed playbooks, plus the ``operation_id -> steps`` reverse index."""

    def __init__(self, playbooks: list[Playbook]) -> None:
        self._playbooks: dict[str, Playbook] = {}
        self._by_operation: dict[str, list[PlaybookStepRef]] = {}
        for playbook in playbooks:
            if playbook.name in self._playbooks:
                raise DuplicatePlaybookError(
                    f"Duplicate playbook name '{playbook.name}'; the name is the enum value clients call "
                    f"get_playbook with, so it must be unique."
                )
            self._playbooks[playbook.name] = playbook
            for index, step in enumerate(playbook.steps):
                self._by_operation.setdefault(step.operation_id, []).append(PlaybookStepRef(playbook, index))

    def names(self) -> list[str]:
        """Registered names, in load order. This list *is* the tool's enum."""
        return list(self._playbooks)

    def list_playbooks(self) -> list[Playbook]:
        return list(self._playbooks.values())

    def get(self, name: str) -> Playbook | None:
        return self._playbooks.get(name)

    def steps_for(self, operation_id: str) -> list[PlaybookStepRef]:
        """Every step that calls ``operation_id``, across all playbooks."""
        return list(self._by_operation.get(operation_id, ()))

    def __len__(self) -> int:
        return len(self._playbooks)


def build_playbook_registry(manifest_directory: Path | str) -> PlaybookRegistry:
    """Load ``<manifest_directory>/playbooks/`` into a registry."""
    return PlaybookRegistry(load_playbooks(Path(manifest_directory) / PLAYBOOK_SUBDIR))


# --- rendering: the four generated channels ---------------------------------


def render_playbook_payload(playbook: Playbook) -> dict[str, Any]:
    """The ``data`` of a ``get_playbook`` call.

    Step numbers and successors are materialised here so the client reads the
    same derived values the hints do, rather than re-deriving them from list
    position and getting it subtly wrong.
    """
    steps: list[dict[str, Any]] = []
    for index in range(len(playbook.steps)):
        ref = PlaybookStepRef(playbook, index)
        step = ref.step
        entry: dict[str, Any] = {
            "step": ref.number,
            "of": ref.total,
            "operation_id": step.operation_id,
            "goal": step.goal,
            "next": ref.next_operations,
        }
        if step.requires:
            entry["requires"] = [requirement.model_dump() for requirement in step.requires]
        if step.incomplete_alone:
            entry["incomplete_alone"] = step.incomplete_alone
        if step.on_failure is not None:
            entry["on_failure"] = step.on_failure.model_dump(exclude_none=True)
        steps.append(entry)
    return {
        "name": playbook.name,
        "title": playbook.title,
        "when_to_use": playbook.when_to_use,
        "entry_operation": playbook.entry_operation,
        "done_when": playbook.done_when,
        "steps": steps,
        "body": playbook.body,
    }


def render_description_tail(refs: list[PlaybookStepRef]) -> str:
    """The bounded pointer appended to a chain tool's ``description``.

    One line, no step bodies: this text rides in the tool definitions, which is
    the wrong channel for anything situational. When an operation belongs to
    several chains the line names them all instead of picking one — which chain
    the agent is in is not knowable from the descriptor. The names need no
    deduping: lint 3 rejects an operation appearing twice in one playbook, so two
    refs for one operation always come from two different files.
    """
    if not refs:
        return ""
    if len(refs) == 1:
        ref = refs[0]
        return (
            f'Step {ref.number} of {ref.total} in playbook "{ref.playbook.name}" — '
            f"call get_playbook for the full chain."
        )
    names = ", ".join(f'"{ref.playbook.name}"' for ref in refs)
    return f"A step in playbooks {names} — call get_playbook for the full chain."


def _render_one_success_hint(ref: PlaybookStepRef) -> dict[str, Any]:
    """The specific hint, for an operation that belongs to exactly one chain."""
    hint: dict[str, Any] = {
        "name": ref.playbook.name,
        "step": ref.label,
        "next": ref.next_operations,
    }
    if ref.step.incomplete_alone:
        hint["incomplete_alone"] = ref.step.incomplete_alone
    hint["runbook"] = f'get_playbook("{ref.playbook.name}")'
    return hint


def render_success_hint(refs: list[PlaybookStepRef]) -> dict[str, Any] | None:
    """The ``playbook`` envelope field for a call that has chain work after it.

    Takes *every* step that calls this operation, because the honest answer
    depends on how many there are. The transport is stateless and nothing in a
    request says which chain the agent is following, so when an operation belongs
    to several chains the server cannot know which one this call belongs to.

    Three cases:

    * **No chain work left** (every candidate step is final) — ``None``. A chain
      that is over has nothing to nudge, and an "you are done" field on every
      terminal call is pure cost.
    * **Exactly one chain** — the specific hint: step number, successors and
      ``incomplete_alone``. Byte-identical to what a single-playbook deployment
      has always sent.
    * **Several chains** — a deliberately vaguer hint that names the candidates
      and points at ``get_playbook``, and asserts nothing else. Picking the first
      chain and stating its step number and ``incomplete_alone`` as fact is the
      tempting alternative and it is wrong: an agent importing a TikTok product
      would be told "nothing is in the store yet" when the truth for its chain is
      "listed at cost with no supplier attached". A pointer costs one extra call;
      a confident wrong warning costs the user money. Only chains with work left
      are named — one that is over is not something to be nudged towards.
    """
    pending = [ref for ref in refs if not ref.is_final]
    if not pending:
        return None
    if len(refs) == 1:
        return _render_one_success_hint(refs[0])
    return {
        "in": [ref.playbook.name for ref in pending],
        "step_depends_on_chain": True,
        "runbook": "get_playbook(<the chain you are in, from `in`>)",
    }


def hint_size(hint: dict[str, Any]) -> int:
    """Serialized size of an envelope hint, as the size lint measures it."""
    return len(json.dumps(hint, separators=(",", ":"), ensure_ascii=False))


def _render_one_failure_hint(ref: PlaybookStepRef, on_failure: OnFailure) -> str:
    """The specific tail, for an operation that belongs to exactly one chain."""
    parts = [f'Playbook "{ref.playbook.name}" step {ref.label}:']
    parts.append("this step is idempotent." if on_failure.idempotent else "this step is not idempotent.")
    if on_failure.left_behind:
        parts.append(on_failure.left_behind)
    if on_failure.verify_with is not None:
        parts.append(f"Verify with {on_failure.verify_with.operation_id} ({on_failure.verify_with.how}).")
    parts.append(on_failure.then)
    if on_failure.ask_user:
        parts.append("Ask the user before retrying.")
    return " ".join(parts)


def _render_merged_failure_hint(refs: list[PlaybookStepRef]) -> str:
    """The tail for an operation in several chains: only what holds in all of them.

    Every clause errs towards caution, because this is the string that stops a
    duplicated write and the server does not know which chain the caller is in:

    * **idempotent** — "not idempotent" if *any* candidate says so. Telling an
      agent a blind retry is safe when one chain says it isn't is the one mistake
      that costs the user a duplicate listing.
    * **left_behind** / **verify_with** — emitted only when every candidate that
      declared ``on_failure`` says the same thing. The verification tool is the
      most valuable clause here and in practice the chains agree on it (it is a
      property of the write, not of the recipe), so this usually survives.
    * **then** — never merged. What the chain does next is the one genuinely
      chain-scoped field, so it is replaced by a pointer to ``get_playbook``.
    * **ask_user** — set if *any* candidate sets it.

    A candidate that declares no ``on_failure`` at all contributes nothing: a
    missing declaration is not evidence that retrying is safe. It is still named,
    so the caller can see its own chain in the list.
    """
    declared = [ref.step.on_failure for ref in refs if ref.step.on_failure is not None]
    names = ", ".join(f'"{ref.playbook.name}"' for ref in refs)
    parts = [f"Playbook step in {names}:"]
    idempotent = all(on_failure.idempotent for on_failure in declared)
    parts.append("this step is idempotent." if idempotent else "this step is not idempotent.")
    left_behind = {on_failure.left_behind for on_failure in declared}
    if len(left_behind) == 1 and (agreed := next(iter(left_behind))):
        parts.append(agreed)
    verifiers = {
        (on_failure.verify_with.operation_id, on_failure.verify_with.how) if on_failure.verify_with else None
        for on_failure in declared
    }
    if len(verifiers) == 1 and (verifier := next(iter(verifiers))) is not None:
        parts.append(f"Verify with {verifier[0]} ({verifier[1]}).")
    parts.append("Recovery differs by chain — call get_playbook for yours.")
    if any(on_failure.ask_user for on_failure in declared):
        parts.append("Ask the user before retrying.")
    return " ".join(parts)


def render_failure_hint(refs: list[PlaybookStepRef]) -> str | None:
    """The chain-consequence tail appended to a failing call's error text.

    ``None`` when no candidate step declares ``on_failure`` — and then the error
    the client receives is byte-identical to what it was before playbooks existed.

    As with the success hint, the specific form is used only when the operation
    belongs to exactly one chain. With several, the tail states just what holds
    across all of them (see :func:`_render_merged_failure_hint`) — including when
    only one of them declared ``on_failure``, since that chain's ``then`` still
    cannot be asserted for a caller who may be in another.
    """
    declared = [ref for ref in refs if ref.step.on_failure is not None]
    if not declared:
        return None
    if len(refs) == 1:
        ref = declared[0]
        return _render_one_failure_hint(ref, ref.step.on_failure)
    return _render_merged_failure_hint(refs)


# Lead-in for the generated ``instructions`` section. Hand-written once, here,
# rather than per playbook: the per-playbook half is generated from the files,
# so the index cannot drift from what ``get_playbook`` actually serves.
_INDEX_HEADING = "## Playbooks"
_INDEX_LEAD = (
    "Chains of tools that only finish the job together — stopping halfway leaves work that looks done. "
    "`get_playbook` returns the runbook for one."
)


def build_playbook_index(registry: PlaybookRegistry) -> str:
    """The generated ``## Playbooks`` section of the server ``instructions``.

    Three lines plus one bullet per chain — an index, not a catalogue. It counts
    against the RD-90 ``instructions`` size cap, which is precisely why the
    runbooks themselves are behind ``get_playbook``.
    """
    if not len(registry):
        return ""
    lines = [_INDEX_HEADING, "", _INDEX_LEAD, ""]
    for playbook in registry.list_playbooks():
        lines.append(
            f"- `{playbook.name}` — {playbook.when_to_use} Start with `{playbook.entry_operation}`; "
            f"call `get_playbook` for the runbook."
        )
    return "\n".join(lines)


# --- boot lints (the D5 family: every one of these refuses to boot) ---------


def _operation_input_names(registry: ManifestRegistry, operation_id: str) -> set[str]:
    """Every name a ``requires[].param`` may legitimately resolve to.

    Declared parameters, plus the ``body_schema`` property names at any depth —
    a body path is authored as ``body.new_products[].asin``, so the lint checks
    each dotted segment against the properties the schema declares somewhere,
    rather than walking the schema positionally. That is deliberately loose: it
    catches a renamed or invented field, which is the drift that matters, and
    does not pretend to type-check a path.
    """
    operation = registry.get(operation_id)
    if operation is None:
        return set()
    names = {parameter.name for parameter in operation.parameters}
    if operation.has_json_body:
        names.add("body")

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    names.update(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(operation.body_schema)
    return names


def _path_segments(param: str) -> list[str]:
    """``body.new_products[].asin`` -> ``[body, new_products, asin]``."""
    return [segment.replace("[]", "") for segment in param.split(".") if segment]


def _assert_references_resolve(playbook: Playbook, registry: ManifestRegistry) -> None:
    """Lint 1: every operation a playbook names must be a registered operation.

    A playbook that points at a tool the server does not serve is the phantom-
    tool failure in a new costume: the text reads authoritatively and the agent's
    call fails with something it cannot act on.
    """
    referenced: list[tuple[str, str]] = [("entry_operation", playbook.entry_operation)]
    for step in playbook.steps:
        referenced.append(("steps[].operation_id", step.operation_id))
        referenced.extend(("requires[].from_operation", req.from_operation) for req in step.requires)
        referenced.extend(("then[]", successor) for successor in step.then)
        if step.on_failure is not None and step.on_failure.verify_with is not None:
            referenced.append(("on_failure.verify_with.operation_id", step.on_failure.verify_with.operation_id))
    for field, operation_id in referenced:
        if registry.get(operation_id) is None:
            raise PlaybookError(f"Playbook '{playbook.name}' {field} names unregistered operation '{operation_id}'.")


def _assert_requires_resolve(playbook: Playbook, registry: ManifestRegistry) -> None:
    """Lint 2: a ``requires[].param`` must name an input its own step accepts."""
    for step in playbook.steps:
        known = _operation_input_names(registry, step.operation_id)
        for requirement in step.requires:
            for segment in _path_segments(requirement.param):
                if segment not in known:
                    raise PlaybookError(
                        f"Playbook '{playbook.name}' step '{step.operation_id}' requires "
                        f"'{requirement.param}', but '{segment}' is not a parameter or body field of "
                        f"that operation."
                    )


def _assert_steps_reachable(playbook: Playbook) -> None:
    """Lint 3b: the chain's own graph — no operation twice, and every reference
    between steps (``entry_operation``, ``then[]``) names a step, with all of them
    reachable from the entry.

    This is where every *chain-local* reference is checked. Lint 1 asks a different
    question of a different set: ``requires[].from_operation`` and
    ``on_failure.verify_with`` legitimately point outside the chain (the pilot's
    step 1 takes ``store_ids`` from ``list_stores_api``, which is used by
    everything and is deliberately not a step), so all lint 1 can ask of those is
    that the operation is registered. ``entry_operation`` and ``then[]`` are the
    two that must resolve *within* the playbook, and both are enforced here.

    A ``then`` naming a registered operation that is not a step used to pass every
    lint, because the walk below simply skipped successors it could not find. The
    result reached clients: the step stopped counting as final, so it emitted a
    hint whose ``next`` recommended a tool outside the chain — in the case that
    surfaced it, a destructive publish — and read as "step 3 of 3, next: …", which
    is incoherent for numbering that comes from list position. Only one shape of
    it was caught, and by accident: a dangling successor that *replaces* a valid
    one orphans the rest of the chain and trips the reachability check, while one
    added *beside* a valid one is invisible.

    An unreachable step is the other half: silently dead documentation. It renders
    into ``get_playbook``'s payload and its operation's description tail, but no
    successor points at it, so nothing tells an agent to get there.

    One operation may belong to several *playbooks* but not appear twice in the
    same one: every reference between steps is keyed by ``operation_id``, so a
    second occurrence has no addressable identity. Left unchecked it fails as a
    confusing *unreachable* error (the reverse index keeps only the first
    position), and it renders the operation's description tail as
    ``playbooks "x", "x"``. Rejecting it by name is the honest version: a chain
    that genuinely calls one tool twice needs a step key that isn't the operation
    id, which is a schema change, not an authoring accident.
    """
    if not playbook.steps:
        raise PlaybookError(f"Playbook '{playbook.name}' declares no steps.")
    index_by_operation: dict[str, int] = {}
    for index, step in enumerate(playbook.steps):
        if step.operation_id in index_by_operation:
            raise PlaybookError(
                f"Playbook '{playbook.name}' lists operation '{step.operation_id}' twice (steps "
                f"{index_by_operation[step.operation_id] + 1} and {index + 1}); steps are addressed by "
                f"operation_id, so a repeated one cannot be pointed at, numbered or rendered."
            )
        index_by_operation[step.operation_id] = index
    if playbook.entry_operation not in index_by_operation:
        raise PlaybookError(
            f"Playbook '{playbook.name}' entry_operation '{playbook.entry_operation}' is not one of its "
            f"steps; step numbering is positional, so the entry point has to be a step."
        )
    for step in playbook.steps:
        for successor in step.then:
            if successor not in index_by_operation:
                raise PlaybookError(
                    f"Playbook '{playbook.name}' step '{step.operation_id}' names successor "
                    f"'{successor}', which is not a step of this playbook. 'then' is the next step in "
                    f"*this* chain, so a registered operation outside it is not a successor: it would "
                    f"stop this step counting as final and put a tool the chain never contains into the "
                    f"'next' the client is handed. Add it as a step, or drop it."
                )

    reachable: set[int] = set()
    frontier = [index_by_operation[playbook.entry_operation]]
    while frontier:
        index = frontier.pop()
        if index in reachable:
            continue
        reachable.add(index)
        # Every successor resolves: the authored ``then`` was checked above, and
        # the positional default is a step by construction. No missing-key guard,
        # so a future reference kind cannot go quietly missing here.
        for successor in PlaybookStepRef(playbook, index).next_operations:
            frontier.append(index_by_operation[successor])

    unreachable = [playbook.steps[i].operation_id for i in range(len(playbook.steps)) if i not in reachable]
    if unreachable:
        raise PlaybookError(
            f"Playbook '{playbook.name}' has steps unreachable from '{playbook.entry_operation}': "
            f"{', '.join(unreachable)}."
        )


def _assert_destructive_steps_declare_consequences(playbook: Playbook, registry: ManifestRegistry) -> None:
    """Lints 4 and 5: a destructive step has to say what stopping and retrying cost.

    Lint 4 — if an agent can stop at this step and leave damage, the file has to
    say what the damage is. Lint 5 — if the retry is the risk, the file has to
    name a *read-only* operation that establishes whether the write landed. The
    obvious verification tool is often the wrong one: polling a bulk job needs
    an id a failed write never returned, so verification has to go through a
    list/read operation instead.
    """
    for index, step in enumerate(playbook.steps):
        operation = registry.get(step.operation_id)
        if operation is None or not operation.annotations.destructive_hint:
            continue
        ref = PlaybookStepRef(playbook, index)
        if not ref.is_final and not step.incomplete_alone:
            raise PlaybookError(
                f"Playbook '{playbook.name}' step '{step.operation_id}' is destructive and not final, "
                f"so it must declare 'incomplete_alone' — what is left broken if an agent stops here."
            )
        on_failure = step.on_failure
        if on_failure is None or on_failure.idempotent:
            continue
        if on_failure.verify_with is None:
            raise PlaybookError(
                f"Playbook '{playbook.name}' step '{step.operation_id}' is destructive and not "
                f"idempotent, so its on_failure must declare 'verify_with'; 'retry on failure' without "
                f"a way to check for partial success duplicates the write."
            )
        verifier = registry.get(on_failure.verify_with.operation_id)
        if verifier is None or not verifier.annotations.read_only_hint:
            raise PlaybookError(
                f"Playbook '{playbook.name}' step '{step.operation_id}' verifies with "
                f"'{on_failure.verify_with.operation_id}', which is not advertised readOnlyHint; "
                f"verifying a failed write must not be able to write again."
            )


def _assert_text_within_budgets(playbook: Playbook) -> None:
    """Lint 6: every rendered string fits the channel it is delivered on.

    Rendered, not authored: the caps apply to what a client actually receives,
    which includes the generated prefixes. Enforced at boot rather than
    truncated at runtime — half a sentence about a duplicated write is worse
    than a deploy that refuses to start.

    What is checked here is the *specific* rendering — the one this file's author
    controls on their own. The strings that depend on every playbook at once (the
    description tail, and the merged hints an operation in several chains renders)
    are checked over the merged index instead, since no single file can be blamed
    for them.
    """
    if len(playbook.body) > BODY_MAX_CHARS:
        raise PlaybookError(
            f"Playbook '{playbook.name}' body is {len(playbook.body)} chars, over the "
            f"{BODY_MAX_CHARS}-char limit; it is lazy, but not free once fetched."
        )
    for index, step in enumerate(playbook.steps):
        ref = PlaybookStepRef(playbook, index)
        hint = render_success_hint([ref])
        if hint is not None and hint_size(hint) > ENVELOPE_HINT_MAX_CHARS:
            raise PlaybookError(
                f"Playbook '{playbook.name}' step '{step.operation_id}' renders a "
                f"{hint_size(hint)}-char envelope hint, over the {ENVELOPE_HINT_MAX_CHARS}-char limit; "
                f"it is serialized twice per call and repeats on every poll, so 'incomplete_alone' has "
                f"to be one short sentence."
            )
        failure = render_failure_hint([ref])
        if failure is not None and len(failure) > FAILURE_HINT_MAX_CHARS:
            raise PlaybookError(
                f"Playbook '{playbook.name}' step '{step.operation_id}' renders a {len(failure)}-char "
                f"failure hint, over the {FAILURE_HINT_MAX_CHARS}-char limit."
            )


def assert_playbooks_valid(playbooks: PlaybookRegistry, registry: ManifestRegistry) -> None:
    """Run every playbook boot lint. Raises :class:`PlaybookError` on the first
    failure, at boot, so a broken chain can never reach a client.

    ``playbooks`` is already name-unique (the registry raises on a duplicate as
    it indexes), so lint 3a is enforced by construction.
    """
    for playbook in playbooks.list_playbooks():
        _assert_references_resolve(playbook, registry)
        _assert_requires_resolve(playbook, registry)
        _assert_steps_reachable(playbook)
        _assert_destructive_steps_declare_consequences(playbook, registry)
        _assert_text_within_budgets(playbook)
    # Three rendered strings depend on *all* playbooks at once, because an
    # operation in two chains renders differently from the same file: the
    # description tail, and both hints in their merged form. Their caps are
    # therefore checked over the merged index — no single file can be blamed, and
    # a per-file check would pass right up until the second chain landed.
    for operation_id in {step.operation_id for pb in playbooks.list_playbooks() for step in pb.steps}:
        refs = playbooks.steps_for(operation_id)
        tail = render_description_tail(refs)
        if len(tail) > DESCRIPTION_TAIL_MAX_CHARS:
            raise PlaybookError(
                f"Operation '{operation_id}' renders a {len(tail)}-char playbook description tail, over "
                f"the {DESCRIPTION_TAIL_MAX_CHARS}-char limit; the description carries a pointer, never "
                f"a step body."
            )
        if len(refs) < 2:
            continue  # The specific rendering; already checked against its own file.
        # The merged *success* hint is deliberately not checked. It carries only
        # the chain names plus a fixed pointer, and the description tail above
        # carries the same names in a longer sentence under a tighter cap — so the
        # tail always overruns first and a check here could never fire. That is a
        # coupling, not a coincidence: ``test_the_tail_cap_subsumes_the_merged_hint_cap``
        # pins the headroom, so lengthening the hint's fixed text fails loudly and
        # tells you to reinstate this check.
        merged_failure = render_failure_hint(refs)
        if merged_failure is not None and len(merged_failure) > FAILURE_HINT_MAX_CHARS:
            raise PlaybookError(
                f"Operation '{operation_id}' is a step in {len(refs)} playbooks and renders a "
                f"{len(merged_failure)}-char merged failure tail, over the {FAILURE_HINT_MAX_CHARS}-char "
                f"limit; the merged tail keeps only what the chains agree on, so shorten the shared "
                f"'left_behind' / 'verify_with' text or a playbook name."
            )
