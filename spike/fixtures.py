"""RD-82 — synthetic probe images whose content cannot be guessed.

Shared by ``probe_server.py`` (Phase 1, stdio) and ``probe_extension.py``
(Phase 2, staging) so both phases measure the same thing.

Design constraint learned the hard way: the first probe drew an index digit and
``index+1`` bars, and the tool description *said so*. Claude then described the
image perfectly without necessarily having seen it — the answer was derivable
from the description plus the arguments, so the run proved nothing about whether
pixels arrived.

Every fixture therefore carries a deterministic 4-digit code derived from
``sha256(size:index)``. The code appears **only in the pixels** — never in the
tool description, never in the text envelope. A model that reads it back
correctly must have received the image. Run ``python spike/fixtures.py`` to
print the expected codes and check the answers.

Digits are drawn as seven-segment glyphs rather than text so no font file is
needed and they stay legible at any size.
"""

import hashlib
import io
import random

from PIL import Image, ImageDraw

SIZES = (252, 384, 1200)  # 252 = 9x9 patches exactly; 1200 = 43x43 = 1849 visual tokens

# Colour is a second, independent channel: also absent from the description.
PALETTE = [
    ("red", (200, 55, 55)),
    ("blue", (45, 110, 200)),
    ("green", (50, 150, 90)),
    ("amber", (225, 160, 40)),
    ("purple", (135, 75, 185)),
]

# Seven-segment layout: which of segments a..g is lit for each digit.
_SEGMENTS = {
    "0": "abcdef",
    "1": "bc",
    "2": "abdeg",
    "3": "abcdg",
    "4": "bcfg",
    "5": "acdfg",
    "6": "acdefg",
    "7": "abc",
    "8": "abcdefg",
    "9": "abcdfg",
}


def code_for(size: int, index: int) -> str:
    """The 4-digit code baked into fixture ``(size, index)``. Pixels only."""
    return str(int(hashlib.sha256(f"{size}:{index}".encode()).hexdigest()[:6], 16) % 10000).zfill(4)


def colour_for(index: int) -> tuple[str, tuple[int, int, int]]:
    return PALETTE[index % len(PALETTE)]


def _draw_digit(draw: ImageDraw.ImageDraw, digit: str, x: float, y: float, w: float, h: float, t: float) -> None:
    """Seven-segment digit in a w x h box at (x, y); t is stroke thickness."""
    lit = _SEGMENTS[digit]
    bar = (255, 255, 255)
    mid = y + h / 2
    if "a" in lit:
        draw.rectangle([x, y, x + w, y + t], fill=bar)
    if "b" in lit:
        draw.rectangle([x + w - t, y, x + w, mid], fill=bar)
    if "c" in lit:
        draw.rectangle([x + w - t, mid, x + w, y + h], fill=bar)
    if "d" in lit:
        draw.rectangle([x, y + h - t, x + w, y + h], fill=bar)
    if "e" in lit:
        draw.rectangle([x, mid, x + t, y + h], fill=bar)
    if "f" in lit:
        draw.rectangle([x, y, x + t, mid], fill=bar)
    if "g" in lit:
        draw.rectangle([x, mid - t / 2, x + w, mid + t / 2], fill=bar)


def build(size: int, index: int, noisy: bool = False) -> bytes:
    """A JPEG carrying the (size, index) code in large seven-segment digits.

    ``noisy=True`` fills the background with deterministic random pixels instead
    of flat colour. Identical dimensions — so identical visual-token cost — but
    roughly an order of magnitude more bytes, because noise defeats JPEG. That
    separates a byte-based tool-result ceiling from a token-based one: if the
    cutoff count stays put, the limit counts pixels; if it drops, it counts bytes.
    """
    _, rgb = colour_for(index)
    if noisy:
        rnd = random.Random(f"{size}:{index}")  # noqa: S311 - test fixture, not crypto
        img = Image.new("RGB", (size, size))
        img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(size * size)])
    else:
        img = Image.new("RGB", (size, size), rgb)
    draw = ImageDraw.Draw(img)

    border = max(2, size // 50)
    draw.rectangle([border, border, size - border, size - border], outline=(255, 255, 255), width=border)

    code = code_for(size, index)
    gap = size * 0.04
    digit_w = (size * 0.72 - 3 * gap) / 4
    digit_h = size * 0.34
    stroke = max(2, int(digit_w * 0.18))
    left = size * 0.14
    top = (size - digit_h) / 2
    for i, ch in enumerate(code):
        _draw_digit(draw, ch, left + i * (digit_w + gap), top, digit_w, digit_h, stroke)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


if __name__ == "__main__":
    print("Expected fixture contents — check the model's answers against this.\n")
    print(f"{'size':>6} {'index':>6} {'code':>6}  colour")
    for s in SIZES:
        for i in range(5):
            print(f"{s:>6} {i:>6} {code_for(s, i):>6}  {colour_for(i)[0]}")
