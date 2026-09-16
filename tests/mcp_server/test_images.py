"""RD-92: the ``images`` manifest block — extraction and the boot lints.

Two halves. ``extract_images`` reads image URLs out of a payload whose shape it
does not know, through the shared ``payload_paths`` resolver; the shapes it has
to survive are the ones a real upstream produces, including the ones where a
field is simply absent. ``assert_images_usable`` refuses to boot on a block that
would otherwise ship looking like protection and quietly do nothing.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from autods_mcp_server.images import (
    IMAGES_KEY,
    MAX_IMAGES,
    PAYLOAD_KIND,
    ImagesError,
    assert_images_usable,
    extract_images,
    truncation_note,
)
from autods_mcp_server.manifests.loader import build_registry
from autods_mcp_server.manifests.schema import ManifestOperation

_NOTES = "Read-only. Thumbnails ride beside `data`."


def _operation(**images: object) -> ManifestOperation:
    """A minimal read operation carrying an ``images`` block."""
    return ManifestOperation(
        operation_id="probe_op",
        method="GET",
        path="/probe",
        notes=_NOTES,
        base_url_key="products_research",
        annotations={"title": "Probe", "readOnlyHint": True},
        images=images or None,
    )


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


def test_a_flat_list_of_url_strings_is_extracted_in_order() -> None:
    """The products-research shape, verified against staging: ``results[]`` with
    ``images`` as a flat list of URL strings."""
    operation = _operation(item_path="results", image_paths=["images.*"], label_path="title", id_path="_id")
    data = {
        "results": [
            {"_id": "a1", "title": "First", "images": ["https://cdn.shopify.com/x/one.jpg"]},
            {"_id": "a2", "title": "Second", "images": ["https://ae01.alicdn.com/kf/Sxx.jpg"]},
        ]
    }
    block = extract_images(operation, data)
    assert block is not None
    assert block["kind"] == PAYLOAD_KIND
    assert block["shown"] == 2
    assert block["total"] == 2
    assert [item["id"] for item in block["items"]] == ["a1", "a2"]
    assert [item["label"] for item in block["items"]] == ["First", "Second"]
    # Each URL came back rewritten to its host's thumbnail rung.
    assert block["items"][0]["images"][0]["url"].endswith("width=256")
    assert block["items"][1]["images"][0]["url"].endswith("_220x220.jpg")


def test_a_nested_list_of_image_objects_is_extracted() -> None:
    """The AutoDSApi ``list_products`` shape: ``images[]`` of objects with a
    ``url``, plus the hero in ``main_picture_url.url``."""
    operation = _operation(
        item_path="results",
        image_paths=["main_picture_url.url", "images.*.url"],
        label_path="title",
        id_path="id",
    )
    data = {
        "results": [
            {
                "id": "p1",
                "title": "Headlamp",
                "main_picture_url": {"url": "https://images.autods.com/hero.png"},
                "images": [{"url": "https://images.autods.com/second.png"}],
            }
        ]
    }
    block = extract_images(operation, data)
    assert block is not None
    # First matching path wins, so the hero is taken and ``images`` is not
    # consulted at all — that ordering is the whole point of a path list.
    assert block["items"][0]["images"] == [{"url": "https://images.autods.com/hero.png"}]


def test_an_omitted_item_path_treats_the_root_object_as_the_single_item() -> None:
    """The by-id read has no ``results`` wrapper, which is what an empty
    ``item_path`` means."""
    operation = _operation(image_paths=["images.*"], label_path="title", id_path="id", per_item=3, max=1)
    data = {"id": "x9", "title": "One product", "images": ["https://a.test/1.jpg", "https://a.test/2.jpg"]}
    block = extract_images(operation, data)
    assert block is not None
    assert block["total"] == 1
    assert len(block["items"][0]["images"]) == 2


def test_an_omitted_item_path_also_accepts_a_root_that_arrives_as_a_list() -> None:
    """Several AutoDS reads answer the same request with either a bare object or
    a one-element list (``/users/list/`` is the documented case), so a root list
    is the items themselves. Reading it as "no items" would drop every picture
    from a payload that plainly has them, silently and with a 200."""
    operation = _operation(image_paths=["images.*"], label_path="title", id_path="id", per_item=2, max=1)
    data = [{"id": "x9", "title": "One product", "images": ["https://a.test/1.jpg"]}]
    block = extract_images(operation, data)
    assert block is not None
    assert block["total"] == 1
    assert block["items"][0]["id"] == "x9"
    assert block["items"][0]["images"] == [{"url": "https://a.test/1.jpg"}]


def test_a_later_path_is_used_when_the_earlier_ones_are_missing_or_null() -> None:
    """An upstream that drops an optional field must fall through, not fail."""
    operation = _operation(
        item_path="results",
        image_paths=["main_picture_url.url", "images.*.url", "variations.*.main_picture_url.url"],
    )
    data = {
        "results": [
            {
                "main_picture_url": None,
                "images": [],
                "variations": [{"main_picture_url": {"url": "https://a.test/v.jpg"}}],
            }
        ]
    }
    block = extract_images(operation, data)
    assert block is not None
    assert block["items"][0]["images"] == [{"url": "https://a.test/v.jpg"}]


def test_a_wildcard_over_an_empty_list_yields_no_images_and_no_error() -> None:
    operation = _operation(item_path="results", image_paths=["images.*"])
    block = extract_images(operation, {"results": [{"images": []}]})
    assert block is not None
    assert block["items"] == [{"images": []}]


def test_an_item_with_no_image_is_still_listed() -> None:
    """Dropping the pictureless entries would silently renumber the result set
    the user is choosing from. The widget renders a placeholder instead."""
    operation = _operation(item_path="results", image_paths=["images.*"], label_path="title")
    data = {"results": [{"title": "No picture"}, {"title": "Has one", "images": ["https://a.test/1.jpg"]}]}
    block = extract_images(operation, data)
    assert block is not None
    assert block["shown"] == 2
    assert block["items"][0] == {"images": [], "label": "No picture"}


def test_non_http_values_are_ignored() -> None:
    """Upstream image fields carry the odd relative path or empty string."""
    operation = _operation(item_path="results", image_paths=["images.*"])
    data = {"results": [{"images": ["", "/relative.jpg", None, 7, "https://a.test/ok.jpg"]}]}
    block = extract_images(operation, data)
    assert block is not None
    assert block["items"][0]["images"] == [{"url": "https://a.test/ok.jpg"}]


def test_duplicate_urls_within_one_item_are_collapsed() -> None:
    """``main_picture_url`` is normally also the first entry of ``images``, so a
    gallery would otherwise open on the same picture twice."""
    operation = _operation(item_path="results", image_paths=["images.*"], per_item=4, max=5)
    data = {"results": [{"images": ["https://a.test/1.jpg", "https://a.test/1.jpg", "https://a.test/2.jpg"]}]}
    block = extract_images(operation, data)
    assert block is not None
    assert [image["url"] for image in block["items"][0]["images"]] == [
        "https://a.test/1.jpg",
        "https://a.test/2.jpg",
    ]


def test_over_cap_results_are_truncated_with_an_explicit_note() -> None:
    """The silent cap is the failure this whole ticket exists to prevent: RD-82
    watched a client keep 16 of 20 images, report "19 of 20", and describe a
    half-decoded JPEG as a different product."""
    operation = _operation(item_path="results", image_paths=["images.*"], max=3)
    data = {"results": [{"images": [f"https://a.test/{n}.jpg"]} for n in range(9)]}
    block = extract_images(operation, data)
    assert block is not None
    assert block["shown"] == 3
    assert block["total"] == 9
    assert block["note"] == "Thumbnails shown for the first 3 of 9 results; the rest are in `data`."


def test_no_note_when_nothing_was_dropped() -> None:
    assert truncation_note(4, 4) is None
    assert truncation_note(5, 4) is None


def test_a_long_label_is_truncated() -> None:
    """Marketplace titles run to hundreds of characters of keyword stuffing, and
    twenty of those is most of the block's weight."""
    operation = _operation(item_path="results", image_paths=["images.*"], label_path="title")
    data = {"results": [{"title": "x" * 400, "images": ["https://a.test/1.jpg"]}]}
    block = extract_images(operation, data)
    assert block is not None
    assert len(block["items"][0]["label"]) == 120


def test_an_operation_without_an_images_block_extracts_nothing() -> None:
    assert extract_images(_operation(), {"results": [{"images": ["https://a.test/1.jpg"]}]}) is None


@pytest.mark.parametrize(
    "data",
    [
        {"results": []},
        {"other": [{"images": ["https://a.test/1.jpg"]}]},
        {},
        None,
        "not json",
        [],
    ],
)
def test_a_payload_with_no_addressable_items_extracts_nothing(data: object) -> None:
    """No items means no block at all, so the envelope stays exactly as it was —
    an upstream shape we did not anticipate must not add a confusing empty
    ``images`` field."""
    operation = _operation(item_path="results", image_paths=["images.*"])
    assert extract_images(operation, data) is None


def test_the_widget_name_travels_on_the_block() -> None:
    operation = _operation(item_path="results", image_paths=["images.*"], widget="product-grid")
    block = extract_images(operation, {"results": [{"images": ["https://a.test/1.jpg"]}]})
    assert block is not None
    assert block["widget"] == "product-grid"


def test_the_envelope_key_is_a_sibling_of_data() -> None:
    """Documents the placement rule the transport relies on: ``data`` stays the
    upstream payload verbatim, so the block can only be its sibling."""
    assert IMAGES_KEY == "images"


# --------------------------------------------------------------------------
# Boot lints
# --------------------------------------------------------------------------


def test_a_block_with_no_image_paths_is_rejected() -> None:
    with pytest.raises(ImagesError, match="no 'image_paths'"):
        assert_images_usable(_operation(item_path="results", image_paths=[]))


@pytest.mark.parametrize(
    "block",
    [
        {"item_path": "results", "image_paths": ["images[].url"]},
        {"item_path": "results[]", "image_paths": ["images.*"]},
        {"item_path": "results", "image_paths": ["images.*"], "label_path": "titles[0]"},
    ],
)
def test_bracket_notation_is_rejected(block: dict[str, object]) -> None:
    """RD-92 sketched the paths in ``images[].url``; the landed decision is the
    ``*`` notation ``business_errors`` already speaks. This lint is what keeps
    the decision from rotting back into a second synonym."""
    with pytest.raises(ImagesError, match="bracket notation"):
        assert_images_usable(_operation(**block))


def test_a_per_item_of_zero_is_rejected() -> None:
    with pytest.raises(ImagesError, match="per_item"):
        assert_images_usable(_operation(item_path="results", image_paths=["images.*"], per_item=0))


def test_a_max_of_zero_is_rejected() -> None:
    with pytest.raises(ImagesError, match="images.max"):
        assert_images_usable(_operation(item_path="results", image_paths=["images.*"], max=0))


def test_a_block_over_the_image_ceiling_is_rejected() -> None:
    with pytest.raises(ImagesError, match=f"{MAX_IMAGES}-image ceiling"):
        assert_images_usable(_operation(item_path="results", image_paths=["images.*"], per_item=3, max=8))


def test_a_widget_nothing_serves_is_rejected() -> None:
    with pytest.raises(ImagesError, match="no ui:// resource serves"):
        assert_images_usable(_operation(item_path="results", image_paths=["images.*"], widget="product-carousel"))


def test_notes_that_never_mention_thumbnails_are_rejected() -> None:
    """Same shape as the ``business_errors`` ``ok`` lint: the block populates a
    field, and a field nobody documented is a field the model was never told to
    read."""
    operation = ManifestOperation(
        operation_id="quiet_op",
        method="GET",
        path="/quiet",
        notes="Read-only. Returns products.",
        base_url_key="products_research",
        annotations={"title": "Quiet", "readOnlyHint": True},
        images={"item_path": "results", "image_paths": ["images.*"]},
    )
    with pytest.raises(ImagesError, match="never mention thumbnails"):
        assert_images_usable(operation)


def test_a_valid_block_passes() -> None:
    assert_images_usable(
        _operation(item_path="results", image_paths=["images.*"], per_item=1, max=20, widget="product-grid")
    )


def test_an_operation_with_no_block_passes() -> None:
    assert_images_usable(_operation())


# --------------------------------------------------------------------------
# The per-item link (RD-92, addition 2026-09-16)
# --------------------------------------------------------------------------


def _linking_operation(link: dict[str, object], **overrides: object) -> ManifestOperation:
    """A ``list_products``-shaped operation: a body-gated link needs a body schema."""
    images: dict[str, object] = {
        "item_path": "results",
        "image_paths": ["main_picture_url.url"],
        "id_path": "id",
        "label_path": "title",
        "link": link,
    }
    images.update(overrides)
    return ManifestOperation(
        operation_id="probe_op",
        method="POST",
        path="/probe",
        notes=_NOTES,
        base_url_key="autods_api",
        annotations={"title": "Probe", "readOnlyHint": True},
        body_schema={"type": "object", "properties": {"product_status": {"type": "integer"}}},
        images=images,
    )


def test_a_link_to_a_route_nothing_serves_is_rejected() -> None:
    """A route name is the only part of a link a lint can check, and it is the
    part that would otherwise ship as a confident 404."""
    with pytest.raises(ImagesError, match="product-link table does not serve"):
        assert_images_usable(_linking_operation({"route": "wishlist_item"}))


def test_half_a_link_gate_is_rejected() -> None:
    """A gate with only a field name matches every call — the opposite of what
    whoever wrote it meant, and invisible in review."""
    with pytest.raises(ImagesError, match="half a link gate"):
        assert_images_usable(_linking_operation({"route": "store_product", "when_body_field": "product_status"}))
    with pytest.raises(ImagesError, match="half a link gate"):
        assert_images_usable(_linking_operation({"route": "store_product", "when_body_equals": 2}))


def test_a_gate_on_a_body_field_the_schema_never_declares_is_rejected() -> None:
    """Same class as the phantom filter fields: the gate would simply never
    match, and a link that never appears looks exactly like a link nobody
    configured."""
    with pytest.raises(ImagesError, match="body_schema' does not declare"):
        assert_images_usable(
            _linking_operation({"route": "store_product", "when_body_field": "status", "when_body_equals": 2})
        )


def test_a_link_whose_route_needs_an_id_is_rejected_without_an_id_path() -> None:
    with pytest.raises(ImagesError, match="declares no 'id_path'"):
        assert_images_usable(_linking_operation({"route": "store_product"}, id_path=""))


def test_a_valid_link_block_passes() -> None:
    assert_images_usable(
        _linking_operation({"route": "store_product", "when_body_field": "product_status", "when_body_equals": 2})
    )


def test_each_item_carries_its_own_product_page_link() -> None:
    operation = _linking_operation(
        {"route": "store_product", "when_body_field": "product_status", "when_body_equals": 2}
    )
    data = {
        "results": [
            {"id": 4242, "title": "Bed Sheets", "main_picture_url": {"url": "https://i.ebayimg.com/x/s-l1600.jpg"}},
            {"id": 4243, "title": "Denim Vest", "main_picture_url": {"url": "https://i.ebayimg.com/y/s-l1600.jpg"}},
        ]
    }
    block = extract_images(
        operation,
        data,
        arguments={"store_ids": "7", "body": {"product_status": 2}},
        app_base_url="https://v2-staging.autods.com",
    )
    assert block is not None
    assert [item["link"] for item in block["items"]] == [
        "https://v2-staging.autods.com/products/4242",
        "https://v2-staging.autods.com/products/4243",
    ]


def test_a_call_outside_the_gate_carries_no_link_at_all() -> None:
    """Drafts, ended, untracked, scheduled and pre-draft products render exactly
    as they did before links existed: the key is absent, not empty."""
    operation = _linking_operation(
        {"route": "store_product", "when_body_field": "product_status", "when_body_equals": 2}
    )
    data = {"results": [{"id": 4242, "main_picture_url": {"url": "https://i.ebayimg.com/x/s-l1600.jpg"}}]}
    block = extract_images(
        operation,
        data,
        arguments={"store_ids": "7", "body": {"product_status": 1}},
        app_base_url="https://v2-staging.autods.com",
    )
    assert block is not None
    assert "link" not in block["items"][0]


def test_an_item_with_no_id_carries_no_link() -> None:
    operation = _linking_operation(
        {"route": "store_product", "when_body_field": "product_status", "when_body_equals": 2}
    )
    data = {"results": [{"main_picture_url": {"url": "https://i.ebayimg.com/x/s-l1600.jpg"}}]}
    block = extract_images(
        operation,
        data,
        arguments={"body": {"product_status": 2}},
        app_base_url="https://v2-staging.autods.com",
    )
    assert block is not None
    assert "link" not in block["items"][0]
    assert "id" not in block["items"][0]


def test_an_operation_with_no_link_block_is_unaffected_by_the_arguments() -> None:
    """The regression guard: passing the two new arguments to a block that does
    not link must produce exactly the block it produced before."""
    operation = _operation(item_path="results", image_paths=["images.*"], id_path="_id")
    data = {"results": [{"_id": "a1", "images": ["https://cdn.shopify.com/x/one.jpg"]}]}
    linked = extract_images(operation, data, arguments={"body": {"product_status": 2}}, app_base_url="https://app.test")
    assert linked == extract_images(operation, data)


# --------------------------------------------------------------------------
# Every shipped block, against the payload its own upstream really returns
# --------------------------------------------------------------------------
#
# The lints above check a block's *shape*; they cannot check that its paths
# address anything, because that needs the upstream payload. A block can pass
# every lint and resolve nothing — which is exactly how ``get_recommended_products``
# shipped a grid whose cells were all empty: it declared ``images.*`` against a
# payload whose only picture is a singular ``imgUrl``. Nothing failed. The tool
# answered 200, the envelope advertised ``shown: 20 / total: 20``, and every item
# carried an empty list. It took a human looking at a live client to see it.
#
# So each shipped block is run against a recorded sample of its own upstream's
# shape. A new images block with no sample fails here rather than shipping blind.

_SAMPLE_FILE = Path(__file__).parent / "data" / "images_payload_samples.json"


def _payload_samples() -> dict[str, Any]:
    samples = json.loads(_SAMPLE_FILE.read_text(encoding="utf-8"))
    return {key: value for key, value in samples.items() if not key.startswith("_")}


def test_every_shipped_images_block_resolves_a_url_against_its_own_payload(
    bundled_manifest_dir: Path,
) -> None:
    """A declared block must actually yield a picture for the shape it ships against."""
    registry = build_registry(bundled_manifest_dir)
    carrying = [op for op in registry.list_operations() if op.images]
    assert carrying, "no shipped operation declares an images block — this test proves nothing"

    samples = _payload_samples()
    unsampled = sorted(op.operation_id for op in carrying if op.operation_id not in samples)
    assert not unsampled, (
        f"these operations declare an images block with no recorded payload sample: {unsampled}. "
        f"Add one to {_SAMPLE_FILE.name}, taken from a real response — the boot lints cannot tell "
        f"whether the declared image_paths address anything in the payload."
    )

    for operation in carrying:
        payload = samples[operation.operation_id]
        block = extract_images(operation, payload)
        assert block is not None, f"{operation.operation_id}: declared an images block but extracted nothing"

        items = block["items"]
        assert items, f"{operation.operation_id}: extracted no items from its own payload sample"

        empty = [item.get("id") for item in items if not item.get("images")]
        assert not empty, (
            f"{operation.operation_id}: {len(empty)} of {len(items)} item(s) resolved no image URL from "
            f"image_paths={operation.images.image_paths!r}. The widget renders those as empty cells "
            f"while the payload still carries a picture — check the path against a real response."
        )


_JSON_ARRAY = re.compile(r"\[[^\[\]]*\]")


def _projection_examples(notes: str) -> list[list[str]]:
    """Every JSON list-of-strings literal in ``notes`` — i.e. its projection examples."""
    examples: list[list[str]] = []
    for candidate in _JSON_ARRAY.findall(notes):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue  # prose like "{results: [...]}", not an example
        if isinstance(parsed, list) and parsed and all(isinstance(entry, str) for entry in parsed):
            examples.append(parsed)
    return examples


def _covers(projected: str, image_path: str) -> bool:
    """Would projecting ``projected`` keep ``image_path`` in the response?"""
    return image_path == projected or image_path.startswith(f"{projected}.")


def test_a_projection_example_keeps_the_field_its_own_images_block_reads(
    bundled_manifest_dir: Path,
) -> None:
    """An operation that tells the model to narrow the response must not hand it a
    projection that drops the pictures.

    ``list_products`` shipped exactly that: "ALWAYS pass `projection`" followed by an
    example listing neither `main_picture_url` nor `images`. Following the instruction
    literally produced a grid where every cell read "no image" — the tool answering 200,
    the widget rendering, and nothing anywhere reporting a fault. The warning to project
    `main_picture_url` was in the same `notes`, two paragraphs below the example that
    contradicted it, and the example is the half that gets copied.
    """
    registry = build_registry(bundled_manifest_dir)
    for operation in registry.list_operations():
        if not operation.images or not operation.notes:
            continue
        if "projection" not in operation.notes:
            continue

        examples = _projection_examples(operation.notes)
        assert examples, (
            f"{operation.operation_id}: notes talk about `projection` and the operation declares an "
            f"images block, but no example projection could be parsed out to check."
        )
        paths = operation.images.image_paths
        for example in examples:
            assert any(_covers(field, path) for field in example for path in paths), (
                f"{operation.operation_id}: the example projection {example!r} keeps none of "
                f"image_paths={paths!r}, so an agent copying it gets a grid of empty cells. "
                f"Project the field the images block reads — for a nested path, the prefix that "
                f"contains it."
            )


def test_no_payload_sample_outlives_the_block_it_documents(bundled_manifest_dir: Path) -> None:
    """A sample for an operation that no longer declares a block is dead weight that
    reads as coverage."""
    registry = build_registry(bundled_manifest_dir)
    declaring = {op.operation_id for op in registry.list_operations() if op.images}
    stale = sorted(set(_payload_samples()) - declaring)
    assert not stale, f"payload samples with no images block left to check: {stale}"
