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

**Decoding is bounded three ways, and every one of them is about the pod rather
than about the picture.** This runs in the request process — one uvicorn worker,
no ``--workers``, 1300m of CPU and 3150Mi of memory — so an image big enough to
be interesting is an image big enough to evict the process that is serving every
other caller:

* ``draft()`` before ``load()`` lets libjpeg decode straight to a reduced size in
  the DCT domain, so the full-resolution buffer is never allocated. Measured on a
  batch of 20 6000×6000 JPEGs: 1452 ms and +1497 MB RSS before, 303 ms and no
  measurable RSS growth after, for byte-identical output. Ordering is the whole
  trick — the previous version called ``load()`` and converted at full size, so
  ``thumbnail()``'s own internal draft had nothing left to save.
* :data:`MAX_DECODE_PIXELS` refuses an absurd frame before any buffer exists,
  measured *after* the draft so it bounds what will actually be decoded rather
  than what the supplier published. ``max_bytes`` cannot do this job: it bounds
  the wire, and RSS scales with pixels — a 6.17 MB JPEG measured 10000×10000 and
  cost 383 MB to decode on its own, which at the fetch width here was ~2.2 GB
  against a 3.08 GB limit.
* :data:`_DECODE_WORKERS` is a private pool, so the ceiling is process-wide.
  ``_CONCURRENCY`` bounds one *call*; nothing bounds how many calls opt in at
  once, so a per-call limit multiplies. The private pool also keeps image work
  out of the default executor, which analytics uses for its Mixpanel sends.

**Fetching is bounded the same two ways, for the same reason.** The bytes a
download is holding cost memory exactly like a decoded frame does, and the
decode pool makes that worse rather than better: with only four decodes running,
a finished download waits for a slot with its whole body resident, so buffers
accumulate instead of turning over.

* :data:`_MAX_CONCURRENT_FETCHES` is a process-wide gate in front of the
  per-call ``_CONCURRENCY``. Without it, ``_CONCURRENCY`` × however many calls
  opt in at once is the real number of live download buffers — the same
  multiplication the private decode pool exists to stop, applied to bytes on the
  wire instead of pixels in memory.
* ``deadline_seconds`` bounds the whole pass on the clock. ``timeout_seconds``
  is httpx's *per-operation* timeout, and on a streamed body the read timeout
  restarts on every chunk — so a host trickling one byte every few seconds never
  trips it and holds the tool call open until ``max_bytes``. Every other limit
  here bounds a quantity; this is the only one that bounds elapsed time.
"""

import asyncio
import base64
import functools
import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from PIL import Image, UnidentifiedImageError

from autods_mcp_server.image_rewrites import is_allowed_origin
from autods_mcp_server.images import BASE64_EDGE_PX, MAX_IMAGES
from autods_mcp_server.logging import get_logger

_logger = get_logger(__name__)

# Encoded thumbnails are JPEG: it is universally decodable by the clients, and
# at 252 px the artefacts are invisible while the payload is a few KB.
THUMBNAIL_MIME_TYPE = "image/jpeg"
_JPEG_QUALITY = 80

# How many of the (already capped) images to pull at once, within **one call**.
# Small on purpose: these are third-party CDNs, the work sits on the tool-call
# path, and 20 simultaneous connections to one sharded host looks like abuse.
_CONCURRENCY = 6

# The same bound again, process-wide — the gate ``_CONCURRENCY`` alone cannot
# be. A per-call limit is multiplied by however many calls opt in at the same
# moment, and each in-flight download holds up to ``max_bytes`` of body in
# memory while it waits its turn at the four-worker decode pool. 12 × the 8 MB
# default is ~96 MB of raw bytes at the worst case, which sits beside the decode
# footprint rather than competing with it for the same headroom.
_MAX_CONCURRENT_FETCHES = 12

# Concurrent *decodes*, process-wide — deliberately not the same knob as the
# fetch width above, because the two bound different resources. A fetch waits on
# a socket; a decode holds pixel buffers and burns the container's CPU quota, and
# the number of tool calls opting in at once is not something this module gets to
# choose. Four keeps the worst case (see MAX_DECODE_PIXELS) inside the memory
# limit with the app's own baseline still resident.
_DECODE_WORKERS = 4

# Redirect hops allowed per image. CDNs use them for their size ladders, so zero
# would cost real pictures; each hop is re-checked against the origin allowlist,
# so the number only bounds how long a chain may be.
_MAX_REDIRECTS = 5

# Ceiling on the frame this process will decode, in pixels — read after ``draft``
# has had its say, so it bounds the buffer that will actually be allocated rather
# than the dimensions the supplier published. NOT a setting, for the same reason
# the 20 × 252 px cap is not one: it is a bound on what this process can survive,
# so raising it by config is a way to OOM the pod rather than a way to tune it.
# 25M px is ~5000 × 5000 — comfortably above any product photo a supplier
# actually publishes, and small enough that four concurrent worst-case decodes
# (a full-frame RGBA PNG, which ``draft`` cannot help with) stay a few hundred MB
# inside the container limit. Over it, the image is dropped like any other
# failure and its URL still reaches the client.
MAX_DECODE_PIXELS = 25_000_000

# Decodes run here rather than in ``asyncio.to_thread``'s default executor. That
# pool sizes itself from ``os.cpu_count()``, which reports the *node's* CPUs
# inside a container (measured: 32, against a 1300m quota), and it is shared with
# the Mixpanel sends in ``analytics`` — which already have a gotcha about one
# hung call pinning a worker.
_encoder_pool: ThreadPoolExecutor | None = None


def _encoders() -> ThreadPoolExecutor:
    """The private decode pool, created on first use.

    Lazily built and rebuildable rather than made at import time, so
    :func:`shutdown_encoders` can be called by an app lifespan without leaving
    the module permanently unusable — a second ``create_app()`` in the same
    process (which is every test that builds an app) gets a working pool back.
    """
    global _encoder_pool  # noqa: PLW0603 - a process-wide pool is the point
    if _encoder_pool is None:
        _encoder_pool = ThreadPoolExecutor(max_workers=_DECODE_WORKERS, thread_name_prefix="thumbnail")
    return _encoder_pool


def shutdown_encoders() -> None:
    """Drop the decode pool and the fetch gate. From the app lifespan; safe twice."""
    global _encoder_pool, _fetch_gate, _fetch_gate_loop  # noqa: PLW0603 - see _encoders
    pool, _encoder_pool = _encoder_pool, None
    _fetch_gate, _fetch_gate_loop = None, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


# The process-wide fetch gate, and the loop it belongs to. An
# ``asyncio.Semaphore`` binds itself to the first loop that awaits it and then
# refuses every other one, so it cannot be built at import time: production has
# one loop for the life of the process, but each test gets a fresh one. Holding
# the loop beside it and rebuilding when it changes keeps the bound real in
# production without making the module unusable anywhere else.
_fetch_gate: asyncio.Semaphore | None = None
_fetch_gate_loop: asyncio.AbstractEventLoop | None = None


def _fetches() -> asyncio.Semaphore:
    """The process-wide download gate, created on first use in this loop."""
    global _fetch_gate, _fetch_gate_loop  # noqa: PLW0603 - a process-wide gate is the point
    loop = asyncio.get_running_loop()
    if _fetch_gate is None or _fetch_gate_loop is not loop:
        _fetch_gate = asyncio.Semaphore(_MAX_CONCURRENT_FETCHES)
        _fetch_gate_loop = loop
    return _fetch_gate


# A browser-ish User-Agent. Several supplier hosts answer a bare client with a
# 403 and an HTML body; RD-82's probe used the same shape.
_HEADERS = {
    "user-agent": "Mozilla/5.0 (compatible; autods-mcp-thumbnails/1.0)",
    "accept": "image/*",
}


def create_image_client() -> httpx.AsyncClient:
    """The HTTP client this path fetches supplier images with.

    Its own client, not the dispatcher's, for one reason that is easy to miss:
    httpx keeps a cookie jar **on the client** and files every response's
    ``Set-Cookie`` into it, whatever the request asked for. Sharing the
    dispatcher's client would therefore collect cookies from every supplier CDN
    the server ever touched and keep them for the life of the process. The jar
    is domain-scoped, so none of it would ever have been *sent* to an AutoDS
    upstream — the cost is unbounded state, and a "no credentials of any kind"
    claim that only held on the way out.

    ``follow_redirects`` is ``False`` at the client level as well as per
    request: this path walks hops by hand so each new origin is re-checked
    against the allowlist, and a client-level default of ``True`` would make
    forgetting the per-request override silently unsafe rather than noisy.
    Timeouts are per request, from settings.
    """
    return httpx.AsyncClient(follow_redirects=False)


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
    """Decode, downscale to the cap, re-encode. ``None`` if the bytes are unusable.

    Runs in the private decode pool (Pillow is synchronous and CPU-bound). The
    cap is a *long-edge* bound rather than a square resize, so aspect ratio
    survives and the token cost is at most ``ceil(252/28)²`` = 81.

    **The order of operations here is load-bearing, not style.** Downscaling
    happens as early as each mode allows, so the full-resolution frame is either
    never decoded (JPEG, via ``draft``) or never copied (everything else):

    * ``draft`` first, so libjpeg decodes to a reduced size directly. It is a
      no-op for PNG and friends, which is why the pixel ceiling exists too.
    * palette and bilevel frames leave their mode *before* resampling, because
      Pillow silently forces ``NEAREST`` on modes ``P`` and ``1`` and a
      nearest-neighbour downscale of a product photo looks broken.
    * everything else is thumbnailed while still in its source mode, so the
      conversion and the white-flatten below operate on a 252 px frame instead
      of a full-size one.
    """
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.draft("RGB", (BASE64_EDGE_PX, BASE64_EDGE_PX))
            # Measured *after* the draft, which is the whole point: ``draft``
            # rewrites ``size`` to what libjpeg will actually hand back, so a
            # 6000 × 6000 JPEG reports the ~750 px frame it decodes to and sails
            # through, while a PNG of the same dimensions — where ``draft`` is a
            # no-op and the full frame really is allocated — is refused. Checking
            # the published dimensions instead would reject the cheap case along
            # with the expensive one. Nothing is decoded yet either way, so the
            # refusal costs nothing. ``max_bytes`` cannot do this job at all:
            # it bounds the wire, and memory scales with pixels.
            pixels = image.width * image.height
            if pixels > MAX_DECODE_PIXELS:
                _logger.debug("thumbnail_pixels_rejected", pixels=pixels, max_pixels=MAX_DECODE_PIXELS)
                return None
            frame = image.convert("RGBA") if image.mode in ("P", "1") else image
            frame.thumbnail((BASE64_EDGE_PX, BASE64_EDGE_PX), Image.LANCZOS)
            # A transparent image flattened onto white: JPEG has no alpha, and
            # Pillow's default conversion would render transparency black, which
            # turns a cut-out product photo into a silhouette.
            if frame.mode in ("RGBA", "LA"):
                rgba = frame.convert("RGBA")
                canvas = Image.new("RGB", rgba.size, (255, 255, 255))
                canvas.paste(rgba, mask=rgba.split()[-1])
                frame = canvas
            else:
                # ``convert`` always returns a new image, so the result outlives
                # the ``with`` block that closes the source.
                frame = frame.convert("RGB")
            buffer = io.BytesIO()
            frame.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        # ``DecompressionBombError`` is named explicitly because it subclasses
        # ``Exception`` directly — not ``OSError`` or ``ValueError`` — so the
        # obvious tuple does not catch it. Left out, a 68-byte crafted PNG
        # escapes this function, propagates through the gather below and drops
        # the *whole* batch instead of one image.
        return None
    return Thumbnail(data=base64.b64encode(buffer.getvalue()).decode("ascii"), mime_type=THUMBNAIL_MIME_TYPE)


async def _fetch_one(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
) -> Thumbnail | None:
    chunks: list[bytes] = []
    received = 0
    target = url
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            # Checked per hop, not once. An allowlist applied only to the URL we
            # were handed is defeated by the first ``Location:`` header — which
            # is exactly the kind of thing a supplier CDN is entitled to send —
            # so following redirects automatically would hand an allowlisted host
            # the power to point this process anywhere.
            if not is_allowed_origin(target):
                _logger.debug("thumbnail_origin_refused", url=target, source=url)
                return None
            # No credentials of any kind: these are public CDN objects, and the
            # caller's bearer token must never leave the AutoDS upstreams.
            #
            # Streamed rather than buffered so ``max_bytes`` can *stop* a
            # download instead of auditing one that already finished: reading
            # ``.content`` meant a hostile or misconfigured host had the whole
            # body resident before the ceiling was consulted, which made the
            # ceiling a report rather than a limit.
            async with client.stream(
                "GET",
                target,
                headers=_HEADERS,
                timeout=timeout_seconds,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    # Hops are walked by hand rather than by ``follow_redirects``
                    # so each new origin goes back through the check above. CDNs
                    # do use redirects for their size ladders, so refusing them
                    # outright (as the dispatcher does, because it *is* carrying
                    # a token) would cost real images.
                    location = response.headers.get("location")
                    if not location:
                        return None
                    target = urljoin(target, location)
                    continue
                if response.status_code != 200:
                    _logger.debug("thumbnail_fetch_status", url=target, status=response.status_code)
                    return None
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        _logger.debug("thumbnail_size_rejected", url=target, size=received, max_bytes=max_bytes)
                        return None
                    chunks.append(chunk)
                break
        else:
            _logger.debug("thumbnail_redirects_exhausted", url=url, hops=_MAX_REDIRECTS)
            return None
    except httpx.HTTPError as exc:
        _logger.debug("thumbnail_fetch_failed", url=target, error=str(exc))
        return None
    if not received:
        return None
    # Content-Type is not consulted on purpose: the AutoDS scraper bucket serves
    # genuine PNG bytes under ``image/jpeg``, and browsers sniff images anyway.
    # Pillow reading the bytes is the only check that means anything.
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_encoders(), functools.partial(_encode, b"".join(chunks)))


async def fetch_thumbnails(
    client: httpx.AsyncClient,
    urls: list[str],
    *,
    timeout_seconds: float,
    max_bytes: int,
    deadline_seconds: float,
) -> ThumbnailBatch:
    """Fetch and downscale up to :data:`~...images.MAX_IMAGES` of ``urls``.

    Order is preserved, so thumbnail *n* corresponds to item *n* of the
    ``images`` envelope block minus whatever failed — which is why the note
    exists rather than leaving the correspondence to be guessed at.

    ``deadline_seconds`` bounds the pass as a whole. It is one *absolute*
    moment shared by every image rather than a per-image timeout, because a
    per-image bound is what ``timeout_seconds`` already is and it does not add
    up to anything: a batch of twenty each finishing just inside their own
    timeout is still twenty timeouts long. Sharing the deadline also means the
    images that did land are kept — each one gives up on its own, the batch is
    never cancelled as a unit, and a partial set is exactly what the note
    downstream is for.
    """
    wanted = [url for url in urls if url][:MAX_IMAGES]
    if not wanted:
        return ThumbnailBatch(thumbnails=[], requested=0)

    semaphore = asyncio.Semaphore(_CONCURRENCY)
    deadline = asyncio.get_running_loop().time() + deadline_seconds

    async def bounded(url: str) -> Thumbnail | None:
        # The deadline wraps the semaphores rather than sitting inside them, so
        # it covers the time an image spends *queued* as well as the time it
        # spends transferring. Queueing is where a slow batch actually spends
        # itself, and a deadline that only started once a slot came free would
        # bound the one part that was never the problem.
        async with asyncio.timeout_at(deadline), semaphore, _fetches():
            return await _fetch_one(client, url, timeout_seconds=timeout_seconds, max_bytes=max_bytes)

    # ``return_exceptions`` so one bad frame costs one thumbnail. Without it a
    # single raise anywhere in the batch propagates out of this function, and the
    # bulk ``except`` in the transport turns it into *zero* thumbnails with no
    # note explaining why — the same silent-short-set failure RD-82 caught the
    # client committing, reproduced on our side.
    results = await asyncio.gather(*(bounded(url) for url in wanted), return_exceptions=True)
    thumbnails: list[Thumbnail] = []
    for url, result in zip(wanted, results, strict=True):
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                raise result  # cancellation and friends are not ours to swallow
            if isinstance(result, TimeoutError):
                # The shared deadline, not a fault: this image ran out of time
                # while the ones before it were still transferring. Logged on
                # its own level because it says "the batch was too slow", which
                # is a different thing to diagnose than "this image was bad".
                _logger.warning("thumbnail_deadline_exceeded", url=url, deadline_seconds=deadline_seconds)
                continue
            _logger.warning("thumbnail_encode_failed", url=url, error=str(result))
            continue
        if result is not None:
            thumbnails.append(result)
    return ThumbnailBatch(thumbnails=thumbnails, requested=len(wanted))
