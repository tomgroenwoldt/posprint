"""The gallery: nothing is public until it is approved by hand."""

from __future__ import annotations

from datetime import timezone

import pytest

from posprintweb.store import Store

QUOTAS = dict(cooldown_seconds=0, per_ip_daily=0, global_daily=0)


@pytest.fixture()
def store():
    return Store(":memory:", timezone.utc)


def printed(store, message, ip="1.2.3.4", name="tom", now=1000.0):
    """A message that actually reached paper - the only kind that is eligible."""
    res = store.reserve(ip, name, message, now=now, **QUOTAS)
    store.finish(res, "printed", "job-1")
    return res.row_id


# -- approval gate --------------------------------------------------------


def test_a_fresh_print_is_not_public(store):
    printed(store, "hello")
    assert store.gallery() == []
    assert len(store.review_queue()) == 1


def test_approving_makes_it_public(store):
    row = printed(store, "hello")
    assert store.set_gallery(row, "approved") is True
    assert [e["message"] for e in store.gallery()] == ["hello"]
    assert store.review_queue() == []


def test_hiding_takes_it_back_off(store):
    row = printed(store, "hello")
    store.set_gallery(row, "approved")
    store.set_gallery(row, "hidden")
    assert store.gallery() == []
    # Hidden is a decision, not a deferral: it does not come back to the queue.
    assert store.review_queue() == []


def test_hidden_stays_out_of_the_queue_and_the_gallery(store):
    row = printed(store, "nope")
    store.set_gallery(row, "hidden")
    assert store.gallery() == []
    assert store.review_queue() == []


# -- what is eligible -----------------------------------------------------


def test_a_shadowed_message_never_reaches_the_queue(store):
    """The whole point of the quiet filter is that it did not happen."""
    res = store.reserve("1.1.1.1", "", "slur", now=1000.0, **QUOTAS)
    store.finish(res, "shadowed")
    assert store.review_queue() == []
    assert store.set_gallery(res.row_id, "approved") is False
    assert store.gallery() == []


def test_a_failed_print_cannot_be_approved(store):
    """It produced no paper, so there is nothing to show off."""
    res = store.reserve("1.1.1.1", "", "hello", now=1000.0, **QUOTAS)
    store.release(res)
    assert store.review_queue() == []
    assert store.set_gallery(res.row_id, "approved") is False


def test_setting_an_unknown_row_is_false_not_an_error(store):
    assert store.set_gallery(9999, "approved") is False


def test_an_unknown_value_is_a_programming_error(store):
    row = printed(store, "hello")
    with pytest.raises(ValueError):
        store.set_gallery(row, "featured")


# -- the public projection ------------------------------------------------


def test_the_public_gallery_never_carries_an_ip(store):
    """The column exists on the row; it must not exist on the way out."""
    row = printed(store, "hello", ip="203.0.113.9")
    store.set_gallery(row, "approved")
    entry = store.gallery()[0]
    assert "ip" not in entry
    assert "203.0.113.9" not in repr(entry)


def test_the_queue_does_carry_an_ip(store):
    """The owner's view. Six from one address is most of a decision."""
    printed(store, "hello", ip="203.0.113.9")
    assert store.review_queue()[0]["ip"] == "203.0.113.9"


# -- ordering and paging --------------------------------------------------


def test_newest_first(store):
    for i in range(3):
        row = printed(store, f"message {i}", now=1000.0 + i)
        store.set_gallery(row, "approved")
    assert [e["message"] for e in store.gallery()] == \
        ["message 2", "message 1", "message 0"]


def test_pages_are_disjoint(store):
    for i in range(5):
        row = printed(store, f"message {i}", now=1000.0 + i)
        store.set_gallery(row, "approved")

    first = store.gallery(limit=2)
    second = store.gallery(limit=2, before_id=first[-1]["id"])
    assert [e["message"] for e in first] == ["message 4", "message 3"]
    assert [e["message"] for e in second] == ["message 2", "message 1"]
    assert not {e["id"] for e in first} & {e["id"] for e in second}


def test_approved_can_be_read_back_and_taken_down(store):
    """Publishing is reversible; that is the point of hidden being a state."""
    row = printed(store, "hello")
    store.set_gallery(row, "approved")

    listed = store.review_queue(gallery="approved")
    assert [e["message"] for e in listed] == ["hello"]

    store.set_gallery(row, "hidden")
    assert store.gallery() == []
    assert store.review_queue(gallery="approved") == []
    assert [e["message"] for e in store.review_queue(gallery="hidden")] == ["hello"]


def test_hidden_can_go_back_to_the_queue(store):
    row = printed(store, "hello")
    store.set_gallery(row, "hidden")
    store.set_gallery(row, "new")
    assert [e["message"] for e in store.review_queue()] == ["hello"]


def test_an_unknown_list_is_a_programming_error(store):
    with pytest.raises(ValueError):
        store.review_queue(gallery="featured")


def test_counts(store):
    a = printed(store, "one", now=1000.0)
    b = printed(store, "two", now=1001.0)
    printed(store, "three", now=1002.0)
    store.set_gallery(a, "approved")
    store.set_gallery(b, "hidden")
    assert store.review_counts() == {"new": 1, "approved": 1, "hidden": 1}
