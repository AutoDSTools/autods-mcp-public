"""Server ``instructions``: assembly from the manifests, and the size lint (RD-90).

``InitializeResult.instructions`` is the one text channel that reaches a client
without being attached to a tool. The SDK carries whatever is passed as
``Server(instructions=…)`` through ``create_initialization_options()`` into the
handshake, and clients surface it in the model's system prompt — for Claude Code
under an "MCP Server Instructions" heading.

**Why the cap.** That text rides in the system prompt on every model turn for
the life of the conversation, and it sits in the cached prefix — so editing it
invalidates prompt caching for everything downstream. It is at once the most
expensive channel and the least reliable one (surfacing it is client discretion).
So it is an *index*, not a reference manual: where to start, and the two or three
invariants that hold across every tool. Anything needed to form a particular call
belongs in that tool's ``inputSchema``; anything describing one tool's contract
belongs in its ``description``/``notes``. Those arrive *with* the tool, which is
both cheaper and more reliable.

The lint enforces the budget rather than the content, because content is the one
thing a reviewer can see. ``INSTRUCTIONS_HARD_LIMIT`` fails the boot; the
post-retiering target the manifests are written against is
``INSTRUCTIONS_TARGET`` (asserted by the tests, not at boot, so a new manifest
isn't blocked from landing by a few hundred chars while the text is trimmed).

An **empty** per-manifest ``instructions`` is legitimate and common: a domain
whose whole contract is tier 1/2 has nothing to say here (``users.json``,
``stores.json``). There is deliberately no non-empty lint — requiring text per
manifest would push filler into the most expensive channel.
"""

from autods_mcp_server.manifests.schema import Manifest

# Boot fails above this. ~1.5k tokens: an amount of always-resident system
# prompt the server can justify spending on every turn of every conversation.
INSTRUCTIONS_HARD_LIMIT = 6000

# What the committed manifests are written to fit inside. The gap to the hard
# limit is headroom for a manifest landing before its text has been trimmed.
INSTRUCTIONS_TARGET = 4000

# Blank line between manifests, so concatenated markdown sections don't run
# together into one paragraph.
_SEPARATOR = "\n\n"


class InstructionsTooLargeError(ValueError):
    """The concatenated manifest ``instructions`` exceed the hard size limit."""


def build_instructions(manifests: list[Manifest]) -> str:
    """Concatenate the per-manifest ``instructions``, in the order given.

    Callers pass the list straight from ``load_manifests``, which reads files in
    sorted-filename order. That ordering is load-bearing twice over: a
    replica-dependent order would give different replicas a different system
    prompt for the same server, and it would break prompt-cache reuse across
    conversations. Keep it deterministic.

    Manifests with empty (or whitespace-only) ``instructions`` contribute
    nothing — not even a separator.
    """
    blocks = [manifest.instructions.strip() for manifest in manifests]
    return _SEPARATOR.join(block for block in blocks if block)


def assert_instructions_within_limit(instructions: str) -> None:
    """Boot lint: refuse to start if the assembled instructions are too large.

    Raises:
        InstructionsTooLargeError: above ``INSTRUCTIONS_HARD_LIMIT`` characters.
            Raised at boot, like the other manifest lints, so oversized text
            can't reach a client's system prompt.
    """
    size = len(instructions)
    if size > INSTRUCTIONS_HARD_LIMIT:
        raise InstructionsTooLargeError(
            f"Concatenated manifest instructions are {size} chars, over the "
            f"{INSTRUCTIONS_HARD_LIMIT}-char limit. instructions ride in the client's system "
            f"prompt on every turn — move per-call detail into the tool inputSchema and "
            f"per-tool contract into the tool description/notes."
        )
