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
    return text.strip()


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
    text: str, *, max_chars: int, max_lines: int, blocklist: tuple[str, ...] = ()
) -> str:
    """Clean and validate a message body. Returns the cleaned text."""
    text = clean(text)

    if not text:
        raise Rejected("Nothing to print.")
    if len(text) > max_chars:
        raise Rejected(f"Too long: {len(text)} characters, the limit is {max_chars}.")

    lines = text.split("\n")
    if len(lines) > max_lines:
        raise Rejected(f"Too many lines: {len(lines)}, the limit is {max_lines}.")
    if _has_flood(text):
        raise Rejected("That looks like a character flood. Paper is not free.")

    _screen(text, blocklist)
    return text


def check_name(text: str, *, max_chars: int, blocklist: tuple[str, ...] = ()) -> str:
    """Clean and validate the optional sender name. May return ''."""
    text = clean(text).replace("\n", " ").strip()
    if not text:
        return ""
    if len(text) > max_chars:
        raise Rejected(f"Name is too long: the limit is {max_chars} characters.")
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
