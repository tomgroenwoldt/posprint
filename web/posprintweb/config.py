"""Configuration, entirely from environment variables.

Mirrors the posprint service's approach: every setting has a default that boots,
except POSPRINTWEB_UPSTREAM_KEY, which has no safe default and is checked at
startup.
"""

from __future__ import annotations

import codecs
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


def _env_codepage(name: str, default: str) -> str:
    """Resolve a code page name, failing at startup rather than mid-request.

    An unknown codec would otherwise raise LookupError inside the first request
    that contained a non-ASCII character, which is a confusing 500 for whoever
    happens to be typing at the time.
    """
    raw = os.environ.get(name, default).strip().lower() or default
    try:
        codecs.lookup(raw)
    except LookupError as exc:
        raise SystemExit(f"{name}={raw!r} is not a known code page") from exc
    return raw


def _env_choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    """A typo here would silently change a privacy setting, so it is fatal."""
    raw = os.environ.get(name, default).strip().lower() or default
    if raw not in allowed:
        raise SystemExit(f"{name}={raw!r} must be one of: {', '.join(allowed)}")
    return raw


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
    site_blurb: str = "This prints on a real thermal printer in my flat."

    # Columns of the target paper. Only used to draw the preview and to reject
    # obviously oversized input; the real formatting happens upstream.
    columns: int = 48

    # Must match posprint's POSPRINT_CODEPAGE. The printer has no glyphs
    # outside it, so this is what decides which characters are refused rather
    # than printed as '?'. Set them together or the page will promise something
    # the paper cannot deliver.
    codepage: str = "cp858"

    # -- camera -----------------------------------------------------------
    # A live view of the printer. The RTSP URL carries credentials and stays in
    # this process; the browser only ever sees a JPEG from /api/camera.jpg.
    #
    # camera_mode is the privacy decision. This is a camera in a home, on a URL
    # strangers already have, so it is worth choosing deliberately:
    #
    #   always      - live whenever anyone has the page open
    #   after_print - live for camera_window_seconds once something prints, so
    #                 a visitor sees their own receipt appear and nothing else
    #   off         - disabled
    camera_url: str = ""
    camera_mode: str = "always"
    camera_window_seconds: int = 90
    camera_fps: int = 0              # 0 = whatever the camera sends
    camera_width: int = 0            # 0 = no rescaling; stream2 is already 640x360
    camera_quality: int = 6          # ffmpeg -q:v, 2 best .. 31 worst
    camera_idle_timeout: int = 15
    # Frames are shared, so the camera and the home uplink see one decode no
    # matter how many people watch. What does scale per viewer is bytes leaving
    # the VPS - and upstream from the flat, which is the scarcer of the two.
    camera_max_viewers: int = 6
    # Presence of this file stops the feed at once, no restart. The camera
    # equivalent of POSPRINTWEB_KILLSWITCH, and separate from it on purpose:
    # cutting the picture should not have to mean cutting the printing.
    camera_killswitch: str = "/etc/posprintweb-camera.disabled"

    # -- braille art ------------------------------------------------------
    # Braille characters are printed as a decoded bitmap rather than as text,
    # because the printer has no glyphs for them. That needs its own limits:
    # max_chars measures the wrong thing entirely once the message is a
    # picture, since 500 cells might be 72 wide and 7 tall or 8 wide and 62,
    # and those cost very different amounts of roll.
    braille_enabled: bool = True
    braille_max_cols: int = 72     # 72*2=144 dots, so scale 4 fills an 80mm head
    braille_max_rows: int = 40
    braille_max_scale: int = 8     # keeps small drawings from filling the roll
    braille_max_dots: int = 640    # ~80mm: the paper budget for one picture
    # Percent of dots allowed to be black. A thermal head makes black by
    # heating, so a filled-in image prints slowly and runs hot. Line art is
    # ~12% and a dithered photograph 30-50%, so this refuses the solid
    # rectangle without refusing real pictures. Note 50 would be too tight:
    # that is exactly "every other dot", an ordinary dithering pattern.
    braille_max_ink: int = 55
    printer_dots: int = 576        # match posprint's POSPRINT_DOTS (384 for 58mm)

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
    # This one answers the sender: "that message was blocked".
    blocklist_path: str = ""
    blocklist: tuple[str, ...] = ()

    # The quiet counterpart. A match is accepted, charged against the sender's
    # quota, logged, and never printed. Nothing about it appears in /api/status
    # or on the page, so someone probing which words get through learns
    # nothing and has nothing to iterate against.
    shadowlist_path: str = ""
    shadowlist: tuple[str, ...] = ()

    # A real print takes about a second of printer time. Returning instantly
    # would make a swallowed message obvious to anyone watching the clock.
    shadow_delay_ms: int = 900

    # Not keyed on IP, because an attacker's IP is not a scarce resource.
    # Refuses a message whose content has already been printed within this
    # many hours, however it has been re-spaced or re-cased. 0 disables.
    repeat_hours: int = 24
    # A burst cap that does not end the day for everyone the way the daily
    # budget would. 0 disables.
    global_hourly: int = 30

    # The short-window cap, and the only limit that meaningfully answers a
    # flood from a rented proxy pool. Everything keyed on IP is worth nothing
    # against someone renting a new address per request - one such run managed
    # 2.6 prints a second across 50 addresses, none of them repeated.
    #
    # A minute is deliberately the shortest useful window. It is fatal to a
    # flood, invisible to a person (the per-IP cooldown is already 60s, so one
    # visitor cannot reach it alone), and self-healing: the worst a legitimate
    # visitor waits is until the oldest print in the window ages out, which is
    # under a minute and is reported exactly in Retry-After. That is the
    # difference from global_hourly, whose flat ten-minute answer was what made
    # it feel like a punishment. 0 disables.
    global_burst: int = 8
    global_burst_seconds: int = 60

    # Proof of work: a cost per print, paid in CPU, that nothing rentable
    # substitutes for. The flood that prompted the burst cap never loaded the
    # page at all - it posted straight to /api/print, which is why a button or
    # a checkbox would have changed nothing.
    #
    # Difficulty is in leading zero bits, so each bit doubles the work. 18 is
    # about 260k hashes: under a second in a browser, once per print, against a
    # 60s cooldown nobody notices. 0 disables the check entirely.
    pow_bits: int = 18
    pow_ttl_seconds: int = 300

    # Siege mode: the only thing here that is a guarantee rather than a cost.
    #
    # Every other control raises the price of abuse and hopes the price is high
    # enough. This one removes the outcome: while under siege, nothing reaches
    # paper until it has been looked at. An attacker who can pay every other
    # cost still cannot make the printer print.
    #
    # The trigger is *rejections*, not prints. A flood generates hundreds of
    # refusals a minute bouncing off the burst cap; a room full of friends
    # taking turns generates almost none, because they are not hammering. That
    # difference is what keeps this switched off during ordinary busy periods.
    #
    # 0 disables, and the printer goes back to printing whatever gets past the
    # quotas.
    hold_threshold: int = 20
    hold_window_seconds: int = 300
    # How long a siege lasts once triggered, refreshed by further rejections.
    hold_for_seconds: int = 1800
    # Held messages cost a database row, so a long siege has a ceiling. Past
    # it, new messages are refused outright rather than queued.
    hold_max_queue: int = 200
    # The second trigger, and the one that exists because this repository is
    # public. Refusals only happen when someone overshoots; a reader who knows
    # the thresholds can pace exactly at the burst cap, never overshoot, and
    # print all day without ever tripping it. Volume catches that. Nobody sends
    # sixty messages an hour to a stranger's printer, however politely spaced.
    hold_volume: int = 60
    hold_volume_seconds: int = 3600

    # The visual puzzle offered during a siege. Not a wall - no captcha is -
    # but a fast lane: solve it and print now rather than waiting in the queue.
    # Failing it is not refusal, only the ordinary wait, which is what keeps it
    # from locking out anyone who cannot see the picture.
    captcha_enabled: bool = True

    # The forwarding header is attacker-controlled unless something trusted
    # overwrites it. Only turn this on when a reverse proxy or tunnel is
    # actually in front, otherwise every rate limit becomes bypassable with one
    # header.
    trust_proxy: bool = False

    # Exactly one header is consulted, and only when trust_proxy is on. Set it
    # to match the proxy actually in front: cf-connecting-ip behind Cloudflare,
    # x-forwarded-for behind Caddy, nginx and friends. Naming a header the
    # proxy does not overwrite is a silent bypass, not a loud misconfiguration.
    client_ip_header: str = "x-forwarded-for"

    # How many proxies of our own stand in front. The client address is that
    # many entries from the *end* of the forwarding header, because each proxy
    # appends the peer it saw: the last entry is the one ours wrote, and
    # anything to the left of it is whatever the sender chose to claim.
    #
    # 1 for a single Caddy or nginx. 2 if Cloudflare sits in front of that.
    # Getting it too high fails safe - the header will be shorter than the
    # chain claims and the socket peer is used instead.
    proxy_hops: int = 1

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

        from .shadow import load as _load_shadow

        shadow_path = os.environ.get("POSPRINTWEB_SHADOWLIST", "").strip()
        shadow_terms = _load_shadow(shadow_path)

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
            codepage=_env_codepage("POSPRINTWEB_CODEPAGE", "cp858"),
            camera_url=os.environ.get("POSPRINTWEB_CAMERA_URL", "").strip(),
            camera_mode=_env_choice(
                "POSPRINTWEB_CAMERA_MODE", "always",
                ("after_print", "always", "off"),
            ),
            camera_window_seconds=_env_int("POSPRINTWEB_CAMERA_WINDOW", 90),
            camera_fps=_env_int("POSPRINTWEB_CAMERA_FPS", 0),
            camera_width=_env_int("POSPRINTWEB_CAMERA_WIDTH", 0),
            camera_quality=_env_int("POSPRINTWEB_CAMERA_QUALITY", 6),
            camera_idle_timeout=_env_int("POSPRINTWEB_CAMERA_IDLE", 15),
            camera_max_viewers=_env_int("POSPRINTWEB_CAMERA_MAX_VIEWERS", 6),
            camera_killswitch=os.environ.get(
                "POSPRINTWEB_CAMERA_KILLSWITCH", "/etc/posprintweb-camera.disabled"
            ),
            braille_enabled=_env_bool("POSPRINTWEB_BRAILLE", True),
            braille_max_cols=_env_int("POSPRINTWEB_BRAILLE_MAX_COLS", 72),
            braille_max_rows=_env_int("POSPRINTWEB_BRAILLE_MAX_ROWS", 40),
            braille_max_scale=_env_int("POSPRINTWEB_BRAILLE_MAX_SCALE", 8),
            braille_max_dots=_env_int("POSPRINTWEB_BRAILLE_MAX_DOTS", 640),
            braille_max_ink=_env_int("POSPRINTWEB_BRAILLE_MAX_INK", 55),
            printer_dots=_env_int("POSPRINTWEB_PRINTER_DOTS", 576),
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
            shadowlist_path=shadow_path,
            shadowlist=shadow_terms,
            shadow_delay_ms=_env_int("POSPRINTWEB_SHADOW_DELAY_MS", 900),
            repeat_hours=_env_int("POSPRINTWEB_REPEAT_HOURS", 24),
            global_hourly=_env_int("POSPRINTWEB_GLOBAL_HOURLY", 30),
            captcha_enabled=_env_bool("POSPRINTWEB_CAPTCHA", True),
            hold_volume=_env_int("POSPRINTWEB_HOLD_VOLUME", 60),
            hold_volume_seconds=_env_int(
                "POSPRINTWEB_HOLD_VOLUME_SECONDS", 3600),
            hold_threshold=_env_int("POSPRINTWEB_HOLD_THRESHOLD", 20),
            hold_window_seconds=_env_int("POSPRINTWEB_HOLD_WINDOW_SECONDS", 300),
            hold_for_seconds=_env_int("POSPRINTWEB_HOLD_FOR_SECONDS", 1800),
            hold_max_queue=_env_int("POSPRINTWEB_HOLD_MAX_QUEUE", 200),
            pow_bits=_env_int("POSPRINTWEB_POW_BITS", 18),
            pow_ttl_seconds=_env_int("POSPRINTWEB_POW_TTL_SECONDS", 300),
            global_burst=_env_int("POSPRINTWEB_GLOBAL_BURST", 8),
            global_burst_seconds=_env_int(
                "POSPRINTWEB_GLOBAL_BURST_SECONDS", 60),
            trust_proxy=_env_bool("POSPRINTWEB_TRUST_PROXY", False),
            proxy_hops=_env_int("POSPRINTWEB_PROXY_HOPS", 1),
            client_ip_header=os.environ.get(
                "POSPRINTWEB_CLIENT_IP_HEADER", "x-forwarded-for"
            )
            .strip()
            .lower(),
            db_path=os.environ.get(
                "POSPRINTWEB_DB", "/var/lib/posprintweb/prints.db"
            ),
            admin_keys=tuple(_env_list("POSPRINTWEB_ADMIN_KEYS")),
        )
