"""Persistent rate limiting and an audit log, in one SQLite file.

Why not an in-memory dict: the limits guard a physical resource. A process
restart must not hand everyone a fresh quota, and when something does go wrong
the first question is "who sent that, and when" — which needs the log anyway.

The reservation is taken *before* the upstream call and released only on
failure. Checking first and inserting afterwards would let two concurrent
requests both pass the same quota check.
"""

from __future__ import annotations

import sqlite3
import threading
import time
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

                cur.execute(
                    "INSERT INTO prints (ts, day, ip, name, message, state) "
                    "VALUES (?, ?, ?, ?, ?, 'pending')",
                    (now, day, ip, name, message),
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
