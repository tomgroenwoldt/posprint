"""Siege detection: noticing that the printer is under attack rather than busy.

Everything else here is a price. The burst cap prices paper, proof of work
prices a request, the quotas price an address. A determined sender pays all of
them and keeps going - which is what happened: the flood came back, hit the
per-minute cap, and settled in to occupy every slot it allowed.

Prices bound the damage. They do not stop it. So this watches for the shape of
an attack and, while it sees one, takes the printer out of the sender's reach
entirely: messages queue for approval instead of printing. That is a guarantee
rather than a cost, and it is the only thing in the codebase that is.

**The first signal is refusals, not prints.** A flood bounces off the burst cap
hundreds of times a minute, because it keeps trying. Friends taking turns at a
party generate prints and almost no refusals, because people wait. Counting
prints alone would put a busy evening and an attack in the same bucket;
refusals separate them cleanly and err toward leaving a busy night alone.

**The second signal is volume, and it exists because this repository is
public.** Refusals only appear when someone overshoots a limit. A reader of
this file knows the thresholds, and the obvious response is to pace exactly at
the burst cap and never overshoot: no refusals, no siege, and a receipt every
seven seconds forever. So sustained volume triggers it too. Nobody sends sixty
messages an hour to a printer in a stranger's flat for an hour on end, however
politely they space them out.

Publishing the mechanism is fine. Publishing the *numbers* is not, which is why
they are configuration and not constants - the defaults in this file are a
starting point, and a deployment under attack should not be running them.

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
        volume: int = 60,
        volume_seconds: float = 3600.0,
    ) -> None:
        self.threshold = threshold
        self.window = window_seconds
        self.hold_for = hold_for_seconds
        self.volume = volume
        self.volume_window = volume_seconds
        self._refusals: deque[float] = deque()
        self._prints: deque[float] = deque()
        self._until: float = 0.0
        self._announced = False

    def printed(self, now: float | None = None) -> None:
        """Record one message that reached paper.

        The signal that catches a sender who stays politely under every limit.
        They can avoid refusals entirely by pacing; they cannot avoid the
        receipts, which are the thing being objected to.
        """
        now = time.time() if now is None else now
        self._prints.append(now)
        self._trim(now)
        if self.volume > 0 and len(self._prints) >= self.volume:
            self._start(now, f"{len(self._prints)} prints in "
                             f"{int(self.volume_window / 60)} min")

    def refused(self, now: float | None = None) -> None:
        """Record one request the rate limits turned away."""
        now = time.time() if now is None else now
        self._refusals.append(now)
        self._trim(now)

        if self.threshold > 0 and len(self._refusals) >= self.threshold:
            self._start(now, f"{len(self._refusals)} refusals in "
                             f"{int(self.window)}s")

    def _start(self, now: float, why: str) -> None:
        # Refreshed on every trigger, so a siege ends a fixed time after the
        # *last* sign of trouble rather than the first. Someone who keeps going
        # keeps the printer locked, which is the right way round.
        self._until = now + self.hold_for
        if not self._announced:
            log.warning("siege: %s - holding prints for review", why)
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
        self._prints.clear()
        self._until = 0.0
        self._announced = False

    def status(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        self._trim(now)
        return {
            "active": self.active(now),
            "refusals_in_window": len(self._refusals),
            "threshold": self.threshold,
            "prints_in_window": len(self._prints),
            "volume": self.volume,
            "seconds_left": self.seconds_left(now),
        }

    def _trim(self, now: float) -> None:
        cutoff = now - self.window
        while self._refusals and self._refusals[0] < cutoff:
            self._refusals.popleft()
        cutoff = now - self.volume_window
        while self._prints and self._prints[0] < cutoff:
            self._prints.popleft()
