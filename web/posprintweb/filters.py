"""Input sanitising and screening for untrusted, internet-facing text."""

from __future__ import annotations

import re
import unicodedata

# Newline and tab survive; everything else in C0/C1 does not.
#
# This is a security control, not tidiness. The message ends up inside an
# ESC/POS text block, and 0x1B is the ESC byte itself — a visitor who could get
# a raw 0x1B through would be issuing printer commands: change the codepage,
# kick the cash drawer, feed the whole roll onto the floor. Strip the control
# range and that entire class of injection disappears.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# Zero-width and bidi-override characters. Invisible on screen, useless on a
# thermal printer, and a standard trick for slipping text past a blocklist.
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")

_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)
_MANY_BLANKS = re.compile(r"\n{4,}")

# A run this long is never real text; it is someone trying to eat the roll.
_FLOOD_RUN = 30

# The printer has one 8-bit code page and no font outside it. posprint degrades
# on the way out rather than failing — 'é' becomes 'e', '—' becomes '-' — but a
# character it cannot map at all becomes '?'. A Korean message therefore prints
# as a strip of question marks: the visitor spent their print, I get nothing
# readable, and the paper is gone either way.
#
# So it is refused here instead, before a reservation is taken, with the
# offending characters named.
#
# This mirrors posprint.escpos._FALLBACK rather than importing it. The two
# services deploy to separate containers and this one has no posprint checkout
# on disk; test_fallbacks_match_the_printer holds the copies together.
FALLBACK = {
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
    " ": " ",  # nbsp
    "‑": "-",  # non-breaking hyphen
    "€": "EUR",
    "£": "GBP",
}


def printable_charset(codepage: str) -> str:
    """Every character the code page can express, as one string.

    Derived from the codec rather than written out by hand, so it stays correct
    when POSPRINTWEB_CODEPAGE changes. The page fetches this and uses it to
    show what will really come out of the printer.
    """
    out = []
    for i in range(32, 256):
        try:
            out.append(bytes([i]).decode(codepage))
        except UnicodeDecodeError:  # gaps are normal; cp1252 has several
            continue
    return "".join(out)


def _encodable(text: str, codepage: str) -> bool:
    try:
        text.encode(codepage)
    except UnicodeEncodeError:
        return False
    return True


def _fold(ch: str) -> str:
    """Strip accents: 'é' -> 'e'. Returns '' only for pure combining marks."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)
    )


def unprintable(text: str, codepage: str) -> list[str]:
    """The distinct characters that would reach the paper as '?'.

    Walks the same three steps as posprint's encode_text — the character
    itself, an explicit replacement, then accent folding — and reports what
    survives none of them.
    """
    bad: list[str] = []
    for ch in text:
        if ch in "\n\t ":
            continue
        candidates = (ch, FALLBACK.get(ch, ""), _fold(ch))
        if any(c and _encodable(c, codepage) for c in candidates):
            continue
        if ch not in bad:
            bad.append(ch)
    return bad


def _check_printable(text: str, codepage: str) -> None:
    bad = unprintable(text, codepage)
    if not bad:
        return
    shown = " ".join(bad[:6])
    more = f" (and {len(bad) - 6} more)" if len(bad) > 6 else ""
    raise Rejected(
        f"The printer has no glyph for: {shown}{more}. It is a thermal receipt "
        f"printer with a single Latin character set, so Korean, Chinese, "
        f"Japanese, Cyrillic, Greek and emoji cannot be printed at all."
    )


class Rejected(Exception):
    """Input the site refuses to print, with a reason shown to the sender."""


def clean(text: str) -> str:
    """Normalise untrusted text into something safe to hand upstream."""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    text = _INVISIBLE.sub("", text)
    text = text.replace("\t", "    ")
    text = _TRAILING_WS.sub("", text)
    # Cap consecutive blank lines; 200 newlines is a paper-waste attack that
    # passes a naive character count.
    text = _MANY_BLANKS.sub("\n\n\n", text)
    # Trim blank lines top and bottom, but NOT indentation. A plain .strip()
    # here ate the leading spaces of the first line only, so ASCII art arrived
    # with its top row shifted left and every other row intact - which reads as
    # a broken printer rather than a mangled string.
    return text.strip("\n")


def _has_flood(text: str) -> bool:
    run = 1
    for prev, ch in zip(text, text[1:]):
        if ch == prev and not ch.isspace():
            run += 1
            if run >= _FLOOD_RUN:
                return True
        else:
            run = 1
    return False


def check_message(
    text: str,
    *,
    max_chars: int,
    max_lines: int,
    blocklist: tuple[str, ...] = (),
    codepage: str = "cp858",
) -> str:
    """Clean and validate a message body. Returns the cleaned text."""
    text = clean(text)

    # `text` may now be all whitespace: clean() no longer strips indentation,
    # so a message of nothing but spaces survives it.
    if not text.strip():
        raise Rejected("Nothing to print.")
    if len(text) > max_chars:
        raise Rejected(f"Too long: {len(text)} characters, the limit is {max_chars}.")

    lines = text.split("\n")
    if len(lines) > max_lines:
        raise Rejected(f"Too many lines: {len(lines)}, the limit is {max_lines}.")
    if _has_flood(text):
        raise Rejected("That looks like a character flood. Paper is not free.")

    _check_printable(text, codepage)
    _screen(text, blocklist)
    return text


def check_name(
    text: str,
    *,
    max_chars: int,
    blocklist: tuple[str, ...] = (),
    codepage: str = "cp858",
) -> str:
    """Clean and validate the optional sender name. May return ''."""
    text = clean(text).replace("\n", " ").strip()
    if not text:
        return ""
    if len(text) > max_chars:
        raise Rejected(f"Name is too long: the limit is {max_chars} characters.")
    _check_printable(text, codepage)
    _screen(text, blocklist)
    return text


def _screen(text: str, blocklist: tuple[str, ...]) -> None:
    if not blocklist:
        return
    # Fold to a comparable form: casefold, strip accents, and drop the
    # separators commonly used to break up a word (f-u-c-k, f.u.c.k).
    folded = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    squeezed = re.sub(r"[^a-z0-9]", "", folded)
    for term in blocklist:
        if term in folded or (len(term) > 3 and re.sub(r"[^a-z0-9]", "", term) in squeezed):
            raise Rejected("That message was blocked. Try being nicer.")
