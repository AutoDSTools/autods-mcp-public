"""CDN thumbnail-URL rewrite table (RD-92), measured in RD-82.

A product image URL from an upstream payload points at whatever the supplier's
CDN serves — usually the full-size original. Most of the big image hosts also
serve a smaller variant of the same image at a *derived* URL, so asking for a
thumbnail is a string transformation, not an extra request: rewrite the URL and
the CDN hands back a ~256 px version of the same picture.

That matters in two different ways, and only one of them is about tokens:

* In a **widget** the bytes cost bandwidth, not vision tokens, so a rewrite is
  an optimisation — a 5 KB thumbnail in a 120 px cell instead of a 900 KB
  original. Nothing breaks without it (RD-82 measured 51% of ``list_products``
  images sitting in a bucket with no small variant at all, and
  ``v2-frontend`` renders those same raw URLs in a production product grid).
* On the **base64** path it decides how many bytes we have to pull and decode
  before downscaling. See ``image_fetch``; the hard 252 px guarantee is made
  there, by resampling, never by trusting a rewrite to have worked.

**This table is data, not code.** Adding a host means adding one row below: the
hostname test that collapses its shards into one family, the CSP origin(s) the
widget needs for it, and either an ordered list of regex substitutions (first
match wins) or a set of query parameters to set. There are deliberately only
those two rule kinds — every host RD-82 measured fits one of them, and a third
kind would be a per-host code branch wearing a table's clothes.

It lives in Python rather than in a JSON file next to the manifests for one
practical reason: ``load_manifests`` globs ``manifests/*.json``
non-recursively, so a config file dropped there would be parsed as a manifest
and rejected on the missing ``server_name``. It is not per-operation data
anyway — a CDN's URL grammar has nothing to do with which tool returned the URL.

The rewrites are the ones RD-82 verified against real bytes across a
200-image / 77-host sample (``claude_projects/cdn_image_sample``); the fixture
in ``tests/mcp_server/data/cdn_rewrite_sample.tsv`` pins this module against
that measurement, host family by host family.
"""

import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# The thumbnail edge length the rewrites ask each CDN for. 256 rather than 252
# because every host's ladder is expressed in round numbers; the base64 path
# downscales to its own 252 px cap afterwards, so this only has to be *small*.
TARGET_PX = 256

# How a family's ``hosts`` entry is matched against the URL's hostname.
HostMatchKind = Literal["substring", "prefix", "suffix"]


@dataclass(frozen=True)
class Sub:
    """One regex substitution attempt against the whole URL.

    Applied with ``count=1``. A family's substitutions are tried in order and
    the first one that matches wins, which is how "already a thumbnail" is
    expressed: an identity substitution that matches the sized form and returns
    it unchanged, placed ahead of the rule that would size it again.
    """

    pattern: re.Pattern[str]
    replacement: str


@dataclass(frozen=True)
class HostFamily:
    """One CDN family: how to recognise it, what CSP needs, how to resize."""

    name: str
    # Ordered hostname tests. Evaluated in table order across families too, so
    # a narrower family must sit above a broader one.
    hosts: tuple[tuple[HostMatchKind, str], ...]
    # Origins the widget's CSP must allow for this family's images to load.
    # Wildcard subdomains are CSP source expressions and are what the sharded
    # hosts (TikTok's p16/p19, Alibaba's ae01/ae02, eBay's i/i1) need.
    resource_domains: tuple[str, ...] = ()
    # Exactly one of the two rule kinds per family (asserted by a test): an
    # ordered substitution list, or query parameters to set.
    subs: tuple[Sub, ...] = ()
    query: tuple[tuple[str, str], ...] = field(default=())


def _sub(pattern: str, replacement: str, *, flags: int = 0) -> Sub:
    return Sub(pattern=re.compile(pattern, flags), replacement=replacement)


# The table. Order is significant: matched top to bottom, first hit wins.
HOST_FAMILIES: tuple[HostFamily, ...] = (
    # TikTok: .../xxx~tplv-<tpl>-crop-webp:1500:1500.webp?<signed params>. The
    # signed query (t/ps/shp/shcp) is left untouched — it authorises the object,
    # not the size, so the rewritten URL stays valid for the same lifetime.
    HostFamily(
        name="ttcdn",
        hosts=(("substring", "ttcdn"), ("substring", "tiktokcdn")),
        resource_domains=(
            "https://*.ttcdn-us.com",
            "https://*.ttcdn.com",
            "https://*.tiktokcdn.com",
            "https://*.tiktokcdn-us.com",
        ),
        subs=(_sub(r":\d+:\d+(\.\w+)", rf":{TARGET_PX}:{TARGET_PX}\1"),),
    ),
    # Alibaba/AliExpress: a fixed ladder appended to the filename. 220 is the
    # rung nearest our target, so this family yields 220 px, not 256.
    HostFamily(
        name="alicdn",
        hosts=(("substring", "alicdn"), ("substring", "aliexpress-media")),
        resource_domains=(
            "https://*.alicdn.com",
            "https://*.aliexpress-media.com",
        ),
        subs=(
            # Already sized — keep it, don't stack a second suffix.
            _sub(r"(_\d+x\d+\.(?:jpg|png|webp))$", r"\1"),
            _sub(r"(\.(?:jpg|jpeg|png|webp))$", r"\1_220x220.jpg", flags=re.IGNORECASE),
        ),
    ),
    # Amazon: .../I/<id>._AC_SL1500_.jpg (replace the modifier block) or
    # .../I/<id>.jpg (insert one).
    HostFamily(
        name="media-amazon",
        hosts=(("substring", "media-amazon"),),
        resource_domains=(
            "https://m.media-amazon.com",
            "https://images-na.ssl-images-amazon.com",
        ),
        subs=(
            _sub(r"\._[A-Za-z0-9_,]+_\.(jpg|jpeg|png)$", rf"._SS{TARGET_PX}_.\1", flags=re.IGNORECASE),
            _sub(r"\.(jpg|jpeg|png)$", rf"._SS{TARGET_PX}_.\1", flags=re.IGNORECASE),
        ),
    ),
    HostFamily(
        name="cdn.shopify",
        hosts=(("substring", "cdn.shopify"),),
        resource_domains=("https://cdn.shopify.com",),
        query=(("width", str(TARGET_PX)),),
    ),
    # eBay has two grammars. The legacy one tops out at a 400 px rung, so this
    # family is "partial" coverage in RD-82's table rather than full.
    HostFamily(
        name="ebayimg",
        hosts=(("substring", "ebayimg"),),
        resource_domains=("https://*.ebayimg.com",),
        subs=(
            _sub(r"/\$_\d+\.(\w+)$", r"/$_1.\1"),
            _sub(r"/s-l\d+\.(\w+)$", r"/s-l225.\1"),
        ),
    ),
    # Wix: the transformation is a path suffix that repeats the filename, so the
    # substitution captures the whole URL and the filename separately.
    HostFamily(
        name="wixstatic",
        hosts=(("substring", "wixstatic"),),
        resource_domains=("https://static.wixstatic.com",),
        subs=(
            _sub(
                r"^(.*/media/([^/]+?\.(?:jpg|jpeg|png|webp)))$",
                rf"\1/v1/fill/w_{TARGET_PX},h_{TARGET_PX},al_c,q_80/\2",
                flags=re.IGNORECASE,
            ),
        ),
    ),
    # Walmart blocks parameterised requests from some networks (see the
    # WAF-hostile-hosts risk in RD-92): the rewrite either works or the image
    # fails to load and the cell falls back to the placeholder. It must never
    # fall back to the *full-size* URL, which is why nothing here retries.
    HostFamily(
        name="walmartimages",
        hosts=(("substring", "walmartimages"),),
        resource_domains=("https://*.walmartimages.com",),
        query=(("odnHeight", str(TARGET_PX)), ("odnWidth", str(TARGET_PX)), ("odnBg", "FFFFFF")),
    ),
    HostFamily(
        name="etsystatic",
        hosts=(("substring", "etsystatic"),),
        resource_domains=("https://*.etsystatic.com",),
        subs=(_sub(r"il_(fullxfull|\d+xN?\d*)\.", "il_170x135."),),
    ),
    # The two AutoDS-owned hosts. No rewrite exists — RD-82 measured 51% of
    # ``list_products`` images here, and putting a thumbnail source in front of
    # the bucket is explicitly a different repository's ticket. They are in the
    # table anyway, because the CSP origins are exactly as load-bearing as a
    # rewrite: without them the widget's cells render as broken images.
    HostFamily(
        name="autods-s3-scraper",
        hosts=(("prefix", "autods-scraper-images"),),
        resource_domains=("https://autods-scraper-images.s3-us-west-2.amazonaws.com",),
    ),
    HostFamily(
        name="autods-cdn",
        hosts=(("suffix", "images.autods.com"),),
        resource_domains=("https://images.autods.com", "https://dropship-media.s3.amazonaws.com"),
    ),
)


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        # An unparseable URL is upstream data, not a bug on our side — it just
        # belongs to no family and gets no rewrite.
        return ""


def family_of(url: str) -> HostFamily | None:
    """The :class:`HostFamily` serving ``url``, or ``None`` for the long tail.

    The long tail is large and irreducible: RD-82's store-quote sample had 59
    host families across 70 images, most of them merchant self-hosts. A miss
    here is the normal case, not an error.
    """
    host = _host_of(url)
    if not host:
        return None
    for candidate in HOST_FAMILIES:
        for kind, value in candidate.hosts:
            if (
                (kind == "substring" and value in host)
                or (kind == "prefix" and host.startswith(value))
                or (kind == "suffix" and host.endswith(value))
            ):
                return candidate
    return None


def _with_query(url: str, params: tuple[tuple[str, str], ...]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    query.update(params)
    return urlunparse(parsed._replace(query=urlencode(query)))


def rewrite_thumbnail(url: str) -> str | None:
    """A thumbnail URL for ``url``, or ``None`` when its host offers no rewrite.

    ``None`` is a first-class answer and callers must keep the original URL in
    that case — a host with no small variant still has a perfectly good image.
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    matched = family_of(url)
    if matched is None:
        return None
    for rule in matched.subs:
        rewritten, count = rule.pattern.subn(rule.replacement, url, count=1)
        if count:
            return rewritten
    if matched.query:
        return _with_query(url, matched.query)
    return None


def resource_domains() -> list[str]:
    """Every CSP origin the widgets need, deduplicated, in table order.

    This is what ``_meta.ui.csp.resourceDomains`` declares. It is an allowlist,
    so an image from the long tail of merchant self-hosts is *blocked by the
    host's CSP* and the cell falls back to its placeholder — the URL is still in
    ``structuredContent`` either way. Widening this to a bare wildcard was
    considered and rejected: it would hand the widget's frame permission to
    fetch from anywhere, for a few percent of rows that already degrade
    gracefully.
    """
    seen: dict[str, None] = {}
    for candidate in HOST_FAMILIES:
        for domain in candidate.resource_domains:
            seen.setdefault(domain, None)
    return list(seen)
