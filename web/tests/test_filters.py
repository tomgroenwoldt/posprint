"""Tests for input sanitising. These are the security-relevant ones."""

from __future__ import annotations

import pytest

from posprintweb.filters import Rejected, check_message, check_name, clean

LIMITS = {"max_chars": 500, "max_lines": 20}


def test_escape_byte_is_stripped():
    """The whole reason clean() exists.

    A bare 0x1B reaching the encoder would let a visitor issue ESC/POS
    commands: \\x1B\\x70 kicks the cash drawer.
    """
    out = clean("hello\x1b\x70\x00\x19 world")
    assert "\x1b" not in out
    assert "\x00" not in out
    assert out == "hellop world"


def test_newlines_and_tabs_survive():
    assert clean("a\r\nb\tc") == "a\nb    c"


def test_zero_width_characters_are_stripped():
    # Used to break a word up so a blocklist misses it.
    assert clean("he​llo‍") == "hello"


def test_bidi_override_is_stripped():
    assert "‮" not in clean("nice‮drow")


def test_blank_line_flood_is_capped():
    out = clean("top" + "\n" * 200 + "bottom")
    assert out.count("\n") == 3


def test_repeated_character_flood_is_rejected():
    with pytest.raises(Rejected, match="flood"):
        check_message("A" * 200, **LIMITS)


def test_flood_check_ignores_whitespace_runs():
    # Long runs of spaces are already collapsed to nothing dangerous by the
    # width limit; they must not trip the flood detector.
    check_message("hello" + " " * 60 + "world", **LIMITS)


def test_too_long_is_rejected():
    with pytest.raises(Rejected, match="Too long"):
        check_message("ab" * 400, max_chars=100, max_lines=20)


def test_too_many_lines_is_rejected():
    with pytest.raises(Rejected, match="Too many lines"):
        check_message("\n".join(str(i) for i in range(50)), max_chars=500, max_lines=20)


def test_empty_after_cleaning_is_rejected():
    with pytest.raises(Rejected, match="Nothing to print"):
        check_message("   \x00 \n\n ", **LIMITS)


def test_blocklist_matches_plainly():
    with pytest.raises(Rejected, match="blocked"):
        check_message("you are a badword ok", blocklist=("badword",), **LIMITS)


def test_blocklist_survives_separator_evasion():
    with pytest.raises(Rejected, match="blocked"):
        check_message("b-a-d-w-o-r-d", blocklist=("badword",), **LIMITS)


def test_blocklist_survives_accent_evasion():
    with pytest.raises(Rejected, match="blocked"):
        check_message("bàdwörd", blocklist=("badword",), **LIMITS)


def test_blocklist_is_case_insensitive():
    with pytest.raises(Rejected, match="blocked"):
        check_message("BADWORD", blocklist=("badword",), **LIMITS)


def test_trailing_whitespace_goes_but_indentation_stays():
    """Deliberate asymmetry.

    Trailing spaces are invisible and never meant; leading spaces are how you
    draw. This used to .strip() both, which quietly shifted the first line of
    any ASCII art left while leaving the rest of it alone.
    """
    assert check_message("  Hello, printer!  ", **LIMITS) == "  Hello, printer!"


def test_blank_lines_are_trimmed_but_indentation_survives():
    assert check_message("\n\n  drawing\n   here \n\n", **LIMITS) == "  drawing\n   here"


def test_whitespace_only_message_is_rejected():
    with pytest.raises(Rejected, match="Nothing to print"):
        check_message("    ", **LIMITS)


def test_name_is_flattened_to_one_line():
    assert check_name("Tom\nGroenwoldt", max_chars=32) == "Tom Groenwoldt"


def test_empty_name_is_allowed():
    assert check_name("   ", max_chars=32) == ""


def test_long_name_is_rejected():
    with pytest.raises(Rejected, match="Name is too long"):
        check_name("x" * 50, max_chars=32)
