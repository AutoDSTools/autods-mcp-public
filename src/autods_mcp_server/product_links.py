"""Links from a rendered product back to its page in the AutoDS web app (RD-92).

A thumbnail grid the user picks from is a dead end without this. They choose a
product by eye and then have to find the same one again by title in the app, in
a different tab, from a search that may not even return it first. So every item
the ``images`` block publishes can carry a ``link`` beside its image URLs, and
the widget renders the picture as an anchor.

**The route table below is closed, and lives here rather than in manifest
text.** The paths belong to another repository's router (``v2-frontend``, via
``ui-platform-commons/routePaths``) and the host belongs to the environment, so
a manifest composing either would hold half a URL that nothing can check. A
manifest names a route; ``assert_link_usable`` refuses to boot on a name this
table does not serve. That is the only part of a link a lint can verify, and it
is the part that would otherwise ship as a confident 404.

**Which product type a link points at is mostly a property of the tool**, so it
is manifest data — but not always. ``list_products`` returns drafts or active
products according to ``product_status`` in the *request body*, and the response
carries nothing that tells the two apart, so the gate reads the arguments of the
call rather than the payload that came back. That is why ``extract_images``
takes the arguments at all.

**A missing link is the intended outcome, not a failure.** No id, no configured
host, a gate that does not match, a status with no page that lists it — each of
those yields no link, and the widget then renders exactly what it rendered
before this existed. The opposite (an anchor built from half a URL, or one that
opens a page not holding the product) looks like a working link and is worse
than none: the user clicks, lands somewhere plausible, and concludes the product
is gone.
"""

from collections.abc import Mapping
from typing import Any, NamedTuple

from autods_mcp_server.manifests.schema import LinkBlock


class _Route(NamedTuple):
    """One row of the table below.

    ``status`` is the product status the *path* already carries, for the two
    store routes that hold it as a literal (``/products/{id}&2``). It is not
    extra configuration: it is the number that is already written in ``path``,
    named so a lint can read it. A route whose path carries no status leaves it
    ``None``, and any gate may select that route.
    """

    path: str
    status: int | None = None


# route name -> the page it opens, ``{id}`` substituted with the item's id.
#
# Every row is copied from ``AutoDSTools/ui-platform-commons``'s
# ``src/routePaths.ts``, which is the app's single source for these, and two of
# them are not what they look like:
#
# * a store product's page takes **two** values in one segment joined by ``&`` —
#   ``SINGLE_PRODUCT_PATH = '/products/:productId&:productStatus'`` — so
#   ``/products/4242`` matches no route at all. It opens the app and lands
#   somewhere else, which is the failure this module exists to avoid and which
#   looks exactly like success from the outside.
# * the Marketplace catalog page is ``/marketplace/all-products/:id``, not
#   ``/marketplace/products/:id``.
#
# Trending products (``/marketplace/trending-products/:id``) are deliberately
# absent: they come from the ads-spy surface and no tool returns them, so a row
# here would be config nothing can reach.
_ROUTES: dict[str, _Route] = {
    # Store products, by status. The status is part of the path, not a query
    # parameter, and it must equal the status the gate selecting this route
    # matches on — ``assert_images_usable`` refuses a manifest that pairs them
    # differently, because ``store_draft`` behind a ``product_status: 2`` gate
    # would send every active product to a draft page and still open the app.
    "store_draft": _Route("/upload/{id}&1", status=1),
    "store_product": _Route("/products/{id}&2", status=2),
    # Research catalog. ``get_winning_products`` is the hand-picked surface and
    # only it — it calls ``/products/winning``, which is what the app's
    # hand-picked page calls; the other grids exclude winning products, so the
    # two sets never overlap.
    "hand_picked_product": _Route("/marketplace/hand-picked-products/{id}"),
    "marketplace_product": _Route("/marketplace/all-products/{id}"),
}


def route_names() -> list[str]:
    """Every route a manifest ``images.links[].route`` may name."""
    return list(_ROUTES)


def route_needs_id(route: str) -> bool:
    """Whether this route's path has an ``{id}`` to fill in.

    Not every product page is per-product — RD-92's draft row is the whole
    ``/upload`` list, because the app has no draft deep link — so "needs an id"
    is a property of the route, not of linking in general.
    """
    row = _ROUTES.get(route)
    return row is not None and "{id}" in row.path


def route_status(route: str) -> int | None:
    """The product status this route's path already carries, if any.

    Only the two store routes have one. It exists so the boot lint can check
    that a gate selects the page its own status belongs to; nothing on the
    request path reads it, because by then the manifest has already been
    checked. An unknown route answers ``None`` — the route-name lint is what
    rejects that, and reporting "no status" for a route that does not exist
    would be the wrong complaint.
    """
    row = _ROUTES.get(route)
    return row.status if row is not None else None


def gate_matches(block: LinkBlock, arguments: Mapping[str, Any] | None) -> bool:
    """Whether this call's arguments satisfy the block's gate.

    An ungated block always matches. A gated one reads the request body, which
    is where the only thing distinguishing one product type from another on
    ``list_products`` lives. Anything that is not the expected value — a missing
    body, a missing field, a different status, a string where an integer was
    documented — does not match, so the failure mode of an unexpected call shape
    is "no link", never a link to the wrong page.
    """
    if not block.when_body_field:
        return True
    body = (arguments or {}).get("body")
    if not isinstance(body, Mapping):
        return False
    return body.get(block.when_body_field) == block.when_body_equals


def build_link(
    block: LinkBlock,
    *,
    identifier: str | None,
    arguments: Mapping[str, Any] | None,
    base_url: str | None,
) -> str | None:
    """The web-app URL for one item, or ``None`` when there is nothing honest to build.

    Args:
        block: the operation's ``images.link`` configuration.
        identifier: the item's id, as ``images`` already resolved it through
            ``id_path``. A route that needs one and does not have one gets no
            link rather than a URL with a hole in it.
        arguments: the tool call's arguments, for the gate.
        base_url: the environment's web-app host, without a trailing slash.
    """
    if not base_url:
        return None
    row = _ROUTES.get(block.route)
    if row is None:
        return None
    if not gate_matches(block, arguments):
        return None
    path = row.path
    if "{id}" in path:
        if not identifier:
            return None
        path = path.replace("{id}", identifier)
    return f"{base_url.rstrip('/')}{path}"
