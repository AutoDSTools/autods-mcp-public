"""RD-92: the CDN thumbnail-rewrite table, pinned against RD-82's measurement.

``rewrite_coverage.py`` in ``claude_projects/cdn_image_sample`` is the reference
implementation. It cannot be a test as it stands: it fetches every rewritten URL
from the live CDN and decodes it with Pillow, so it needs the network, 77
third-party hosts, and ~20 s — and it would go red the day eBay retires a size
rung, which is a finding about eBay and not a regression in this repository.

So the measurement is frozen instead. ``data/cdn_rewrite_sample.tsv`` carries
real URLs from that 200-image sample, one small set per host family plus a few
long-tail merchant hosts, each with the rewrite the reference implementation
produced on the run whose output is in ``staging_results.txt``. These tests
assert the table-driven implementation reproduces it exactly, which is the part
that can actually regress here: a table row edited, a family's match order
changed, a regex that stops anchoring.

Re-running the live probe stays a manual step (and belongs in the release
checks, where a *CDN* change is news); regenerating the fixture from a newer
probe run is how a deliberate change to a rung gets recorded.
"""

import csv
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pytest

from autods_mcp_server.image_rewrites import (
    HOST_FAMILIES,
    TARGET_PX,
    family_of,
    is_allowed_origin,
    resource_domains,
    rewrite_thumbnail,
)

_FIXTURE = Path(__file__).parent / "data" / "cdn_rewrite_sample.tsv"


def _rows() -> list[dict[str, str]]:
    with _FIXTURE.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def _ids(rows: list[dict[str, str]]) -> list[str]:
    return [f"{row['family']}-{index}" for index, row in enumerate(rows)]


_ROWS = _rows()


@pytest.mark.parametrize("row", _ROWS, ids=_ids(_ROWS))
def test_rewrite_matches_the_rd82_reference(row: dict[str, str]) -> None:
    """Every sampled URL rewrites to exactly what the reference produced.

    An empty ``expected_rewrite`` is a real answer, not a missing one: the host
    offers no small variant, and the caller must keep the original URL. Getting
    ``None`` wrong in *that* direction is the dangerous one — a rewrite invented
    for a host that does not support it produces a 404 or, worse on the base64
    path, a full-size original.
    """
    expected = row["expected_rewrite"] or None
    assert rewrite_thumbnail(row["url"]) == expected


def test_every_table_family_has_a_fixture_row() -> None:
    """A family added to the table without a sample row is untested config.

    The table is the only thing standing between a supplier CDN and a broken
    image, and a row nobody sampled is a guess. This is the lint that makes
    adding a family cost a fixture row.
    """
    sampled = {row["family"] for row in _ROWS}
    declared = {candidate.name for candidate in HOST_FAMILIES}
    assert declared <= sampled, f"host families with no fixture row: {sorted(declared - sampled)}"


def test_every_fixture_row_lands_in_the_family_it_was_sampled_from() -> None:
    """Matching is order-sensitive, and the order is easy to break silently.

    ``alicdn`` must win over a bare hostname match, ``autods-s3-scraper`` is a
    prefix and ``autods-cdn`` a suffix. Reordering the table to put a broad
    substring family above a narrow one would reroute a host to the wrong
    rewrite — which produces a URL that 404s, not an exception.
    """
    for row in _ROWS:
        matched = family_of(row["url"])
        name = matched.name if matched else "(long tail)"
        assert name == row["family"], f"{row['url']} matched {name}, sampled as {row['family']}"


def test_a_family_declares_substitutions_or_query_params_but_not_both() -> None:
    """``rewrite_thumbnail`` tries substitutions first and falls through to the
    query rule, so a family declaring both would make the query rule
    unreachable for any URL a substitution matched — dead config that reads as
    a second chance."""
    for candidate in HOST_FAMILIES:
        assert not (candidate.subs and candidate.query), f"{candidate.name} declares both rule kinds"


def test_an_already_sized_alicdn_url_is_not_sized_twice() -> None:
    """The identity substitution guarding the append rule is load-bearing.

    Without it the suffix stacks (``..._220x220.jpg_220x220.jpg``) and the CDN
    404s — with no error anywhere on our side, just an empty grid cell.
    """
    sized = "https://ae01.alicdn.com/kf/Sabc123.jpg_220x220.jpg"
    assert rewrite_thumbnail(sized) == sized


def test_a_query_rewrite_keeps_every_other_parameter_exactly_as_it_was() -> None:
    """A rewrite asks for a smaller variant and must change nothing else.

    Some of the rest of the query authorises the object rather than sizing it,
    so losing a parameter turns a working picture into a broken one — and the
    obvious ``dict(parse_qsl(...))`` loses two kinds: a blank-valued key, and
    every repeat of a key but the last.
    """
    rewritten = rewrite_thumbnail("https://cdn.shopify.com/s/files/1/a.jpg?v=1699&ref=&tag=x&tag=y")
    query = parse_qsl(urlparse(rewritten).query, keep_blank_values=True)

    assert ("width", "256") in query  # the rewrite itself
    assert ("v", "1699") in query  # a signing/versioning parameter survives
    assert ("ref", "") in query  # blank value survives
    assert [value for name, value in query if name == "tag"] == ["x", "y"]  # repeats survive


def test_a_query_rewrite_replaces_its_own_parameter_rather_than_repeating_it() -> None:
    """An image that already carries the sizing parameter must come back with
    one copy of it, not two — a CDN handed ``width=1500&width=256`` is entitled
    to honour either."""
    rewritten = rewrite_thumbnail("https://cdn.shopify.com/s/files/1/a.jpg?width=1500")
    query = parse_qsl(urlparse(rewritten).query, keep_blank_values=True)
    assert [value for name, value in query if name == "width"] == ["256"]


def test_a_url_that_is_not_http_gets_no_rewrite() -> None:
    """Upstream payloads carry the odd empty string, relative path or ``data:``
    URI. None of them is rewritable and none of them may raise."""
    for value in ("", "not a url", "/relative/path.jpg", "data:image/gif;base64,R0lGOD"):
        assert rewrite_thumbnail(value) is None


def test_resource_domains_cover_every_family_and_are_deduplicated() -> None:
    """The CSP allowlist is derived from the same table, so a family added for
    its rewrite also gets its origin approved — the two failure modes (no
    thumbnail, no image at all) have one cause and one place to fix."""
    domains = resource_domains()
    assert len(domains) == len(set(domains))
    for candidate in HOST_FAMILIES:
        for domain in candidate.resource_domains:
            assert domain in domains
        # Every family must contribute at least one origin: a rewrite is
        # useless if the host's images are blocked by the sandbox policy.
        assert candidate.resource_domains, f"{candidate.name} declares no CSP origin"


def test_the_autods_scraper_bucket_is_declared() -> None:
    """RD-82 proved the CSP mechanism against Amazon and Shopify but never
    tested the scraper bucket, which is 51% of ``list_products`` rows. It is
    declared here; whether the *host* approves it is the one thing only a live
    run can answer, which is why it is also a release check."""
    assert "https://autods-scraper-images.s3-us-west-2.amazonaws.com" in resource_domains()


def test_the_target_size_is_the_one_the_table_was_measured_at() -> None:
    """RD-82 verified 256px against real bytes per host. Changing this constant
    silently invalidates every fixture row and every measured rung."""
    assert TARGET_PX == 256


# --------------------------------------------------------------------------
# is_allowed_origin: the same list, used as a gate rather than as a hint
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://images.autods.com/hero.png",  # exact host
        "https://cdn.shopify.com/s/files/1/x.jpg",  # exact host
        "https://p16-oec.ttcdn-us.com/tos/x.webp",  # subdomain under a wildcard
        "https://i.ebayimg.com/images/g/x/s-l1600.jpg",
        "https://autods-scraper-images.s3-us-west-2.amazonaws.com/x.png",
    ],
)
def test_a_declared_origin_may_be_fetched(url: str) -> None:
    assert is_allowed_origin(url)


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("https://merchant-self-host.example/hero.jpg", "the long tail, blocked by the CSP too"),
        ("http://169.254.169.254/latest/meta-data/", "link-local, and no scheme/host match"),
        ("https://alicdn.attacker.example/x.jpg", "family_of matches this by substring; the gate must not"),
        ("https://cdn.shopify.com.attacker.example/x.jpg", "suffix-looking, different host"),
        ("https://notcdn.shopify.com/x.jpg", "not a subdomain of a declared exact host"),
        ("http://images.autods.com/hero.png", "declared for https only, as CSP would have it"),
        ("https://ttcdn-us.com/x.webp", "the apex is not covered by *.ttcdn-us.com"),
        ("file:///etc/passwd", "not http(s) at all"),
        ("", "empty"),
        ("not a url", "unparseable"),
    ],
)
def test_an_undeclared_origin_may_not_be_fetched(url: str, why: str) -> None:
    """The substring cases are the point of this test.

    ``family_of`` recognises a host with ``"alicdn" in host``, which is fine for
    picking a rewrite rule — guessing wrong yields a URL that simply is not an
    image — and unsafe for deciding what an in-cluster process may connect to.
    Reusing it as the gate was the obvious shortcut and would have accepted
    ``alicdn.attacker.example``.
    """
    assert not is_allowed_origin(url), why


def test_every_declared_origin_passes_its_own_gate() -> None:
    """The gate and the CSP declaration cannot drift apart: whatever the widget
    is allowed to load, the server is allowed to fetch, and nothing else."""
    for domain in resource_domains():
        scheme, _, host = domain.partition("://")
        sample = f"{scheme}://{host[2:] if host.startswith('*.') else host}"
        if host.startswith("*."):
            sample = f"{scheme}://sub.{host[2:]}"
        assert is_allowed_origin(f"{sample}/some/image.jpg"), domain
