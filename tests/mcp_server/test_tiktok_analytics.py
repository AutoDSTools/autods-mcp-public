"""RD-69 — the four TikTok Analytics read tools.

The upstream endpoints are plain GETs, so the checks here are about the two
things that fail silently rather than loudly:

* **The two ids.** The details and overview endpoints take the row's 32-hex
  ``id`` as ``product_id``; the videos endpoint takes the 19-digit
  ``internal_id``. Upstream, the wrong one gives an opaque ``upstream_error``
  on the first two and an empty 200 on the third — no error at all. So both
  descriptions name the other id, each id carries a ``pattern`` that refuses
  the other one locally, and the dispatcher sends each under its own name.
* **Tier placement** (RD-90). Everything needed to form a call — the range
  syntax, the allowed values, the date form — is on the parameter, not in the
  expensive ``instructions`` channel, and the server index names the tools.
"""

import json
from pathlib import Path

import httpx
import pytest

from autods_mcp_server.manifests import build_registry, load_manifests
from autods_mcp_server.manifests.schema import ManifestOperation
from autods_mcp_server.tools import ParameterPatternError, build_tools
from tests.mcp_server.conftest import mcp_client_session

TIKTOK_OPS = (
    "get_tiktok_products",
    "get_tiktok_product_details",
    "get_tiktok_product_overview",
    "get_tiktok_product_videos",
)

# The combined "min-max" filters of GET /tiktok-analytics/products, as the
# ProductsResearch controller declares them.
RANGE_PARAMS = (
    "price",
    "product_score",
    "commission_rate",
    "sold_units_during_7d",
    "sold_units_during_30d",
    "sold_units_during_60d",
    "sold_units_during_90d",
    "sold_qty",
    "gmv_7d",
    "gmv_30d",
    "gmv_60d",
    "gmv_90d",
    "gmv",
    "sold_growth_7d",
    "sold_growth_30d",
    "sold_growth_60d",
    "sold_growth_90d",
)

# Shapes of the two ids, taken from staging rows.
_ROW_ID = "ee794ba31e68dafa50197d76463a9688"
_INTERNAL_ID = "1729893779001479400"


@pytest.fixture
def registry(bundled_manifest_dir: Path):
    return build_registry(bundled_manifest_dir)


def _param(registry, operation_id: str, name: str):
    return next(p for p in registry.get(operation_id).parameters if p.name == name)


# --- registration --------------------------------------------------------------


@pytest.mark.parametrize("operation_id", TIKTOK_OPS)
def test_every_tiktok_tool_is_a_read_on_products_research(registry, operation_id: str) -> None:
    op = registry.get(operation_id)

    assert op is not None, f"{operation_id} not registered"
    assert op.base_url_key == "products_research"
    assert op.method == "GET"
    assert op.path.startswith("/tiktok-analytics/")
    assert op.annotations.title
    assert op.annotations.read_only_hint is True
    assert op.annotations.destructive_hint is False


def test_the_products_table_declares_every_controller_filter(registry) -> None:
    """A filter the manifest leaves out is one the agent cannot use; one it
    invents is dropped upstream in silence. Both directions, against the
    controller's own parameter list."""
    names = {p.name for p in registry.get("get_tiktok_products").parameters}

    expected = {"limit", "offset", "sort_field", "sort_order", "category", "product_status", *RANGE_PARAMS}
    assert names == expected
    assert all(p.location == "query" for p in registry.get("get_tiktok_products").parameters)


# --- the two ids -----------------------------------------------------------------


@pytest.mark.parametrize("operation_id", ["get_tiktok_product_details", "get_tiktok_product_overview"])
def test_the_row_id_tools_warn_against_the_internal_id(registry, operation_id: str) -> None:
    description = _param(registry, operation_id, "product_id").description

    assert "32-character hex" in description
    assert "NOT the 19-digit `internal_id`" in description
    assert "internal_id" not in {p.name for p in registry.get(operation_id).parameters}


def test_the_videos_tool_warns_against_the_row_id(registry) -> None:
    """The wrong id here is the silent case — an empty list, no error — so the
    warning is on the parameter and the notes say how to recognise it."""
    op = registry.get("get_tiktok_product_videos")
    description = _param(registry, "get_tiktok_product_videos", "internal_id").description

    assert "19-digit" in description
    assert "NOT the 32-character hex `id`" in description
    assert "invalid_arguments" in description
    assert "product_id" not in {p.name for p in op.parameters}
    assert "related_videos_qty" in op.notes
    assert "empty list" not in description
    assert "{quantity: 0, videos: []}" in op.notes


def test_the_table_notes_say_which_id_feeds_which_tool(registry) -> None:
    notes = registry.get("get_tiktok_products").notes

    for tool in ("get_tiktok_product_details", "get_tiktok_product_overview", "get_tiktok_product_videos"):
        assert tool in notes
    # The TikTok Analytics import (RD-96) and its recovery lookup take the
    # internal_id too — the mistake upload_products' own notes warn about.
    assert "upload_products" in notes
    assert "find_product_by_buy_id" in notes


def test_the_import_recipe_states_the_real_row_id_length(bundled_manifest_dir: Path) -> None:
    """RD-96 wrote the row id as 24-character hex; every staging row is 32."""
    notes = build_registry(bundled_manifest_dir).get("upload_products").notes

    assert "32-character hex `id`" in notes
    assert "24-character hex `id` names the row" not in notes


async def test_each_id_is_sent_under_its_own_name(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    upstream_calls: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(200, json={})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        details = await session.call_tool("get_tiktok_product_details", {"product_id": _ROW_ID})
        overview = await session.call_tool(
            "get_tiktok_product_overview",
            {"product_id": _ROW_ID, "start_date": "2026-09-01", "end_date": "2026-09-08"},
        )
        videos = await session.call_tool(
            "get_tiktok_product_videos", {"internal_id": _INTERNAL_ID, "sort_field": "stats.play_count"}
        )

    assert not details.is_error and not overview.is_error and not videos.is_error
    by_path = {request.url.path: request for request in upstream_calls}
    assert dict(by_path["/tiktok-analytics/product"].url.params) == {"product_id": _ROW_ID}
    assert dict(by_path["/tiktok-analytics/overview"].url.params) == {
        "product_id": _ROW_ID,
        "start_date": "2026-09-01",
        "end_date": "2026-09-08",
    }
    assert dict(by_path["/tiktok-analytics/videos"].url.params) == {
        "internal_id": _INTERNAL_ID,
        "sort_field": "stats.play_count",
    }
    assert all(request.method == "GET" for request in upstream_calls)


@pytest.mark.parametrize(
    ("operation_id", "missing"),
    [
        ("get_tiktok_product_details", "product_id"),
        ("get_tiktok_product_overview", "product_id"),
        ("get_tiktok_product_videos", "internal_id"),
    ],
)
async def test_a_missing_id_is_refused_locally(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token, operation_id: str, missing: str
) -> None:
    upstream_calls: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(200, json={})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(operation_id, {})

    assert result.is_error is True
    assert result.content[0].text.startswith("invalid_arguments: ")
    assert missing in result.content[0].text
    assert upstream_calls == []


@pytest.mark.parametrize(
    ("operation_id", "arguments", "bad"),
    [
        # Each id where the other belongs — the mistake both descriptions warn about.
        ("get_tiktok_product_details", {"product_id": _INTERNAL_ID}, "product_id"),
        ("get_tiktok_product_overview", {"product_id": _INTERNAL_ID}, "product_id"),
        ("get_tiktok_product_videos", {"internal_id": _ROW_ID}, "internal_id"),
        # Upstream ids are lowercase md5 hex, and the lookup is case-sensitive.
        ("get_tiktok_product_details", {"product_id": _ROW_ID.upper()}, "product_id"),
        ("get_tiktok_product_details", {"product_id": _ROW_ID + "0"}, "product_id"),
    ],
)
async def test_an_id_of_the_wrong_format_is_refused_locally(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token, operation_id: str, arguments, bad: str
) -> None:
    """ProductsResearch answers the first two with a 500 and the videos read with
    an empty 200, so the ``pattern`` on each id is what turns both into an error
    that names the parameter, before anything is sent."""
    upstream_calls: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(200, json={})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(operation_id, arguments)

    assert result.is_error is True
    assert result.content[0].text.startswith("invalid_arguments: ")
    assert bad in result.content[0].text
    assert upstream_calls == []


@pytest.mark.parametrize(
    ("operation_id", "name", "pattern"),
    [
        ("get_tiktok_product_details", "product_id", "^[0-9a-f]{32}$"),
        ("get_tiktok_product_overview", "product_id", "^[0-9a-f]{32}$"),
        ("get_tiktok_product_videos", "internal_id", "^[0-9]+$"),
    ],
)
def test_each_id_pattern_reaches_the_input_schema(registry, operation_id: str, name: str, pattern: str) -> None:
    """The pattern is only a guard if the client-facing schema carries it: that
    same schema is what the argument validator is compiled from. The internal id
    is not pinned to 19 digits, which is what TikTok ids look like, not a rule."""
    (tool,) = build_tools([registry.get(operation_id)])

    assert tool.input_schema["properties"][name]["pattern"] == pattern


@pytest.mark.parametrize(
    ("parameter", "message"),
    [
        # A JSON Schema pattern is a search, so this one accepts any value that
        # merely contains 32 hex characters.
        ({"name": "product_id", "in": "query", "pattern": "[0-9a-f]{32}"}, "unanchored"),
        ({"name": "product_id", "in": "query", "pattern": "^[0-9a-f{32}$"}, "does not compile"),
        ({"name": "limit", "in": "query", "schema_type": "int", "pattern": "^[0-9]+$"}, "on a 'int' parameter"),
    ],
)
def test_a_parameter_pattern_that_would_check_nothing_is_refused(parameter, message: str) -> None:
    operation = ManifestOperation.model_validate(
        {
            "operation_id": "probe",
            "method": "GET",
            "path": "/probe",
            "base_url_key": "products_research",
            "annotations": {"title": "Probe", "readOnlyHint": True},
            "parameters": [parameter],
        }
    )
    with pytest.raises(ParameterPatternError, match=message):
        build_tools([operation])


async def test_the_table_sends_only_the_filters_given(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """Every filter is optional and has an upstream default; an unset one must
    not reach the upstream as an empty value, which its regex would refuse."""
    upstream_calls: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(200, json={"quantity": 0, "products": []})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(
            "get_tiktok_products",
            {"limit": 5, "sort_field": "gmv_7d", "commission_rate": "10-", "sold_qty": "1000-5000"},
        )

    assert result.is_error is False
    assert (result.structured_content or {})["data"] == {"quantity": 0, "products": []}
    assert len(upstream_calls) == 1
    assert upstream_calls[0].url.path == "/tiktok-analytics/products"
    assert dict(upstream_calls[0].url.params) == {
        "limit": "5",
        "sort_field": "gmv_7d",
        "commission_rate": "10-",
        "sold_qty": "1000-5000",
    }


# --- tier placement (RD-90) ------------------------------------------------------


@pytest.mark.parametrize("name", RANGE_PARAMS)
def test_every_range_filter_carries_its_syntax(registry, name: str) -> None:
    param = _param(registry, "get_tiktok_products", name)

    assert param.schema_type == "str"
    assert '"min-max"' in param.description


def test_the_price_filter_spells_out_the_whole_range_syntax(registry) -> None:
    """The one place the full syntax is written; the others point at it."""
    description = _param(registry, "get_tiktok_products", "price").description

    for example in ('"10-20"', '"-50"', '"60-"'):
        assert example in description
    assert "upstream_client_error" in description


def test_commission_and_growth_are_percentages(registry) -> None:
    """The upstream's own parameter text gives "0.05-0.2" for commission_rate;
    staging stores 5, 10, 30. A fraction there matches almost everything."""
    commission = _param(registry, "get_tiktok_products", "commission_rate").description
    growth = _param(registry, "get_tiktok_products", "sold_growth_7d").description

    assert "percent" in commission and "not a fraction" in commission
    assert "percent" in growth


def test_the_raw_score_filter_says_its_scale_is_not_the_normalised_one(registry) -> None:
    """Staging: ``product_score=50-`` matches nothing, ``0.5-`` a handful."""
    description = _param(registry, "get_tiktok_products", "product_score").description

    assert "product_score_norm" in description
    assert "matches nothing" in description


@pytest.mark.parametrize(
    ("operation_id", "name", "values"),
    [
        (
            "get_tiktok_products",
            "sort_field",
            (
                "price",
                "product_score_norm",
                "commission_rate",
                "sold_units_during_7d",
                "sold_units_during_90d",
                "sold_qty",
                "gmv_7d",
                "gmv",
                "sold_growth_7d",
                "sold_growth_90d",
                "viral_score",
                "top_sales",
                "top_sales_in_category",
            ),
        ),
        ("get_tiktok_products", "sort_order", ("asc", "desc")),
        ("get_tiktok_products", "product_status", ("available", "discontinued", "all")),
        ("get_tiktok_product_videos", "sort_order", ("asc", "desc")),
        (
            "get_tiktok_product_videos",
            "sort_field",
            ("created_at", "stats.play_count", "stats.digg_count", "stats.comment_count", "interaction_rate"),
        ),
    ],
)
def test_allowed_values_are_listed_on_the_parameter(registry, operation_id: str, name: str, values) -> None:
    description = _param(registry, operation_id, name).description

    for value in values:
        assert value in description, f"{operation_id}.{name} does not list {value!r}"


def test_allowed_values_are_prose_not_a_json_enum(registry) -> None:
    """The ``products_research.json`` convention: an enum narrower than the API
    silently rejects valid calls, so values are described, not enforced."""
    for tool in build_tools([registry.get(op) for op in TIKTOK_OPS]):
        for name, prop in tool.input_schema["properties"].items():
            assert "enum" not in prop, f"{tool.name}.{name} carries a JSON enum"


@pytest.mark.parametrize(
    ("operation_id", "name"),
    [
        ("get_tiktok_product_overview", "start_date"),
        ("get_tiktok_product_overview", "end_date"),
        ("get_tiktok_product_videos", "start_date"),
        ("get_tiktok_product_videos", "end_date"),
    ],
)
def test_every_date_names_its_format(registry, operation_id: str, name: str) -> None:
    assert "YYYY-MM-DD" in _param(registry, operation_id, name).description


def test_the_overview_end_date_is_documented_as_exclusive(registry) -> None:
    """Upstream says "End date, included" and then builds the series up to the
    day before it. The videos end date really is included, so the two differ."""
    overview_end = _param(registry, "get_tiktok_product_overview", "end_date").description
    videos_end = _param(registry, "get_tiktok_product_videos", "end_date").description

    assert "day BEFORE" in overview_end
    assert "included" in videos_end


def test_the_overview_counts_are_documented_as_including_the_end_date(registry) -> None:
    """The overview's series stops the day before ``end_date``, but its
    ``related_videos_qty`` / ``affiliates_qty`` filter with ``end_date + 1 day``,
    so they cover one day more. Both the parameter and the notes must say so, or
    a user comparing them with the daily figures gets numbers that do not add up."""
    op = registry.get("get_tiktok_product_overview")
    overview_end = _param(registry, "get_tiktok_product_overview", "end_date").description

    assert "counts DO include this day" in overview_end
    assert "they include `end_date` itself" in op.notes
    assert "one day more than the series" in op.notes
    assert "count only videos published in the range" not in op.notes


def test_the_manifest_puts_nothing_in_instructions(bundled_manifest_dir: Path) -> None:
    (manifest,) = [m for m in load_manifests(bundled_manifest_dir) if m.domain == "tiktok_analytics"]

    assert manifest.instructions == ""


def test_the_server_index_names_every_tiktok_tool(bundled_manifest_dir: Path) -> None:
    """A tool missing from ``_server.json`` is missing from the only channel
    that lists the whole server."""
    index = json.loads((bundled_manifest_dir / "_server.json").read_text())["instructions"]

    for operation_id in TIKTOK_OPS:
        assert f"`{operation_id}`" in index
    # Their enum values are strings, so the index's integer-enum rule has to
    # name them as an exception.
    assert "TikTok Analytics tools are the exception" in index


@pytest.mark.parametrize(
    ("operation_id", "widget"),
    [("get_tiktok_products", "product-grid"), ("get_tiktok_product_details", "product-card")],
)
def test_the_two_product_reads_draw_a_widget_without_base64(registry, operation_id: str, widget: str) -> None:
    """The user picks from the grid, which costs no vision tokens; ``base64`` is
    off so the set of tools offering ``include_images`` stays the RD-92 one."""
    block = registry.get(operation_id).images

    assert block.widget == widget
    assert block.base64 == "off"
    assert [(link.route, link.internal_id_path) for link in block.links] == [
        ("tiktok_analytics_product", "internal_id")
    ]


def test_the_videos_read_draws_no_grid(registry) -> None:
    """Video covers carry an ``x-expires`` signature that had already passed on
    the staging rows RD-69 read, so a grid of them would be empty cells."""
    assert registry.get("get_tiktok_product_videos").images is None


def test_the_text_is_implementation_agnostic(registry) -> None:
    blob = " ".join(registry.get(op).model_dump_json() for op in TIKTOK_OPS).lower()

    for forbidden in ("elastic", "mongo", "celery", "redis", "painless", "index"):
        assert forbidden not in blob, f"TikTok Analytics text names an internal detail: {forbidden}"
