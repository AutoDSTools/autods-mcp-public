"""RD-92: the opt-in base64 thumbnail path, end to end.

The synthetic ``include_images`` argument is the awkward part of this feature
and the tests reflect that. It is *ours*, not the upstream's, and it has to be
in two places at once without being in a third:

* present in the ``inputSchema`` the client sees — which is also the schema
  ``_build_validators`` compiles from, and since mcp 2.x that validator is the
  only schema gate on the call path, so a field the validator has not been told
  about is rejected before the dispatcher ever runs;
* **absent** from the outbound upstream request, which
  ``dispatch._build_request`` already guarantees by reading only declared
  parameters and ``body`` — a guarantee worth pinning precisely because it is
  passive;
* absent from every operation that has no image surface, and from
  ``list_products``, where the base64 path is deliberately withheld.

The cap is the other half: 20 images at 252 px, enforced by resampling here
rather than by trusting a CDN rewrite, because past that ceiling the *client*
truncates silently and misdescribes what it kept (RD-82).
"""

import base64
import io
from pathlib import Path

import httpx
import pytest
from PIL import Image

from autods_mcp_server.image_fetch import fetch_thumbnails
from autods_mcp_server.images import BASE64_EDGE_PX, INCLUDE_IMAGES_ARG, MAX_IMAGES
from tests.mcp_server.conftest import mcp_client_session


def _png(width: int, height: int, *, mode: str = "RGB") -> bytes:
    image = Image.new(mode, (width, height), (200, 30, 30) if mode == "RGB" else (200, 30, 30, 128))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _decoded(data: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(data)))


# --------------------------------------------------------------------------
# The schema seam
# --------------------------------------------------------------------------


async def test_include_images_is_advertised_only_where_there_is_an_image_surface(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        tools = await session.list_tools()

    carries = {tool.name for tool in tools.tools if INCLUDE_IMAGES_ARG in (tool.input_schema.get("properties") or {})}
    assert carries == {
        "search_products",
        "get_winning_products",
        "get_similar_products",
        "get_recommended_products",
        "get_product_by_id",
    }
    # `list_products` renders a widget but withholds base64 on purpose: roughly
    # half of those images have no small variant to fetch, so the model would be
    # handed full-size originals to downscale. Deferred, not forgotten.
    assert "list_products" not in carries
    # And nothing without an image surface advertises it at all.
    assert "upload_products" not in carries
    assert "get_playbook" not in carries


async def test_include_images_defaults_to_off_and_says_what_it_costs(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """Tier 1: the cost/benefit decision is the model's, so the price is on the
    argument itself rather than in prose somewhere it may never read."""
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        tools = await session.list_tools()

    schema = next(t for t in tools.tools if t.name == "search_products").input_schema
    field = schema["properties"][INCLUDE_IMAGES_ARG]
    assert field["type"] == "boolean"
    assert field["default"] is False
    assert str(BASE64_EDGE_PX) in field["description"]
    assert str(MAX_IMAGES) in field["description"]
    assert "vision tokens" in field["description"]


async def test_include_images_is_accepted_by_the_validator_and_never_sent_upstream(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """Both halves in one call: the argument clears our own schema gate, and the
    upstream request carries no trace of it."""
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(
            "search_products",
            {
                "body": {"order_by": {"name": "created_at", "direction": "desc"}, "filters": []},
                INCLUDE_IMAGES_ARG: True,
            },
        )

    assert result.is_error is False
    # One upstream call, and the synthetic field is in neither the body nor the query.
    assert len(seen) == 1
    assert INCLUDE_IMAGES_ARG not in seen[0].content.decode()
    assert INCLUDE_IMAGES_ARG not in str(seen[0].url)


async def test_a_non_boolean_include_images_is_refused_before_any_upstream_work(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """Since mcp 2.x nothing else validates arguments, so the injected field has
    to be typed in the schema the validators compile from or a string sails
    straight through to a truthiness check."""
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(
            "search_products",
            {
                "body": {"order_by": {"name": "created_at", "direction": "desc"}, "filters": []},
                INCLUDE_IMAGES_ARG: "yes",
            },
        )

    assert result.is_error is True
    assert INCLUDE_IMAGES_ARG in result.content[0].text
    assert seen == []


async def test_include_images_false_attaches_no_image_blocks(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"_id": "a", "images": ["https://a.test/1.png"]}]})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(
            "search_products",
            {
                "body": {"order_by": {"name": "created_at", "direction": "desc"}, "filters": []},
                INCLUDE_IMAGES_ARG: False,
            },
        )

    assert [block.type for block in result.content] == ["text"]
    assert "attached" not in (result.structured_content or {})["images"]


async def test_include_images_true_attaches_capped_thumbnails(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """The whole path: the upstream answer, then one fetch per image, then
    ImageContent blocks capped at 252 px however large the originals were."""

    def upstream(request: httpx.Request) -> httpx.Response:
        if "products-research.test" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "results": [{"_id": f"id{n}", "images": [f"https://images.autods.com/{n}.png"]} for n in range(3)]
                },
            )
        # Deliberately far over the cap, so a pass-through would be obvious.
        return httpx.Response(200, content=_png(1200, 900), headers={"content-type": "image/png"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(
            "search_products",
            {
                "body": {"order_by": {"name": "created_at", "direction": "desc"}, "filters": []},
                INCLUDE_IMAGES_ARG: True,
            },
        )

    assert result.is_error is False
    blocks = [block for block in result.content if block.type == "image"]
    assert len(blocks) == 3
    for block in blocks:
        assert block.mime_type == "image/jpeg"
        image = _decoded(block.data)
        assert max(image.size) <= BASE64_EDGE_PX
        # Aspect ratio survives — a square resize would distort every product.
        assert image.size == (BASE64_EDGE_PX, 189)
    # The envelope records how many landed, so the correspondence with `images`
    # is readable rather than inferred from the block count.
    assert (result.structured_content or {})["images"]["attached"] == 3


async def test_a_short_thumbnail_set_says_so(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """A silently short set is precisely the failure RD-82 caught the client
    committing; this server does not repeat it."""

    def upstream(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "products-research.test" in url:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"_id": "ok", "images": ["https://images.autods.com/good.png"]},
                        {"_id": "gone", "images": ["https://images.autods.com/missing.png"]},
                    ]
                },
            )
        if url.endswith("missing.png"):
            return httpx.Response(404)
        return httpx.Response(200, content=_png(300, 300), headers={"content-type": "image/png"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(
            "search_products",
            {
                "body": {"order_by": {"name": "created_at", "direction": "desc"}, "filters": []},
                INCLUDE_IMAGES_ARG: True,
            },
        )

    assert len([b for b in result.content if b.type == "image"]) == 1
    images = (result.structured_content or {})["images"]
    assert images["attached"] == 1
    assert "1 of 2 thumbnails" in images["note"]


# --------------------------------------------------------------------------
# fetch_thumbnails on its own
# --------------------------------------------------------------------------


async def test_transparency_is_flattened_onto_white() -> None:
    """JPEG has no alpha, and Pillow's default conversion renders it black —
    which turns a cut-out product photo into a silhouette."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_png(60, 60, mode="RGBA"), headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(client, ["https://a.test/1.png"], timeout_seconds=1, max_bytes=1_000_000)

    assert len(batch.thumbnails) == 1
    assert _decoded(batch.thumbnails[0].data).mode == "RGB"


async def test_bytes_that_are_not_an_image_are_dropped() -> None:
    """A Content-Type is not evidence: RD-82's sample held ten hosts answering
    200 with an HTML body — WordPress 404 pages and parking pages — under an
    image content type. Pillow reading the bytes is the only real check."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>404</html>", headers={"content-type": "image/jpeg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(client, ["https://a.test/1.jpg"], timeout_seconds=1, max_bytes=1_000_000)

    assert batch.thumbnails == []
    assert batch.requested == 1
    assert "0 of 1 thumbnails" in (batch.note or "")


async def test_genuine_png_bytes_under_an_image_jpeg_content_type_still_decode() -> None:
    """The AutoDS scraper bucket serves exactly this mismatch. It is cosmetic —
    which is why the Content-Type ticket stayed in another repository — and this
    is what says so."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_png(400, 400), headers={"content-type": "image/jpeg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(client, ["https://a.test/x.jpg"], timeout_seconds=1, max_bytes=1_000_000)

    assert len(batch.thumbnails) == 1


async def test_an_oversized_response_is_refused_before_decoding() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_png(400, 400), headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(client, ["https://a.test/x.png"], timeout_seconds=1, max_bytes=10)

    assert batch.thumbnails == []


async def test_a_transport_failure_drops_one_image_not_the_batch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("dead.png"):
            raise httpx.ConnectError("no route to host")
        return httpx.Response(200, content=_png(100, 100), headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(
            client,
            ["https://a.test/dead.png", "https://a.test/live.png"],
            timeout_seconds=1,
            max_bytes=1_000_000,
        )

    assert len(batch.thumbnails) == 1
    assert batch.requested == 2


async def test_the_fetch_is_capped_at_the_image_ceiling() -> None:
    """A defence in depth: the manifest lint already bounds ``per_item × max``
    at 20, but this path takes a plain list of URLs and must not depend on the
    caller having respected that."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=_png(50, 50), headers={"content-type": "image/png"})

    urls = [f"https://a.test/{n}.png" for n in range(MAX_IMAGES + 15)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(client, urls, timeout_seconds=1, max_bytes=1_000_000)

    assert len(calls) == MAX_IMAGES
    assert len(batch.thumbnails) == MAX_IMAGES
    assert batch.note is None


async def test_no_credentials_are_sent_to_a_cdn() -> None:
    """These are public CDN objects and the caller's bearer token must never
    leave the AutoDS upstreams."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_png(50, 50), headers={"content-type": "image/png"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"authorization": "Bearer should-not-be-here"},
    ) as client:
        await fetch_thumbnails(client, ["https://a.test/1.png"], timeout_seconds=1, max_bytes=1_000_000)

    # The client-level header is the shape a future refactor could introduce;
    # what matters is that nothing in this module adds one.
    assert seen[0].headers.get("accept") == "image/*"


@pytest.mark.parametrize("urls", [[], [""], ["", None]])
async def test_an_empty_url_list_does_no_work(urls: list) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be reached
        raise AssertionError("no fetch should happen")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(client, urls, timeout_seconds=1, max_bytes=1_000_000)

    assert batch.thumbnails == []
    assert batch.requested == 0
    assert batch.note is None
