"""Tests for quota accounting."""

from __future__ import annotations

from datetime import timezone

import pytest

from posprintweb.store import QuotaExceeded, Store

QUOTAS = {"cooldown_seconds": 60, "per_ip_daily": 3, "global_daily": 10}


@pytest.fixture()
def store():
    s = Store(":memory:", timezone.utc)
    yield s
    s.close()


def test_first_print_is_allowed(store):
    res = store.reserve("1.2.3.4", "tom", "hi", now=1000.0, **QUOTAS)
    assert res.row_id > 0


def test_cooldown_blocks_the_second_print(store):
    store.reserve("1.2.3.4", "", "hi", now=1000.0, **QUOTAS)
    with pytest.raises(QuotaExceeded) as exc:
        store.reserve("1.2.3.4", "", "again", now=1030.0, **QUOTAS)
    assert exc.value.retry_after == 31


def test_cooldown_expires(store):
    store.reserve("1.2.3.4", "", "hi", now=1000.0, **QUOTAS)
    store.reserve("1.2.3.4", "", "again", now=1061.0, **QUOTAS)


def test_cooldown_is_per_ip(store):
    store.reserve("1.2.3.4", "", "hi", now=1000.0, **QUOTAS)
    store.reserve("5.6.7.8", "", "hi", now=1001.0, **QUOTAS)


def test_daily_cap_per_ip(store):
    for i in range(3):
        store.reserve("1.2.3.4", "", "hi", now=1000.0 + i * 100, **QUOTAS)
    with pytest.raises(QuotaExceeded, match="today's 3 prints"):
        store.reserve("1.2.3.4", "", "hi", now=1400.0, **QUOTAS)


def test_global_cap(store):
    quotas = {"cooldown_seconds": 0, "per_ip_daily": 0, "global_daily": 2}
    store.reserve("1.1.1.1", "", "a", now=1000.0, **quotas)
    store.reserve("2.2.2.2", "", "b", now=1001.0, **quotas)
    with pytest.raises(QuotaExceeded, match="daily paper budget"):
        store.reserve("3.3.3.3", "", "c", now=1002.0, **quotas)


def test_released_reservation_frees_the_quota(store):
    """A printer failure is not the visitor's fault; the slot comes back."""
    quotas = {"cooldown_seconds": 0, "per_ip_daily": 1, "global_daily": 10}
    res = store.reserve("1.2.3.4", "", "hi", now=1000.0, **quotas)
    store.release(res)
    store.reserve("1.2.3.4", "", "hi", now=1001.0, **quotas)


def test_released_reservation_is_still_logged(store):
    res = store.reserve("1.2.3.4", "", "hi", now=1000.0, **QUOTAS)
    store.release(res)
    assert store.recent()[0]["state"] == "rejected"


def test_day_rollover_resets_the_cap(store):
    quotas = {"cooldown_seconds": 0, "per_ip_daily": 1, "global_daily": 10}
    store.reserve("1.2.3.4", "", "hi", now=1_000_000.0, **quotas)
    with pytest.raises(QuotaExceeded):
        store.reserve("1.2.3.4", "", "hi", now=1_000_100.0, **quotas)
    store.reserve("1.2.3.4", "", "hi", now=1_000_000.0 + 86400, **quotas)


def test_counts_reports_usage(store):
    store.reserve("1.2.3.4", "", "hi", now=1000.0, **QUOTAS)
    store.reserve("5.6.7.8", "", "hi", now=1001.0, **QUOTAS)
    c = store.counts("1.2.3.4", now=1002.0)
    assert c["used_today"] == 1
    assert c["global_today"] == 2


def test_log_records_the_message(store):
    res = store.reserve("1.2.3.4", "tom", "hello world", now=1000.0, **QUOTAS)
    store.finish(res, "printed", "job-1")
    row = store.recent()[0]
    assert row["ip"] == "1.2.3.4"
    assert row["message"] == "hello world"
    assert row["job_id"] == "job-1"
    assert row["state"] == "printed"


def test_concurrent_reservations_cannot_exceed_the_cap(store):
    """Reserve-then-print, not check-then-print: the quota holds under races."""
    import threading

    quotas = {"cooldown_seconds": 0, "per_ip_daily": 0, "global_daily": 5}
    granted, denied = [], []

    def attempt(i):
        try:
            store.reserve(f"10.0.0.{i}", "", "hi", now=1000.0, **quotas)
            granted.append(i)
        except QuotaExceeded:
            denied.append(i)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(granted) == 5
    assert len(denied) == 15


# -- the short-window global cap ------------------------------------------
#
# The one limit that answers a flood from a rented proxy pool. Every check
# keyed on IP is worth nothing to someone renting a new address per request,
# and random text walks straight past the repeat fingerprint.

BURST = {"cooldown_seconds": 0, "per_ip_daily": 0, "global_daily": 0,
         "global_burst": 3, "global_burst_seconds": 60}


def test_a_burst_is_capped_however_many_addresses_it_comes_from(store):
    for i in range(3):
        store.reserve(f"10.0.0.{i}", "", f"m{i}", now=1000.0 + i, **BURST)
    with pytest.raises(QuotaExceeded):
        store.reserve("10.0.0.99", "", "m99", now=1003.0, **BURST)


def test_the_window_slides_one_slot_at_a_time(store):
    """Spaced out, so each print ages out separately and the window is seen
    opening one slot at a time rather than emptying all at once."""
    for at in (1000.0, 1030.0, 1050.0):
        store.reserve(f"10.0.0.{int(at)}", "", f"m{at}", now=at, **BURST)

    with pytest.raises(QuotaExceeded):
        store.reserve("10.0.0.99", "", "a", now=1055.0, **BURST)

    # The one at t=1000 leaves the window at t=1060, freeing exactly one slot.
    store.reserve("10.0.0.99", "", "a", now=1061.0, **BURST)
    with pytest.raises(QuotaExceeded):
        store.reserve("10.0.0.98", "", "b", now=1062.0, **BURST)

    # The next opens when t=1030 ages out, and not before.
    store.reserve("10.0.0.98", "", "b", now=1091.0, **BURST)


def test_retry_after_is_when_a_slot_actually_frees(store):
    """A number someone can act on, rather than a flat guess.

    global_hourly answered a flat ten minutes however close the window was to
    opening, which is what made it feel like a punishment rather than a queue.
    """
    for i in range(3):
        store.reserve(f"10.0.0.{i}", "", f"m{i}", now=1000.0 + i, **BURST)

    with pytest.raises(QuotaExceeded) as exc:
        store.reserve("10.0.0.99", "", "x", now=1030.0, **BURST)
    # Oldest at 1000 ages out at 1060, so 30 seconds away, +1 to round up.
    assert exc.value.retry_after == 31


def test_a_blocked_attempt_does_not_extend_the_block(store):
    """Hammering must not push the window out, or a flood locks the printer
    for as long as it keeps trying."""
    for i in range(3):
        store.reserve(f"10.0.0.{i}", "", f"m{i}", now=1000.0 + i, **BURST)

    for attempt in range(50):               # the flood, still going
        with pytest.raises(QuotaExceeded):
            store.reserve(f"10.1.0.{attempt}", "", f"f{attempt}",
                          now=1010.0 + attempt * 0.4, **BURST)

    # The window is still measured from the three that got through.
    store.reserve("10.0.0.99", "", "after", now=1061.0, **BURST)


def test_the_flood_that_prompted_this(store):
    """The real shape: 50 addresses, none repeated, random text, 2.6/second.

    Every other defence misses it - the addresses are all different, so the
    cooldown and the per-IP daily never fire, and the messages are all
    different, so the repeat fingerprint never fires either.
    """
    allowed = 0
    for i in range(50):
        try:
            store.reserve(f"172.59.{i}.{i}", f"name{i}", f"random text {i}",
                          now=1000.0 + i / 2.6,
                          cooldown_seconds=60, per_ip_daily=5, global_daily=200,
                          repeat_hours=24, global_burst=8,
                          global_burst_seconds=60)
            allowed += 1
        except QuotaExceeded:
            pass
    assert allowed == 8                     # 8 receipts, not 50


def test_zero_disables_it(store):
    off = {**BURST, "global_burst": 0}
    for i in range(20):
        store.reserve(f"10.0.0.{i}", "", f"m{i}", now=1000.0 + i, **off)
