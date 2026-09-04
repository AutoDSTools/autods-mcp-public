"""Product-image extraction from an upstream payload (RD-92).

Product photos arrive as plain CDN URLs buried somewhere in the response — a
different somewhere per operation — so neither the model nor the user ever sees
a picture, and "pick the offer that looks like this" is impossible to do
visually. This module is the generic half of the fix: per-operation
configuration is manifest data (:class:`~...manifests.schema.ImagesBlock`: where
the items are, where each item's images are, what to caption them with), and the
renderer here knows nothing about any particular upstream.

What it produces is attached to the envelope as an ``images`` field **beside**
``data`` — never inside it, the same rule as ``business_error`` (RD-90) and
``playbook`` (RD-100), and for the same reason: ``data`` is the upstream payload
verbatim and ``dispatch.py`` stays a pure forwarder. An operation with no
``images`` block gets an envelope byte-identical to the one it got before this
existed.

**Paths go through** ``payload_paths`` — the same resolver ``business_errors``
uses, in the same ``*``-wildcard notation. RD-92 sketched the paths in a
``images[].url`` bracket spelling; that was rewritten to ``images.*.url`` rather
than teaching the resolver a second synonym, because one notation across every
manifest block is worth more than matching a sketch, and because extending a
module ``business_errors`` depends on is the larger blast radius. The lint below
rejects a bracketed path outright, so the decision cannot rot back.

**This block duplicates bytes that are already in ``data``, and that is a real
cost, deliberately paid.** A 20-item page adds roughly 2 KB of URLs and labels
to a payload that already contains them. It buys three things: one resolver and
one rewrite table (in Python, not a second pair in JavaScript that would drift
silently); a self-describing payload the widget can find *by shape*, which
matters because how a host hands the tool result to an MCP App is the one link
in this chain nobody has measured; and, in the clients that render no widget at
all — Claude Code, Cursor, MCP Inspector, and Claude Desktop over a remote
connector — a compact ordered list of "here are the pictures" instead of the
same URLs scattered through 40 KB of product documents. It costs *text* tokens
and is capped by ``max`` however large the page is; the zero-vision-token claim
the widget path rests on is untouched.
"""

import re
from typing import Any

from autods_mcp_server.image_rewrites import rewrite_thumbnail
from autods_mcp_server.manifests.schema import ImagesBlock, ManifestOperation
from autods_mcp_server.payload_paths import resolve_path
from autods_mcp_server.widgets import widget_names

# Envelope key the extracted images are published under — a sibling of ``data``.
IMAGES_KEY = "images"

# The tag a widget looks for. The host's channel for handing a tool result to an
# MCP App is not something RD-82 or RD-97 ever measured (both probes rendered
# hard-coded URLs), so the widgets search every inbound message for an object
# carrying this marker instead of trusting a channel name. Changing it means
# changing both HTML assets.
PAYLOAD_KIND = "autods.images/1"

# Hard ceiling on images per result, server-side. RD-82: Claude Desktop
# truncates at ~25k visual tokens and says nothing — one run claimed "19 of 20"
# while holding 16, and described a half-decoded JPEG as a different product. So
# the cap is ours to enforce; the client's is not a cap, it is a corruption.
MAX_IMAGES = 20

# Longest edge of a base64 thumbnail. 252 = 9 × 28, and vision tokens are
# ceil(w/28) × ceil(h/28), so a square one costs exactly 81 — 1,620 for a full
# set of 20, against 36,980 at 1200 px.
BASE64_EDGE_PX = 252

# The synthetic argument that opts a call into base64 image blocks. It is not an
# upstream parameter and never becomes one: ``dispatch._build_request`` reads
# only declared parameters and ``body``, so an undeclared key is already
# dropped. Injected into ``inputSchema`` — which is also what the argument
# validators are compiled from, and since mcp 2.x that validator is the only
# schema gate on the call path, so injecting it anywhere else would get the call
# rejected before the dispatcher ever saw it.
INCLUDE_IMAGES_ARG = "include_images"

# Captions are for a 120 px grid cell, not for reading. Product titles routinely
# run past 200 characters of keyword stuffing, and 20 of those is most of the
# block's weight.
_MAX_LABEL_CHARS = 120

# Bracket notation from RD-92's sketch. Rejected rather than translated: see the
# module docstring.
_BRACKET_PATH = re.compile(r"[\[\]]")

# The loose proxy for "this tool tells the model what it will and won't see",
# in the same spirit as the ``ok`` mention ``business_errors`` requires. No lint
# can check that the sentence around it is *right*; this one catches the block
# being added with the tier-2 text forgotten entirely, which is the failure that
# actually happens.
_THUMBNAIL_MENTION = re.compile(r"thumbnail", re.IGNORECASE)


class ImagesError(ValueError):
    """An operation's ``images`` block can never fire, or is undocumented."""


def assert_images_usable(operation: ManifestOperation) -> None:
    """Boot lint for one operation's ``images`` block.

    Every failure here is otherwise silent — a block with no paths simply never
    matches, a per-item cap of zero renders an empty grid, and a widget name
    nothing serves produces a tool that asks the host to render a resource the
    host cannot read. All of them look fine in review.

    Raises:
        ImagesError: at boot, like the other manifest lints, so a malformed
            block can never reach a client.
    """
    block = operation.images
    if block is None:
        return
    op_id = operation.operation_id

    if not block.image_paths:
        raise ImagesError(
            f"Operation '{op_id}' declares 'images' with no 'image_paths'; the block can never match "
            f"and is dead config."
        )
    for path in [block.item_path, *block.image_paths, block.label_path, block.id_path]:
        if path and _BRACKET_PATH.search(path):
            raise ImagesError(
                f"Operation '{op_id}' uses bracket notation in image path '{path}'; this server's "
                f"payload paths are dotted with '*' as the wildcard (e.g. 'images.*.url')."
            )
    if block.per_item < 1:
        raise ImagesError(f"Operation '{op_id}' sets images.per_item={block.per_item}; at least one image per item.")
    if block.max < 1:
        raise ImagesError(f"Operation '{op_id}' sets images.max={block.max}; at least one item.")
    if block.per_item * block.max > MAX_IMAGES:
        raise ImagesError(
            f"Operation '{op_id}' sets images.per_item × images.max = {block.per_item * block.max}, over the "
            f"{MAX_IMAGES}-image ceiling; past it the client truncates silently and misdescribes what it kept."
        )
    if block.widget is not None and block.widget not in widget_names():
        raise ImagesError(
            f"Operation '{op_id}' names widget '{block.widget}', which no ui:// resource serves; "
            f"registered widgets: {', '.join(widget_names()) or 'none'}."
        )
    if not _THUMBNAIL_MENTION.search(operation.notes or ""):
        raise ImagesError(
            f"Operation '{op_id}' declares 'images' but its 'notes' never mention thumbnails; a tool whose "
            f"result renders pictures in some clients and not others has to say so on the tool itself."
        )


def _items(data: Any, block: ImagesBlock) -> list[Any]:
    """The items an ``images`` block addresses, in payload order.

    An omitted ``item_path`` means the root payload *is* the item, which is the
    shape a by-id read returns. A root that arrives as a **list** is taken as
    the items themselves rather than as nothing: several AutoDS reads answer
    with either a bare object or a one-element list for the same request (see
    the ``/users/list/`` gotcha), and returning ``[]`` for the list form would
    mean no pictures at all, silently, for a payload that plainly has them.

    Otherwise the path is resolved and each match that is itself a list is
    flattened one level — ``"results"`` resolves to the results list, not to its
    elements.
    """
    if not block.item_path:
        if isinstance(data, list):
            return [item for item in data if item is not None]
        return [data] if isinstance(data, dict) else []
    found: list[Any] = []
    for match in resolve_path(data, block.item_path):
        if isinstance(match, list):
            found.extend(match)
        elif match is not None:
            found.append(match)
    return found


def _first_str(item: Any, path: str) -> str | None:
    """The first non-empty string (or int, stringified) the path addresses."""
    if not path:
        return None
    for value in resolve_path(item, path):
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None


def _urls_for(item: Any, block: ImagesBlock) -> list[str]:
    """Up to ``per_item`` image URLs for one item, first matching path wins.

    Ordered paths rather than a union, because a product carries several
    plausible image fields and they are *not* equally good: the manifest lists
    the best one first and the fallbacks after it. Stopping at the first path
    that yields anything is what makes that ordering mean something.
    """
    for path in block.image_paths:
        urls: list[str] = []
        for value in resolve_path(item, path):
            if not isinstance(value, str):
                continue
            url = value.strip()
            if url.lower().startswith(("http://", "https://")) and url not in urls:
                urls.append(url)
            if len(urls) >= block.per_item:
                break
        if urls:
            return urls
    return []


def extract_images(operation: ManifestOperation, data: Any) -> dict[str, Any] | None:
    """The ``images`` envelope block for one successful upstream response.

    Returns ``None`` when the operation declares no ``images`` block, or when
    the payload holds no items at all — the envelope must stay exactly as it was
    in both cases.

    An item with **no** image is still listed. The block is a view of the result
    set the user is choosing from, so dropping the pictureless entries would
    silently renumber it; the widget renders those cells as a placeholder, which
    is the truth.
    """
    block = operation.images
    if block is None:
        return None

    items = _items(data, block)
    if not items:
        return None

    shown = items[: block.max]
    rendered: list[dict[str, Any]] = []
    for item in shown:
        images = [{"url": rewrite_thumbnail(url) or url} for url in _urls_for(item, block)]
        entry: dict[str, Any] = {"images": images}
        identifier = _first_str(item, block.id_path)
        if identifier is not None:
            entry["id"] = identifier
        label = _first_str(item, block.label_path)
        if label is not None:
            entry["label"] = label[:_MAX_LABEL_CHARS]
        rendered.append(entry)

    payload: dict[str, Any] = {
        "kind": PAYLOAD_KIND,
        "shown": len(rendered),
        "total": len(items),
        "items": rendered,
    }
    if block.widget is not None:
        payload["widget"] = block.widget
    note = truncation_note(len(rendered), len(items))
    if note is not None:
        payload["note"] = note
    return payload


def truncation_note(shown: int, total: int) -> str | None:
    """The honest-truncation sentence, or ``None`` when nothing was dropped.

    RD-82's decisive failure was a *silent* cap: the client kept 16 of 20 images,
    reported "19 of 20", and described a half-decoded JPEG as a different
    product. A cap the caller is told about is a limitation; a cap it isn't told
    about is a wrong answer, so this sentence is not decoration.
    """
    if shown >= total:
        return None
    return f"Thumbnails shown for the first {shown} of {total} results; the rest are in `data`."
