"""Tests for the byte encoder.

These assert on exact byte sequences, because the whole point of this module is
that a printer 200km away interprets them correctly and there is no feedback
loop. Getting `GS ( k` length prefixes wrong fails silently as a blank receipt.
"""

from __future__ import annotations

import io

import pytest

from posprint.escpos import (
    ESC,
    GS,
    EscposBuilder,
    EscposError,
    encode_text,
    status_page,
)


def b58() -> EscposBuilder:
    return EscposBuilder(width_chars=32, dots=384, codepage="cp858")


# -- text encoding --------------------------------------------------------


def test_ascii_roundtrips():
    assert encode_text("Hello", "cp437") == b"Hello"


def test_accents_survive_in_cp858():
    # cp858 has these natively; they must not be mangled to ASCII.
    out = encode_text("é", "cp858")
    assert out == "é".encode("cp858")
    assert out != b"e"


def test_accents_degrade_rather_than_raise_in_cp437():
    # cp437 lacks 'ő'; we want 'o', not an exception and not a dropped line.
    assert encode_text("ő", "cp437") == b"o"


def test_euro_transliterates_when_codepage_lacks_it():
    assert encode_text("€5", "cp437") == b"EUR5"


def test_euro_kept_when_codepage_has_it():
    assert encode_text("€", "cp858") == "€".encode("cp858")


def test_smart_punctuation_is_flattened():
    assert encode_text("“quoted” — yes…", "cp437") == b'"quoted" - yes...'


def test_unmappable_falls_back_to_question_mark():
    assert encode_text("中", "cp437") == b"?"


def test_rejects_unknown_codepage():
    with pytest.raises(EscposError):
        EscposBuilder(codepage="cp999")


# -- column layout --------------------------------------------------------


def test_columns_pads_to_exact_width():
    b = b58()
    b.columns("Coffee", "3.50")
    line = b.bytes().decode("cp858").rstrip("\n")
    assert len(line) == 32
    assert line.startswith("Coffee")
    assert line.endswith("3.50")


def test_columns_truncates_long_left_without_overflowing():
    b = b58()
    b.columns("A very long product name that will not fit", "12.00")
    line = b.bytes().decode("cp858").rstrip("\n")
    assert len(line) == 32, f"line was {len(line)} cols: {line!r}"
    assert line.endswith("12.00")


def test_columns_truncation_does_not_reintroduce_width_via_translit():
    # Regression: an ellipsis truncation marker was expanded to '...' during
    # encoding, silently pushing the line 2 columns over the paper width.
    b = b58()
    b.columns("x" * 100, "9.99")
    line = b.bytes().decode("cp858").rstrip("\n")
    assert len(line) == 32


def test_columns_right_side_always_survives():
    b = b58()
    b.columns("name", "1234567890123456789012345678901234567890")
    line = b.bytes().decode("cp858").rstrip("\n")
    assert len(line) == 32


def test_rule_matches_paper_width():
    b = b58()
    b.rule("-")
    assert b.bytes().decode("cp858").rstrip("\n") == "-" * 32


def test_wrapped_never_exceeds_width():
    b = b58()
    b.wrapped("the quick brown fox jumps over the lazy dog " * 3)
    for line in b.bytes().decode("cp858").split("\n"):
        assert len(line) <= 32


# -- commands -------------------------------------------------------------


def test_init_sets_codepage():
    b = EscposBuilder(codepage="cp858")
    b.init()
    assert b.bytes().startswith(ESC + b"@")
    assert ESC + b"t" + bytes([19]) in b.bytes()  # 19 == cp858


def test_size_packs_nibbles():
    b = b58()
    b.size(3, 2)
    # width-1 in the high nibble, height-1 in the low nibble
    assert b.bytes() == GS + b"!" + bytes([(2 << 4) | 1])


def test_size_rejects_out_of_range():
    with pytest.raises(EscposError):
        b58().size(9, 1)


def test_cut_feeds_before_cutting():
    # The cutter sits above the head; cutting without a feed slices the receipt.
    b = b58()
    b.cut(partial=True, feed_before=4)
    data = b.bytes()
    assert data == ESC + b"d" + bytes([4]) + GS + b"V" + bytes([66, 0])


def test_barcode_uses_explicit_length_form():
    b = b58()
    b.barcode("ABC", "code128", height=60, width=2, hri="below")
    data = b.bytes()
    # CODE128 needs the {B code-set prefix; length counts it.
    payload = b"{BABC"
    assert GS + b"k" + bytes([73, len(payload)]) + payload in data
    assert GS + b"h" + bytes([60]) in data
    assert GS + b"w" + bytes([2]) in data


def test_barcode_rejects_non_ascii():
    with pytest.raises(EscposError):
        b58().barcode("café", "code128")


def test_barcode_rejects_unknown_symbology():
    with pytest.raises(EscposError):
        b58().barcode("123", "qr")


def test_qr_length_prefixes_are_correct():
    b = b58()
    b.qr("hi", size=5, ecc="M")
    data = b.bytes()
    # store: pL/pH count cn+fn+m+data == 3 + len(payload)
    assert GS + b"(k" + bytes([5, 0]) + b"\x31\x50\x30hi" in data
    # model / size / ecc are all 3- and 4-byte bodies
    assert GS + b"(k" + bytes([4, 0]) + b"\x31\x41\x32\x00" in data
    assert GS + b"(k" + bytes([3, 0]) + bytes([0x31, 0x43, 5]) in data
    assert GS + b"(k" + bytes([3, 0]) + bytes([0x31, 0x45, 49]) in data
    # print
    assert GS + b"(k" + bytes([3, 0]) + b"\x31\x51\x30" in data


def test_qr_length_prefix_handles_two_byte_lengths():
    b = b58()
    payload = "u" * 300
    b.qr(payload)
    body_len = 3 + 300
    assert GS + b"(k" + bytes([body_len & 0xFF, body_len >> 8]) in b.bytes()


def test_drawer_kick_converts_ms_to_2ms_units():
    b = b58()
    b.drawer_kick(pin=0, on_ms=100, off_ms=200)
    assert b.bytes() == ESC + b"p" + bytes([0, 50, 100])


# -- raster images --------------------------------------------------------


def make_png(width: int, height: int, colour: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("L", (width, height), colour).save(buf, format="PNG")
    return buf.getvalue()


def parse_raster(data: bytes) -> tuple[int, int, bytes]:
    idx = data.index(GS + b"v0")
    header = data[idx : idx + 8]
    width_bytes = header[4] | (header[5] << 8)
    height = header[6] | (header[7] << 8)
    return width_bytes, height, data[idx + 8 :]


def test_all_black_image_sets_every_bit():
    b = b58()
    b.image(make_png(64, 4, 0), max_width=64, dither=False)
    width_bytes, height, payload = parse_raster(b.bytes())
    assert (width_bytes, height) == (8, 4)
    # 1 == black on the printer, so a black image is all 0xFF.
    assert payload == b"\xff" * 32


def test_all_white_image_clears_every_bit():
    b = b58()
    b.image(make_png(64, 4, 255), max_width=64, dither=False)
    _, _, payload = parse_raster(b.bytes())
    assert payload == b"\x00" * 32


def test_non_byte_aligned_width_masks_padding_bits():
    # Regression: PIL pads rows with 0 (white); inverting to the printer's
    # 1=black convention turned that padding into a black stripe down the edge.
    b = b58()
    b.image(make_png(12, 2, 255), max_width=12, dither=False)
    width_bytes, height, payload = parse_raster(b.bytes())
    assert (width_bytes, height) == (2, 2)
    assert payload == b"\x00" * 4, f"padding bits leaked: {payload!r}"


def test_image_is_clamped_to_paper_width():
    b = b58()  # 384 dots
    b.image(make_png(2000, 10, 0), dither=False)
    width_bytes, _, _ = parse_raster(b.bytes())
    assert width_bytes == 384 // 8


def test_image_scales_height_proportionally():
    b = b58()
    b.image(make_png(768, 100, 0), max_width=384, dither=False)
    width_bytes, height, _ = parse_raster(b.bytes())
    assert width_bytes == 48
    assert height == 50


def test_transparency_flattens_onto_white_not_black():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (64, 4), (0, 0, 0, 0)).save(buf, format="PNG")
    b = b58()
    b.image(buf.getvalue(), max_width=64, dither=False)
    _, _, payload = parse_raster(b.bytes())
    assert payload == b"\x00" * 32, "fully transparent image should print blank"


# -- self test page -------------------------------------------------------


def test_status_page_builds_and_cuts():
    b = status_page(b58(), [("device", "/dev/usb/lp0")])
    data = b.bytes()
    assert data.startswith(ESC + b"@")
    assert data.endswith(GS + b"V" + bytes([66, 0]))
    assert b"POSPRINT" in data
