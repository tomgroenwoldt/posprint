"""Siege mode: the one control that is a guarantee rather than a price.

Everything else raises the cost of abuse. This removes the outcome - while the
printer is under attack, nothing reaches paper without a decision - so the
tests here are mostly about the trigger being right. A siege that fires during
an ordinary busy evening would be worse than the attack it prevents.
"""

from __future__ import annotations

from posprintweb.siege import Siege


def test_it_is_off_to_begin_with():
    assert Siege(threshold=3).active(now=1000.0) is False


def test_enough_refusals_start_it():
    s = Siege(threshold=3, window_seconds=300.0, hold_for_seconds=1800.0)
    for i in range(2):
        s.refused(now=1000.0 + i)
    assert s.active(now=1002.0) is False

    s.refused(now=1002.0)
    assert s.active(now=1003.0) is True


def test_refusals_outside_the_window_do_not_count():
    """Three refusals spread over a day is someone occasionally out of quota,
    not an attack."""
    s = Siege(threshold=3, window_seconds=300.0)
    s.refused(now=1000.0)
    s.refused(now=1400.0)          # 400s later, first has aged out
    s.refused(now=1500.0)
    assert s.active(now=1501.0) is False


def test_prints_alone_never_trigger_it():
    """The signal is refusals. A room full of friends taking turns produces
    prints and almost no refusals, because people wait for each other - which
    is exactly the case that must not be mistaken for a flood."""
    s = Siege(threshold=3, window_seconds=300.0)
    assert s.active(now=9999.0) is False       # nothing was ever refused


def test_it_ends_after_the_hold_expires():
    s = Siege(threshold=2, window_seconds=300.0, hold_for_seconds=1800.0)
    s.refused(now=1000.0)
    s.refused(now=1001.0)
    assert s.active(now=1500.0) is True
    assert s.active(now=1001.0 + 1800.0) is False


def test_continuing_to_hammer_keeps_it_on():
    """Otherwise a flood just has to outlast the timer.

    "Still going" means a burst, not one lone request: a single refusal long
    after the others correctly does not sustain anything, because by then the
    window holds only that one.
    """
    s = Siege(threshold=2, window_seconds=300.0, hold_for_seconds=600.0)
    s.refused(now=1000.0)
    s.refused(now=1001.0)
    assert s.active(now=1500.0) is True         # expires at 1601

    for i in range(3):                          # still hammering
        s.refused(now=1500.0 + i)
    assert s.active(now=1700.0) is True         # pushed out past 1601
    assert s.active(now=2103.0) is False        # 600s after the last of them


def test_one_straggler_does_not_extend_a_siege():
    """The counterpart: once the hammering stops, the timer runs out even if
    the occasional refusal still trickles in."""
    s = Siege(threshold=2, window_seconds=300.0, hold_for_seconds=600.0)
    s.refused(now=1000.0)
    s.refused(now=1001.0)

    s.refused(now=1500.0)          # alone in the window by now
    assert s.active(now=1602.0) is False


def test_it_can_be_ended_by_hand():
    """A timer cannot know the wave has passed; the person looking at the
    queue can."""
    s = Siege(threshold=2, hold_for_seconds=1800.0)
    s.refused(now=1000.0)
    s.refused(now=1001.0)
    assert s.active(now=1002.0) is True

    s.lift(now=1002.0)
    assert s.active(now=1003.0) is False
    # And lifting really clears the evidence, so it does not snap straight back
    # on with the next single refusal.
    s.refused(now=1003.0)
    assert s.active(now=1004.0) is False


def test_zero_threshold_disables_it():
    s = Siege(threshold=0, window_seconds=300.0)
    for i in range(500):
        s.refused(now=1000.0 + i)
    assert s.active(now=1500.0) is False


def test_the_refusal_log_does_not_grow_without_bound():
    """A flood must not be able to make memory grow by being refused."""
    s = Siege(threshold=10, window_seconds=60.0)
    for i in range(5000):
        s.refused(now=1000.0 + i * 0.1)        # 500 seconds of hammering
    assert s.status(now=1500.0)["refusals_in_window"] <= 601


def test_status_reports_what_the_page_shows():
    s = Siege(threshold=2, window_seconds=300.0, hold_for_seconds=600.0)
    s.refused(now=1000.0)
    s.refused(now=1001.0)
    status = s.status(now=1010.0)
    assert status["active"] is True
    assert status["refusals_in_window"] == 2
    assert status["threshold"] == 2
    assert 580 <= status["seconds_left"] <= 600


# -- the volume trigger ---------------------------------------------------
#
# The signal a reader of the source cannot pace around. Refusals only appear
# when a sender overshoots a limit, and this repository is public - anyone can
# read the thresholds and stay politely under them. Receipts are the thing
# being objected to, so counting receipts is what cannot be tiptoed past.


def test_a_paced_sender_who_never_overshoots_is_still_caught():
    """The bypass the published thresholds would otherwise hand over."""
    s = Siege(threshold=5, window_seconds=300.0, volume=10,
              volume_seconds=3600.0, hold_for_seconds=1800.0)

    # Perfectly behaved: a print every 30 seconds, never a single refusal.
    for i in range(9):
        s.printed(now=1000.0 + i * 30)
    assert s.active(now=1300.0) is False        # still plausibly a busy evening

    s.printed(now=1000.0 + 9 * 30)
    assert s.active(now=1300.0) is True         # sustained volume is not


def test_volume_ages_out_of_its_window():
    """A busy hour last night is not an attack now."""
    s = Siege(threshold=5, volume=3, volume_seconds=3600.0,
              hold_for_seconds=60.0)
    s.printed(now=1000.0)
    s.printed(now=2000.0)
    s.printed(now=1000.0 + 3700)                # first has aged out
    assert s.active(now=1000.0 + 3800) is False


def test_zero_volume_disables_that_signal():
    s = Siege(threshold=5, volume=0, volume_seconds=3600.0)
    for i in range(500):
        s.printed(now=1000.0 + i)
    assert s.active(now=1500.0) is False


def test_either_signal_alone_is_enough():
    refusals = Siege(threshold=2, volume=0)
    refusals.refused(now=1000.0)
    refusals.refused(now=1001.0)
    assert refusals.active(now=1002.0) is True

    volume = Siege(threshold=0, volume=2, volume_seconds=3600.0)
    volume.printed(now=1000.0)
    volume.printed(now=1001.0)
    assert volume.active(now=1002.0) is True
