"""A visual puzzle: the fast lane past a siege.

Read this first, because the framing decides whether the thing is worth having.

**No captcha is human-only.** Vision models solve image grids at roughly human
accuracy, OCR reads distorted text, speech recognition handles the audio
fallbacks, and anything machines genuinely cannot do a solving farm will do for
a fraction of a cent. Every commercial captcha is a detection heuristic wearing
a puzzle costume, and the ones that lean on IP reputation are answering exactly
the question a residential proxy pool exists to launder.

So this is not sold as a wall. What it actually buys here is two things:

**Obscurity, which is real if unglamorous.** A bespoke puzzle on one person's
printer has no existing solver. Writing one costs an attacker an afternoon for
a target whose payoff is amusement. That is a genuine barrier, just not a
permanent one, and it is the one advantage a small site has that a big one
cannot buy.

**A fast lane, which is the actual point.** Without it, a siege makes everyone
wait for the owner. With it, someone who solves the puzzle prints immediately
and everyone else queues as before. Nobody is locked out - which is also how
this stays usable for anyone who cannot see the picture, since failing it is
not refusal, only the ordinary wait.

The token construction is the same trick as challenge.py: the signature covers
the answer, so the server can *check* a submitted answer without ever storing
or revealing the right one. Stateless apart from spent nonces.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import logging
import os
import random
import secrets
import time

log = logging.getLogger("posprintweb.captcha")

TILES = 6
_COLS = 3

# Deliberately not a text captcha. Distorted letters are the single most
# thoroughly solved variety, and they are miserable to read besides.
_SHAPES = ("circle", "square", "triangle", "diamond")
_COLOURS = (
    (198, 76, 58), (58, 122, 198), (72, 158, 92),
    (176, 122, 44), (128, 84, 176), (60, 60, 66),
)


class BadCaptcha(Exception):
    """Wrong, expired, forged, or already used. The sender is told none of
    which - every variant means the same thing to an honest page, which asks
    for another puzzle."""


def _key() -> bytes:
    configured = os.environ.get("POSPRINTWEB_CAPTCHA_SECRET", "").strip()
    if configured:
        return configured.encode()
    return secrets.token_bytes(32)


_KEY = _key()


def _sign(nonce: str, issued: int, answer: int) -> str:
    """The signature covers the answer, which is what makes this stateless.

    Nothing stores the right answer and nothing transmits it. Verification
    recomputes the signature with whatever the sender claims: it matches only
    if the claim is correct.
    """
    payload = f"{nonce}.{issued}.{answer}".encode()
    return hmac.new(_KEY, payload, hashlib.sha256).hexdigest()[:32]


def _draw(draw, shape, box, colour, rotation: float) -> None:
    from PIL import ImageDraw  # noqa: F401  (typing only)

    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    r = (x1 - x0) / 2

    if shape == "circle":
        draw.ellipse(box, fill=colour)
        return

    if shape == "square":
        points = [(-r, -r), (r, -r), (r, r), (-r, r)]
    elif shape == "triangle":
        points = [(0, -r), (r, r * 0.8), (-r, r * 0.8)]
    else:                                   # diamond
        points = [(0, -r), (r, 0), (0, r), (-r, 0)]

    import math

    cos, sin = math.cos(rotation), math.sin(rotation)
    draw.polygon(
        [(cx + px * cos - py * sin, cy + px * sin + py * cos) for px, py in points],
        fill=colour,
    )


def render(answer: int, seed: int, tile: int = 96) -> bytes:
    """The puzzle image: one tile differs, the rest match.

    Determined entirely by (answer, seed) so it never has to be stored - the
    token carries both, and the picture is regenerated on demand.
    """
    from PIL import Image, ImageDraw

    rng = random.Random(seed)
    rows = (TILES + _COLS - 1) // _COLS
    pad = 10
    width = _COLS * tile + pad * (_COLS + 1)
    height = rows * tile + pad * (rows + 1)

    img = Image.new("RGB", (width, height), (247, 245, 241))
    draw = ImageDraw.Draw(img)

    shape = rng.choice(_SHAPES)
    colour = rng.choice(_COLOURS)
    # Exactly one property differs, so the odd one out is unambiguous rather
    # than a matter of taste. A puzzle a person can argue with is a puzzle they
    # will fail.
    if rng.random() < 0.5:
        odd_shape = rng.choice([s for s in _SHAPES if s != shape])
        odd_colour = colour
    else:
        odd_shape = shape
        odd_colour = rng.choice([c for c in _COLOURS if c != colour])

    for i in range(TILES):
        col, row = i % _COLS, i // _COLS
        x = pad + col * (tile + pad)
        y = pad + row * (tile + pad)
        inset = tile * 0.16
        box = (x + inset, y + inset, x + tile - inset, y + tile - inset)
        # Every tile is rotated differently, so the odd one cannot be found by
        # looking for the tile that is not identical to its neighbours.
        rotation = rng.uniform(0, 6.283)
        if i == answer:
            _draw(draw, odd_shape, box, odd_colour, rotation)
        else:
            _draw(draw, shape, box, colour, rotation)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


class Captchas:
    def __init__(self, ttl: float = 300.0) -> None:
        self.ttl = ttl
        self._spent: dict[str, float] = {}

    def issue(self, now: float | None = None) -> dict:
        """A puzzle, as a data URI so the page needs no second request."""
        now = time.time() if now is None else now
        self._prune(now)

        answer = secrets.randbelow(TILES)
        seed = secrets.randbelow(2**31)
        nonce = secrets.token_hex(12)
        issued = int(now)
        token = f"{nonce}.{issued}.{seed}.{_sign(nonce, issued, answer)}"

        png = render(answer, seed)
        return {
            "token": token,
            "tiles": TILES,
            "columns": _COLS,
            "image": "data:image/png;base64," + base64.b64encode(png).decode(),
            "expires_in": int(self.ttl),
        }

    def redeem(self, token: str, answer: int, now: float | None = None) -> None:
        """Check one attempt and spend the puzzle, or raise BadCaptcha.

        Spent whether the answer was right or wrong. Otherwise one puzzle could
        be guessed six times, which at six tiles is not a captcha at all.
        """
        now = time.time() if now is None else now
        self._prune(now)

        parts = token.split(".") if token else []
        if len(parts) != 4:
            raise BadCaptcha("malformed")
        nonce, issued_raw, _seed, sig = parts

        try:
            issued = int(issued_raw)
        except ValueError:
            raise BadCaptcha("malformed") from None

        if nonce in self._spent:
            raise BadCaptcha("already used")
        if not (-60 <= now - issued <= self.ttl):
            raise BadCaptcha("expired")
        if not isinstance(answer, int) or not 0 <= answer < TILES:
            raise BadCaptcha("out of range")

        # One attempt per puzzle, right or wrong.
        self._spent[nonce] = now

        if not hmac.compare_digest(sig, _sign(nonce, issued, answer)):
            raise BadCaptcha("wrong")

    def _prune(self, now: float) -> None:
        if len(self._spent) < 32:
            return
        cutoff = now - self.ttl
        for nonce in [n for n, at in self._spent.items() if at < cutoff]:
            del self._spent[nonce]

    @property
    def outstanding(self) -> int:
        return len(self._spent)
