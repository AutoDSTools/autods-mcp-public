"""The opt-in base64 thumbnail path (RD-92).

The widget path costs zero vision tokens and is what a *user* choosing between
products wants. It is useless for the other case: when the **model** has to
judge the picture itself — a hero shot that is actually a size chart, a
watermark, a collage of six unrelated items, an offer that does not look like
the product it claims to be. For that the bytes have to reach the model, and
RD-82 established there is no free way to do it:

* ``annotations.audience: ["user"]`` is ignored by every client, so there is no
  display-only channel. Every image the model is shown, it pays for.
* ``ImageContent`` renders only inside the collapsed tool block, never inline in
  the prose — it is a model-facing channel, not a user-facing one.
* Vision tokens are ``ceil(w/28) × ceil(h/28)`` per image. Twenty at 252 px is
  1,620; the same twenty at 1200 px is 36,980 **and truncates**, silently, with
  the client misdescribing what it kept.

So the cap is enforced here, on our side, and it is enforced by *resampling* —
never by trusting that a CDN rewrite produced a small variant. A rewrite is an
optimisation on this path (fewer bytes to pull); ``image_rewrites`` covers only
about half of ``list_products`` rows and tops out at a 400 px rung on eBay, so a
guarantee built on it would be no guarantee at all.

Everything here is best-effort per image and fails closed to *fewer* images,
never to a bigger one: a fetch that times out, a host that 403s a parameterised
request, bytes that are not an image, a frame too large to decode — each drops
that one thumbnail and says so in the note. A failure never breaks the tool
call, because the URLs and the text envelope are the contract and the pictures
are the enhancement.
"""

import asyncio
import base64
import io
from dataclasses import dataclass

import httpx
from PIL import Image, UnidentifiedImageError

from autods_mcp_server.images import BASE64_EDGE_PX, MAX_IMAGES
from autods_mcp_server.logging import get_logger

_logger = get_logger(__name__)

# Encoded thumbnails are JPEG: it is universally decodable by the clients, and
# at 252 px the artefacts are invisible while the payload is a few KB.
THUMBNAIL_MIME_TYPE = "image/jpeg"
_JPEG_QUALITY = 80

# How many of the (already capped) images to pull at once. Small on purpose:
# these are third-party CDNs, the work sits on the tool-call path, and 20
# simultaneous connections to one sharded host looks like abuse.
_CONCURRENCY = 6

# A browser-ish User-Agent. Several supplier hosts answer a bare client with a
# 403 and an HTML body; RD-82's probe used the same shape.
_HEADERS = {
    "user-agent": "Mozilla/5.0 (compatible; autods-mcp-thumbnails/1.0)",
    "accept": "image/*",
}


@dataclass(frozen=True)
class Thumbnail:
    """One successfully fetched, downscaled, re-encoded image."""

    data: str  # base64, no data: prefix — ImageContent carries the mime type
    mime_type: str


@dataclass(frozen=True)
class ThumbnailBatch:
    """The result of one base64 pass: what was attached, and what was not."""

    thumbnails: list[Thumbnail]
    requested: int

    @property
    def note(self) -> str | None:
        """The sentence explaining a short set, or ``None`` when none is needed.

        A silently short set is the exact failure RD-82 caught the *client*
        committing, so this server does not commit it too.
        """
        missing = self.requested - len(self.thumbnails)
        if missing <= 0:
            return None
        return (
            f"{len(self.thumbnails)} of {self.requested} thumbnails could be attached at "
            f"{BASE64_EDGE_PX}px; {missing} could not be fetched or decoded. Their URLs are in "
            f"`images` and `data`."
        )


def _encode(raw: bytes) -> Thumbnail | None:
    """Decode, downscale to the cap, re-encode. ``None`` if the bytes are not an image.

    Runs in a worker thread (Pillow is synchronous and CPU-bound). The cap is a
    *long-edge* bound rather than a square resize, so aspect ratio survives and
    the token cost is at most ``ceil(252/28)²`` = 81.
    """
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            # A palette or alpha image flattened onto white: JPEG has no alpha,
            # and Pillow's default conversion would render transparency black,
            # which turns a cut-out product photo into a silhouette.
            if image.mode in ("RGBA", "LA", "P"):
                rgba = image.convert("RGBA")
                canvas = Image.new("RGB", rgba.size, (255, 255, 255))
                canvas.paste(rgba, mask=rgba.split()[-1])
                frame = canvas
            elif image.mode != "RGB":
                frame = image.convert("RGB")
            else:
                frame = image.copy()
        frame.thumbnail((BASE64_EDGE_PX, BASE64_EDGE_PX), Image.LANCZOS)
        buffer = io.BytesIO()
        frame.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    except (UnidentifiedImageError, OSError, ValueError):
        # Includes the decompression-bomb guard Pillow raises on absurd
        # dimensions, and the truncated-file case a flaky CDN produces.
        return None
    return Thumbnail(data=base64.b64encode(buffer.getvalue()).decode("ascii"), mime_type=THUMBNAIL_MIME_TYPE)


async def _fetch_one(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
) -> Thumbnail | None:
    try:
        # No credentials of any kind: these are public CDN objects, and the
        # caller's bearer token must never leave the AutoDS upstreams.
        # Redirects are followed because most CDNs use them for their size
        # ladders — unlike the dispatcher, which refuses them precisely because
        # it *is* carrying a token.
        response = await client.get(
            url,
            headers=_HEADERS,
            timeout=timeout_seconds,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        _logger.debug("thumbnail_fetch_failed", url=url, error=str(exc))
        return None
    if response.status_code != 200:
        _logger.debug("thumbnail_fetch_status", url=url, status=response.status_code)
        return None
    raw = response.content
    if not raw or len(raw) > max_bytes:
        _logger.debug("thumbnail_size_rejected", url=url, size=len(raw), max_bytes=max_bytes)
        return None
    # Content-Type is not consulted on purpose: the AutoDS scraper bucket serves
    # genuine PNG bytes under ``image/jpeg``, and browsers sniff images anyway.
    # Pillow reading the bytes is the only check that means anything.
    return await asyncio.to_thread(_encode, raw)


async def fetch_thumbnails(
    client: httpx.AsyncClient,
    urls: list[str],
    *,
    timeout_seconds: float,
    max_bytes: int,
) -> ThumbnailBatch:
    """Fetch and downscale up to :data:`~...images.MAX_IMAGES` of ``urls``.

    Order is preserved, so thumbnail *n* corresponds to item *n* of the
    ``images`` envelope block minus whatever failed — which is why the note
    exists rather than leaving the correspondence to be guessed at.
    """
    wanted = [url for url in urls if url][:MAX_IMAGES]
    if not wanted:
        return ThumbnailBatch(thumbnails=[], requested=0)

    semaphore = asyncio.Semaphore(_CONCURRENCY)

    async def bounded(url: str) -> Thumbnail | None:
        async with semaphore:
            return await _fetch_one(client, url, timeout_seconds=timeout_seconds, max_bytes=max_bytes)

    results = await asyncio.gather(*(bounded(url) for url in wanted))
    return ThumbnailBatch(thumbnails=[t for t in results if t is not None], requested=len(wanted))
