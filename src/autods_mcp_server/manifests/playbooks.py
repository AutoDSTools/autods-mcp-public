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
    the agent is in is not knowable from the descriptor.
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


def render_success_hint(ref: PlaybookStepRef) -> dict[str, Any] | None:
    """The ``playbook`` envelope field for a step that has work after it.

    ``None`` for a final step: a chain that is over has nothing to nudge, and an
    "you are done" field on every terminal call is pure cost.
    """
    if ref.is_final:
        return None
    hint: dict[str, Any] = {
        "name": ref.playbook.name,
        "step": ref.label,
        "next": ref.next_operations,
    }
    if ref.step.incomplete_alone:
        hint["incomplete_alone"] = ref.step.incomplete_alone
    hint["runbook"] = f'get_playbook("{ref.playbook.name}")'
    return hint


def hint_size(hint: dict[str, Any]) -> int:
    """Serialized size of an envelope hint, as the size lint measures it."""
    return len(json.dumps(hint, separators=(",", ":"), ensure_ascii=False))


def render_failure_hint(ref: PlaybookStepRef) -> str | None:
    """The chain-consequence tail appended to a failing step's error text.

    ``None`` when the step declares no ``on_failure`` — and then the error the
    client receives is byte-identical to what it was before playbooks existed.
    """
    on_failure = ref.step.on_failure
    if on_failure is None:
        return None
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
    """Lint 3b: ``entry_operation`` is a step, and every step is reachable.

    An unreachable step is silently dead documentation — it renders into
    ``get_playbook``'s payload and its operation's description tail, but no
    successor ever points at it, so nothing tells an agent to get there.
    """
    if not playbook.steps:
        raise PlaybookError(f"Playbook '{playbook.name}' declares no steps.")
    index_by_operation: dict[str, int] = {}
    for index, step in enumerate(playbook.steps):
        index_by_operation.setdefault(step.operation_id, index)
    if playbook.entry_operation not in index_by_operation:
        raise PlaybookError(
            f"Playbook '{playbook.name}' entry_operation '{playbook.entry_operation}' is not one of its "
            f"steps; step numbering is positional, so the entry point has to be a step."
        )

    reachable: set[int] = set()
    frontier = [index_by_operation[playbook.entry_operation]]
    while frontier:
        index = frontier.pop()
        if index in reachable:
            continue
        reachable.add(index)
        for successor in PlaybookStepRef(playbook, index).next_operations:
            target = index_by_operation.get(successor)
            if target is not None:
                frontier.append(target)

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

    The description tail is *not* checked here: it is the one rendered string
    that depends on every playbook at once (an operation in two chains renders a
    different line), so it is checked once over the merged index instead.
    """
    if len(playbook.body) > BODY_MAX_CHARS:
        raise PlaybookError(
            f"Playbook '{playbook.name}' body is {len(playbook.body)} chars, over the "
            f"{BODY_MAX_CHARS}-char limit; it is lazy, but not free once fetched."
        )
    for index, step in enumerate(playbook.steps):
        ref = PlaybookStepRef(playbook, index)
        hint = render_success_hint(ref)
        if hint is not None and hint_size(hint) > ENVELOPE_HINT_MAX_CHARS:
            raise PlaybookError(
                f"Playbook '{playbook.name}' step '{step.operation_id}' renders a "
                f"{hint_size(hint)}-char envelope hint, over the {ENVELOPE_HINT_MAX_CHARS}-char limit; "
                f"it is serialized twice per call and repeats on every poll, so 'incomplete_alone' has "
                f"to be one short sentence."
            )
        failure = render_failure_hint(ref)
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
    # The description tail is the one rendered string that depends on *all*
    # playbooks at once (an operation in two chains renders a different line),
    # so its cap is checked once over the merged index.
    for operation_id in {step.operation_id for pb in playbooks.list_playbooks() for step in pb.steps}:
        tail = render_description_tail(playbooks.steps_for(operation_id))
        if len(tail) > DESCRIPTION_TAIL_MAX_CHARS:
            raise PlaybookError(
                f"Operation '{operation_id}' renders a {len(tail)}-char playbook description tail, over "
                f"the {DESCRIPTION_TAIL_MAX_CHARS}-char limit; the description carries a pointer, never "
                f"a step body."
            )
