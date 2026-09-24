"""Removing named fields from an upstream payload before it reaches the client (RD-146).

Until RD-146 the rule was absolute: ``data`` is the upstream payload verbatim,
and everything the server adds goes *beside* it. Two kinds of answer broke that
rule's premise. Some are too big for an MCP client to hold — a page of similar
products is a page of full product documents, and one carrying scraped image
galleries measured 267 KB on staging, most of it URLs. And some carry
credentials the model never needs and must not see: an Intercom user token on
``get_current_user``, the store tokens on ``list_stores_api``. No query
parameter on those upstreams trims either.

So an operation may name fields to **remove**, as manifest data
(:class:`~...manifests.schema.OmitEntry`). The limits are what keep this from
becoming per-operation response logic:

* **Remove only, never keep-only.** A keep-list silently drops every field the
  upstream adds later; a remove-list drops only what someone chose to drop.
* **Remove after reading.** The transport runs ``business_errors`` and
  ``images`` against the full payload first, so an error code or a picture can
  never be trimmed away before it is detected.
* **Say so.** The paths that matched are published as an ``omitted`` field
  beside ``data``, so neither the model nor a person debugging concludes the
  upstream sent less than it did.
* **Every path carries its reason and is proven against a recorded answer.**
  The boot lint below checks the declaration; ``test_omit.py`` runs every
  shipped block against a recorded upstream payload and fails on a path that
  matches nothing.

The dispatcher is untouched and still returns the upstream payload verbatim.
That matters beyond the tool call: ``identity.py`` resolves the caller through
the dispatcher, not the transport, and reads fields no manifest could remove.
"""

import copy
import re
from collections.abc import Collection
from typing import Any

from autods_mcp_server.manifests.schema import ManifestOperation
from autods_mcp_server.payload_paths import WILDCARD, resolve_path

# Envelope key the removed paths are published under — a sibling of ``data``.
OMITTED_KEY = "omitted"

# Bracket notation is rejected rather than translated, for the reason given in
# ``images.py``: one path notation across every manifest block.
_BRACKET_PATH = re.compile(r"[\[\]]")

# The loose proxy for "this tool tells the model some fields are removed", in
# the same spirit as the ``ok`` mention ``business_errors`` requires and the
# thumbnail mention ``images`` requires. No lint can check that the sentence
# around it is right; this one catches a block added with the notes forgotten.
_OMITTED_MENTION = re.compile(r"\bomitted\b")


class OmitError(ValueError):
    """An operation's ``omit`` block can never match, or is undocumented."""


def _parent_path_and_key(path: str) -> tuple[str, str]:
    """Split ``a.*.b`` into the path of the dicts to edit (``a.*``) and the key (``b``)."""
    parent, _, key = path.rpartition(".")
    return parent, key


def apply_omit(operation: ManifestOperation, data: Any) -> tuple[Any, list[dict[str, str]] | None]:
    """``data`` with the operation's ``omit`` paths removed, and what was removed.

    Returns ``(data, None)`` — the very same object, untouched — when the
    operation declares no ``omit`` block or when no declared path matches this
    answer, so the envelope stays exactly as it was. Otherwise returns a copy
    with every matching key deleted, and one ``{"path", "reason"}`` entry per
    path that matched (plus ``see`` when another tool returns the data), in
    declaration order.

    The input is never modified: it is copied first, because the payload the
    dispatcher handed over is not ours to edit in place.
    """
    if not operation.omit:
        return data, None

    trimmed = copy.deepcopy(data)
    removed: list[dict[str, str]] = []
    for entry in operation.omit:
        parent_path, key = _parent_path_and_key(entry.path)
        parents = resolve_path(trimmed, parent_path) if parent_path else [trimmed]
        matched = False
        for parent in parents:
            if isinstance(parent, dict) and key in parent:
                del parent[key]
                matched = True
        if matched:
            record = {"path": entry.path, "reason": entry.reason}
            if entry.see:
                record["see"] = entry.see
            removed.append(record)

    if not removed:
        return data, None
    return trimmed, removed


def unmatched_credential_paths(
    operation: ManifestOperation, data: Any, omitted: list[dict[str, str]] | None
) -> list[str]:
    """The ``credential`` paths that removed nothing from a non-empty answer.

    For a size path, matching nothing only means the answer stays big. For a
    credential it means the token reaches the client — and it happens silently
    the day the upstream changes its shape (``list_stores_api`` wrapping its list
    in ``{results: [...]}``, a renamed ``store`` key), because the recorded
    samples in ``test_omit.py`` only prove the shape seen when they were taken.
    So the transport reports these paths instead of letting the answer pass
    unnoticed. An empty answer (no stores yet) has nothing to leak and reports
    nothing.
    """
    if not data:
        return []
    matched = {entry["path"] for entry in omitted or []}
    return [entry.path for entry in operation.omit if entry.reason == "credential" and entry.path not in matched]


def assert_omit_usable(operation: ManifestOperation, served: Collection[str]) -> None:
    """Boot lint for one operation's ``omit`` block.

    ``served`` is every operation id this server exposes, which a ``see`` must
    name. Each failure here is otherwise silent: a malformed path simply
    matches nothing, and a block the notes never mention removes data the model
    was never told is missing.

    Raises:
        OmitError: at boot, like the other manifest lints, so a malformed block
            can never reach a client.
    """
    if not operation.omit:
        return
    op_id = operation.operation_id

    if operation.handler is not None:
        raise OmitError(
            f"Operation '{op_id}' declares 'omit' but is answered locally; the server writes that answer "
            f"itself, so there is no upstream field to remove."
        )

    seen: set[str] = set()
    for entry in operation.omit:
        path = entry.path
        segments = path.split(".")
        if not path or any(not segment for segment in segments):
            raise OmitError(f"Operation '{op_id}' declares omit path {path!r} with an empty segment.")
        if _BRACKET_PATH.search(path):
            raise OmitError(
                f"Operation '{op_id}' uses bracket notation in omit path '{path}'; this server's payload "
                f"paths are dotted with '*' as the wildcard (e.g. 'results.*.description')."
            )
        if segments[-1] == WILDCARD:
            raise OmitError(
                f"Operation '{op_id}' declares omit path '{path}' ending in '*'; the last segment is the key "
                f"removed, so it has to name one. Remove the parent field instead."
            )
        if path in seen:
            raise OmitError(f"Operation '{op_id}' declares omit path '{path}' twice.")
        seen.add(path)
        if not entry.detail.strip():
            raise OmitError(
                f"Operation '{op_id}' declares omit path '{path}' with no 'detail'; say why this field may be "
                f"removed, and what showed it."
            )
        if entry.reason == "available_from_tool":
            if not entry.see:
                raise OmitError(
                    f"Operation '{op_id}' removes '{path}' as available from another tool but names none in 'see'."
                )
            if entry.see == op_id or entry.see not in served:
                raise OmitError(
                    f"Operation '{op_id}' sends the model to '{entry.see}' for '{path}', which is not another "
                    f"tool this server serves."
                )
        elif entry.see:
            raise OmitError(
                f"Operation '{op_id}' sets 'see' on omit path '{path}' with reason '{entry.reason}'; 'see' "
                f"goes with 'available_from_tool' only."
            )

    if not _OMITTED_MENTION.search(operation.notes or ""):
        raise OmitError(
            f"Operation '{op_id}' declares 'omit' but its 'notes' never mention the '{OMITTED_KEY}' field; a "
            f"tool whose answer has fields removed has to say so on the tool itself."
        )
