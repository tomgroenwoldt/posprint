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

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

from .config import Config
from .filters import Rejected, check_message, check_name
from .models import PrintMessage
from .store import QuotaExceeded, Store
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
store = Store(cfg.db_path, TZ) if TZ else Store(cfg.db_path, datetime.now().astimezone().tzinfo)
upstream = Upstream(cfg.upstream_url, cfg.upstream_key, cfg.upstream_timeout)


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

    X-Forwarded-For is a request header like any other: without a trusted proxy
    rewriting it, anyone can send a fresh one per request and mint themselves
    unlimited quota. Hence the explicit opt-in.
    """
    if cfg.trust_proxy:
        fwd = request.headers.get("cf-connecting-ip") or request.headers.get(
            "x-forwarded-for", ""
        )
        if fwd:
            return fwd.split(",")[0].strip()
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
        "you": {
            "used_today": counts["used_today"],
            "remaining_today": max(0, cfg.per_ip_daily - counts["used_today"]),
        },
        "printed_today": counts["global_today"],
    }


@app.post("/api/print", summary="Print one message")
async def print_message(
    req: PrintMessage, request: Request, x_admin_key: str | None = Header(None)
) -> JSONResponse:
    admin = is_admin(x_admin_key)

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

    try:
        message = check_message(
            req.message,
            max_chars=cfg.max_chars,
            max_lines=cfg.max_lines,
            blocklist=cfg.blocklist,
        )
        name = check_name(
            req.name, max_chars=cfg.max_name_chars, blocklist=cfg.blocklist
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
            )
        except QuotaExceeded as exc:
            return JSONResponse(
                {"detail": exc.reason},
                status_code=429,
                headers={"Retry-After": str(exc.retry_after)},
            )

    try:
        result = await upstream.print_message(
            message=message, name=name, columns=cfg.columns, when=now_local()
        )
    except UpstreamError as exc:
        # The visitor did nothing wrong, so give the quota back.
        if reservation is not None:
            store.release(reservation)
        log.error("print failed for %s: %s", ip, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if reservation is not None:
        store.finish(reservation, "printed", result.get("job_id", ""))

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


@app.get("/admin/log", dependencies=[Depends(require_admin)], include_in_schema=False)
async def admin_log(limit: int = 50) -> dict:
    return {"prints": store.recent(limit)}


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    # no-store: the page embeds nothing per-visitor, but a stale copy after a
    # limit change is confusing, and this is not a high-traffic site.
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
