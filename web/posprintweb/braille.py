"""Braille art: the one thing this printer renders as a picture, not as text.

U+2800-U+28FF has no glyph in any ESC/POS code page, so braille art sent as
characters comes out as a strip of question marks. filters.py refuses it for
exactly that reason, alongside Korean and emoji.

But braille art is not really text. Every character in the block encodes a 2x4
grid of dots, so a W x H grid of them *is* a 2W x 4H bitmap wearing a costume.
Decoding it recovers the picture exactly - nothing dithered, nothing
approximated - and the printer can render that as graphics.

Two things make this its own module rather than a branch inside filters.py:

- The paper cost is shaped differently. A 500-character text message is about
  40mm of paper no matter how it is arranged. 500 braille cells might be 72
  wide and 7 tall, or 8 wide and 62 tall, and those cost wildly different
  amounts of roll. So the limits here are a grid and a height in dots, not a
  character count.
- Nothing else in the public surface produces bytes for the printer. Keeping
  that in one small file makes it obvious that the only thing a visitor
  influences is the contents of a bitmap.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from PIL import Image

from .filters import Rejected

# Bit -> (column, row) within the 2x4 cell. Dots 7 and 8 (0x40, 0x80) are the
# bottom row; they were added to the standard later, which is why they sit out
# of sequence rather than following 0x20.
DOTS = {
    0x01: (0, 0), 0x08: (1, 0),
    0x02: (0, 1), 0x10: (1, 1),
    0x04: (0, 2), 0x20: (1, 2),
    0x40: (0, 3), 0x80: (1, 3),
}

BASE = 0x2800
BRAILLE = re.compile(r"[⠀-⣿]")

# Whitespace is tolerated inside art because people pad with ordinary spaces.
# Anything else alongside braille is refused rather than silently blanked: a
# caption cannot be drawn as dots, and printing half the message is worse than
# saying so.
_ALLOWED_ALONGSIDE = re.compile(r"[⠀-⣿\s]")


@dataclass(frozen=True)
class Art:
    """A validated, rendered piece of braille art."""

    text: str          # the cleaned braille, as typed - what gets logged
    png: bytes
    cols: int
    rows: int
    scale: int
    ink: float         # fraction of dots that are black, 0.0 - 1.0

    @property
    def height_dots(self) -> int:
        return self.rows * 4 * self.scale

    @property
    def height_mm(self) -> float:
        return self.height_dots / 203 * 25.4   # 203dpi head


def contains(text: str) -> bool:
    return bool(BRAILLE.search(text))


def _grid(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return lines


def decode(lines: list[str]) -> Image.Image:
    """Braille grid -> 1-bit image, two pixels wide and four tall per cell."""
    width = max(len(ln) for ln in lines)
    img = Image.new("1", (width * 2, len(lines) * 4), 1)   # 1 = white
    px = img.load()
    for row, line in enumerate(lines):
        for col, ch in enumerate(line):
            bits = ord(ch) - BASE
            if not 0 <= bits <= 0xFF:
                continue                                    # padding space
            for bit, (dx, dy) in DOTS.items():
                if bits & bit:
                    px[col * 2 + dx, row * 4 + dy] = 0       # 0 = black
    return img


def ink_fraction(lines: list[str]) -> float:
    """How much of the picture is black, 0.0 to 1.0.

    Each braille character carries eight dots, so this is just the set bits
    over the total bits - the same number as black pixels over total pixels,
    without building the image.

    It matters because a thermal head makes a dot by heating an element, and a
    solid black area holds every element on at once. The printer survives it -
    they throttle rather than burn - but it prints slowly, drains the roll's
    contrast and, for a public endpoint, is the cheapest way to make the
    machine work hard. Line art is around 12%; a dithered photograph runs 30-50%;
    a solid rectangle is 100%, which is the thing worth refusing.
    """
    dots = 0
    cells = 0
    for line in lines:
        for ch in line:
            bits = ord(ch) - BASE
            if 0 <= bits <= 0xFF:
                dots += bin(bits).count("1")
                cells += 1
    return dots / (cells * 8) if cells else 0.0


def scale_for(cols: int, rows: int, *, printer_dots: int, max_scale: int,
              max_dots: int) -> int:
    """Whole-number enlargement only.

    Stretching to fill the head exactly would make some dots four pixels across
    and others five. On a 1-bit image that reads as a texture crawling through
    the picture, so a smaller uniform scale beats a larger ragged one.

    Shared with the page's estimate; keep the two in step.
    """
    scale = min(printer_dots // (cols * 2), max_scale)
    if rows * 4 * scale > max_dots:
        scale = max_dots // (rows * 4)
    return max(1, scale)


def prepare(text: str, *, max_cols: int, max_rows: int, printer_dots: int,
            max_scale: int, max_dots: int, max_ink: float = 1.0) -> Art:
    """Validate and render, or raise Rejected with something a visitor can act on."""
    stray = {ch for ch in text if not _ALLOWED_ALONGSIDE.match(ch)}
    if stray:
        shown = " ".join(sorted(stray)[:6])
        raise Rejected(
            f"Braille art has to be on its own - the printer draws it as a "
            f"picture and cannot mix text into it. Remove: {shown}"
        )

    lines = _grid(text)
    if not lines:
        raise Rejected("Nothing to print.")

    rows = len(lines)
    cols = max(len(ln) for ln in lines)
    if cols > max_cols:
        raise Rejected(
            f"That art is {cols} characters wide; the limit is {max_cols}."
        )
    if rows > max_rows:
        raise Rejected(f"That art is {rows} lines tall; the limit is {max_rows}.")

    ink = ink_fraction(lines)
    if ink > max_ink:
        raise Rejected(
            f"That picture is {ink * 100:.0f}% solid black and the limit is "
            f"{max_ink * 100:.0f}%. A thermal printer makes black by heating "
            f"the paper, so a filled-in image prints slowly and runs hot. Try "
            f"something more like line art."
        )

    scale = scale_for(cols, rows, printer_dots=printer_dots,
                      max_scale=max_scale, max_dots=max_dots)
    img = decode(lines)
    img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Art(text="\n".join(lines), png=buf.getvalue(), cols=cols, rows=rows,
               scale=scale, ink=ink)
