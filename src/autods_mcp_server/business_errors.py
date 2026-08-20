"""Business-error detection for HTTP 200 responses that carry a rejection (RD-90).

Some upstreams answer a request they refused with ``200 OK`` and an error code
inside the payload. Nothing on the normal error path notices: the dispatcher's
``ok`` is ``True`` (it mirrors the HTTP status and nothing else),
``errors.map_upstream_error`` never runs, and an agent that branches on ``ok``
reports success for a call that did nothing.

This module is the generic half of the fix. Per-operation configuration is
manifest data (``business_errors``: where to look, and what each code means for
recovery); the renderer here is one function that knows nothing about any
specific upstream. What it produces is attached to the envelope as a
``business_error`` field *beside* ``data`` — never inside it, so the upstream
payload the client receives is unchanged and ``dispatch.py`` stays a pure
forwarder.
"""

from typing import Any

from autods_mcp_server.manifests.schema import ManifestOperation
from autods_mcp_server.payload_paths import resolve_path

# Envelope key the detected rejections are published under. A sibling of
# ``data``, so ``data`` itself stays byte-identical to the upstream payload.
BUSINESS_ERROR_KEY = "business_error"

# What to say about a code the manifest doesn't map. Surfacing the code without
# a recovery hint is still much better than silence: the point of the block is
# that ``ok`` alone must not be read as success, and a code the manifest hasn't
# caught up with yet is exactly when that trap bites.
#
# Deliberately does *not* claim the request was not applied. A per-item path
# (``data.*.error.errorCode``) matches when one item of a page failed and the
# rest landed, so "not applied" would be a false statement about the call as a
# whole — and this hint is the one the model reads precisely when nobody has
# curated the code's meaning yet.
_UNMAPPED_HINT = (
    "The upstream reported this business error code. Do not treat the call as applied — "
    "read `data` to see what actually landed."
)


def _codes_at(data: Any, paths: list[str]) -> list[str]:
    """Every non-empty error code the configured paths address, in path order."""
    found: list[str] = []
    for path in paths:
        for value in resolve_path(data, path):
            # Codes are strings upstream; an int is accepted rather than
            # silently ignored. Anything else (dict, list, bool, None) is not a
            # code — and empty/whitespace means "no error", which several
            # upstreams use in place of omitting the field.
            if isinstance(value, str) and value.strip():
                found.append(value.strip())
            elif isinstance(value, int) and not isinstance(value, bool):
                found.append(str(value))
    return found


def detect_business_errors(operation: ManifestOperation, data: Any) -> list[dict[str, str]] | None:
    """The ``business_error`` payload for one successful upstream response.

    Returns ``None`` when the operation declares no ``business_errors`` block or
    when no configured path matches a code — the overwhelmingly common case, and
    the one where the envelope must stay exactly as it was.

    Otherwise returns one ``{"code", "message"}`` entry per *distinct* code, in
    first-seen order. Distinct rather than per-occurrence: a page of 100 items
    that all failed the same way is one fact the model needs, not a hundred
    repetitions of it that crowd out the payload itself.
    """
    config = operation.business_errors
    if config is None or not config.paths:
        return None

    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for code in _codes_at(data, config.paths):
        if code in seen:
            continue
        seen.add(code)
        entries.append({"code": code, "message": config.codes.get(code, _UNMAPPED_HINT)})
    return entries or None
