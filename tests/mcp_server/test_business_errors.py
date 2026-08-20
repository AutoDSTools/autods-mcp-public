"""RD-90 P5 — business rejections that arrive inside an HTTP 200.

Two layers: the shared wildcard path resolver (``payload_paths``, which RD-92's
``images`` block reuses) and the manifest-driven renderer that turns a matched
code into a ``business_error`` field beside ``data``.

The invariant worth guarding hardest is the *negative* one: an operation without
a ``business_errors`` block, or one whose paths don't match, must produce an
envelope byte-identical to what clients have always seen. ``data`` is never
touched either way — the dispatcher stays a pure forwarder and the upstream
contract is untouched.
"""

import json
from pathlib import Path
from typing import Any

import httpx

from autods_mcp_server.business_errors import detect_business_errors
from autods_mcp_server.manifests.schema import Manifest, ManifestOperation
from autods_mcp_server.payload_paths import resolve_path
from tests.mcp_server.conftest import mcp_client_session

_CODES = {
    "PRODUCT_OOS": "Offer is out of stock. Choose a different offer.",
    "SHIPPING_UNAVAILABLE": "Supplier does not ship to the selected country.",
}


def _operation(**overrides: Any) -> ManifestOperation:
    payload: dict[str, Any] = {
        "operation_id": "scan_offer",
        "method": "POST",
        "path": "/scan",
        "annotations": {"title": "Scan Offer", "readOnlyHint": True},
        "business_errors": {"paths": ["scraper_error.errorCode", "data.*.error.errorCode"], "codes": _CODES},
    }
    payload.update(overrides)
    return ManifestOperation.model_validate(payload)


# --- the shared path resolver ------------------------------------------------


def test_resolves_a_plain_dotted_path() -> None:
    assert resolve_path({"a": {"b": {"c": 1}}}, "a.b.c") == [1]


def test_wildcard_fans_out_over_a_list() -> None:
    payload = {"data": [{"x": 1}, {"x": 2}, {"x": 3}]}
    assert resolve_path(payload, "data.*.x") == [1, 2, 3]


def test_wildcard_fans_out_over_dict_values() -> None:
    payload = {"items": {"first": {"x": 1}, "second": {"x": 2}}}
    assert resolve_path(payload, "items.*.x") == [1, 2]


def test_wildcard_skips_entries_missing_the_tail() -> None:
    payload = {"data": [{"x": 1}, {"y": 2}]}
    assert resolve_path(payload, "data.*.x") == [1]


def test_nested_wildcards_compose() -> None:
    payload = {"a": [{"b": [{"c": 1}, {"c": 2}]}, {"b": [{"c": 3}]}]}
    assert resolve_path(payload, "a.*.b.*.c") == [1, 2, 3]


def test_a_path_that_does_not_fit_the_payload_yields_nothing() -> None:
    """A shape mismatch is not an error — an upstream that omits an optional
    field must not become a 500 on our side."""
    assert resolve_path({"a": 1}, "a.b.c") == []
    assert resolve_path({"a": None}, "a.b") == []
    assert resolve_path(None, "a") == []
    assert resolve_path("a string", "a") == []
    assert resolve_path([1, 2], "*.x") == []


def test_an_explicit_null_is_returned_not_dropped() -> None:
    """Callers decide whether a null counts; only structural misses are dropped."""
    assert resolve_path({"a": {"b": None}}, "a.b") == [None]


def test_an_empty_path_matches_nothing() -> None:
    assert resolve_path({"a": 1}, "") == []
    assert resolve_path({"a": 1}, ".") == []


# --- the renderer -----------------------------------------------------------


def test_no_block_means_no_business_error() -> None:
    operation = _operation(business_errors=None)
    assert detect_business_errors(operation, {"scraper_error": {"errorCode": "PRODUCT_OOS"}}) is None


def test_a_matching_code_is_rendered_with_its_recovery_hint() -> None:
    result = detect_business_errors(_operation(), {"scraper_error": {"errorCode": "PRODUCT_OOS"}})
    assert result == [{"code": "PRODUCT_OOS", "message": _CODES["PRODUCT_OOS"]}]


def test_a_clean_payload_produces_nothing() -> None:
    assert detect_business_errors(_operation(), {"data": [{"title": "ok"}]}) is None


def test_a_null_or_blank_code_is_not_an_error() -> None:
    """Several upstreams send the field as null/empty rather than omitting it."""
    assert detect_business_errors(_operation(), {"scraper_error": {"errorCode": None}}) is None
    assert detect_business_errors(_operation(), {"scraper_error": {"errorCode": "  "}}) is None


def test_codes_are_deduplicated_across_a_page() -> None:
    """A page of 100 items that all failed the same way is one fact, not a
    hundred repetitions that crowd out the payload."""
    payload = {"data": [{"error": {"errorCode": "PRODUCT_OOS"}} for _ in range(100)]}
    assert detect_business_errors(_operation(), payload) == [{"code": "PRODUCT_OOS", "message": _CODES["PRODUCT_OOS"]}]


def test_several_codes_are_reported_in_path_then_document_order() -> None:
    payload = {
        "scraper_error": {"errorCode": "SHIPPING_UNAVAILABLE"},
        "data": [{"error": {"errorCode": "PRODUCT_OOS"}}],
    }
    assert [entry["code"] for entry in detect_business_errors(_operation(), payload)] == [
        "SHIPPING_UNAVAILABLE",
        "PRODUCT_OOS",
    ]


def test_an_unmapped_code_is_still_surfaced() -> None:
    """The whole point of the block is that ``ok`` must not be read as success —
    which is exactly when a code the manifest hasn't caught up with bites."""
    result = detect_business_errors(_operation(), {"scraper_error": {"errorCode": "BRAND_NEW_CODE"}})
    assert result[0]["code"] == "BRAND_NEW_CODE"
    assert result[0]["message"]


def test_the_unmapped_hint_claims_nothing_about_what_was_applied() -> None:
    """A per-item path matches when one item of a page failed and the rest
    landed, so the generic hint must not assert the call did nothing — it points
    at ``data`` instead. The mapped hints are author-written per code and can be
    as specific as the code warrants; this one can't be."""
    payload = {"data": [{"error": {"errorCode": "UNKNOWN_CODE"}}, {"title": "this one landed"}]}
    message = detect_business_errors(_operation(), payload)[0]["message"]

    assert "was not applied" not in message
    assert "`data`" in message


def test_the_block_round_trips_through_the_manifest_schema() -> None:
    manifest = Manifest.model_validate_json(
        json.dumps(
            {
                "server_name": "demo",
                "operations": [
                    {
                        "operation_id": "scan",
                        "method": "POST",
                        "path": "/scan",
                        "annotations": {"title": "Scan", "readOnlyHint": True},
                        "business_errors": {"paths": ["a.b"], "codes": {"X": "hint"}},
                    }
                ],
            }
        )
    )
    block = manifest.operations[0].business_errors
    assert block.paths == ["a.b"]
    assert block.codes == {"X": "hint"}


# --- end to end through the transport ---------------------------------------

_SCAN_MANIFEST: dict[str, Any] = {
    "server_name": "demo",
    "operations": [
        {
            "operation_id": "scan_offer",
            "method": "GET",
            "path": "/scan",
            "summary": "Scan one supplier offer.",
            "notes": "`ok` is a transport-level signal only: a rejected scan still answers 200.",
            "annotations": {"title": "Scan Offer", "readOnlyHint": True},
            "business_errors": {"paths": ["scraper_error.errorCode"], "codes": _CODES},
        }
    ],
}


def _scan_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "manifests"
    directory.mkdir(exist_ok=True)
    (directory / "scan.json").write_text(json.dumps(_SCAN_MANIFEST), encoding="utf-8")
    return directory


def _upstream(payload: dict[str, Any]):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


async def test_a_200_with_an_error_payload_carries_a_business_error(
    mcp_settings, make_mcp_app, tmp_path: Path, access_token
) -> None:
    upstream_payload = {"scraper_error": {"errorCode": "PRODUCT_OOS", "message": "oos"}}
    settings = mcp_settings(manifest_dir=_scan_dir(tmp_path))
    app, runtime = make_mcp_app(settings, upstream_handler=_upstream(upstream_payload))

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("scan_offer", {})

    envelope = result.structured_content
    assert result.is_error is False
    # The transport-level signal still says "the upstream answered 2xx"…
    assert envelope["ok"] is True
    # …and the business rejection rides beside ``data``, never inside it.
    assert envelope["business_error"] == [{"code": "PRODUCT_OOS", "message": _CODES["PRODUCT_OOS"]}]
    assert envelope["data"] == upstream_payload


async def test_a_clean_200_envelope_is_unchanged(mcp_settings, make_mcp_app, tmp_path: Path, access_token) -> None:
    """The negative case, pinned: no match → the exact envelope clients have
    always seen, in both the structured content and the text block."""
    upstream_payload = {"scraper_error": None, "results": [{"title": "a product"}]}
    settings = mcp_settings(manifest_dir=_scan_dir(tmp_path))
    app, runtime = make_mcp_app(settings, upstream_handler=_upstream(upstream_payload))

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool("scan_offer", {})

    expected = {"operation_id": "scan_offer", "status": 200, "ok": True, "data": upstream_payload}
    assert result.structured_content == expected
    assert result.content[0].text == json.dumps(expected, indent=2)
