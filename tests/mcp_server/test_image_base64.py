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
* absent from every operation that has no image surface, and from the four
  discovery grids and ``list_products``, where the base64 path is deliberately
  withheld — it is offered on ``get_product_by_id`` and nowhere else.

The cap is the other half: 20 images at 252 px, enforced by resampling here
rather than by trusting a CDN rewrite, because past that ceiling the *client*
truncates silently and misdescribes what it kept (RD-82).
"""

import asyncio
import base64
import io
import struct
import subprocess
import sys
import textwrap
import zlib
from pathlib import Path

import httpx
import pytest
import structlog
from PIL import Image
from structlog.testing import capture_logs

from autods_mcp_server import mcp_transport
from autods_mcp_server.image_fetch import (
    _CONCURRENCY,
    _MAX_CONCURRENT_FETCHES,
    MAX_DECODE_PIXELS,
    fetch_thumbnails,
)
from autods_mcp_server.images import BASE64_EDGE_PX, INCLUDE_IMAGES_ARG, MAX_IMAGES
from tests.mcp_server.conftest import mcp_client_session

# Source edge for the peak-RSS regression below. 3000 px is a real product-photo
# size (the scraper bucket is full of them) and is where the old ordering cost
# ~460 MB for a batch of 20 — big enough that a regression is unmistakable,
# small enough that the test stays a couple of seconds.
_SOURCE_EDGE = 3000
_MAX_BATCH_RSS_GROWTH_MB = 150.0

# The one tool that still offers the base64 opt-in. The four discovery grids
# withheld it (see the advertisement test): a result set is something the user
# picks from, which the widget does for free, so judging a picture — the only
# thing base64 is for — happens on the single-product read.
_BASE64_TOOL = "get_product_by_id"
_BASE64_TOOL_ARGS = {"product_id": "0123456789abcdef01234567"}

# An origin the CSP allowlist declares, so the server may fetch it. `a.test` and
# friends are refused before any request is made, which is the point of the gate
# and would otherwise make every test here pass for the wrong reason.
_CDN = "https://images.autods.com"


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
    # One tool, and it is the single-product read.
    #
    # The four discovery grids render a widget and withhold base64: a result set
    # is something the *user* picks from, which the widget does at zero vision
    # tokens, and offering the flag there mostly buys a model that sets it out of
    # habit and pays ~1,620 tokens on every subsequent turn. Judging a picture is
    # a per-product act, so the per-product read is where the flag belongs until
    # there is a tool whose whole job is comparing images (RD-95's offer scan).
    assert carries == {"get_product_by_id"}
    # `list_products` renders a widget but withholds base64 for its own reason:
    # roughly half of those images have no small variant to fetch, so the model
    # would be handed full-size originals to downscale. Deferred, not forgotten.
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

    schema = next(t for t in tools.tools if t.name == _BASE64_TOOL).input_schema
    field = schema["properties"][INCLUDE_IMAGES_ARG]
    assert field["type"] == "boolean"
    assert field["default"] is False
    assert str(BASE64_EDGE_PX) in field["description"]
    assert str(MAX_IMAGES) in field["description"]
    assert "vision tokens" in field["description"]
    # The bill is not one-off: ImageContent blocks stay in the conversation, so a
    # long session re-sends them every turn. A model shown only the per-call
    # figure is being quoted a fraction of the real price.
    assert "every later turn" in field["description"]


async def test_include_images_is_accepted_by_the_validator_and_never_sent_upstream(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    """Both halves in one call: the argument clears our own schema gate, and the
    upstream request carries no trace of it."""
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "p1", "images": []})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(_BASE64_TOOL, {**_BASE64_TOOL_ARGS, INCLUDE_IMAGES_ARG: True})

    assert result.is_error is False
    # One upstream call, and the synthetic field is in neither the body nor the query.
    assert len(seen) == 1
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
        return httpx.Response(200, json={"id": "p1", "images": []})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(_BASE64_TOOL, {**_BASE64_TOOL_ARGS, INCLUDE_IMAGES_ARG: "yes"})

    assert result.is_error is True
    assert INCLUDE_IMAGES_ARG in result.content[0].text
    assert seen == []


async def test_include_images_false_attaches_no_image_blocks(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token
) -> None:
    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"_id": "a", "images": ["https://images.autods.com/1.png"]}]})

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
            return httpx.Response(200, json={"id": "p1", "images": [f"{_CDN}/{n}.png" for n in range(3)]})
        # Deliberately far over the cap, so a pass-through would be obvious.
        return httpx.Response(200, content=_png(1200, 900), headers={"content-type": "image/png"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(_BASE64_TOOL, {**_BASE64_TOOL_ARGS, INCLUDE_IMAGES_ARG: True})

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
            return httpx.Response(200, json={"id": "p1", "images": [f"{_CDN}/good.png", f"{_CDN}/missing.png"]})
        if url.endswith("missing.png"):
            return httpx.Response(404)
        return httpx.Response(200, content=_png(300, 300), headers={"content-type": "image/png"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(
            _BASE64_TOOL,
            {**_BASE64_TOOL_ARGS, INCLUDE_IMAGES_ARG: True},
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
        batch = await fetch_thumbnails(
            client, ["https://images.autods.com/1.png"], timeout_seconds=1, max_bytes=1_000_000, deadline_seconds=5
        )

    assert len(batch.thumbnails) == 1
    assert _decoded(batch.thumbnails[0].data).mode == "RGB"


async def test_bytes_that_are_not_an_image_are_dropped() -> None:
    """A Content-Type is not evidence: RD-82's sample held ten hosts answering
    200 with an HTML body — WordPress 404 pages and parking pages — under an
    image content type. Pillow reading the bytes is the only real check."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>404</html>", headers={"content-type": "image/jpeg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(
            client, ["https://images.autods.com/1.jpg"], timeout_seconds=1, max_bytes=1_000_000, deadline_seconds=5
        )

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
        batch = await fetch_thumbnails(
            client, ["https://images.autods.com/x.jpg"], timeout_seconds=1, max_bytes=1_000_000, deadline_seconds=5
        )

    assert len(batch.thumbnails) == 1


async def test_an_oversized_response_is_refused_before_decoding() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_png(400, 400), headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(
            client, ["https://images.autods.com/x.png"], timeout_seconds=1, max_bytes=10, deadline_seconds=5
        )

    assert batch.thumbnails == []


async def test_a_transport_failure_drops_one_image_not_the_batch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("dead.png"):
            raise httpx.ConnectError("no route to host")
        return httpx.Response(200, content=_png(100, 100), headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(
            client,
            ["https://images.autods.com/dead.png", "https://images.autods.com/live.png"],
            timeout_seconds=1,
            max_bytes=1_000_000,
            deadline_seconds=5,
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

    urls = [f"https://images.autods.com/{n}.png" for n in range(MAX_IMAGES + 15)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(client, urls, timeout_seconds=1, max_bytes=1_000_000, deadline_seconds=5)

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
        await fetch_thumbnails(
            client, ["https://images.autods.com/1.png"], timeout_seconds=1, max_bytes=1_000_000, deadline_seconds=5
        )

    # The client-level header is the shape a future refactor could introduce;
    # what matters is that nothing in this module adds one.
    assert seen[0].headers.get("accept") == "image/*"


@pytest.mark.parametrize("urls", [[], [""], ["", None]])
async def test_an_empty_url_list_does_no_work(urls: list) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be reached
        raise AssertionError("no fetch should happen")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(client, urls, timeout_seconds=1, max_bytes=1_000_000, deadline_seconds=5)

    assert batch.thumbnails == []
    assert batch.requested == 0
    assert batch.note is None


# --------------------------------------------------------------------------
# Decoding is bounded by what the pod can survive, not by what the image wants
#
# This path runs in the request process — one uvicorn worker, no ``--workers``,
# 1300m CPU and 3150Mi of memory — so the ways a picture can hurt are the ways
# it can evict the process serving every other caller. Each test below pins one
# of those, and each failure mode was measured rather than imagined.
# --------------------------------------------------------------------------


def _bomb_png(width: int, height: int) -> bytes:
    """A tiny PNG whose *header* claims enormous dimensions.

    No pixel data to speak of — the point is that Pillow decides how much to
    allocate from the header, so a decompression bomb costs an attacker nothing
    to serve. At 68 bytes this is the whole payload.
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 10)) + chunk(b"IEND", b"")
    )


async def test_a_decompression_bomb_drops_one_image_not_the_batch() -> None:
    """``DecompressionBombError`` subclasses ``Exception`` directly — not
    ``OSError``, not ``ValueError`` — so the obvious except tuple misses it.

    Uncaught it escapes ``_encode``, propagates through the gather, and is
    swallowed whole by the bulk ``except`` in the transport: every thumbnail in
    the call disappears, ``attached`` is never set, and no note explains it. A
    68-byte response from one supplier host is enough to do that to a 20-image
    result, which makes this a hostile-input bug and not only a robustness one.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("bomb.png"):
            return httpx.Response(200, content=_bomb_png(20000, 20000), headers={"content-type": "image/png"})
        return httpx.Response(200, content=_png(100, 100), headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(
            client,
            ["https://images.autods.com/bomb.png", "https://images.autods.com/good.png"],
            timeout_seconds=1,
            max_bytes=1_000_000,
            deadline_seconds=5,
        )

    assert len(batch.thumbnails) == 1
    assert batch.requested == 2
    assert "1 of 2 thumbnails" in (batch.note or "")


async def test_a_frame_over_the_pixel_ceiling_is_refused_before_it_is_decoded() -> None:
    """``max_bytes`` bounds the wire; RSS scales with *pixels*, and the two come
    apart badly. A measured 6.17 MB JPEG was 10000 × 10000 and cost 383 MB to
    decode on its own — inside the byte ceiling, nowhere near inside the memory
    limit once several land at once."""
    over = _bomb_png(6000, 6000)  # 36M px, past MAX_DECODE_PIXELS, well under the bomb threshold
    assert MAX_DECODE_PIXELS < 6000 * 6000
    assert len(over) < 1_000, "the point is that a huge frame costs the sender nothing"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=over, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(
            client, ["https://images.autods.com/huge.png"], timeout_seconds=1, max_bytes=8_000_000, deadline_seconds=5
        )

    assert batch.thumbnails == []


async def test_the_pixel_ceiling_measures_the_decode_not_the_publication() -> None:
    """The same dimensions that a PNG is refused for, a JPEG is accepted at.

    ``draft`` rewrites ``size`` to what libjpeg will actually return, so the
    ceiling has to be read after it — otherwise the cheap case (a large JPEG,
    which never materialises its full frame) is rejected alongside the expensive
    one (a large PNG, which does). Getting this backwards costs thumbnails on
    exactly the high-resolution supplier photos worth looking at.
    """
    seed = Image.frombytes("RGB", (750, 750), bytes(range(256)) * (750 * 750 * 3 // 256 + 1))
    buffer = io.BytesIO()
    seed.resize((6000, 6000), Image.BILINEAR).save(buffer, format="JPEG", quality=70)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=buffer.getvalue(), headers={"content-type": "image/jpeg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(
            client, ["https://images.autods.com/big.jpg"], timeout_seconds=1, max_bytes=8_000_000, deadline_seconds=5
        )

    assert MAX_DECODE_PIXELS < 6000 * 6000
    assert len(batch.thumbnails) == 1
    assert max(_decoded(batch.thumbnails[0].data).size) == BASE64_EDGE_PX


async def test_the_download_stops_at_the_byte_ceiling_instead_of_auditing_it() -> None:
    """Reading ``.content`` and then checking its length is a report, not a
    limit: the body is already resident by the time the ceiling is consulted."""
    served = 0

    async def body():
        nonlocal served
        for _ in range(200):
            served += 1
            yield b"\x00" * 4096

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body(), headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(
            client, ["https://images.autods.com/big.png"], timeout_seconds=1, max_bytes=20_000, deadline_seconds=5
        )

    assert batch.thumbnails == []
    # 200 chunks x 4 KB is 800 KB on offer against a 20 KB ceiling; it should
    # stop within a chunk or two of the limit rather than draining the response.
    assert served <= 10, f"pulled {served} chunks past a {20_000}-byte ceiling"


async def test_a_palette_image_is_not_downscaled_by_nearest_neighbour() -> None:
    """Pillow silently forces ``NEAREST`` on modes ``P`` and ``1``, so a palette
    frame has to leave its mode *before* it is resampled.

    That ordering is easy to lose while rearranging this function for speed —
    and losing it produces a thumbnail that is merely ugly, which no other test
    would notice. A fine checkerboard averages to mid-grey under a real filter
    and stays pure black-and-white under nearest.
    """
    checker = Image.new("P", (600, 600))
    checker.putpalette([0, 0, 0] + [255, 255, 255] + [0] * (256 * 3 - 6))
    checker.putdata([(x + y) % 2 for y in range(600) for x in range(600)])
    buffer = io.BytesIO()
    checker.save(buffer, format="PNG")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=buffer.getvalue(), headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(
            client, ["https://images.autods.com/p.png"], timeout_seconds=1, max_bytes=1_000_000, deadline_seconds=5
        )

    assert len(batch.thumbnails) == 1
    # A 1x1 BOX resize is the mean of every pixel, and does not go through the
    # deprecated getdata().
    mean = _decoded(batch.thumbnails[0].data).convert("L").resize((1, 1), Image.BOX).getpixel((0, 0))
    assert 100 < mean < 155, f"a resampled checkerboard should average to mid-grey, got {mean}"


async def test_an_origin_outside_the_csp_allowlist_is_never_contacted() -> None:
    """The base64 path opens connections from inside the cluster, to URLs that
    arrive in scraped supplier data. The widget path fetches the same URLs from
    the viewer's browser under `_meta.ui.csp.resourceDomains`, so the server-side
    fetch reuses that list and is never the looser of the two.

    The assertion that matters is that the handler is not reached at all: a
    refusal after the request has gone out is not a refusal.
    """
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be reached
        reached.append(str(request.url))
        return httpx.Response(200, content=_png(50, 50), headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(
            client,
            ["http://169.254.169.254/latest/meta-data/", "https://merchant-self-host.example/hero.jpg"],
            timeout_seconds=1,
            max_bytes=1_000_000,
            deadline_seconds=5,
        )

    assert reached == []
    assert batch.thumbnails == []
    assert batch.requested == 2


async def test_a_redirect_off_the_allowlist_is_refused_at_the_hop() -> None:
    """An allowlist applied only to the URL we were handed is defeated by the
    first `Location:` header, so `follow_redirects` is off and the hops are
    walked by hand. A CDN is entitled to redirect; it is not entitled to choose
    what this process connects to."""
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        reached.append(url)
        if url.endswith("ladder.png"):
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, content=_png(50, 50), headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(
            client, [f"{_CDN}/ladder.png"], timeout_seconds=1, max_bytes=1_000_000, deadline_seconds=5
        )

    assert reached == [f"{_CDN}/ladder.png"], "the redirect target must never be requested"
    assert batch.thumbnails == []


async def test_a_redirect_within_the_allowlist_is_followed() -> None:
    """Size ladders are the reason redirects are followed at all — refusing them
    outright would cost real pictures, which is why this is a per-hop check and
    not a blanket `follow_redirects=False`."""

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/original.png":
            # Relative, as a size ladder usually is — the hop has to be resolved
            # against the current URL before it can be checked.
            return httpx.Response(301, headers={"location": "/resized/original.png"})
        return httpx.Response(200, content=_png(400, 400), headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(
            client, [f"{_CDN}/original.png"], timeout_seconds=1, max_bytes=1_000_000, deadline_seconds=5
        )

    assert seen == ["/original.png", "/resized/original.png"]
    assert len(batch.thumbnails) == 1


async def test_a_redirect_loop_terminates() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": f"{_CDN}/round.png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(
            client, [f"{_CDN}/round.png"], timeout_seconds=1, max_bytes=1_000_000, deadline_seconds=5
        )

    assert batch.thumbnails == []


# --------------------------------------------------------------------------
# The two bounds that are about elapsed time and live buffers, not about pixels
# --------------------------------------------------------------------------


async def test_a_trickling_host_cannot_hold_the_batch_open_past_the_deadline() -> None:
    """``timeout_seconds`` cannot do this job, which is the whole point.

    It is httpx's *per-operation* timeout, and on a streamed body the read
    timeout restarts on every chunk — so a host that sends a little data every
    so often never trips it and keeps the tool call open until ``max_bytes``.
    The deadline is the only bound on how long a caller waits, and a slow image
    has to cost that one image rather than the answer.
    """
    good = _png(40, 40)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/slow.png":
            await asyncio.sleep(30)  # never completes inside the deadline
        return httpx.Response(200, content=good, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await fetch_thumbnails(
            client,
            [f"{_CDN}/slow.png", f"{_CDN}/fast.png"],
            timeout_seconds=30,  # deliberately longer than the deadline
            max_bytes=1_000_000,
            deadline_seconds=0.5,
        )

    # The image that could be fetched is kept; only the slow one is dropped. A
    # deadline that cancelled the batch as a unit would throw away work that had
    # already succeeded, for no gain.
    assert len(batch.thumbnails) == 1
    assert batch.requested == 2
    assert batch.note is not None and "1 of 2" in batch.note


async def test_downloads_are_gated_process_wide_not_only_per_call() -> None:
    """``_CONCURRENCY`` bounds one call, and calls are what multiply.

    This is the same argument the private decode pool exists for, applied to the
    bytes a download holds rather than to the pixels a decode allocates. Six
    concurrent fetches per call is fine; six times however many callers opted in
    at the same moment is not, and nothing in a single call can see that.
    """
    live = 0
    peak = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(0.05)
            return httpx.Response(200, content=_png(40, 40), headers={"content-type": "image/png"})
        finally:
            live -= 1

    # Enough simultaneous callers that the per-call limit alone would allow far
    # more than the gate does: 5 calls x _CONCURRENCY (6) = 30 in flight.
    urls = [f"{_CDN}/{n}.png" for n in range(MAX_IMAGES)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await asyncio.gather(
            *(
                fetch_thumbnails(client, urls, timeout_seconds=5, max_bytes=1_000_000, deadline_seconds=20)
                for _ in range(5)
            )
        )

    assert peak <= _MAX_CONCURRENT_FETCHES, f"{peak} concurrent downloads, gate is {_MAX_CONCURRENT_FETCHES}"
    # And the gate is genuinely binding here — otherwise the assertion above
    # would pass for the wrong reason on a machine that happened to serialise.
    assert peak > _CONCURRENCY


async def test_a_bulk_fetch_failure_still_reports_a_short_set(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token, monkeypatch
) -> None:
    """The one path that could still go silently short.

    ``return_exceptions=True`` made a single bad frame cost one thumbnail, but
    the bulk ``except`` in ``_attach_thumbnails`` remained: it swallowed the
    failure and returned nothing, leaving a caller that asked for pictures with
    no pictures and no sentence saying why. Swallowing the exception is right —
    a slow CDN must not turn a good answer into an error — but it has to leave
    the same trace a per-image failure would.
    """

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "p1", "images": [f"{_CDN}/1.png", f"{_CDN}/2.png"]})

    async def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the whole batch fell over")

    monkeypatch.setattr(mcp_transport, "fetch_thumbnails", explode)

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    async with mcp_client_session(app, runtime, token=access_token) as session:
        result = await session.call_tool(_BASE64_TOOL, {**_BASE64_TOOL_ARGS, INCLUDE_IMAGES_ARG: True})

    # The call still succeeds and still carries the URLs — the pictures were
    # never the contract.
    assert result.is_error is False
    images = (result.structured_content or {})["images"]
    assert images["attached"] == 0
    assert "0 of 2" in images["note"]


async def test_the_thumbnail_pass_records_its_own_duration(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token, monkeypatch
) -> None:
    """``tool_call`` is emitted before any of this runs, so its ``latency_ms``
    stops at the upstream response. This is the only path on the server that
    does third-party network work, and ``thumbnails_attached`` is the only line
    that can say how long it took."""

    def upstream(request: httpx.Request) -> httpx.Response:
        if "products-research.test" in str(request.url):
            return httpx.Response(200, json={"id": "p1", "images": [f"{_CDN}/1.png"]})
        return httpx.Response(200, content=_png(60, 60), headers={"content-type": "image/png"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    with capture_logs() as logs:
        monkeypatch.setattr(mcp_transport, "_audit_logger", structlog.get_logger("audit-test"))
        async with mcp_client_session(app, runtime, token=access_token) as session:
            await session.call_tool(_BASE64_TOOL, {**_BASE64_TOOL_ARGS, INCLUDE_IMAGES_ARG: True})

    attached = [line for line in logs if line.get("event") == "thumbnails_attached"]
    assert len(attached) == 1
    assert attached[0]["attached"] == 1
    assert isinstance(attached[0]["elapsed_ms"], float)


async def test_include_images_is_on_the_audit_line_of_a_failed_call_too(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token, monkeypatch
) -> None:
    """The field's documented reading is "absent means the tool does not offer
    it", which is what makes a logged ``false`` mean "offered and declined". If
    it only appeared on successful calls, every failure on a tool that *does*
    offer it would read as a tool that does not."""

    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "upstream is having a day"})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    with capture_logs() as logs:
        monkeypatch.setattr(mcp_transport, "_audit_logger", structlog.get_logger("audit-test"))
        async with mcp_client_session(app, runtime, token=access_token) as session:
            await session.call_tool(_BASE64_TOOL, {**_BASE64_TOOL_ARGS, INCLUDE_IMAGES_ARG: True})

    audit = [line for line in logs if line.get("event") == "tool_call"]
    assert len(audit) == 1
    assert audit[0]["include_images"] is True
    assert audit[0]["error_type"] == "upstream_error"


async def test_a_tool_that_does_not_offer_the_flag_omits_it_entirely(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path, access_token, monkeypatch
) -> None:
    """The other half of the reading above: absent, not ``false``."""

    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    app, runtime = make_mcp_app(settings, upstream_handler=upstream)

    with capture_logs() as logs:
        monkeypatch.setattr(mcp_transport, "_audit_logger", structlog.get_logger("audit-test"))
        async with mcp_client_session(app, runtime, token=access_token) as session:
            await session.call_tool(
                "search_products",
                {"body": {"order_by": {"name": "created_at", "direction": "desc"}, "filters": []}},
            )

    audit = [line for line in logs if line.get("event") == "tool_call"]
    assert len(audit) == 1
    assert "include_images" not in audit[0]


def test_the_image_fetch_does_not_share_the_dispatcher_s_client(
    mcp_settings, make_mcp_app, bundled_manifest_dir: Path
) -> None:
    """Two clients, because httpx files every response's cookies into the
    client's own jar whatever the request asked for. One shared client would
    collect a cookie from every supplier CDN the server ever fetched and hold it
    for the life of the process — domain-scoped, so never *sent* to an AutoDS
    upstream, but unbounded state on the client the upstreams use."""
    settings = mcp_settings(manifest_dir=bundled_manifest_dir)
    _app, runtime = make_mcp_app(settings)

    assert runtime.image_client is not runtime.http_client
    # Hops are walked by hand so every new origin is re-checked against the
    # allowlist; a client-level default of True would make forgetting the
    # per-request override silently unsafe.
    assert runtime.image_client.follow_redirects is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="peak RSS is read from /proc")
def test_a_batch_of_large_jpegs_never_materialises_a_full_size_buffer() -> None:
    """The memory bound, pinned where it is actually observable.

    ``draft()`` before ``load()`` is what keeps this path cheap, and it is
    invisible: reorder ``_encode`` so the convert happens first and the output
    stays byte-identical while a batch of 20 goes from ~90 ms and no measurable
    growth to ~440 ms and +464 MB. Nothing but the process's own high-water mark
    can tell the difference, so this runs in a subprocess and reads it.

    The threshold is deliberately loose — it is there to catch a regression back
    to full-size decoding (which is measured in hundreds of MB), not to police
    allocator noise.
    """
    script = textwrap.dedent(f"""
        import asyncio, io, os
        from PIL import Image
        from autods_mcp_server.image_fetch import _CONCURRENCY, _encode
        from autods_mcp_server.images import MAX_IMAGES

        def hwm_mb():
            with open("/proc/self/status") as fh:
                for line in fh:
                    if line.startswith("VmHWM:"):
                        return int(line.split()[1]) / 1024

        # Photographic entropy, so the JPEG is a realistic size rather than a
        # flat colour that compresses to nothing.
        seed = Image.frombytes("RGB", (375, 375), os.urandom(375 * 375 * 3))
        buf = io.BytesIO()
        seed.resize(({_SOURCE_EDGE}, {_SOURCE_EDGE}), Image.BILINEAR).save(buf, format="JPEG", quality=85)
        raw = buf.getvalue()
        del seed
        baseline = hwm_mb()

        async def main():
            sem = asyncio.Semaphore(_CONCURRENCY)
            async def one():
                async with sem:
                    return await asyncio.to_thread(_encode, raw)
            done = await asyncio.gather(*(one() for _ in range(MAX_IMAGES)))
            assert all(t is not None for t in done), "the batch itself must succeed"

        asyncio.run(main())
        print(f"{{baseline:.1f}} {{hwm_mb():.1f}}")
    """)
    completed = subprocess.run(  # noqa: S603 - our own interpreter, literal script
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    baseline, peak = (float(v) for v in completed.stdout.split())
    growth = peak - baseline
    assert growth < _MAX_BATCH_RSS_GROWTH_MB, (
        f"a batch of {MAX_IMAGES} {_SOURCE_EDGE}px JPEGs grew peak RSS by {growth:.0f} MB "
        f"(baseline {baseline:.0f} MB). Full-size decoding is back — check that _encode still "
        f"drafts and thumbnails before it converts."
    )
