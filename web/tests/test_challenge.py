"""Proof of work: the one cost a rented address cannot pay."""

from __future__ import annotations

import hashlib

import pytest

from posprintweb.challenge import (
    BadChallenge, Challenges, leading_zero_bits, solved,
)

# Low, so the tests spend microseconds rather than a second each. The bits are
# a cost dial, not a correctness one - every property here holds at any value.
BITS = 8


def answer(challenge: str, bits: int = BITS) -> int:
    """What the page does, in four lines."""
    counter = 0
    while not solved(challenge, counter, bits):
        counter += 1
    return counter


@pytest.fixture()
def challenges():
    return Challenges(bits=BITS, ttl=300.0)


def test_a_solved_challenge_is_accepted(challenges):
    issued = challenges.issue(now=1000.0)["challenge"]
    challenges.redeem(issued, answer(issued), now=1001.0)


def test_the_wrong_counter_is_refused(challenges):
    issued = challenges.issue(now=1000.0)["challenge"]
    good = answer(issued)
    with pytest.raises(BadChallenge):
        challenges.redeem(issued, good + 1, now=1001.0)


def test_a_challenge_cannot_be_spent_twice(challenges):
    """Otherwise the cost is paid once and the flood continues as before."""
    issued = challenges.issue(now=1000.0)["challenge"]
    counter = answer(issued)
    challenges.redeem(issued, counter, now=1001.0)
    with pytest.raises(BadChallenge):
        challenges.redeem(issued, counter, now=1002.0)


def test_a_forged_challenge_is_refused(challenges):
    """The signature is the only thing between a sender and a free print."""
    issued = challenges.issue(now=1000.0)["challenge"]
    nonce, ts, sig = issued.split(".")

    for forged in (
        f"{nonce}.{ts}.{'0' * len(sig)}",          # no signature worth the name
        f"{'f' * len(nonce)}.{ts}.{sig}",          # someone else's signature
        f"{nonce}.{int(ts) + 500}.{sig}",          # a newer expiry, same signature
    ):
        with pytest.raises(BadChallenge):
            challenges.redeem(forged, answer(forged), now=1001.0)


def test_an_expired_challenge_is_refused(challenges):
    issued = challenges.issue(now=1000.0)["challenge"]
    with pytest.raises(BadChallenge):
        challenges.redeem(issued, answer(issued), now=1000.0 + 301)


def test_a_malformed_challenge_is_refused_not_crashed(challenges):
    for junk in ("", "nonsense", "a.b", "a.b.c.d", "a.notanumber.c"):
        with pytest.raises(BadChallenge):
            challenges.redeem(junk, 1, now=1000.0)


def test_spent_nonces_do_not_accumulate_forever(challenges):
    """Bounded by the issue rate over the TTL, not by uptime - or a long flood
    would be a memory leak with extra steps."""
    for i in range(80):
        issued = challenges.issue(now=1000.0 + i)["challenge"]
        challenges.redeem(issued, answer(issued), now=1000.0 + i)
    assert challenges.outstanding == 80

    # Well past the TTL, they are no longer redeemable and so not worth keeping.
    later = challenges.issue(now=2000.0)["challenge"]
    challenges.redeem(later, answer(later), now=2000.0)
    assert challenges.outstanding == 1


def test_verifying_is_one_hash_however_hard_the_search_was():
    """The asymmetry the whole idea rests on: the sender searches, the printer
    checks once."""
    issued = "some.challenge.value"
    counter = answer(issued, bits=12)
    assert solved(issued, counter, 12)
    assert not solved(issued, counter + 1, 24)


def test_leading_zero_bits_counts_bits_not_bytes():
    assert leading_zero_bits(bytes([0xFF])) == 0
    assert leading_zero_bits(bytes([0x7F])) == 1
    assert leading_zero_bits(bytes([0x01])) == 7
    assert leading_zero_bits(bytes([0x00, 0x80])) == 8
    assert leading_zero_bits(bytes([0x00, 0x00, 0x10])) == 19
    assert leading_zero_bits(bytes(32)) == 256


def test_the_digest_is_plain_sha256_of_challenge_dot_counter():
    """Pinned, because the page reimplements this in JavaScript. If the recipe
    here ever changes, every browser silently stops being able to print."""
    digest = hashlib.sha256(b"abc.42").digest()
    assert solved("abc", 42, leading_zero_bits(digest))
    assert not solved("abc", 42, leading_zero_bits(digest) + 1)
