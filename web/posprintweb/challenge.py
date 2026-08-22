"""Proof of work: a cost per print that a proxy pool cannot pay.

Every other limit here is keyed on something the sender can rent. Addresses are
cheap - one flood managed 50 prints in 19 seconds from 50 different real
addresses - and so is a forwarding header. CPU time is not.

The shape is the same as any captcha, because there is only one shape that
works: the server issues a challenge, the client proves something about it, and
the server checks the proof before doing anything. What differs is the proof.
A captcha's proof is "some service believes you are a person", which for a
residential proxy pool is exactly the judgement being laundered. This proof is
"a few hundred thousand SHA-256 hashes were computed", which nobody can buy
their way around - they can only pay for it, per print, forever.

What it is not: a wall. Someone with real hardware can still push through, just
not for free and not at 2.6 a second. Combined with siege mode, the point is
that a flood can no longer run for nothing while a person waits.

Challenges are stateless apart from the spent-nonce set: an HMAC over the nonce
and the issue time means nothing has to be stored to know we issued it, and
nothing can be forged without the secret.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time

log = logging.getLogger("posprintweb.challenge")


class BadChallenge(Exception):
    """The proof did not check out. The reason is deliberately not shown to the
    sender: every variant means the same thing to an honest page, which simply
    asks for a new challenge and solves it again."""


def _secret() -> bytes:
    """The HMAC key.

    From the environment if it is set, so challenges survive a restart and
    would survive more than one worker. Otherwise random per process, which is
    fine on its own: a challenge lives for minutes, so the worst a restart does
    is make anyone mid-solve fetch a new one.
    """
    configured = os.environ.get("POSPRINTWEB_POW_SECRET", "").strip()
    if configured:
        return configured.encode()
    return secrets.token_bytes(32)


_KEY = _secret()


def _sign(nonce: str, issued: int) -> str:
    return hmac.new(
        _KEY, f"{nonce}.{issued}".encode(), hashlib.sha256
    ).hexdigest()[:32]


def issue(now: float | None = None) -> str:
    """A fresh challenge. Opaque to the client, which only echoes it back."""
    now = time.time() if now is None else now
    nonce = secrets.token_hex(16)
    issued = int(now)
    return f"{nonce}.{issued}.{_sign(nonce, issued)}"


def leading_zero_bits(digest: bytes) -> int:
    """How many zero bits the digest starts with."""
    bits = 0
    for byte in digest:
        if byte:
            return bits + (8 - byte.bit_length())
        bits += 8
    return bits


def solved(challenge: str, counter: int, bits: int) -> bool:
    """Whether this counter is an answer to this challenge at this difficulty.

    Verification is one hash however hard the search was, which is the whole
    point of the construction: the asymmetry is what makes it a cost to the
    sender and not to the printer.
    """
    digest = hashlib.sha256(f"{challenge}.{counter}".encode()).digest()
    return leading_zero_bits(digest) >= bits


class Challenges:
    """Issues challenges and spends them exactly once.

    Single use is not optional. A solved challenge that can be replayed is a
    one-off cost rather than a per-print one, and a flood would happily pay it
    once and then continue exactly as before.
    """

    def __init__(self, bits: int = 18, ttl: float = 300.0) -> None:
        self.bits = bits
        self.ttl = ttl
        self._spent: dict[str, float] = {}

    def issue(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        self._prune(now)
        return {
            "challenge": issue(now),
            "bits": self.bits,
            "expires_in": int(self.ttl),
        }

    def redeem(self, challenge: str, counter: int, now: float | None = None) -> None:
        """Check a proof and spend it, or raise BadChallenge."""
        now = time.time() if now is None else now
        self._prune(now)

        parts = challenge.split(".") if challenge else []
        if len(parts) != 3:
            raise BadChallenge("malformed")
        nonce, issued_raw, sig = parts

        try:
            issued = int(issued_raw)
        except ValueError:
            raise BadChallenge("malformed") from None

        # compare_digest, because a plain == leaks where the first difference
        # is and this is the only thing standing between a sender and a forged
        # challenge they never had to solve.
        if not hmac.compare_digest(sig, _sign(nonce, issued)):
            raise BadChallenge("not ours")

        age = now - issued
        # A small negative age is clock jitter between issue and redeem; a
        # large one is a challenge minted with a stolen secret or a wrong clock.
        if age < -60 or age > self.ttl:
            raise BadChallenge("expired")

        if nonce in self._spent:
            raise BadChallenge("already used")

        if not solved(challenge, counter, self.bits):
            raise BadChallenge("wrong answer")

        self._spent[nonce] = now

    def _prune(self, now: float) -> None:
        """Forget nonces that could no longer be redeemed anyway.

        Bounded by the issue rate over the TTL rather than by uptime, so a long
        flood costs memory for five minutes and not forever.
        """
        if len(self._spent) < 32:
            return
        cutoff = now - self.ttl
        for nonce in [n for n, at in self._spent.items() if at < cutoff]:
            del self._spent[nonce]

    @property
    def outstanding(self) -> int:
        return len(self._spent)
