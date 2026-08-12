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
