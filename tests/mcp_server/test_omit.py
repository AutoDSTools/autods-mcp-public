"""RD-146: the ``omit`` manifest block — removal, the boot lint, and the shipped blocks.

Three halves. ``apply_omit`` removes named keys from a payload whose shape it
does not know, through the shared ``payload_paths`` resolver, and must leave an
answer it has nothing to remove exactly as it was. ``assert_omit_usable``
refuses to boot on a block that would silently match nothing or that the tool's
notes never mention. And every shipped block is run against a recorded answer of
its own upstream, because a well-formed path that matches nothing passes every
lint and removes nothing.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from autods_mcp_server import mcp_transport
from autods_mcp_server.images import IMAGES_KEY
from autods_mcp_server.manifests.loader import build_registry
from autods_mcp_server.manifests.schema import ManifestOperation
from autods_mcp_server.omit import (
    OMITTED_KEY,
    OmitError,
    apply_omit,
    assert_omit_usable,
    unmatched_credential_paths,
)
from autods_mcp_server.payload_paths import resolve_path
from tests.mcp_server.conftest import mcp_client_session

_NOTES = "Read-only. Removed fields are listed in `omitted` beside `data`."
_SERVED = {"probe_op", "get_product_by_id"}


def _operation(*entries: dict[str, Any], notes: str = _NOTES, handler: str | None = None) -> ManifestOperation:
    """A minimal read operation carrying an ``omit`` block."""
    return ManifestOperation(
        operation_id="probe_op",
        method="" if handler else "GET",
        path="" if handler else "/probe",
        notes=notes,
        base_url_key=None if handler else "products_research",
        handler=handler,
        annotations={"title": "Probe", "readOnlyHint": True},
        omit=list(entries),
    )


def _entry(path: str, reason: str = "internal_detail", **extra: str) -> dict[str, Any]:
    return {"path": path, "reason": reason, "detail": "Measured on staging.", **extra}


# --------------------------------------------------------------------------
# Removal
# --------------------------------------------------------------------------


def test_an_operation_without_a_block_gets_the_very_same_object_back() -> None:
    data = {"results": [{"description": "x"}]}
    trimmed, omitted = apply_omit(_operation(), data)
    assert trimmed is data
    assert omitted is None


def test_a_wildcard_path_removes_the_key_from_every_item() -> None:
    data = {"results": [{"id": 1, "description": "a"}, {"id": 2, "description": "b"}, {"id": 3}]}
    trimmed, omitted = apply_omit(_operation(_entry("results.*.description")), data)
    assert trimmed == {"results": [{"id": 1}, {"id": 2}, {"id": 3}]}
    assert omitted == [{"path": "results.*.description", "reason": "internal_detail"}]


def test_a_single_segment_path_removes_a_top_level_key() -> None:
    trimmed, omitted = apply_omit(_operation(_entry("shipping_options")), {"id": 1, "shipping_options": []})
    assert trimmed == {"id": 1}
    assert omitted == [{"path": "shipping_options", "reason": "internal_detail"}]


def test_a_root_that_is_a_list_is_addressed_with_a_leading_wildcard() -> None:
    """``get_current_user`` and ``list_stores_api`` both answer a bare list."""
    data = [{"id": 1, "store": {"id": 5, "token": "t"}}, {"id": 2, "store": {"id": 6, "token": None}}]
    trimmed, _ = apply_omit(_operation(_entry("*.store.token", "credential")), data)
    assert trimmed == [{"id": 1, "store": {"id": 5}}, {"id": 2, "store": {"id": 6}}]


def test_a_key_holding_null_is_still_removed() -> None:
    """A shape where the field is present but empty still carries the field;
    leaving ``null`` behind would tell the model the field exists."""
    trimmed, omitted = apply_omit(_operation(_entry("a")), {"a": None, "b": 1})
    assert trimmed == {"b": 1}
    assert omitted is not None


def test_only_the_paths_that_matched_are_reported_in_declaration_order() -> None:
    operation = _operation(_entry("results.*.missing"), _entry("results.*.b"), _entry("results.*.a"))
    _, omitted = apply_omit(operation, {"results": [{"a": 1, "b": 2}]})
    assert [entry["path"] for entry in omitted or []] == ["results.*.b", "results.*.a"]


def test_an_answer_with_nothing_to_remove_is_returned_untouched() -> None:
    """The byte-identical guarantee holds per answer, not only per operation:
    an empty result page must not grow an ``omitted`` field."""
    data: dict[str, Any] = {"results": []}
    trimmed, omitted = apply_omit(_operation(_entry("results.*.description")), data)
    assert trimmed is data
    assert omitted is None


def test_the_tool_to_call_instead_travels_with_the_entry() -> None:
    operation = _operation(_entry("results.*.variations", "available_from_tool", see="get_product_by_id"))
    _, omitted = apply_omit(operation, {"results": [{"variations": []}]})
    assert omitted == [{"path": "results.*.variations", "reason": "available_from_tool", "see": "get_product_by_id"}]


def test_the_input_payload_is_never_modified() -> None:
    data = {"results": [{"description": "a", "nested": {"x": 1}}]}
    snapshot = json.loads(json.dumps(data))
    apply_omit(_operation(_entry("results.*.description")), data)
    assert data == snapshot


@pytest.mark.parametrize("data", [None, "text", 7, [], {"results": "not a list"}, {"results": [1, "x", None]}])
def test_a_shape_that_does_not_fit_removes_nothing_and_raises_nothing(data: object) -> None:
    trimmed, omitted = apply_omit(_operation(_entry("results.*.description")), data)
    assert trimmed is data
    assert omitted is None


def test_a_credential_path_that_matched_nothing_is_reported() -> None:
    operation = _operation(_entry("*.token", "credential"), _entry("*.size"))
    data = {"results": [{"token": "t", "size": 1}]}
    _, omitted = apply_omit(operation, data)
    # Only the credential is reported; a size path matching nothing is harmless.
    assert unmatched_credential_paths(operation, data, omitted) == ["*.token"]


def test_a_credential_path_that_matched_is_not_reported() -> None:
    operation = _operation(_entry("*.token", "credential"))
    data = [{"token": "t"}]
    _, omitted = apply_omit(operation, data)
    assert unmatched_credential_paths(operation, data, omitted) == []


@pytest.mark.parametrize("data", [None, [], {}])
def test_an_empty_answer_reports_no_credential_path(data: object) -> None:
    operation = _operation(_entry("*.token", "credential"))
    assert unmatched_credential_paths(operation, data, None) == []


# --------------------------------------------------------------------------
# Boot lint
# --------------------------------------------------------------------------


def test_a_valid_block_passes() -> None:
    assert_omit_usable(
        _operation(
            _entry("results.*.description", "available_from_tool", see="get_product_by_id"),
            _entry("*.token", "credential"),
        ),
        _SERVED,
    )


def test_an_operation_with_no_block_passes() -> None:
    assert_omit_usable(_operation(), _SERVED)


@pytest.mark.parametrize("path", ["", "results..description", ".results", "results."])
def test_an_empty_segment_is_rejected(path: str) -> None:
    with pytest.raises(OmitError, match="empty segment"):
        assert_omit_usable(_operation(_entry(path)), _SERVED)


def test_bracket_notation_is_rejected() -> None:
    with pytest.raises(OmitError, match="bracket notation"):
        assert_omit_usable(_operation(_entry("results[].description")), _SERVED)


def test_a_path_ending_in_a_wildcard_is_rejected() -> None:
    """``results.*`` would mean "remove every element", which is not a field."""
    with pytest.raises(OmitError, match="ending in"):
        assert_omit_usable(_operation(_entry("results.*")), _SERVED)


def test_a_path_declared_twice_is_rejected() -> None:
    with pytest.raises(OmitError, match="twice"):
        assert_omit_usable(_operation(_entry("a"), _entry("a")), _SERVED)


def test_a_path_with_no_detail_is_rejected() -> None:
    with pytest.raises(OmitError, match="no 'detail'"):
        assert_omit_usable(_operation({"path": "a", "reason": "duplicate", "detail": "  "}), _SERVED)


def test_available_from_tool_without_a_tool_is_rejected() -> None:
    with pytest.raises(OmitError, match="names none"):
        assert_omit_usable(_operation(_entry("a", "available_from_tool")), _SERVED)


@pytest.mark.parametrize("see", ["get_everything", "probe_op"])
def test_a_see_that_names_no_other_served_tool_is_rejected(see: str) -> None:
    with pytest.raises(OmitError, match="not another tool"):
        assert_omit_usable(_operation(_entry("a", "available_from_tool", see=see)), _SERVED)


def test_a_see_on_another_reason_is_rejected() -> None:
    with pytest.raises(OmitError, match="'see' goes with"):
        assert_omit_usable(_operation(_entry("a", "credential", see="get_product_by_id")), _SERVED)


def test_notes_that_never_mention_omitted_are_rejected() -> None:
    with pytest.raises(OmitError, match="never mention"):
        assert_omit_usable(_operation(_entry("a"), notes="Read-only. Returns products."), _SERVED)


def test_a_locally_handled_operation_cannot_declare_omit() -> None:
    with pytest.raises(OmitError, match="answered locally"):
        assert_omit_usable(_operation(_entry("a"), handler="playbook"), _SERVED)


# --------------------------------------------------------------------------
# The shipped blocks, against recorded upstream answers
# --------------------------------------------------------------------------

_SAMPLE_FILE = Path(__file__).parent / "data" / "omit_payload_samples.json"


def _payload_samples() -> dict[str, Any]:
    samples = json.loads(_SAMPLE_FILE.read_text(encoding="utf-8"))
    return {key: value for key, value in samples.items() if not key.startswith("_")}


def _carrying(manifest_dir: Path) -> list[ManifestOperation]:
    return [op for op in build_registry(manifest_dir).list_operations() if op.omit]


def test_every_shipped_omit_path_matches_its_own_recorded_answer(bundled_manifest_dir: Path) -> None:
    """A path that matches nothing passes every lint and removes nothing — the
    answer stays as big, or keeps the credential, and nothing reports it."""
    carrying = _carrying(bundled_manifest_dir)
    assert carrying, "no shipped operation declares an omit block — this test proves nothing"

    samples = _payload_samples()
    unsampled = sorted(op.operation_id for op in carrying if op.operation_id not in samples)
    assert not unsampled, (
        f"these operations declare an omit block with no recorded payload sample: {unsampled}. "
        f"Add one to {_SAMPLE_FILE.name}, taken from a real response."
    )

    for operation in carrying:
        _, omitted = apply_omit(operation, samples[operation.operation_id])
        matched = {entry["path"] for entry in omitted or []}
        missing = [entry.path for entry in operation.omit if entry.path not in matched]
        assert not missing, (
            f"{operation.operation_id}: omit path(s) {missing} match nothing in the recorded answer — "
            f"check them against a real response."
        )


def test_every_shipped_path_is_gone_after_removal(bundled_manifest_dir: Path) -> None:
    samples = _payload_samples()
    for operation in _carrying(bundled_manifest_dir):
        trimmed, _ = apply_omit(operation, samples[operation.operation_id])
        for entry in operation.omit:
            assert resolve_path(trimmed, entry.path) == [], f"{operation.operation_id}: {entry.path} survived"


def test_no_credential_value_survives_anywhere_in_the_answer(bundled_manifest_dir: Path) -> None:
    """For a credential the path matching is not enough: the value itself must
    not appear anywhere in what the client receives."""
    samples = _payload_samples()
    checked = 0
    for operation in _carrying(bundled_manifest_dir):
        sample = samples[operation.operation_id]
        trimmed, _ = apply_omit(operation, sample)
        serialised = json.dumps(trimmed)
        for entry in operation.omit:
            if entry.reason != "credential":
                continue
            for value in resolve_path(sample, entry.path):
                if isinstance(value, str) and value:
                    assert value not in serialised, f"{operation.operation_id}: a {entry.path} value survived"
                    checked += 1
    assert checked, "no credential value was checked — the samples carry none"


def test_no_payload_sample_outlives_the_block_it_documents(bundled_manifest_dir: Path) -> None:
    declaring = {op.operation_id for op in _carrying(bundled_manifest_dir)}
    stale = sorted(set(_payload_samples()) - declaring)
    assert not stale, f"payload samples with no omit block left to check: {stale}"


def test_a_1688_offer_keeps_every_shipping_region_but_the_us_copy(bundled_manifest_dir: Path) -> None:
    """Only ``shipping_by_region.US`` is a copy of ``shipping``. A store that ships
    outside the US makes AutoDSApi fetch that country's shipping onto the same
    offer, and that list exists nowhere else in the answer, so it must stay."""
    operation = next(op for op in _carrying(bundled_manifest_dir) if op.operation_id == "get_1688_product_details")
    sample = json.loads(json.dumps(_payload_samples()["get_1688_product_details"]))
    offer = next(iter(sample["data"].values()))
    gb = [{"shipping_price": 9.4, "shipping_time": 16, "shipping_tag": "Yunexpress Standard GB"}]
    offer["shipping_by_region"]["GB"] = gb
    for variation in offer["variations"]:
        variation["shipping_by_region"]["GB"] = gb

    trimmed, _ = apply_omit(operation, sample)

    trimmed_offer = next(iter(trimmed["data"].values()))
    assert trimmed_offer["shipping_by_region"] == {"GB": gb}
    assert [v["shipping_by_region"] for v in trimmed_offer["variations"]] == [{"GB": gb}, {"GB": gb}]
    assert [v["shipping"] for v in trimmed_offer["variations"]] == [v["shipping"] for v in offer["variations"]]


# --------------------------------------------------------------------------
# End to end, through a real client session
# --------------------------------------------------------------------------


async def test_similar_products_answer_as_cards_with_the_main_image_in_the_grid(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The gallery, variations and description are gone, and the grid shows
    each result's ``img_url`` — the one image the card keeps."""
    upstream_payload = _payload_samples()["get_similar_products"]

    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=upstream_payload)

    app, runtime = make_mcp_app(mcp_settings(manifest_dir=bundled_manifest_dir), upstream_handler=upstream)
    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("get_similar_products", {"product_id": "6862c98dcc7b15769464e1fc"})

    assert result.is_error is False
    envelope = result.structured_content
    assert envelope is not None
    for item in envelope["data"]["results"]:
        assert not {"images", "variations", "description"} & item.keys()
        assert item["img_url"]
    assert [entry["path"] for entry in envelope[OMITTED_KEY]] == [
        "results.*.images",
        "results.*.variations",
        "results.*.description",
    ]
    grid = envelope[IMAGES_KEY]
    assert [len(item["images"]) for item in grid["items"]] == [1, 1]
    # The text block is the same envelope, so it is trimmed too.
    assert "/81PLP-biSZL." not in result.content[0].text


async def test_the_removal_runs_after_images_have_read_the_full_payload(
    mcp_settings, make_mcp_app, write_manifest, access_token
) -> None:
    """The ordering rule, on a probe tool whose ``images`` block reads the very
    field its ``omit`` block removes. No shipped tool does that today, so the
    rule is pinned here rather than through one of them."""
    manifest_dir = write_manifest(
        {
            "server_name": "probe",
            "base_url_key": "products_research",
            "operations": [
                {
                    "operation_id": "probe_gallery",
                    "method": "GET",
                    "path": "/probe",
                    "parameters": [],
                    "has_json_body": False,
                    "request_body_required": False,
                    "notes": "Thumbnails beside `data`; the gallery is listed in `omitted`.",
                    "annotations": {"title": "Probe", "readOnlyHint": True, "destructiveHint": False},
                    "omit": [{"path": "results.*.gallery", "reason": "internal_detail", "detail": "Probe."}],
                    "images": {
                        "item_path": "results",
                        "image_paths": ["gallery.*"],
                        "per_item": 1,
                        "max": 20,
                        "label_path": "title",
                        "id_path": "_id",
                        "widget": "product-grid",
                        "base64": "off",
                    },
                }
            ],
        }
    )

    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": [{"_id": "a", "title": "A", "gallery": ["https://cdn.shopify.com/a.jpg"]}]}
        )

    app, runtime = make_mcp_app(mcp_settings(manifest_dir=manifest_dir), upstream_handler=upstream)
    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("probe_gallery", {})

    envelope = result.structured_content
    assert envelope is not None
    assert "gallery" not in envelope["data"]["results"][0]
    assert [len(item["images"]) for item in envelope[IMAGES_KEY]["items"]] == [1]


async def test_a_credential_never_reaches_the_client(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    upstream_payload = _payload_samples()["list_stores_api"]

    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=upstream_payload)

    app, runtime = make_mcp_app(mcp_settings(manifest_dir=bundled_manifest_dir), upstream_handler=upstream)
    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("list_stores_api", {})

    assert result.is_error is False
    assert "FAKE-STORE-TOKEN" not in result.content[0].text
    assert "FAKE-EBAY-EIAS" not in result.content[0].text
    envelope = result.structured_content
    assert envelope is not None
    assert [store["store"]["id"] for store in envelope["data"]] == [895, 5991]
    assert [entry["reason"] for entry in envelope[OMITTED_KEY]] == ["credential", "credential"]


async def test_a_credential_path_that_stops_matching_is_reported(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token, monkeypatch
) -> None:
    """The upstream wraps its list in ``{results: [...]}``: both token paths
    now match nothing and the tokens reach the client. The answer still goes
    out, but a warning line and a Sentry event name the paths — and neither
    carries the token itself."""
    wrapped = {"results": _payload_samples()["list_stores_api"]}

    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=wrapped)

    reported: list[dict[str, Any]] = []
    monkeypatch.setattr(mcp_transport, "capture_omit_unmatched", lambda **kwargs: reported.append(kwargs))

    app, runtime = make_mcp_app(mcp_settings(manifest_dir=bundled_manifest_dir), upstream_handler=upstream)
    with capture_logs() as logs:
        async with mcp_client_session(app, runtime, token=access_token) as session:
            result = await session.call_tool("list_stores_api", {})

    assert result.is_error is False
    expected = ["*.store.autods_store_token", "*.store.ebay_eias"]
    assert reported == [{"tool_name": "list_stores_api", "paths": expected}]
    warnings = [line for line in logs if line["event"] == "omit_credential_unmatched"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["paths"] == expected
    assert "FAKE-STORE-TOKEN" not in json.dumps(warnings[0])


async def test_a_user_with_no_stores_reports_nothing(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token, monkeypatch
) -> None:
    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    reported: list[dict[str, Any]] = []
    monkeypatch.setattr(mcp_transport, "capture_omit_unmatched", lambda **kwargs: reported.append(kwargs))

    app, runtime = make_mcp_app(mcp_settings(manifest_dir=bundled_manifest_dir), upstream_handler=upstream)
    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("list_stores_api", {})

    assert result.is_error is False
    assert reported == []


async def test_a_block_that_matches_nothing_leaves_the_envelope_untouched(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """An empty page from an operation that does declare a block must look
    exactly as it did before RD-146 — no ``omitted`` field, ``data`` verbatim."""

    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    app, runtime = make_mcp_app(mcp_settings(manifest_dir=bundled_manifest_dir), upstream_handler=upstream)
    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("list_store_quotes", {"store_ids": "5991", "body": {}})

    assert result.is_error is False
    envelope = result.structured_content
    assert envelope is not None
    assert OMITTED_KEY not in envelope
    assert envelope["data"] == {"results": []}
