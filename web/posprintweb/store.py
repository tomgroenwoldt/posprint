"""Persistent rate limiting and an audit log, in one SQLite file.

Why not an in-memory dict: the limits guard a physical resource. A process
restart must not hand everyone a fresh quota, and when something does go wrong
the first question is "who sent that, and when" — which needs the log anyway.

The reservation is taken *before* the upstream call and released only on
failure. Checking first and inserting afterwards would let two concurrent
requests both pass the same quota check.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS prints (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    day      TEXT NOT NULL,
    ip       TEXT NOT NULL,
    name     TEXT NOT NULL DEFAULT '',
    message  TEXT NOT NULL DEFAULT '',
    job_id   TEXT NOT NULL DEFAULT '',
    state    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS prints_ip_ts ON prints (ip, ts);
CREATE INDEX IF NOT EXISTS prints_day   ON prints (day);
"""

# Added after the first release, so it arrives by migration rather than in
# SCHEMA - an existing database must not be rebuilt to gain a column.
MIGRATIONS = [
    "ALTER TABLE prints ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS prints_fp_ts ON prints (fingerprint, ts)",
    # Whether something printed and whether it belongs on a public page are
    # different questions, so this is its own column rather than another
    # `state`. Existing rows default to 'new', which leaves the whole back
    # catalogue unapproved - the safe direction.
    "ALTER TABLE prints ADD COLUMN gallery TEXT NOT NULL DEFAULT 'new'",
    "CREATE INDEX IF NOT EXISTS prints_gallery ON prints (gallery, id)",
]

# Only something that actually reached paper can be shown off. In particular a
# 'shadowed' message must never reach the review queue: the entire point of
# that filter is that it quietly does not exist.
GALLERY_ELIGIBLE = "printed"


def fingerprint(text: str) -> str:
    """Identify a message by what it *looks* like, not by its exact bytes.

    Per-IP limits are the wrong tool against someone who can change address at
    will, so repeats are caught by content instead. Matching raw text would be
    beaten by pressing space once, so the comparison is made on a folded form:
    case, accents and every whitespace character removed.

    What that catches: the same drawing re-sent, re-indented, re-cased, or with
    its lines re-wrapped. What it does not: a genuinely edited message. That is
    the intended line - a visitor who bothers to change the content is sending
    something new, which is all anyone can reasonably ask.
    """
    folded = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = re.sub(r"\s+", "", folded)
    return hashlib.sha256(folded.encode("utf-8")).hexdigest()[:32]


class QuotaExceeded(Exception):
    """Raised with a human-readable reason and the seconds until retry."""

    def __init__(self, reason: str, retry_after: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


@dataclass
class Reservation:
    row_id: int
    ip: str


class Store:
    def __init__(self, path: str, tz: tzinfo) -> None:
        self._tz = tz
        self._lock = threading.Lock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # One connection guarded by a lock. At the request volume a home
        # printer can physically sustain, a pool would be theatre.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        for statement in MIGRATIONS:
            try:
                self._db.execute(statement)
            except sqlite3.OperationalError:
                pass          # already applied; SQLite has no ADD COLUMN IF NOT EXISTS
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _today(self, now: float) -> str:
        return datetime.fromtimestamp(now, self._tz).strftime("%Y-%m-%d")

    def reserve(
        self,
        ip: str,
        name: str,
        message: str,
        *,
        cooldown_seconds: int,
        per_ip_daily: int,
        global_daily: int,
        global_hourly: int = 0,
        repeat_hours: int = 0,
        now: float | None = None,
    ) -> Reservation:
        """Claim one print against the quotas, or raise QuotaExceeded."""
        now = time.time() if now is None else now
        day = self._today(now)

        with self._lock:
            cur = self._db.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                if cooldown_seconds > 0:
                    row = cur.execute(
                        "SELECT ts FROM prints WHERE ip = ? AND state != 'rejected' "
                        "ORDER BY ts DESC LIMIT 1",
                        (ip,),
                    ).fetchone()
                    if row is not None:
                        elapsed = now - row["ts"]
                        if elapsed < cooldown_seconds:
                            wait = int(cooldown_seconds - elapsed) + 1
                            raise QuotaExceeded(
                                f"One print per {cooldown_seconds}s. "
                                f"Try again in {wait}s.",
                                retry_after=wait,
                            )

                if per_ip_daily > 0:
                    used = cur.execute(
                        "SELECT COUNT(*) AS n FROM prints "
                        "WHERE ip = ? AND day = ? AND state != 'rejected'",
                        (ip, day),
                    ).fetchone()["n"]
                    if used >= per_ip_daily:
                        raise QuotaExceeded(
                            f"You have used today's {per_ip_daily} prints. "
                            "Back tomorrow.",
                            retry_after=self._seconds_to_midnight(now),
                        )

                if global_daily > 0:
                    used = cur.execute(
                        "SELECT COUNT(*) AS n FROM prints "
                        "WHERE day = ? AND state != 'rejected'",
                        (day,),
                    ).fetchone()["n"]
                    if used >= global_daily:
                        raise QuotaExceeded(
                            "The printer has hit its daily paper budget. "
                            "Back tomorrow.",
                            retry_after=self._seconds_to_midnight(now),
                        )

                # -- the two checks that do not care about the address ------
                #
                # Everything above is keyed on IP, which is worth little
                # against someone who can change theirs. These are not.

                fp = fingerprint(message)
                if repeat_hours > 0:
                    seen = cur.execute(
                        "SELECT ts FROM prints WHERE fingerprint = ? AND ts > ? "
                        "AND state NOT IN ('rejected') ORDER BY ts DESC LIMIT 1",
                        (fp, now - repeat_hours * 3600),
                    ).fetchone()
                    if seen is not None:
                        wait = int(repeat_hours * 3600 - (now - seen["ts"])) + 1
                        raise QuotaExceeded(
                            "That has already been printed. Send something else.",
                            retry_after=wait,
                        )

                if global_hourly > 0:
                    recent = cur.execute(
                        "SELECT COUNT(*) AS n FROM prints "
                        "WHERE ts > ? AND state != 'rejected'",
                        (now - 3600,),
                    ).fetchone()["n"]
                    if recent >= global_hourly:
                        # Blunts a burst without ending the day for everyone,
                        # which the daily cap alone would do.
                        raise QuotaExceeded(
                            "The printer is busy right now. Try again later.",
                            retry_after=600,
                        )

                cur.execute(
                    "INSERT INTO prints (ts, day, ip, name, message, state, fingerprint) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (now, day, ip, name, message, fp),
                )
                row_id = int(cur.lastrowid or 0)
                self._db.commit()
                return Reservation(row_id=row_id, ip=ip)
            except Exception:
                self._db.rollback()
                raise

    def _seconds_to_midnight(self, now: float) -> int:
        local = datetime.fromtimestamp(now, self._tz)
        tomorrow = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1, int(86400 - (local - tomorrow).total_seconds()))

    def finish(self, res: Reservation, state: str, job_id: str = "") -> None:
        with self._lock:
            self._db.execute(
                "UPDATE prints SET state = ?, job_id = ? WHERE id = ?",
                (state, job_id, res.row_id),
            )
            self._db.commit()

    def release(self, res: Reservation) -> None:
        """Hand the quota back after an upstream failure.

        Marked rather than deleted: a printer that is refusing everything is
        exactly the situation where you want the attempts visible in the log.
        """
        self.finish(res, "rejected")

    def counts(self, ip: str, now: float | None = None) -> dict[str, int]:
        now = time.time() if now is None else now
        day = self._today(now)
        with self._lock:
            cur = self._db.cursor()
            mine = cur.execute(
                "SELECT COUNT(*) AS n FROM prints "
                "WHERE ip = ? AND day = ? AND state != 'rejected'",
                (ip, day),
            ).fetchone()["n"]
            total = cur.execute(
                "SELECT COUNT(*) AS n FROM prints WHERE day = ? AND state != 'rejected'",
                (day,),
            ).fetchone()["n"]
            last = cur.execute(
                "SELECT ts FROM prints WHERE ip = ? AND state != 'rejected' "
                "ORDER BY ts DESC LIMIT 1",
                (ip,),
            ).fetchone()
        return {
            "used_today": int(mine),
            "global_today": int(total),
            "last_ts": int(last["ts"]) if last else 0,
        }

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, ip, name, message, job_id, state FROM prints "
                "ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- gallery ----------------------------------------------------------

    def review_queue(self, limit: int = 50) -> list[dict]:
        """Printed messages waiting on a decision. Includes the IP: this is
        the owner's view, and knowing that six of these came from one address
        is most of what makes a decision easy."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, ip, name, message FROM prints "
                "WHERE state = ? AND gallery = 'new' ORDER BY id DESC LIMIT ?",
                (GALLERY_ELIGIBLE, max(1, min(limit, 200))),
            ).fetchall()
        return [dict(r) for r in rows]

    def gallery(self, limit: int = 30, before_id: int | None = None) -> list[dict]:
        """Approved entries, newest first.

        Keyset pagination on `id` rather than OFFSET: approving something while
        a visitor is paging would shift every later page by one and silently
        skip an entry. No `ip` in the projection - this feeds a public page and
        the column should not be one typo away from it.
        """
        limit = max(1, min(limit, 100))
        with self._lock:
            if before_id is None:
                rows = self._db.execute(
                    "SELECT id, ts, name, message FROM prints "
                    "WHERE gallery = 'approved' ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT id, ts, name, message FROM prints "
                    "WHERE gallery = 'approved' AND id < ? ORDER BY id DESC LIMIT ?",
                    (before_id, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def set_gallery(self, row_id: int, value: str) -> bool:
        """Approve or hide one row. False if there was nothing eligible to change.

        The state check lives in the UPDATE rather than in a read-then-write, so
        there is no window in which a row stops being eligible between the two.
        """
        if value not in ("approved", "hidden", "new"):
            raise ValueError(f"unknown gallery value {value!r}")
        with self._lock:
            cur = self._db.execute(
                "UPDATE prints SET gallery = ? WHERE id = ? AND state = ?",
                (value, row_id, GALLERY_ELIGIBLE),
            )
            self._db.commit()
            return cur.rowcount > 0

    def review_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT gallery, COUNT(*) AS n FROM prints WHERE state = ? "
                "GROUP BY gallery",
                (GALLERY_ELIGIBLE,),
            ).fetchall()
        counts = {"new": 0, "approved": 0, "hidden": 0}
        for row in rows:
            if row["gallery"] in counts:
                counts[row["gallery"]] = int(row["n"])
        return counts
