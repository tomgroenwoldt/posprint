"""Siege detection: noticing that the printer is under attack rather than busy.

Everything else here is a price. The burst cap prices paper, proof of work
prices a request, the quotas price an address. A determined sender pays all of
them and keeps going - which is what happened: the flood came back, hit the
per-minute cap, and settled in to occupy every slot it allowed.

Prices bound the damage. They do not stop it. So this watches for the shape of
an attack and, while it sees one, takes the printer out of the sender's reach
entirely: messages queue for approval instead of printing. That is a guarantee
rather than a cost, and it is the only thing in the codebase that is.

**The signal is refusals, not prints.** A flood bounces off the burst cap
hundreds of times a minute, because it keeps trying. Friends taking turns at a
party generate prints and almost no refusals, because people wait. Counting
prints would put a busy evening and an attack in the same bucket; counting
refusals separates them cleanly, and errs toward leaving an ordinary busy night
alone.

In memory rather than in the database: this is a fact about right now, it
should evaporate on restart, and a flood must not be able to make the disk
grow by being refused.
"""

from __future__ import annotations

import logging
import time
from collections import deque

log = logging.getLogger("posprintweb.siege")


class Siege:
    def __init__(
        self,
        threshold: int = 20,
        window_seconds: float = 300.0,
        hold_for_seconds: float = 1800.0,
    ) -> None:
        self.threshold = threshold
        self.window = window_seconds
        self.hold_for = hold_for_seconds
        self._refusals: deque[float] = deque()
        self._until: float = 0.0
        self._announced = False

    def refused(self, now: float | None = None) -> None:
        """Record one request the rate limits turned away."""
        now = time.time() if now is None else now
        self._refusals.append(now)
        self._trim(now)

        if self.threshold > 0 and len(self._refusals) >= self.threshold:
            # Refreshed on every refusal, so a siege ends a fixed time after
            # the *last* attempt rather than the first. Someone who keeps
            # trying keeps the printer locked, which is the right way round.
            self._until = now + self.hold_for
            if not self._announced:
                log.warning(
                    "siege: %d refusals in %ds - holding prints for review",
                    len(self._refusals), int(self.window),
                )
                self._announced = True

    def active(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if now >= self._until:
            if self._announced:
                log.info("siege over; printing normally again")
                self._announced = False
            return False
        return True

    def seconds_left(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return max(0, int(self._until - now))

    def lift(self, now: float | None = None) -> None:
        """End it by hand. The owner can see what is in the queue and decide
        the wave has passed, which no timer can know."""
        now = time.time() if now is None else now
        self._refusals.clear()
        self._until = 0.0
        self._announced = False

    def status(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        self._trim(now)
        return {
            "active": self.active(now),
            "refusals_in_window": len(self._refusals),
            "threshold": self.threshold,
            "seconds_left": self.seconds_left(now),
        }

    def _trim(self, now: float) -> None:
        cutoff = now - self.window
        while self._refusals and self._refusals[0] < cutoff:
            self._refusals.popleft()
