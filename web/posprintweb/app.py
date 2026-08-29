"""Public web front end for a private receipt printer.

Architecture, and the reason for it:

    browser  --(no credentials)-->  posprintweb  --(X-API-Key)-->  posprint

The posprint API key stays in this process. A static page that called posprint
directly would have to ship the key to every visitor, and that key unlocks
/print/raw (arbitrary ESC/POS) and /drawer (opens the cash drawer). So the
public surface here is deliberately narrow: one text field, one optional name,
and a document assembled server-side.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from html import escape
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import (Depends, FastAPI, Header, HTTPException, Query, Request,
                     Response)
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

from . import braille, shadow
from .camera import Camera
from .config import Config
from .filters import (
    FALLBACK,
    Rejected,
    check_message,
    check_name,
    clean,
    printable_charset,
)
from .captcha import BadCaptcha, Captchas
from .challenge import BadChallenge, Challenges
from .siege import Siege
from .models import GalleryDecision, HeldDecision, PrintMessage
from .store import HELD, QuotaExceeded, Store
from .upstream import Upstream, UpstreamError

log = logging.getLogger("posprintweb")

STATIC = Path(__file__).parent / "static"

cfg = Config.from_env()


def _tz():
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(cfg.timezone)
    except Exception:  # noqa: BLE001
        log.warning("unknown timezone %r, falling back to system local", cfg.timezone)
        return None


TZ = _tz()

# Computed once: it is a couple of hundred characters and the same for every
# request. The page needs it to render a preview that matches the paper.
CHARSET = printable_charset(cfg.codepage)
store = Store(cfg.db_path, TZ) if TZ else Store(cfg.db_path, datetime.now().astimezone().tzinfo)
upstream = Upstream(cfg.upstream_url, cfg.upstream_key, cfg.upstream_timeout)
challenges = Challenges(bits=cfg.pow_bits, ttl=cfg.pow_ttl_seconds)
captchas = Captchas(ttl=cfg.pow_ttl_seconds)
siege = Siege(
    threshold=cfg.hold_threshold,
    window_seconds=cfg.hold_window_seconds,
    hold_for_seconds=cfg.hold_for_seconds,
    volume=cfg.hold_volume,
    volume_seconds=cfg.hold_volume_seconds,
)

camera = Camera(
    cfg.camera_url,
    fps=cfg.camera_fps,
    width=cfg.camera_width,
    quality=cfg.camera_quality,
    idle_timeout=cfg.camera_idle_timeout,
)

# Monotonic timestamp of the last successful print, for the after_print window.
# Deliberately in memory: after a restart the feed is dark until someone prints,
# which is the safe direction to fail.
_last_print: float = 0.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if not cfg.upstream_key:
        log.warning(
            "POSPRINTWEB_UPSTREAM_KEY is unset. This only works if posprint "
            "itself is unauthenticated, which it should not be."
        )
    if not cfg.trust_proxy:
        log.info(
            "trust_proxy is off - rate limiting by socket peer address. "
            "Turn it on only when a trusted reverse proxy or tunnel is in front."
        )
    await upstream.start()
    try:
        yield
    finally:
        await upstream.stop()
        await camera.stop()
        # The store is deliberately not closed here. It is a process-lifetime
        # singleton and lifespan can run more than once per process, so closing
        # it on the first shutdown would leave the second startup with a dead
        # connection. Every write is already committed; there is nothing to
        # flush, and the OS closes the handle at exit.


app = FastAPI(
    title="posprint-web",
    version="1.0.0",
    summary="Public front end that lets strangers print on a home receipt printer",
    lifespan=lifespan,
)

# No CORS middleware on purpose. This service is same-origin: its own page is
# the only intended client, and there is no API key for a third-party site to
# use anyway. Opening CORS would only make it easier to build an abuse harness.


# -- helpers --------------------------------------------------------------


def client_ip(request: Request) -> str:
    """The address the quotas are keyed on.

    A forwarding header is a request header like any other: without a trusted
    proxy overwriting it, anyone can send a fresh one per request and mint
    themselves unlimited quota. Hence the explicit opt-in.

    Exactly one header is consulted, named by POSPRINTWEB_CLIENT_IP_HEADER, and
    only its first value. Reading several headers and taking whichever is
    present is the subtle version of the same bug: behind a proxy that sets
    X-Forwarded-For but does not strip CF-Connecting-IP, a visitor supplies the
    header nobody is overwriting and the rate limiter follows it.

    **Counted from the right, not the left.** Each proxy in the chain appends
    the peer address it saw, so the last entry is the one *our* proxy wrote and
    everything before it is whatever the sender chose to claim. Reading the
    leftmost value trusts the sender; reading from the right trusts the proxy.

    That makes a missing `header_up X-Forwarded-For {remote_host}` in Caddy an
    inefficiency rather than a hole. With the header overwritten there is one
    entry and this reads it; with it appended there are several and this reads
    the only one that was not attacker-supplied.

    proxy_hops is how many proxies of our own are in front. The client is the
    (hops)th entry from the end. If the header is shorter than that, the chain
    is not what the configuration claims, so the socket peer is used instead -
    which is a real address whatever else is wrong.
    """
    if cfg.trust_proxy:
        forwarded = [
            part.strip()
            for part in request.headers.get(cfg.client_ip_header, "").split(",")
            if part.strip()
        ]
        if len(forwarded) >= cfg.proxy_hops >= 1:
            return forwarded[-cfg.proxy_hops]
    return request.client.host if request.client else "unknown"


def now_local() -> datetime:
    return datetime.now(TZ) if TZ else datetime.now()


def in_quiet_hours(when: datetime | None = None) -> bool:
    when = when or now_local()
    start, end = cfg.quiet_start_hour, cfg.quiet_end_hour
    if start == end:
        return False
    hour = when.hour
    if start < end:
        return start <= hour < end
    # Wraps midnight, e.g. 22:00-08:00.
    return hour >= start or hour < end


def killed() -> bool:
    return not cfg.enabled or (
        bool(cfg.killswitch_path) and os.path.exists(cfg.killswitch_path)
    )


def camera_live() -> bool:
    """Whether the feed may be served right now.

    Every gate is checked on each request rather than cached, so the killswitch
    takes effect on the next frame and not on the next restart.
    """
    if cfg.camera_mode == "off" or not camera.configured:
        return False
    if cfg.camera_killswitch and os.path.exists(cfg.camera_killswitch):
        return False
    if killed():
        # If printing is switched off, so is the picture of the printer.
        return False
    if cfg.camera_mode == "always":
        return True
    return (time.monotonic() - _last_print) < cfg.camera_window_seconds


def is_admin(key: str | None) -> bool:
    return bool(key) and key in cfg.admin_keys


def require_admin(x_admin_key: str | None = Header(None)) -> None:
    if not is_admin(x_admin_key):
        raise HTTPException(status_code=404, detail="not found")


# -- routes ---------------------------------------------------------------


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/api/status", summary="Everything the page needs to render itself")
async def status(request: Request) -> dict:
    ip = client_ip(request)
    counts = store.counts(ip)
    printer = await upstream.health()
    return {
        "title": cfg.site_title,
        "blurb": cfg.site_blurb,
        "online": printer.get("ok", False) and not killed(),
        # "ready" | "out_of_paper" | "offline". `online` stays for anything
        # already reading it; this is the field that says which problem it is.
        "printer_state": printer.get("state", "offline"),
        "disabled": killed(),
        "quiet": in_quiet_hours(),
        "quiet_hours": {"start": cfg.quiet_start_hour, "end": cfg.quiet_end_hour},
        "local_time": now_local().strftime("%H:%M"),
        "limits": {
            "max_chars": cfg.max_chars,
            "max_lines": cfg.max_lines,
            "max_name_chars": cfg.max_name_chars,
            "columns": cfg.columns,
            "cooldown_seconds": cfg.cooldown_seconds,
            "per_ip_daily": cfg.per_ip_daily,
        },
        # What the printer can physically render. Sent rather than hardcoded in
        # the page so the preview cannot drift from what actually prints when
        # the code page changes.
        "charset": {"printable": CHARSET, "replacements": FALLBACK},
        # Braille is the exception to the charset: it has no glyphs but is
        # printed as a decoded picture, so the page must not refuse it.
        "camera": {"live": camera_live(), "mode": cfg.camera_mode},
        "braille": {
            "enabled": cfg.braille_enabled,
            "max_cols": cfg.braille_max_cols,
            "max_rows": cfg.braille_max_rows,
            "max_scale": cfg.braille_max_scale,
            "max_dots": cfg.braille_max_dots,
            "max_ink": cfg.braille_max_ink,
            "printer_dots": cfg.printer_dots,
        },
        "you": {
            "used_today": counts["used_today"],
            "remaining_today": max(0, cfg.per_ip_daily - counts["used_today"]),
        },
        "printed_today": counts["global_today"],
    }


@app.get("/api/admin/held", dependencies=[Depends(require_admin)],
         include_in_schema=False)
async def admin_held(limit: int = 50) -> dict:
    """The queue, plus whether the siege that filled it is still running."""
    return {
        "queue": store.held(limit),
        "held": store.held_count(),
        "siege": siege.status(),
        **_render_context(),
    }


@app.post("/api/admin/held", dependencies=[Depends(require_admin)],
          include_in_schema=False)
async def admin_decide_held(req: HeldDecision) -> dict:
    """Print one held message, discard one, or empty the queue.

    Printing here goes through the same upstream call as an ordinary print, so
    a released message is indistinguishable on paper from one that was never
    held. Braille is re-prepared rather than stored as a bitmap: the message is
    the thing that was kept, and the picture is derived from it.
    """
    if req.action == "empty":
        discarded = store.discard_all_held()
        log.info("discarded %d held messages", discarded)
        return {"ok": True, "discarded": discarded, "held": store.held_count()}

    if req.action == "discard":
        if not store.discard_held(req.id):
            raise HTTPException(status_code=404, detail="not in the queue")
        return {"ok": True, "held": store.held_count()}

    if req.action == "lift":
        siege.lift()
        log.info("siege lifted by hand")
        return {"ok": True, "siege": siege.status(), "held": store.held_count()}

    # Claimed inside the UPDATE, so two clicks cannot both start a print.
    row = store.take_held(req.id)
    if row is None:
        raise HTTPException(status_code=404, detail="not in the queue")

    art = None
    if cfg.braille_enabled and braille.contains(row["message"]):
        try:
            art = braille.prepare(
                clean(row["message"]),
                max_cols=cfg.braille_max_cols,
                max_rows=cfg.braille_max_rows,
                printer_dots=cfg.printer_dots,
                max_scale=cfg.braille_max_scale,
                max_dots=cfg.braille_max_dots,
            )
        except braille.Rejected:
            art = None

    try:
        result = await upstream.print_message(
            message=row["message"],
            name=row["name"],
            columns=cfg.columns,
            when=now_local(),
            image_png=art.png if art else None,
        )
    except UpstreamError as exc:
        # Back in the queue rather than lost, so a paper jam during a release
        # does not quietly destroy someone's message.
        store.set_state(req.id, HELD)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    store.set_state(req.id, "printed", result.get("job_id", ""))
    global _last_print
    _last_print = time.monotonic()
    log.info("released a held message (%d chars, job %s)",
             len(row["message"]), result.get("job_id"))
    return {"ok": True, "held": store.held_count()}


@app.get("/api/captcha", include_in_schema=False)
async def api_captcha() -> dict:
    """A puzzle, only worth fetching while a siege is on.

    Not sold as a wall - nothing visual is, and the module says so at length.
    It is a fast lane: solve it and print now rather than waiting in the queue.
    """
    if not cfg.captcha_enabled:
        raise HTTPException(status_code=404, detail="not found")
    return captchas.issue()


@app.get("/api/challenge", include_in_schema=False)
async def api_challenge() -> dict:
    """A puzzle to solve before printing.

    Cheap to issue and cheap to check; the expense is entirely in the search,
    which is the sender's to pay. Not cached, obviously - a reused challenge is
    a one-off cost rather than a per-print one.
    """
    return challenges.issue()


@app.post("/api/print", summary="Print one message")
async def print_message(
    req: PrintMessage, request: Request, x_admin_key: str | None = Header(None)
) -> JSONResponse:
    admin = is_admin(x_admin_key)

    # The outer gate, checked before the quotas so that every attempt costs the
    # sender CPU - including the ones a quota would have refused anyway. An
    # attacker who can probe for free can find the edge of every other limit
    # for nothing.
    #
    # The admin key skips it, which is also the answer to being locked out of
    # your own printer during a flood.
    if cfg.pow_bits > 0 and not admin:
        try:
            challenges.redeem(req.challenge, req.counter)
        except BadChallenge as exc:
            log.info("proof of work refused (%s) from %s", exc, client_ip(request))
            siege.refused()
            # 428: the request is fine, it is missing a precondition. The page
            # fetches a fresh challenge and solves it again; nothing about
            # which part failed is worth telling a sender who is guessing.
            raise HTTPException(
                status_code=428,
                detail="This page needs refreshing before it can print.",
            ) from None

    if killed() and not admin:
        raise HTTPException(
            status_code=503, detail="Printing is switched off right now. Try later."
        )
    if in_quiet_hours() and not admin:
        raise HTTPException(
            status_code=503,
            detail=(
                f"It is {now_local().strftime('%H:%M')} where the printer lives and "
                f"it is asleep until {cfg.quiet_end_hour:02d}:00."
            ),
        )

    art = None
    try:
        # Braille is routed away from the text checks entirely. It would fail
        # every one of them - no glyphs in the code page, far past max_chars -
        # yet it is the one thing the printer can reproduce perfectly, as a
        # decoded bitmap. Its limits are a grid, not a character count.
        if cfg.braille_enabled and braille.contains(req.message):
            art = braille.prepare(
                clean(req.message),
                max_cols=cfg.braille_max_cols,
                max_rows=cfg.braille_max_rows,
                printer_dots=cfg.printer_dots,
                max_scale=cfg.braille_max_scale,
                max_dots=cfg.braille_max_dots,
                max_ink=cfg.braille_max_ink / 100,
            )
            message = art.text
        else:
            message = check_message(
                req.message,
                max_chars=cfg.max_chars,
                max_lines=cfg.max_lines,
                blocklist=cfg.blocklist,
                codepage=cfg.codepage,
            )
        name = check_name(
            req.name,
            max_chars=cfg.max_name_chars,
            blocklist=cfg.blocklist,
            codepage=cfg.codepage,
        )
    except Rejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    ip = client_ip(request)

    if admin:
        reservation = None
    else:
        try:
            reservation = store.reserve(
                ip,
                name,
                message,
                cooldown_seconds=cfg.cooldown_seconds,
                per_ip_daily=cfg.per_ip_daily,
                global_daily=cfg.global_daily,
                global_hourly=cfg.global_hourly,
                global_burst=cfg.global_burst,
                global_burst_seconds=cfg.global_burst_seconds,
                repeat_hours=cfg.repeat_hours,
            )
        except QuotaExceeded as exc:
            # What tells an attack apart from a busy evening. Someone who keeps
            # hammering a closed door reports themselves; people taking turns
            # do not, because they wait.
            siege.refused()
            return JSONResponse(
                {"detail": exc.reason},
                status_code=429,
                headers={"Retry-After": str(exc.retry_after)},
            )

    # The quiet filter. Deliberately after the reservation, so the sender pays
    # for it exactly as if it had printed, and before the upstream call, so no
    # paper moves. The response below is byte-identical to a real success.
    hit = shadow.matches(f"{message}\n{name}", cfg.shadowlist)
    if hit and not admin:
        log.warning("shadowed a message from %s (matched %r): %r", ip, hit, message[:120])
        if reservation is not None:
            store.finish(reservation, "shadowed")
        # Without this the reply comes back far faster than a real print, which
        # is the one tell a determined sender could measure.
        await asyncio.sleep(cfg.shadow_delay_ms / 1000)
        counts = store.counts(ip)
        return JSONResponse(
            {
                "ok": True,
                "state": "printed",
                "remaining_today": max(0, cfg.per_ip_daily - counts["used_today"]),
                "next_allowed_in": cfg.cooldown_seconds,
            },
            status_code=200,
        )

    # Under siege, nothing reaches paper without a decision. This is the only
    # control here that is a guarantee rather than a price: a sender who can
    # afford every other cost still cannot make the printer print.
    #
    # The sender is told the truth, unlike the shadow filter. A held message is
    # a real one that arrived at a bad moment, and someone who wrote it
    # deserves to know it is queued rather than believing it printed.
    if siege.active() and not admin:
        # The fast lane. A siege otherwise makes everyone wait for the owner;
        # this lets a person prove they are one and print immediately, while
        # everything that cannot or will not queues exactly as before.
        #
        # Failing is not refusal, which is deliberate: someone who cannot see
        # the picture is not locked out of the printer, they just wait. That is
        # the whole reason this can be a visual puzzle at all.
        if req.captcha_token and cfg.captcha_enabled:
            try:
                captchas.redeem(req.captcha_token, req.captcha_answer)
                log.info("captcha solved by %s during a siege", ip)
                siege_pass = True
            except BadCaptcha as exc:
                log.info("captcha refused (%s) from %s", exc, ip)
                siege_pass = False
        else:
            siege_pass = False

        if not siege_pass:
            if store.held_count() >= cfg.hold_max_queue:
                if reservation is not None:
                    store.release(reservation)
                raise HTTPException(
                    status_code=503,
                    detail=("The printer is swamped and the queue is full. "
                            "Please try again later."),
                )
            if reservation is not None:
                store.finish(reservation, HELD)
            log.info("held a message from %s (siege, %ds left)",
                     ip, siege.seconds_left())
            counts = store.counts(ip)
            return JSONResponse(
                {
                    "ok": True,
                    "state": "held",
                    "detail": ("The printer is under load right now, so your "
                               "message is in the queue. It will print once I "
                               "have looked at it."),
                    "captcha_offered": cfg.captcha_enabled,
                    "remaining_today": max(
                        0, cfg.per_ip_daily - counts["used_today"]),
                    "next_allowed_in": cfg.cooldown_seconds,
                },
                status_code=202,
            )

    try:
        result = await upstream.print_message(
            message=message,
            name=name,
            columns=cfg.columns,
            when=now_local(),
            image_png=art.png if art else None,
        )
    except UpstreamError as exc:
        # The visitor did nothing wrong, so give the quota back.
        if reservation is not None:
            store.release(reservation)
        log.error("print failed for %s (%s): %s", ip, exc.reason, exc)
        detail = str(exc)
        if exc.reason == "out_of_paper":
            detail = (
                "The printer is out of paper. Nothing was printed and this did "
                "not use up any of your prints - try again once the roll is "
                "changed."
            )
        raise HTTPException(status_code=502, detail=detail) from exc

    if reservation is not None:
        store.finish(reservation, "printed", result.get("job_id", ""))

    # Opens the after_print camera window. Set even in "always" and "off" mode
    # so switching modes needs no restart to behave correctly.
    global _last_print
    _last_print = time.monotonic()
    # The signal that catches a sender who stays politely under every limit.
    # They can pace to avoid refusals; they cannot avoid the receipts.
    if not admin:
        siege.printed()

    log.info("printed %d chars for %s (job %s)", len(message), ip, result.get("job_id"))
    counts = store.counts(ip)
    return JSONResponse(
        {
            "ok": True,
            "state": result.get("state", "printed"),
            "remaining_today": max(0, cfg.per_ip_daily - counts["used_today"]),
            "next_allowed_in": cfg.cooldown_seconds,
        },
        status_code=200,
    )


_NO_CACHE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


@app.get("/api/camera.mjpg", include_in_schema=False)
async def camera_stream() -> StreamingResponse:
    """The live feed, as multipart/x-mixed-replace.

    An <img> pointed at this renders frames as they arrive over one connection.
    Polling a still image instead would mean a request per frame - at the
    camera's native rate, fifteen a second per viewer - to deliver the same
    pixels with more latency.
    """
    if not camera_live():
        # 404, not 403: whether a camera exists at all is not something a
        # closed feed should confirm.
        raise HTTPException(status_code=404, detail="not found")
    if camera.viewers >= cfg.camera_max_viewers:
        # The bottleneck is the flat's upstream bandwidth, not the VPS. Better
        # to turn someone away than to make the feed unwatchable for everyone.
        raise HTTPException(status_code=503, detail="too many people are watching")

    boundary = "posprintframe"

    async def frames():
        # camera_live is passed in rather than checked once above, so the
        # killswitch and the after_print window cut viewers who are already
        # watching. Checking only at the start meant touching the killswitch
        # stopped new viewers and left everyone else looking at the flat.
        async for frame in camera.stream(allowed=camera_live):
            yield (
                f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                f"Content-Length: {len(frame)}\r\n\r\n"
            ).encode() + frame + b"\r\n"

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers=_NO_CACHE,
    )


@app.get("/api/camera.jpg", include_in_schema=False)
async def camera_frame() -> Response:
    """A single JPEG: the poster frame, and a fallback if the stream drops."""
    if not camera_live():
        raise HTTPException(status_code=404, detail="not found")

    frame = await camera.frame()
    if frame is None:
        # Ours to fix - bad RTSP credentials, missing ffmpeg - so it is logged
        # in full and described vaguely in public.
        log.warning("camera produced no frame: %s", camera.last_error)
        raise HTTPException(status_code=503, detail="the camera is not available")

    return Response(content=frame, media_type="image/jpeg", headers=_NO_CACHE)


@app.get("/admin/log", dependencies=[Depends(require_admin)], include_in_schema=False)
async def admin_log(limit: int = 50) -> dict:
    """Recent prints, plus what the camera is doing.

    The viewer count is here because it is the one number that says whether the
    relay is working. With a relay in front this should read 1 however many
    people are watching the site; without one it climbs with the audience, and
    every one of those is an MJPEG stream leaving the flat. Admin-only: how
    many people are watching a camera in someone's home is not public.
    """
    return {"prints": store.recent(limit),
            "camera": camera.status(),
            "siege": siege.status()}


# -- gallery --------------------------------------------------------------


@app.get("/api/gallery", summary="Messages approved for the public gallery")
async def api_gallery(
    limit: int = 30,
    before: int | None = None,
    day: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict:
    """Public. Nothing is here until it has been approved by hand.

    The store does the projection, so `ip` is not one typo away from a page
    strangers can read. `day` is shape-checked here and matched as a parameter
    there, so a malformed one is a 422 and never reaches SQL.
    """
    limit = max(1, min(limit, 100))          # mirrors the clamp in the store
    entries = store.gallery(limit, before, day)
    # Keyset cursor: the caller asks for what comes *before* this id. Only
    # offered when the page came back full, or every gallery of any size would
    # advertise more to come and the page would show a "Show older" button that
    # fetches nothing.
    cursor = entries[-1]["id"] if len(entries) == limit else None
    body = {"entries": entries, "next": cursor, "day": day, **_render_context()}
    # The day list is the same for every page of a walk and only changes when
    # something is approved, so it rides along with the first request and not
    # with each "Show older" after it.
    if before is None:
        body["days"] = store.gallery_days()
    return body


def _render_context() -> dict:
    """What a page needs to draw a message the way the paper shows it.

    Sent with the entries rather than fetched separately so the gallery does
    not have to call /api/status, which would poll the printer for a page that
    has nothing to do with it.
    """
    return {
        "columns": cfg.columns,
        "charset": {"printable": CHARSET, "replacements": FALLBACK},
    }


@app.get("/api/admin/queue", dependencies=[Depends(require_admin)],
         include_in_schema=False)
async def admin_queue(
    limit: int = 50, gallery: Literal["new", "approved", "hidden"] = "new"
) -> dict:
    """One of the three lists. `approved` is how something already published
    gets taken back down."""
    return {
        "queue": store.review_queue(limit, gallery),
        "counts": store.review_counts(),
        **_render_context(),
    }


@app.post("/api/admin/gallery", dependencies=[Depends(require_admin)],
          include_in_schema=False)
async def admin_set_gallery(req: GalleryDecision) -> dict:
    value = {"approve": "approved", "hide": "hidden", "reset": "new"}[req.action]
    if not store.set_gallery(req.id, value):
        # Either no such row, or one that never reached paper. Both are the
        # caller asking for something that does not exist.
        raise HTTPException(status_code=404, detail="not found")
    log.info("gallery: %s -> %s", req.id, value)
    return {"ok": True, "id": req.id, "gallery": value, "counts": store.review_counts()}


NAV_SLOT = "<!--nav:auction-->"


def _fill_auction(html: str, page: str) -> str:
    """Put the auction into the nav and the page, or take it out entirely.

    Done here rather than from /api/status the way the title and blurb are.
    A nav item that appears a moment after the rest of the page has been drawn
    is a link that moves under the cursor as someone reaches for Gallery, and
    with no auction configured the markup should be absent rather than hidden
    - there is nothing to hide.

    Escaped on the way in because these come from the environment and land in
    an href. config._env_url has already refused anything that is not http,
    https or a /path, so this is the second of the two guards rather than the
    only one.
    """
    if not cfg.auction_url:
        # Leaves the slot empty on every page, and PAGES never builds
        # "auction" at all, so /auction is a 404.
        return html.replace(NAV_SLOT, "")

    current = page == "auction"
    html = html.replace(NAV_SLOT, (
        '<a href="/auction" class="nav__link{cls}"{aria}>{label}</a>'
    ).format(
        cls=" nav__link--current" if current else "",
        aria=' aria-current="page"' if current else "",
        label=escape(cfg.auction_label or "Auction"),
    ))

    if page != "auction":
        return html
    note = (f'<p class="auction__note">{escape(cfg.auction_note)}</p>'
            if cfg.auction_note else "")
    return (html
            .replace("<!--auction:url-->", escape(cfg.auction_url, quote=True))
            .replace("<!--auction:note-->", note))


def _versioned_page(name: str) -> str:
    """One HTML page with a build stamp on each asset URL.

    no-cache on /static makes a deploy reach people after one revalidation, but
    a visitor whose browser cached an asset *before* that header existed is
    still holding it under heuristic freshness - roughly a tenth of the file's
    age when they fetched it - and there is no way to reach into their cache
    and say otherwise.

    Changing the URL sidesteps the question. The page itself is no-store, so it
    is always fetched fresh, and a new asset URL is by definition not in
    anyone's cache. That closes the gap for people already stuck, and means
    future deploys land instantly instead of after a revalidation.
    """
    html = (STATIC / name).read_text(encoding="utf-8")
    # removesuffix, because this is called with "auction.html" while the pages
    # are keyed on "auction". Comparing the wrong one of those left the bid
    # button's href set to the literal placeholder text - a page that rendered
    # perfectly and did not do the single thing it exists to do.
    html = _fill_auction(html, name.removesuffix(".html"))
    stamp = max(int(p.stat().st_mtime) for p in STATIC.glob("*.*"))
    # Every reference, rather than a hand-kept list of filenames: receipt.js
    # was missing from one, and that is the file where a stale copy does the
    # most damage - it is the renderer both the preview and the gallery use, so
    # an old one would have them disagree about the same message.
    return re.sub(
        r"/static/([A-Za-z0-9_.-]+\.(?:js|css))",
        lambda m: f"/static/{m.group(1)}?v={stamp}",
        html,
    )


# Built once at import: the files cannot change under a running service, since
# install.sh restarts it.
# "auction" is only built when there is one, which is what makes /auction a
# 404 rather than an empty page on every deployment that is not selling
# anything.
_PAGE_NAMES = ("index", "gallery", "admin") + (
    ("auction",) if cfg.auction_url else ())
PAGES = {name: _versioned_page(f"{name}.html") for name in _PAGE_NAMES}

# no-store on every page: they embed nothing per-visitor, but a stale copy after
# a limit change is confusing, and it is what makes the asset stamping work.
_PAGE_HEADERS = {"Cache-Control": "no-store"}


@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    return HTMLResponse(PAGES["index"], headers=_PAGE_HEADERS)


@app.get("/gallery", include_in_schema=False)
async def gallery_page() -> HTMLResponse:
    return HTMLResponse(PAGES["gallery"], headers=_PAGE_HEADERS)


@app.get("/auction", include_in_schema=False)
async def auction_page() -> HTMLResponse:
    """The framed collage, and a link to the listing.

    404 when POSPRINTWEB_AUCTION_URL is unset, so this costs nothing on a
    deployment that has nothing for sale.
    """
    page = PAGES.get("auction")
    if page is None:
        raise HTTPException(status_code=404, detail="not found")
    return HTMLResponse(page, headers=_PAGE_HEADERS)


@app.get("/admin", include_in_schema=False)
async def admin_page() -> HTMLResponse:
    """An inert shell. Every byte of data on it arrives through the authed
    endpoints below, so serving it unauthenticated gives away only that an
    admin page exists - not what is in it."""
    return HTMLResponse(PAGES["admin"], headers=_PAGE_HEADERS)


class RevalidatingStatic(StaticFiles):
    """Serve /static with must-revalidate semantics.

    Without an explicit Cache-Control, browsers apply heuristic freshness to
    these files and can hold a cached app.js for days. After a deploy that
    means visitors run old JavaScript against a new API: the page went on
    saying "the printer is offline or out of paper" long after the server had
    learned to tell those two apart, and no amount of redeploying fixed it.

    "no-cache" does not mean "do not store" - the browser keeps the file but
    must revalidate before using it. StaticFiles already sends an ETag, so the
    common case is a 304 and a few bytes. On a page this size that is free, and
    it makes a deploy actually reach people.
    """

    def file_response(self, *args, **kwargs):  # noqa: ANN002, ANN003
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


app.mount("/static", RevalidatingStatic(directory=STATIC), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
