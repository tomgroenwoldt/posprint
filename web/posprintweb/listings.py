"""What is for sale, read once at startup from a JSON manifest.

This started as one hardcoded page about one frame. A second object made that
untenable and a fourth made it silly, so the copy and the photographs moved
into data and the page became a loop.

**The manifest lives next to the photographs, in the repository.** That looks
like the wrong side of the config/content line the rest of this service draws
so carefully - but a listing cannot be added without adding its pictures, and
pictures reach the container through `install.sh`, which copies the static
directory. Putting the text somewhere else would mean two places to update and
one deploy either way. `POSPRINTWEB_AUCTIONS` overrides the path for anyone who
wants it elsewhere.

Everything is validated here rather than trusted, and a bad manifest stops the
service at startup instead of rendering a broken page. That is deliberate: the
failure mode this feature already shipped once was a listing that looked
perfect and could not be clicked, and a page nobody can buy from is worse than
a service that refused to start and said why.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class BadManifest(Exception):
    """The manifest is not usable. Raised with something a person can act on."""


def _url(value: str, where: str) -> str:
    """A URL that will be written into an href or a src.

    Same rule as config._env_url, for the same reason: escaping renders
    `javascript:` inert as text and does nothing about it in an attribute.
    """
    if not isinstance(value, str) or not value.strip():
        raise BadManifest(f"{where}: missing")
    value = value.strip()
    if value.startswith("/") or value.lower().startswith(("http://", "https://")):
        return value
    raise BadManifest(
        f"{where}: must be http://, https:// or a /path, got {value!r}")


@dataclass(frozen=True)
class Photo:
    src: str
    alt: str
    caption: str = ""


@dataclass(frozen=True)
class Listing:
    title: str
    url: str
    blurb: str = ""
    note: str = ""
    sold: bool = False
    photos: tuple[Photo, ...] = field(default_factory=tuple)

    @property
    def hero(self) -> Photo | None:
        """The first photograph, shown large. None if there are none at all,
        which is allowed - a listing with no pictures is thin, not broken."""
        return self.photos[0] if self.photos else None

    @property
    def rest(self) -> tuple[Photo, ...]:
        return self.photos[1:]


def _photo(raw: dict, where: str) -> Photo:
    if not isinstance(raw, dict):
        raise BadManifest(f"{where}: each photo must be an object")
    alt = str(raw.get("alt", "")).strip()
    if not alt:
        # Not pedantry. These are photographs of a physical object being sold,
        # and the whole page is images; without alt text a screen reader gets
        # a title, a price and silence.
        raise BadManifest(f"{where}: every photo needs alt text")
    return Photo(src=_url(raw.get("src", ""), f"{where}.src"),
                 alt=alt,
                 caption=str(raw.get("caption", "")).strip())


def _listing(raw: dict, where: str) -> Listing:
    if not isinstance(raw, dict):
        raise BadManifest(f"{where}: each listing must be an object")
    title = str(raw.get("title", "")).strip()
    if not title:
        raise BadManifest(f"{where}: missing title")
    photos = raw.get("photos", [])
    if not isinstance(photos, list):
        raise BadManifest(f"{where}.photos: must be a list")
    return Listing(
        title=title,
        url=_url(raw.get("url", ""), f"{where}.url"),
        blurb=str(raw.get("blurb", "")).strip(),
        note=str(raw.get("note", "")).strip(),
        sold=bool(raw.get("sold", False)),
        photos=tuple(_photo(p, f"{where}.photos[{i}]")
                     for i, p in enumerate(photos)),
    )


def load(path: str | Path) -> tuple[Listing, ...]:
    """Read the manifest. An absent file means nothing is for sale.

    A *missing* file is not an error - that is the ordinary state of a
    deployment that is not selling anything, and it switches the feature off.
    A file that exists and is wrong is an error, because somebody meant it.
    """
    p = Path(path)
    if not p.exists():
        return ()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BadManifest(f"{p}: {exc}") from exc

    # Accept either a bare list or {"listings": [...]}, so the file can grow a
    # sibling key later without breaking.
    if isinstance(raw, dict):
        raw = raw.get("listings", [])
    if not isinstance(raw, list):
        raise BadManifest(f"{p}: expected a list of listings")

    return tuple(_listing(item, f"{p.name}[{i}]")
                 for i, item in enumerate(raw))


def live(listings: tuple[Listing, ...]) -> tuple[Listing, ...]:
    """The ones still biddable. Sold items stay on the page - the point is what
    came off this printer, and a sold lot is still that - but they sort last
    and lose their button."""
    return tuple(x for x in listings if not x.sold)
