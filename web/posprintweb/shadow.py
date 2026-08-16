"""The quiet filter: messages that are accepted, logged, and never printed.

The loud blocklist in filters.py answers a visitor with "that message was
blocked", which tells whoever is probing exactly what to edit. This one does
not. A match is accepted with an ordinary success response, consumes the
sender's quota like any other print, and goes in the log for you to read - it
simply never reaches the paper.

That asymmetry is the entire point. Someone testing which slurs get through
learns nothing, so there is nothing to iterate against, and they spend their
daily quota on receipts that do not exist.

Two deliberate choices:

- **Word boundaries, not substrings.** A shadowed message disappears silently,
  so a false positive is invisible to everyone including you. Substring
  matching would swallow "Scunthorpe" and "classic" and you would never know.
  The loud blocklist can afford to be blunt; this cannot.
- **Separators are allowed *inside* a term, not stripped from the text.** Each
  term becomes a pattern that tolerates punctuation between its letters, so
  f-u-c-k and f.u.c.k are caught while "peacock" still is not. Squeezing the
  whole message first would have caught both.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from functools import lru_cache

log = logging.getLogger("posprintweb.shadow")

# Anything non-alphanumeric may appear between the letters of a term: spaces,
# dots, hyphens, newlines. Zero or more, so the undecorated word matches too.
_GAP = r"[^a-z0-9]*"


def load(path: str) -> tuple[str, ...]:
    """Read a wordlist. Blank lines and # comments ignored."""
    if not path:
        return ()
    try:
        with open(path, encoding="utf-8") as fh:
            terms = tuple(
                line.strip().lower()
                for line in fh
                if line.strip() and not line.lstrip().startswith("#")
            )
    except OSError as exc:
        raise SystemExit(f"POSPRINTWEB_SHADOWLIST unreadable: {exc}") from exc
    log.info("shadow filter active with %d terms", len(terms))
    return terms


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in folded if not unicodedata.combining(c))


@lru_cache(maxsize=512)
def _pattern(term: str) -> re.Pattern[str]:
    """'fuck' -> (?<![a-z0-9])f[^a-z0-9]*u[^a-z0-9]*c[^a-z0-9]*k(?![a-z0-9])

    The lookarounds are what keep 'peacock' and 'Scunthorpe' out: the gaps sit
    between the term's own letters, never at its edges.
    """
    letters = [ch for ch in re.sub(r"[^a-z0-9]", "", term)]
    if not letters:
        return re.compile(r"(?!)")            # never matches
    body = _GAP.join(re.escape(ch) for ch in letters)
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])")


def matches(text: str, terms: tuple[str, ...]) -> str | None:
    """The first term this text trips, or None. Never raises."""
    if not terms:
        return None
    folded = _fold(text)
    for term in terms:
        if _pattern(term).search(folded):
            return term
    return None
