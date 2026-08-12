"""Byte-level ESC/POS encoder for generic 58mm/80mm thermal printers.

Deliberately dependency-light: only the image block needs Pillow, and that import
is deferred so the service still runs without it.

Command references used here are the Epson ESC/POS set, which no-name Chinese
thermal printers clone closely enough for everything below. Where a command is
known to be spottily implemented it is called out in a comment.
"""

from __future__ import annotations

import unicodedata
from typing import Iterable

ESC = b"\x1b"
GS = b"\x1d"
DLE = b"\x10"
FS = b"\x1c"

# Python codec name -> argument for `ESC t n` (select character code table).
CODEPAGES: dict[str, int] = {
    "cp437": 0,
    "katakana": 1,
    "cp850": 2,
    "cp860": 3,
    "cp863": 4,
    "cp865": 5,
    "cp1252": 16,
    "cp866": 17,
    "cp852": 18,
    "cp858": 19,
}

# Last-resort replacements, consulted ONLY for characters the active codepage
# cannot represent.
#
# This must never be applied eagerly. cp858 has a real euro sign at 0xD5, and
# pre-emptively rewriting '€' to 'EUR' would defeat the entire reason for
# choosing that codepage. Same for '×', which cp850 and cp858 both carry.
_FALLBACK = {
    "—": "-",      # em dash
    "–": "-",      # en dash
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "…": "...",
    "•": "*",
    "×": "x",
    "→": "->",
    "←": "<-",
    " ": " ",      # nbsp
    "‑": "-",      # non-breaking hyphen
    "€": "EUR",
    "£": "GBP",
}

# Barcode symbologies for the `GS k m n d1..dn` (explicit-length) form.
BARCODE_TYPES: dict[str, int] = {
    "upca": 65,
    "upce": 66,
    "ean13": 67,
    "ean8": 68,
    "code39": 69,
    "itf": 70,
    "codabar": 71,
    "code93": 72,
    "code128": 73,
}

ALIGN = {"left": 0, "center": 1, "right": 2}
QR_ECC = {"L": 48, "M": 49, "Q": 50, "H": 51}

# Bitwise NOT over a byte string, used to flip PIL's 1=white raster to the
# printer's 1=black convention.
_INVERT = bytes(255 - i for i in range(256))


class EscposError(ValueError):
    """Raised for input a printer could not sensibly render."""


def encode_text(text: str, codepage: str) -> bytes:
    """Encode to the printer's active code page, degrading rather than failing.

    Fidelity first: if the whole string encodes cleanly it is passed through
    untouched. Only characters the codepage genuinely lacks are degraded, in
    order — an explicit replacement, then accent-stripping (NFKD), then '?'.

    A receipt reading 'Groenwoldt' where 'Grönwoldt' was meant beats a 500 at
    the till, but neither beats simply printing 'Grönwoldt' when the codepage
    can express it.
    """
    try:
        return text.encode(codepage)
    except UnicodeEncodeError:
        pass

    out = bytearray()
    for ch in text:
        try:
            out += ch.encode(codepage)
            continue
        except UnicodeEncodeError:
            pass

        replacement = _FALLBACK.get(ch)
        if replacement is not None:
            try:
                out += replacement.encode(codepage)
                continue
            except UnicodeEncodeError:
                pass

        folded = "".join(
            c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)
        )
        try:
            out += folded.encode(codepage)
        except UnicodeEncodeError:
            out += b"?"
    return bytes(out)


class EscposBuilder:
    """Accumulates ESC/POS bytes for one print job.

    Every style method mutates printer state, so the builder tracks nothing and
    callers are expected to reset what they set (or call `reset_styles()`).
    """

    def __init__(self, width_chars: int = 32, dots: int = 384, codepage: str = "cp858"):
        if codepage not in CODEPAGES:
            raise EscposError(
                f"unsupported codepage {codepage!r}; known: {', '.join(sorted(CODEPAGES))}"
            )
        self.width_chars = width_chars
        self.dots = dots
        self.codepage = codepage
        self._buf = bytearray()

    # -- plumbing ---------------------------------------------------------

    def raw(self, data: bytes) -> "EscposBuilder":
        self._buf += data
        return self

    def bytes(self) -> bytes:
        return bytes(self._buf)

    def __len__(self) -> int:
        return len(self._buf)

    # -- setup ------------------------------------------------------------

    def init(self) -> "EscposBuilder":
        """ESC @ - reset to power-on defaults, then select the code page."""
        self.raw(ESC + b"@")
        self.raw(ESC + b"t" + bytes([CODEPAGES[self.codepage]]))
        # Some clones need the international charset pinned to USA explicitly,
        # otherwise they substitute # / $ / @ with locale glyphs.
        self.raw(ESC + b"R" + b"\x00")
        return self

    def reset_styles(self) -> "EscposBuilder":
        return self.bold(False).underline(0).size(1, 1).align("left").invert(False)

    # -- styling ----------------------------------------------------------

    def align(self, mode: str) -> "EscposBuilder":
        if mode not in ALIGN:
            raise EscposError(f"align must be one of {sorted(ALIGN)}, got {mode!r}")
        return self.raw(ESC + b"a" + bytes([ALIGN[mode]]))

    def bold(self, on: bool = True) -> "EscposBuilder":
        return self.raw(ESC + b"E" + bytes([1 if on else 0]))

    def underline(self, weight: int = 1) -> "EscposBuilder":
        if weight not in (0, 1, 2):
            raise EscposError("underline weight must be 0, 1 or 2")
        return self.raw(ESC + b"-" + bytes([weight]))

    def invert(self, on: bool = True) -> "EscposBuilder":
        """White-on-black. GS B is widely but not universally supported."""
        return self.raw(GS + b"B" + bytes([1 if on else 0]))

    def size(self, width: int = 1, height: int = 1) -> "EscposBuilder":
        """GS ! - magnification, 1..8 in each axis packed into one byte."""
        if not (1 <= width <= 8 and 1 <= height <= 8):
            raise EscposError("size multipliers must be between 1 and 8")
        return self.raw(GS + b"!" + bytes([((width - 1) << 4) | (height - 1)]))

    def font(self, which: str = "a") -> "EscposBuilder":
        """Font B is ~1.4x narrower; useful to fit 48 cols on 58mm paper."""
        if which.lower() not in ("a", "b"):
            raise EscposError("font must be 'a' or 'b'")
        return self.raw(ESC + b"M" + bytes([0 if which.lower() == "a" else 1]))

    # -- content ----------------------------------------------------------

    def text(self, text: str, newline: bool = True) -> "EscposBuilder":
        # Normalise line endings; a bare \n is what the printer wants.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.raw(encode_text(text, self.codepage))
        if newline:
            self.raw(b"\n")
        return self

    def feed(self, lines: int = 1) -> "EscposBuilder":
        if not (0 <= lines <= 255):
            raise EscposError("feed lines must be 0..255")
        return self.raw(ESC + b"d" + bytes([lines]))

    def rule(self, char: str = "-", width: int | None = None) -> "EscposBuilder":
        return self.text((char * (width or self.width_chars))[: width or self.width_chars])

    def columns(self, left: str, right: str, width: int | None = None) -> "EscposBuilder":
        """Left text with `right` flushed to the right margin.

        If the pair does not fit, the left side is truncated with an ellipsis so
        the right side (usually a price) always survives intact.
        """
        width = width or self.width_chars
        right = right.strip()
        if len(right) >= width:
            return self.text(right[:width])
        room = width - len(right) - 1
        left = left.strip()
        if len(left) > room:
            # Plain truncation, no ellipsis character: '…' would be transliterated
            # to '...' during encoding and silently push the line over the width.
            left = left[:room]
        return self.text(f"{left}{' ' * (width - len(left) - len(right))}{right}")

    def cut(self, partial: bool = True, feed_before: int = 4) -> "EscposBuilder":
        """GS V 66 n - feed n dots past the head, then cut.

        Feeding first is mandatory: the cutter sits ~15mm above the print head,
        so cutting without a feed slices through the last lines of the receipt.
        """
        self.feed(feed_before)
        return self.raw(GS + b"V" + bytes([66 if partial else 65, 0]))

    def drawer_kick(self, pin: int = 0, on_ms: int = 100, off_ms: int = 200) -> "EscposBuilder":
        """ESC p m t1 t2 - pulse the cash drawer solenoid. Times are in 2ms units."""
        if pin not in (0, 1):
            raise EscposError("drawer pin must be 0 (pin 2) or 1 (pin 5)")
        t1 = max(0, min(255, on_ms // 2))
        t2 = max(0, min(255, off_ms // 2))
        return self.raw(ESC + b"p" + bytes([pin, t1, t2]))

    def barcode(
        self,
        data: str,
        symbology: str = "code128",
        height: int = 64,
        width: int = 3,
        hri: str = "below",
    ) -> "EscposBuilder":
        symbology = symbology.lower()
        if symbology not in BARCODE_TYPES:
            raise EscposError(
                f"unknown barcode type {symbology!r}; known: {', '.join(sorted(BARCODE_TYPES))}"
            )
        if not (1 <= height <= 255):
            raise EscposError("barcode height must be 1..255 dots")
        if not (2 <= width <= 6):
            raise EscposError("barcode module width must be 2..6")

        hri_map = {"none": 0, "above": 1, "below": 2, "both": 3}
        if hri not in hri_map:
            raise EscposError(f"hri must be one of {sorted(hri_map)}")

        payload = data
        if symbology == "code128" and not payload.startswith("{"):
            # CODE128 requires an explicit code set; B covers printable ASCII.
            payload = "{B" + payload

        encoded = payload.encode("ascii", errors="strict") if payload.isascii() else None
        if encoded is None:
            raise EscposError("barcode data must be ASCII")
        if len(encoded) > 255:
            raise EscposError("barcode data too long (max 255 bytes)")

        self.raw(GS + b"H" + bytes([hri_map[hri]]))
        self.raw(GS + b"f" + b"\x00")          # HRI font A
        self.raw(GS + b"h" + bytes([height]))
        self.raw(GS + b"w" + bytes([width]))
        self.raw(GS + b"k" + bytes([BARCODE_TYPES[symbology], len(encoded)]) + encoded)
        return self

    def qr(self, data: str, size: int = 6, ecc: str = "M") -> "EscposBuilder":
        """GS ( k - native QR. Falls to the caller to use an image if unsupported."""
        if not (1 <= size <= 16):
            raise EscposError("qr size must be 1..16")
        if ecc.upper() not in QR_ECC:
            raise EscposError("qr ecc must be L, M, Q or H")

        payload = data.encode("utf-8")
        if len(payload) > 7089:
            raise EscposError("qr payload exceeds QR capacity")

        def fn(body: bytes) -> bytes:
            length = len(body)
            return GS + b"(k" + bytes([length & 0xFF, (length >> 8) & 0xFF]) + body

        self.raw(fn(b"\x31\x41\x32\x00"))                        # model 2
        self.raw(fn(bytes([0x31, 0x43, size])))                  # module size
        self.raw(fn(bytes([0x31, 0x45, QR_ECC[ecc.upper()]])))   # error correction
        self.raw(fn(b"\x31\x50\x30" + payload))                  # store in symbol buffer
        self.raw(fn(b"\x31\x51\x30"))                            # print symbol buffer
        return self

    def image(self, data: bytes, max_width: int | None = None, dither: bool = True) -> "EscposBuilder":
        """GS v 0 - raster bit image. Requires Pillow.

        The image is scaled to the paper width (never up-scaled beyond it) and
        reduced to 1bpp; Floyd-Steinberg by default since photos otherwise come
        out as solid black blocks on a 203dpi head.
        """
        try:
            from PIL import Image  # noqa: PLC0415 - optional dependency
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise EscposError(
                "image printing requires Pillow (pip install pillow)"
            ) from exc

        import io

        target = min(max_width or self.dots, self.dots)
        img = Image.open(io.BytesIO(data))

        # Flatten transparency onto white, otherwise alpha becomes black.
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            canvas = Image.new("RGBA", img.size, (255, 255, 255, 255))
            canvas.alpha_composite(img)
            img = canvas.convert("L")
        else:
            img = img.convert("L")

        if img.width != target:
            height = max(1, round(img.height * target / img.width))
            img = img.resize((target, height), Image.LANCZOS)

        img = img.convert("1", dither=Image.FLOYDSTEINBERG if dither else Image.NONE)

        width_bytes = (img.width + 7) // 8

        # PIL mode "1" already packs 8 pixels per byte, MSB first, rows padded to
        # a byte boundary - but with 1=white, and the printer wants 1=black. So
        # invert wholesale, which is ~100x faster than a per-pixel Python loop.
        packed = bytearray(img.tobytes().translate(_INVERT))

        # The pad bits PIL emits are 0 (white); inverting made them 1 (black),
        # which would print a black stripe down the right edge. Mask them off.
        # Only bites when max_width isn't a multiple of 8.
        pad = width_bytes * 8 - img.width
        if pad:
            mask = (0xFF << pad) & 0xFF
            for row in range(img.height):
                packed[(row + 1) * width_bytes - 1] &= mask

        header = GS + b"v0" + b"\x00" + bytes(
            [
                width_bytes & 0xFF,
                (width_bytes >> 8) & 0xFF,
                img.height & 0xFF,
                (img.height >> 8) & 0xFF,
            ]
        )
        return self.raw(header + bytes(packed))

    # -- convenience ------------------------------------------------------

    def wrapped(self, text: str, width: int | None = None) -> "EscposBuilder":
        """Word-wrap to the column width. The printer hard-wraps mid-word otherwise."""
        import textwrap

        width = width or self.width_chars
        for para in text.replace("\r\n", "\n").split("\n"):
            if not para.strip():
                self.text("")
                continue
            for line in textwrap.wrap(para, width=width) or [""]:
                self.text(line)
        return self


def status_page(builder: EscposBuilder, info: Iterable[tuple[str, str]]) -> EscposBuilder:
    """Self-test page: proves paper width, styles, codepage and codes all work."""
    b = builder
    b.init()
    b.align("center").size(2, 2).bold(True).text("POSPRINT").reset_styles()
    b.align("center").text("self test").feed(1)

    b.align("left")
    b.rule("=")
    b.text(f"width: {b.width_chars} cols / {b.dots} dots")
    b.text("ruler (should end flush):")
    ruler = "".join(str(i % 10) for i in range(1, b.width_chars + 1))
    b.text(ruler)
    b.rule("=")

    b.bold(True).text("bold").bold(False)
    b.underline(1).text("underline").underline(0)
    b.size(2, 1).text("double wide").size(1, 1)
    b.size(1, 2).text("double tall").size(1, 1)
    b.align("center").text("centered").align("right").text("right").align("left")
    b.rule("-")

    b.text(f"codepage: {b.codepage}")
    b.text("accents: e a o u n c ss")
    b.text("é à ö ü ñ ç ß € £")
    b.rule("-")

    for key, value in info:
        b.columns(key, value)
    b.rule("-")

    b.align("center")
    b.barcode("POSPRINT123", "code128", height=60, width=2)
    b.feed(1)
    b.qr("https://github.com/", size=5)
    b.feed(1)
    b.align("left")
    b.cut()
    return b
