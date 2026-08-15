"""Braille art: decoded to a bitmap rather than refused as unprintable."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from posprintweb import braille
from posprintweb.filters import Rejected

LIMITS = dict(max_cols=72, max_rows=40, printer_dots=576, max_scale=8, max_dots=640)

# A 2x1 grid: dot 1 (top-left) in the first cell, dot 8 (bottom-right) in the
# second. Enough to pin orientation without a fixture file.
CORNERS = "⠁⢀"


def test_detection():
    assert braille.contains("⠓⠑⠇⠇⠕")
    assert not braille.contains("hello")
    assert not braille.contains("")


def test_dot_numbering_matches_unicode():
    """Dot 1 is top-left and dot 8 is bottom-right.

    A wrong table would still round-trip - encode and decode would agree with
    each other - so this checks against the standard rather than against us.
    """
    img = braille.decode([CORNERS])
    px = img.load()
    black = {(x, y) for y in range(4) for x in range(4) if px[x, y] == 0}
    assert black == {(0, 0), (3, 3)}


def test_grid_shape():
    img = braille.decode(["⠿⠿⠿", "⠿⠿⠿"])
    assert img.size == (6, 8)          # 2px wide, 4px tall per cell


def test_prepare_produces_a_png():
    art = braille.prepare("⠿⠿\n⠿⠿", **LIMITS)
    assert art.cols == 2 and art.rows == 2
    img = Image.open(io.BytesIO(art.png))
    assert img.size == (4 * art.scale, 8 * art.scale)


def test_text_mixed_into_art_is_refused():
    """A caption cannot be drawn as dots, and half a message is worse than none."""
    with pytest.raises(Rejected, match="on its own"):
        braille.prepare("MEOWLL ⠿⠿", **LIMITS)


def test_spaces_are_allowed_as_padding():
    art = braille.prepare("⠿⠿\n⠿  ", **LIMITS)
    assert art.rows == 2


def test_too_wide_is_refused():
    with pytest.raises(Rejected, match="characters wide"):
        braille.prepare("⠿" * 80, **LIMITS)


def test_too_tall_is_refused():
    with pytest.raises(Rejected, match="lines tall"):
        braille.prepare("\n".join("⠿" * 2 for _ in range(50)), **LIMITS)


def test_scale_is_a_whole_number_and_fits_the_head():
    for cols in range(1, 73):
        scale = braille.scale_for(cols, 4, printer_dots=576, max_scale=8, max_dots=640)
        assert scale >= 1
        assert isinstance(scale, int)
        assert cols * 2 * scale <= 576 or scale == 1


def test_scale_is_capped_by_the_paper_budget():
    """A tall, narrow drawing must not eat the roll just because it is narrow."""
    scale = braille.scale_for(2, 40, printer_dots=576, max_scale=8, max_dots=640)
    assert 40 * 4 * scale <= 640


def test_small_art_is_not_blown_up_to_fill_the_head():
    scale = braille.scale_for(4, 2, printer_dots=576, max_scale=8, max_dots=640)
    assert scale == 8          # max_scale, not 576 // 8 == 72


def test_blank_lines_are_trimmed_but_padded_cells_are_not():
    """U+2800 is a blank cell, not whitespace - it holds the width."""
    art = braille.prepare("\n\n⠿⠀⠀\n\n", **LIMITS)
    assert art.rows == 1
    assert art.cols == 3
