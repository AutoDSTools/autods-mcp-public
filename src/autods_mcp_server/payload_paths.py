"""Dotted-path resolution over an upstream JSON payload, with wildcards.

One small, shared resolver for every manifest block that has to point at a
place *inside* a response payload without the server knowing that payload's
shape. Today's consumer is ``business_errors`` (RD-90); the ``images`` block
(RD-92) needs the same addressing, so this lives on its own rather than being
written twice with two slightly different wildcard semantics.

Path grammar — deliberately minimal, because a manifest is data an editor
writes by hand, not a query language:

* ``a.b.c`` walks dict keys.
* ``*`` matches **every** element of a list or **every** value of a dict at that
  position, so ``data.*.error.errorCode`` reads one field out of every item of a
  result page.
* A list index is *not* addressable on purpose: ``data.0.x`` would encourage
  manifests that depend on upstream ordering. Use ``*``.

Resolution never raises on a shape mismatch. A path that doesn't fit the
payload (missing key, scalar where a dict was expected, ``None`` in the middle)
simply yields no matches — an upstream that drops an optional field must not
turn into a 500 on our side.
"""

from typing import Any

# Segment that matches every element of a list / every value of a dict.
WILDCARD = "*"


def resolve_path(payload: Any, path: str) -> list[Any]:
    """All values in ``payload`` addressed by the dotted ``path``.

    Returns matches in document order (list order, then dict insertion order,
    which for a parsed JSON object is the order the upstream serialised it in —
    so the result is deterministic for a given payload). An empty path, or a
    path that doesn't fit the payload, yields ``[]``.

    ``None`` values *are* returned when the path resolves to an explicit
    ``null`` — callers decide whether a null counts; only structural misses are
    dropped here.
    """
    segments = [segment for segment in path.split(".") if segment]
    if not segments:
        return []

    current: list[Any] = [payload]
    for segment in segments:
        following: list[Any] = []
        for node in current:
            if segment == WILDCARD:
                if isinstance(node, list):
                    following.extend(node)
                elif isinstance(node, dict):
                    following.extend(node.values())
                # Anything else has no children to fan out over.
            elif isinstance(node, dict) and segment in node:
                following.append(node[segment])
        if not following:
            return []
        current = following
    return current
