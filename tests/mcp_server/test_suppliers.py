"""RD-95 — the two supplier scan tools, and the ``fixed_query`` field they need.

Three kinds of check, each against a failure that would otherwise ship silently:

* **The ``business_errors`` paths hit the real wire.** The scrapers report their
  errors in snake_case (``error_code``); the frontend's camelCase view is what
  the older examples in this repo were written against, and a mis-cased path
  never matches — the boot lint only checks that ``paths`` is non-empty. So the
  shipped blocks run against answers recorded live from staging
  (``data/scrapers_payload_samples.json``), not against a payload written here.
* **The two endpoints' state reads are documented separately.** They differ, and
  documenting one shape for both makes "queued" unobservable for products.
* **``fixed_query`` cannot quietly give a constant back to the model**, which is
  what its boot lint is for.

Canonical source for the state machines: ``docs/polling-conventions.md``.
"""

import json
from pathlib import Path

import pytest

from autods_mcp_server.business_errors import detect_business_errors
from autods_mcp_server.images import extract_images
from autods_mcp_server.manifests import build_registry
from autods_mcp_server.manifests.schema import ManifestOperation
from autods_mcp_server.tools import FixedQueryError, build_tools
from tests.mcp_server.conftest import mcp_client_session

_SAMPLES = json.loads((Path(__file__).parent / "data" / "scrapers_payload_samples.json").read_text())

SCAN_OPS = ("search_1688_offers_by_image", "get_1688_product_details")

# Every ``ScraperErrorCode`` value (``autods_models.scraper_items.errors``). All
# of them end the poll loop, so an unmapped one would reach the model as a bare
# string with a generic hint, at exactly the moment it has to decide what next.
SCRAPER_ERROR_CODES = frozenset(
    {
        "UNEXPECTED",
        "NO_OFFERS",
        "PRODUCT_404",
        "PRODUCT_OOS",
        "SHIPPING_UNAVAILABLE",
        "VIRTUAL_PRODUCT",
        "CUSTOMIZABLE_PRODUCT",
        "BLACKLISTED_PRODUCT",
        "TOO_MANY_RETRIES",
        "INVALID_INPUT",
    }
)


@pytest.fixture
def registry(bundled_manifest_dir: Path):
    return build_registry(bundled_manifest_dir)


# --- business_errors against the recorded wire ---------------------------------


@pytest.mark.parametrize(
    ("operation_id", "sample", "expected"),
    [
        ("search_1688_offers_by_image", "failed", ["PRODUCT_404"]),
        ("get_1688_product_details", "failed", ["PRODUCT_404"]),
        ("get_1688_product_details", "invalid_id", ["PRODUCT_OOS"]),
    ],
)
def test_a_failed_scan_surfaces_its_code(registry, operation_id: str, sample: str, expected: list[str]) -> None:
    """The recorded failure of each endpoint produces a ``business_error``. If
    this fails after an edit, the path no longer matches what the upstream
    sends — which is the silent failure the boot lint cannot see."""
    found = detect_business_errors(registry.get(operation_id), _SAMPLES[operation_id][sample])

    assert found is not None
    assert [entry["code"] for entry in found] == expected


@pytest.mark.parametrize("operation_id", SCAN_OPS)
@pytest.mark.parametrize("sample", ["queued", "done"])
def test_a_healthy_scan_surfaces_nothing(registry, operation_id: str, sample: str) -> None:
    """``scraper_error: null`` and ``error: null`` are the healthy answers, and
    must leave the envelope exactly as it was."""
    assert detect_business_errors(registry.get(operation_id), _SAMPLES[operation_id][sample]) is None


@pytest.mark.parametrize("operation_id", SCAN_OPS)
def test_the_paths_use_the_wire_spelling(registry, operation_id: str) -> None:
    """``errorCode`` is how the web app's request helper renames the field, not
    what the upstream sends. Asserted by name as well as by sample, because the
    camelCase form was the spelling of this repo's own examples until RD-95, and
    is the one an author copying from the web app will reach for."""
    paths = registry.get(operation_id).business_errors.paths

    assert paths
    assert all(path.endswith(".error_code") for path in paths)


@pytest.mark.parametrize("operation_id", SCAN_OPS)
def test_every_scraper_error_code_is_mapped(registry, operation_id: str) -> None:
    assert set(registry.get(operation_id).business_errors.codes) == SCRAPER_ERROR_CODES


@pytest.mark.parametrize("operation_id", SCAN_OPS)
def test_the_notes_say_every_code_ends_the_loop(registry, operation_id: str) -> None:
    """``TOO_MANY_RETRIES`` reads like "try again later" and is not: the scan
    already retried itself. Every code is final for that input."""
    notes = registry.get(operation_id).notes.lower()

    assert "every error code is final" in notes
    assert "too_many_retries" in notes
    assert "stop polling" in notes


# --- the two state reads, documented separately ---------------------------------


def test_the_offer_search_documents_its_four_states(registry) -> None:
    notes = registry.get("search_1688_offers_by_image").notes

    assert "`not_in_db` non-empty: the search was just queued" in notes
    assert "`no_info` non-empty with `data` empty: the search is running" in notes
    assert "`data` non-empty: done" in notes
    assert "`scraper_error` set" in notes


def test_the_product_read_does_not_call_not_in_db_the_queued_signal(registry) -> None:
    """On ``/products/scan`` a queued id sits in ``no_info``; ``not_in_db`` is
    always empty (the recorded ``queued`` sample shows both). Documenting
    ``not_in_db`` as "queued" here would make queued unobservable."""
    notes = registry.get("get_1688_product_details").notes

    assert "is in `no_info` and missing from `data`: queued" in notes
    assert "`not_in_db` is always empty here" in notes
    queued = _SAMPLES["get_1688_product_details"]["queued"]
    assert queued["no_info"] and not queued["not_in_db"] and not queued["data"]


def test_the_product_read_says_the_ceiling_is_the_only_stop(registry) -> None:
    """``/products/scan`` has no ``scraper_error``, so a failed read that never
    produced an entry looks like a slow one."""
    notes = registry.get("get_1688_product_details").notes

    assert "no search-wide error field" in notes
    assert "the poll ceiling is the ONLY thing that stops the loop" in notes
    assert "scraper_error" not in json.dumps(_SAMPLES["get_1688_product_details"])


def test_the_offer_search_says_to_try_the_next_image(registry) -> None:
    """One image finding nothing is common and is not "no supplier"."""
    notes = registry.get("search_1688_offers_by_image").notes

    assert "NEXT image" in notes
    assert "every image has come back empty" in notes


def test_the_ids_the_sourcing_write_needs_are_named_in_both_shapes(registry) -> None:
    """The bare offer id and the underscore variation id go to two different
    fields of ``create_1688_sourcing_request``, and swapping them links nothing."""
    offers = registry.get("search_1688_offers_by_image").notes
    details = registry.get("get_1688_product_details").notes

    assert "`alibaba_1688_id`" in offers
    assert "`link.variations_match[].item_id_on_site`" in details
    sample_variation = _SAMPLES["get_1688_product_details"]["done"]["data"]["1048927055094"]["variations"][0]
    assert sample_variation["id"].startswith("1048927055094_")


@pytest.mark.parametrize("operation_id", SCAN_OPS)
def test_both_scans_are_read_only(registry, operation_id: str) -> None:
    annotations = registry.get(operation_id).annotations

    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False
    assert annotations.title


async def test_the_scan_contract_reaches_a_real_client(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The text is only the feature if it arrives. The three things an agent
    acts on — ``ok`` is not success, the cadence, and "stop on an error" — are
    checked on what ``tools/list`` actually carried."""
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}

    for operation_id in SCAN_OPS:
        description = tools[operation_id].description
        assert "transport-level" in description
        assert "every error code is final" in description
        assert set(tools[operation_id].input_schema["properties"]) >= {"full_scrape"}


# --- the pictures the user picks from -------------------------------------------


def test_the_offer_grid_shows_each_offer_as_a_small_thumbnail(registry) -> None:
    """The user picks an offer by looking at it, so the finished search draws a
    grid: one thumbnail per offer, in ``data`` order (which is the order the
    notes tell the agent to list them in), keyed by the offer id the next
    tool takes. 1688's CDN serves the ``_220x220.jpg`` variant — measured,
    ~17 KB against ~110 KB for the original."""
    block = extract_images(registry.get("search_1688_offers_by_image"), _SAMPLES["search_1688_offers_by_image"]["done"])

    assert block is not None and block["widget"] == "product-grid"
    (item,) = block["items"]
    assert item["id"] == "1048927055094"
    assert item["label"].startswith("Mobile phone stand")
    assert item["images"] == [
        {"url": "https://cbu01.alicdn.com/O1CN01YOBlGs24KtKgOQMCf_!!2219064657373-0-cib.jpg_220x220.jpg"}
    ]
    # An offer is not an AutoDS product: there is no web-app page to link to.
    assert "link" not in item


def test_the_variation_grid_is_captioned_with_the_attribute(registry) -> None:
    """The details read draws the offer's variations, captioned with what the
    user matches on (the colour, usually) and keyed by the id the sourcing
    request takes as ``item_id_on_site``."""
    block = extract_images(registry.get("get_1688_product_details"), _SAMPLES["get_1688_product_details"]["done"])

    assert block is not None and block["widget"] == "product-grid"
    (item,) = block["items"]
    assert item["id"] == "1048927055094_6245469138442"
    assert item["label"] == "Desktop Stand 758【Purple】"
    assert item["images"][0]["url"].endswith("-cib.jpg_220x220.jpg")


@pytest.mark.parametrize("operation_id", SCAN_OPS)
@pytest.mark.parametrize("sample", ["queued", "failed"])
def test_an_unfinished_or_failed_scan_draws_no_grid(registry, operation_id: str, sample: str) -> None:
    """Every poll before the answer, and every failure, carries no items. An
    empty grid would read as "no offers" while the scan is still running."""
    assert extract_images(registry.get(operation_id), _SAMPLES[operation_id][sample]) is None


# --- the fixed_query lint ------------------------------------------------------


def _op(**fields) -> ManifestOperation:
    return ManifestOperation.model_validate(
        {
            "operation_id": "scan",
            "method": "GET",
            "path": "/scan",
            "base_url_key": "scrapers_api",
            "annotations": {"title": "Scan", "readOnlyHint": True},
            **fields,
        }
    )


def test_fixed_query_passes_when_it_shares_no_key_with_a_parameter() -> None:
    build_tools([_op(fixed_query={"store": "offers_1688"}, parameters=[{"name": "img_url", "in": "query"}])])


def test_fixed_query_sharing_a_key_with_a_parameter_is_refused() -> None:
    """Whichever side won, the other would be silently ignored — and the
    constant was written because the model must not choose that value."""
    with pytest.raises(FixedQueryError, match="store"):
        build_tools([_op(fixed_query={"store": "offers_1688"}, parameters=[{"name": "store", "in": "query"}])])


def test_fixed_query_on_a_local_operation_is_refused() -> None:
    operation = ManifestOperation.model_validate(
        {
            "operation_id": "local",
            "handler": "playbook",
            "fixed_query": {"store": "offers_1688"},
            "annotations": {"title": "Local", "readOnlyHint": True},
        }
    )
    with pytest.raises(FixedQueryError, match="answered locally"):
        build_tools([operation])
