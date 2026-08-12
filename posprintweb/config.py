"""Configuration, entirely from environment variables.

Mirrors the posprint service's approach: every setting has a default that boots,
except POSPRINTWEB_UPSTREAM_KEY, which has no safe default and is checked at
startup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass(frozen=True)
class Config:
    # -- upstream (the posprint service on the LAN) ------------------------
    upstream_url: str = "http://127.0.0.1:8080"
    upstream_key: str = ""
    upstream_timeout: float = 30.0

    # -- this service -----------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    site_title: str = "Print to my receipt printer"
    site_blurb: str = (
        "This prints on a real thermal printer in my flat. "
        "Be nice — I have to read it."
    )

    # Columns of the target paper. Only used to draw the preview and to reject
    # obviously oversized input; the real formatting happens upstream.
    columns: int = 48

    # -- abuse controls ---------------------------------------------------
    # A public endpoint that consumes a physical, finite resource. All three
    # limits are load-bearing; see README "Threat model".
    cooldown_seconds: int = 60
    per_ip_daily: int = 5
    global_daily: int = 200

    max_chars: int = 500
    max_lines: int = 20
    max_name_chars: int = 32

    # Outside these hours the printer is asleep and so am I. Local to tz.
    quiet_start_hour: int = 22
    quiet_end_hour: int = 8
    timezone: str = "Europe/Berlin"

    # Presence of this file disables printing without a restart or a deploy.
    # `touch` it the moment something goes wrong.
    killswitch_path: str = "/etc/posprintweb.disabled"
    enabled: bool = True

    # Newline-separated, case-insensitive substrings. Empty file = no filter.
    blocklist_path: str = ""
    blocklist: tuple[str, ...] = ()

    # X-Forwarded-For is attacker-controlled unless something trusted rewrites
    # it. Only turn this on when a reverse proxy or tunnel is actually in front,
    # otherwise every rate limit becomes bypassable with one header.
    trust_proxy: bool = False

    db_path: str = "/var/lib/posprintweb/prints.db"

    # Optional bypass for the owner: these keys skip cooldown, quotas and quiet
    # hours. Sent as X-Admin-Key.
    admin_keys: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "Config":
        blocklist: tuple[str, ...] = ()
        path = os.environ.get("POSPRINTWEB_BLOCKLIST", "").strip()
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    blocklist = tuple(
                        line.strip().lower()
                        for line in fh
                        if line.strip() and not line.startswith("#")
                    )
            except OSError as exc:
                raise SystemExit(f"POSPRINTWEB_BLOCKLIST unreadable: {exc}") from exc

        return cls(
            upstream_url=os.environ.get(
                "POSPRINTWEB_UPSTREAM", "http://127.0.0.1:8080"
            ).rstrip("/"),
            upstream_key=os.environ.get("POSPRINTWEB_UPSTREAM_KEY", "").strip(),
            upstream_timeout=float(_env_int("POSPRINTWEB_UPSTREAM_TIMEOUT", 30)),
            host=os.environ.get("POSPRINTWEB_HOST", "0.0.0.0"),
            port=_env_int("POSPRINTWEB_PORT", 8000),
            site_title=os.environ.get("POSPRINTWEB_TITLE", cls.site_title),
            site_blurb=os.environ.get("POSPRINTWEB_BLURB", cls.site_blurb),
            columns=_env_int("POSPRINTWEB_COLUMNS", 48),
            cooldown_seconds=_env_int("POSPRINTWEB_COOLDOWN_SECONDS", 60),
            per_ip_daily=_env_int("POSPRINTWEB_PER_IP_DAILY", 5),
            global_daily=_env_int("POSPRINTWEB_GLOBAL_DAILY", 200),
            max_chars=_env_int("POSPRINTWEB_MAX_CHARS", 500),
            max_lines=_env_int("POSPRINTWEB_MAX_LINES", 20),
            max_name_chars=_env_int("POSPRINTWEB_MAX_NAME_CHARS", 32),
            quiet_start_hour=_env_int("POSPRINTWEB_QUIET_START", 22),
            quiet_end_hour=_env_int("POSPRINTWEB_QUIET_END", 8),
            timezone=os.environ.get("POSPRINTWEB_TZ", "Europe/Berlin").strip(),
            killswitch_path=os.environ.get(
                "POSPRINTWEB_KILLSWITCH", "/etc/posprintweb.disabled"
            ),
            enabled=_env_bool("POSPRINTWEB_ENABLED", True),
            blocklist_path=path,
            blocklist=blocklist,
            trust_proxy=_env_bool("POSPRINTWEB_TRUST_PROXY", False),
            db_path=os.environ.get(
                "POSPRINTWEB_DB", "/var/lib/posprintweb/prints.db"
            ),
            admin_keys=tuple(_env_list("POSPRINTWEB_ADMIN_KEYS")),
        )
