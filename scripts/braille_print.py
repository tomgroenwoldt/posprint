#!/usr/bin/env python3
"""Print Unicode braille art by decoding it back into a bitmap.

Braille art is not text as far as this printer is concerned. The ESC/POS code
pages have no glyphs in U+2800-U+28FF, so sending it as characters produces a
strip of question marks - which is why posprint-web refuses it outright.

But braille art is not really text to begin with. Every character in the block
encodes a 2x4 grid of dots, so a W x H grid of them *is* a 2W x 4H bitmap with
a funny encoding. Decode it and you get the original picture back exactly, with
no dithering and nothing approximated, and the printer renders it as graphics.

    python3 braille_print.py art.txt                  # print it
    python3 braille_print.py art.txt -o out.png       # just write the PNG
    python3 braille_print.py art.txt --scale 3        # force a scale factor

Run it anywhere that can reach posprint; --url and --key default to a local
service reading /etc/posprint.env.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import urllib.request
import uuid

from PIL import Image

# Bit -> (column, row) inside the 2x4 cell. Note dots 7 and 8 (0x40, 0x80) are
# the bottom row and were bolted onto the standard later, which is why they sit
# out of sequence rather than following 0x20.
DOTS = {
    0x01: (0, 0), 0x08: (1, 0),
    0x02: (0, 1), 0x10: (1, 1),
    0x04: (0, 2), 0x20: (1, 2),
    0x40: (0, 3), 0x80: (1, 3),
}

BRAILLE_BASE = 0x2800
PRINTER_DOTS = 576          # 80mm head; override with --width for 58mm (384)


def decode(text: str) -> Image.Image:
    """Braille grid -> 1-bit image, two pixels wide and four tall per cell."""
    lines = [ln for ln in text.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise SystemExit("nothing to print: the file is empty")

    width = max(len(ln) for ln in lines)
    img = Image.new("1", (width * 2, len(lines) * 4), 1)   # 1 = white
    px = img.load()

    stray = set()
    for row, line in enumerate(lines):
        for col, ch in enumerate(line):
            code = ord(ch)
            if not (BRAILLE_BASE <= code <= BRAILLE_BASE + 0xFF):
                # Anything else - stray ASCII pasted into the art, a space used
                # as padding - is simply left blank rather than guessed at.
                if not ch.isspace():
                    stray.add(ch)
                continue
            bits = code - BRAILLE_BASE
            for bit, (dx, dy) in DOTS.items():
                if bits & bit:
                    px[col * 2 + dx, row * 4 + dy] = 0      # 0 = black
    if stray:
        print(f"note: ignored {len(stray)} non-braille character(s): "
              f"{''.join(sorted(stray))[:40]}", file=sys.stderr)
    return img


def upscale(img: Image.Image, target: int, scale: int | None) -> Image.Image:
    """Enlarge by a whole number of pixels.

    Integer scaling with NEAREST is the point: every dot stays exactly the same
    size. Scaling by 4.43 to fill the head precisely would make some dots four
    pixels across and others five, and on a 1-bit image that reads as a texture
    crawling through the picture.
    """
    if scale is None:
        scale = max(1, target // img.width)
    return img.resize((img.width * scale, img.height * scale), Image.NEAREST)


def api_key(explicit: str) -> str:
    if explicit:
        return explicit
    try:
        with open("/etc/posprint.env", encoding="utf-8") as fh:
            found = re.search(r"^POSPRINT_API_KEY=(.*)$", fh.read(), re.M)
            return found.group(1).strip() if found else ""
    except OSError:
        return ""


def post(img: Image.Image, url: str, key: str, timeout: float) -> None:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    payload = buf.getvalue()

    boundary = uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"dither\"\r\n\r\nfalse\r\n",
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"timeout\"\r\n\r\n{timeout}\r\n",
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"braille.png\"\r\nContent-Type: image/png\r\n\r\n",
    ]
    body = b"".join(p.encode() for p in parts) + payload + f"\r\n--{boundary}--\r\n".encode()

    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(url.rstrip("/") + "/print/image", data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout + 15) as resp:
            print(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"posprint returned {exc.code}: {exc.read().decode()[:400]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="UTF-8 text file of braille art, or - for stdin")
    ap.add_argument("-o", "--out", help="write a PNG here instead of printing")
    ap.add_argument("--url", default=os.environ.get("POSPRINT_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--key", default=os.environ.get("POSPRINT_API_KEY", ""))
    ap.add_argument("--width", type=int, default=PRINTER_DOTS,
                    help="printer head width in dots (default 576; use 384 for 58mm)")
    ap.add_argument("--scale", type=int, help="force an integer scale factor")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    raw = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()

    img = decode(raw)
    print(f"decoded {img.width}x{img.height} px", file=sys.stderr)
    img = upscale(img, args.width, args.scale)
    print(f"printing at {img.width}x{img.height} px "
          f"({img.height / 203 * 25.4:.0f}mm of paper)", file=sys.stderr)

    if img.width > args.width:
        print(f"warning: {img.width}px is wider than the {args.width}px head; "
              f"posprint will scale it down and dots will blur", file=sys.stderr)

    if args.out:
        img.save(args.out)
        print(f"wrote {args.out}", file=sys.stderr)
        return
    post(img, args.url, api_key(args.key), args.timeout)


if __name__ == "__main__":
    main()
