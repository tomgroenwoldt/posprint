"""The quiet filter, and the two limits an attacker's IP cannot help with."""

from __future__ import annotations

import pytest

from posprintweb.shadow import matches
from posprintweb.store import Store, QuotaExceeded, fingerprint

TERMS = ("cock", "fuck", "arschloch")


# -- matching -------------------------------------------------------------


def test_plain_hit():
    assert matches("look at this cock", TERMS) == "cock"


def test_case_and_accents_do_not_help():
    assert matches("FÜCK this", ("fuck",)) == "fuck"
    assert matches("ArschLoch", TERMS) == "arschloch"


def test_separators_do_not_help_for_longer_terms():
    assert matches("f-u-c-k you", TERMS) == "fuck"
    assert matches("a.r.s.c.h.l.o.c.h", TERMS) == "arschloch"


def test_word_boundaries_prevent_invisible_false_positives():
    """A shadowed message vanishes silently, so a false positive is unseeable.

    Substring matching would swallow these and neither the sender nor the owner
    would ever know.
    """
    assert matches("Scunthorpe", ("cunt",)) is None
    assert matches("classic assessment", ("ass",)) is None
    assert matches("cockatoo", ("cock",)) is None
    assert matches("Dickens", ("dick",)) is None


def test_separators_inside_a_word_are_not_a_match():
    """Gaps are allowed between a term's letters, not at its edges."""
    assert matches("assets", ("ass",)) is None
    assert matches("peacocks", ("cock",)) is None
    assert matches("dickensian", ("dick",)) is None


def test_empty_list_matches_nothing():
    assert matches("anything at all", ()) is None


# -- repeats --------------------------------------------------------------


def test_fingerprint_ignores_spacing_and_case():
    art = " /\\ /\\\n((ovo))"
    assert fingerprint(art) == fingerprint(art.upper())
    assert fingerprint(art) == fingerprint(art.replace(" ", "  "))
    assert fingerprint(art) == fingerprint(art.replace("\n", "\n\n"))


def test_fingerprint_differs_for_different_content():
    assert fingerprint("hello") != fingerprint("hello!")


@pytest.fixture()
def store():
    from datetime import timezone
    return Store(":memory:", timezone.utc)


QUOTAS = dict(cooldown_seconds=0, per_ip_daily=0, global_daily=0)


def test_the_same_message_is_refused_from_a_different_ip(store):
    """The whole point: rotating address must not buy a second print."""
    store.reserve("1.1.1.1", "", "PENIS ART", now=1000.0, repeat_hours=24, **QUOTAS)
    with pytest.raises(QuotaExceeded, match="already been printed"):
        store.reserve("2.2.2.2", "", "PENIS ART", now=1001.0, repeat_hours=24, **QUOTAS)


def test_respacing_a_repeat_does_not_help(store):
    store.reserve("1.1.1.1", "", "ha ha ha", now=1000.0, repeat_hours=24, **QUOTAS)
    with pytest.raises(QuotaExceeded):
        store.reserve("2.2.2.2", "", "HA  HA\nHA", now=1001.0, repeat_hours=24, **QUOTAS)


def test_a_genuinely_different_message_passes(store):
    store.reserve("1.1.1.1", "", "hello", now=1000.0, repeat_hours=24, **QUOTAS)
    store.reserve("2.2.2.2", "", "goodbye", now=1001.0, repeat_hours=24, **QUOTAS)


def test_repeats_are_allowed_again_after_the_window(store):
    store.reserve("1.1.1.1", "", "same", now=1000.0, repeat_hours=1, **QUOTAS)
    store.reserve("1.1.1.1", "", "same", now=1000.0 + 3700, repeat_hours=1, **QUOTAS)


def test_a_rejected_print_does_not_block_a_retry(store):
    """A failed print produced no paper, so it must not count as 'already sent'."""
    res = store.reserve("1.1.1.1", "", "hello", now=1000.0, repeat_hours=24, **QUOTAS)
    store.release(res)
    store.reserve("1.1.1.1", "", "hello", now=1001.0, repeat_hours=24, **QUOTAS)


def test_shadowed_messages_still_block_repeats(store):
    """Otherwise a caught message could be resent forever, one IP at a time."""
    res = store.reserve("1.1.1.1", "", "slur", now=1000.0, repeat_hours=24, **QUOTAS)
    store.finish(res, "shadowed")
    with pytest.raises(QuotaExceeded):
        store.reserve("9.9.9.9", "", "slur", now=1001.0, repeat_hours=24, **QUOTAS)


# -- burst cap ------------------------------------------------------------


def test_global_hourly_cap_survives_ip_rotation(store):
    for i in range(5):
        store.reserve(f"10.0.0.{i}", "", f"msg {i}", now=1000.0 + i,
                      global_hourly=5, **QUOTAS)
    with pytest.raises(QuotaExceeded, match="busy right now"):
        store.reserve("10.0.0.99", "", "one more", now=1010.0, global_hourly=5, **QUOTAS)


def test_the_hourly_cap_rolls_forward(store):
    for i in range(5):
        store.reserve(f"10.0.0.{i}", "", f"msg {i}", now=1000.0 + i,
                      global_hourly=5, **QUOTAS)
    store.reserve("10.0.0.99", "", "later", now=1000.0 + 3700,
                  global_hourly=5, **QUOTAS)
