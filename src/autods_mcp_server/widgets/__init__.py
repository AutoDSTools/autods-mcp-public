"""MCP Apps widgets: the ``ui://autods/...`` resources and their metadata (RD-92).

An MCP App is an HTML document the host renders in a sandboxed iframe beside
the model's answer. It is served as a **resource** — one ``ui://`` URI per
widget, with mime type ``text/html;profile=mcp-app`` — and a tool opts into it
by carrying ``_meta.ui.resourceUri`` on its descriptor. The host then renders
that widget when the tool returns, and the images the widget shows are fetched
by the *browser*, from the URLs in ``structuredContent``. That is the whole
argument for this path: RD-82 measured it at **zero vision tokens** for any
number of products, against 1,620 for 20 base64 thumbnails at 252 px and 36,980
at 1200 px (which also truncates, silently).

**Assets are hand-written HTML with inline CSS and JS, one file per widget, and
there is no build step.** Two small documents do not justify a Node toolchain
inside a Python service, a second lockfile, and a bundle whose provenance a
reviewer cannot read. They are loaded once at import and served verbatim.

Registering ``on_list_resources`` is what declares the ``resources`` capability
in the handshake; RD-100 already did that for the ``autods://playbook/<name>``
mirror, so this module adds URIs to a **shared** handler rather than declaring
the capability again. The two kinds of resource are unrelated in every other
way, which is precisely the hazard: they now share one list handler and one
read handler, and each must carry its own explicit ``mime_type`` —
``text/markdown`` for a playbook, ``text/html;profile=mcp-app`` for a widget. A
bare string defaults to ``text/plain``, which loses the markdown for one and
stops the host treating the other as an MCP App at all.

**The CSP is declared twice on purpose.** ``_meta.ui.csp`` goes on the
``resources/list`` entry *and* on the ``resources/read`` response. The host
renders from the read; declared only on the list, ``hostCapabilities.sandbox``
comes back empty and the default policy (``img-src 'self' data: blob:
assets.claude.ai``) blocks every supplier image with no error anywhere. This
looks like duplication and is not — see the RD-92 gotchas in ``CLAUDE.md``.
"""

import pathlib
from dataclasses import dataclass
from typing import Any

from autods_mcp_server.image_rewrites import resource_domains

# URI scheme and media type of the widget resources. The ``profile=mcp-app``
# parameter is what marks the document as an MCP App rather than a plain HTML
# resource, and it must survive onto the read response.
WIDGET_RESOURCE_SCHEME = "ui://autods/"
WIDGET_MIME_TYPE = "text/html;profile=mcp-app"

_ASSET_DIR = pathlib.Path(__file__).parent


@dataclass(frozen=True)
class Widget:
    """One ``ui://autods/<name>`` MCP App resource."""

    name: str
    title: str
    description: str
    filename: str

    @property
    def uri(self) -> str:
        return f"{WIDGET_RESOURCE_SCHEME}{self.name}"

    @property
    def html(self) -> str:
        """The asset's bytes. Read per call so a local edit shows up on reload.

        Cheap (two files, a few KB each) and it keeps ``make run --reload``
        honest; a widget that only refreshes on a full restart is exactly the
        kind of friction that makes an editor stop testing changes.
        """
        return (_ASSET_DIR / self.filename).read_text(encoding="utf-8")


WIDGETS: tuple[Widget, ...] = (
    Widget(
        name="product-grid",
        title="Product grid",
        description="Thumbnail grid of the products a search or listing returned.",
        filename="product_grid.html",
    ),
    Widget(
        name="product-card",
        title="Product card",
        description="Single product with its image gallery.",
        filename="product_card.html",
    ),
)

_BY_NAME: dict[str, Widget] = {widget.name: widget for widget in WIDGETS}
_BY_URI: dict[str, Widget] = {widget.uri: widget for widget in WIDGETS}


def widget_names() -> list[str]:
    """Every registered widget name — what a manifest's ``images.widget`` may say."""
    return list(_BY_NAME)


def get_widget(name: str) -> Widget | None:
    return _BY_NAME.get(name)


def widget_for_uri(uri: str) -> Widget | None:
    return _BY_URI.get(uri)


def csp_meta() -> dict[str, Any]:
    """The ``_meta`` block declaring the widget sandbox's approved origins.

    ``resourceDomains`` covers ``<img>`` loads; ``connectDomains`` covers
    ``fetch``. Both are declared with the same list — a widget that can display
    an image from a host can already read its bytes through the ``<img>`` cache,
    so withholding ``connect`` buys nothing and would break the ``fetch``→blob
    fallback RD-82 verified.
    """
    domains = resource_domains()
    return {"ui": {"csp": {"resourceDomains": domains, "connectDomains": domains}}}


def tool_meta(widget_name: str) -> dict[str, Any]:
    """The ``_meta`` a tool descriptor carries to render ``widget_name``.

    Must be attached through a real MCP model field (``meta`` / ``_meta``): mcp
    2.x dropped ``extra="allow"``, so any *other* key stuffed into a ``Tool`` is
    accepted at construction and then discarded with no error and no data.
    """
    widget = _BY_NAME[widget_name]
    return {"ui": {"resourceUri": widget.uri, **csp_meta()["ui"]}}
