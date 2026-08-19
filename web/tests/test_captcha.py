"""The visual puzzle.

It is a fast lane past a siege, not a wall - no captcha is one. So these tests
are about it being a *sound* check (unforgeable, one attempt, expiring) rather
than about it being unsolvable, which it is not and cannot be.
"""

from __future__ import annotations

import pytest

from posprintweb.captcha import TILES, BadCaptcha, Captchas, render


@pytest.fixture()
def captchas():
    return Captchas(ttl=300.0)


def solve(issued: dict) -> int:
    """Brute force, which is what six tiles costs.

    The signature covers the answer, so trying all six is exactly what an
    attacker would do offline - and it is why redeem() spends the puzzle on the
    first attempt, right or wrong. This helper only works because it is calling
    the private signer, not the endpoint.
    """
    from posprintweb.captcha import _sign

    nonce, issued_at, _seed, sig = issued["token"].split(".")
    for answer in range(TILES):
        if _sign(nonce, int(issued_at), answer) == sig:
            return answer
    raise AssertionError("no answer matched")


def test_the_right_answer_is_accepted(captchas):
    issued = captchas.issue(now=1000.0)
    captchas.redeem(issued["token"], solve(issued), now=1001.0)


def test_a_wrong_answer_is_refused(captchas):
    issued = captchas.issue(now=1000.0)
    wrong = (solve(issued) + 1) % TILES
    with pytest.raises(BadCaptcha):
        captchas.redeem(issued["token"], wrong, now=1001.0)


def test_one_attempt_per_puzzle_right_or_wrong(captchas):
    """Six tiles means guessing works one time in six. Without this, a puzzle
    could simply be tried six times and it would not be a check at all."""
    issued = captchas.issue(now=1000.0)
    answer = solve(issued)
    wrong = (answer + 1) % TILES

    with pytest.raises(BadCaptcha):
        captchas.redeem(issued["token"], wrong, now=1001.0)
    # Even the correct answer is now too late.
    with pytest.raises(BadCaptcha):
        captchas.redeem(issued["token"], answer, now=1002.0)


def test_a_solved_puzzle_cannot_be_replayed(captchas):
    issued = captchas.issue(now=1000.0)
    answer = solve(issued)
    captchas.redeem(issued["token"], answer, now=1001.0)
    with pytest.raises(BadCaptcha):
        captchas.redeem(issued["token"], answer, now=1002.0)


def test_a_forged_token_is_refused(captchas):
    issued = captchas.issue(now=1000.0)
    nonce, at, seed, sig = issued["token"].split(".")
    for forged in (
        f"{nonce}.{at}.{seed}.{'0' * len(sig)}",
        f"{'f' * len(nonce)}.{at}.{seed}.{sig}",
        f"{nonce}.{int(at) + 900}.{seed}.{sig}",
    ):
        with pytest.raises(BadCaptcha):
            for answer in range(TILES):
                captchas.redeem(forged, answer, now=1001.0)


def test_an_expired_puzzle_is_refused(captchas):
    issued = captchas.issue(now=1000.0)
    with pytest.raises(BadCaptcha):
        captchas.redeem(issued["token"], solve(issued), now=1000.0 + 301)


def test_malformed_input_is_refused_not_crashed(captchas):
    for junk in ("", "a.b", "a.b.c", "a.b.c.d.e", "x.notanumber.1.y"):
        with pytest.raises(BadCaptcha):
            captchas.redeem(junk, 0, now=1000.0)


def test_an_out_of_range_answer_is_refused(captchas):
    issued = captchas.issue(now=1000.0)
    for answer in (-1, TILES, 999):
        with pytest.raises(BadCaptcha):
            captchas.redeem(issued["token"], answer, now=1001.0)


def test_the_answer_never_travels_to_the_client(captchas):
    """The signature covers the answer, so the server can check a claim without
    ever storing or sending the right one."""
    from posprintweb.captcha import _sign

    issued = captchas.issue(now=1000.0)
    assert "answer" not in issued

    # Every candidate answer signs differently, which is what makes the
    # signature a check rather than a label. Without the key, telling which of
    # the six is the real one means guessing.
    nonce, at, _seed, sig = issued["token"].split(".")
    signatures = {_sign(nonce, int(at), a) for a in range(TILES)}
    assert len(signatures) == TILES
    assert sig in signatures


def test_the_picture_is_a_png_and_reproducible():
    """Rendered from (answer, seed) alone, so nothing has to be stored between
    the request that issues it and the one that checks it."""
    first = render(answer=2, seed=1234)
    assert first.startswith(bytes([0x89, 0x50, 0x4E, 0x47]))   # PNG magic
    assert first == render(answer=2, seed=1234)
    assert first != render(answer=3, seed=1234)


def test_issuing_produces_a_data_uri(captchas):
    issued = captchas.issue(now=1000.0)
    assert issued["image"].startswith("data:image/png;base64,")
    assert issued["tiles"] == TILES
    # Small enough to inline rather than costing a second request.
    assert len(issued["image"]) < 60_000
