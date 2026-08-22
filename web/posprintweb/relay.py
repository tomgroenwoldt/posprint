"""The camera relay: one feed in, many viewers out.

    camera --> CT 111 (ffmpeg) --> relay on the VPS --+--> viewer
                                                      +--> viewer
              one connection out of the flat          +--> viewer

Caddy's reverse_proxy opens a *separate* connection per viewer, so before this
existed, ten people watching meant ten MJPEG streams leaving a domestic uplink.
That is the only reason CAMERA_MAX_VIEWERS was ever as low as six - not the
VPS, which has an order of magnitude more upload than a flat does.

This is deliberately not the whole site. It has no database, no printer, no
admin surface and no pages: it pulls one URL and hands out what it gets, so the
thing exposed on a rented machine is as small as the job allows.

**The container stays the authority on whether the feed may be shown.** It
holds the RTSP credentials, the camera mode and the killswitch; this only ever
sees the picture the site already publishes. When the container says the feed
is off, the pull gets a 404 and viewers here get one too - the privacy decision
does not move to the VPS along with the bandwidth.

Run it with:

    POSPRINTWEB_RELAY_UPSTREAM=http://<container>:8000/api/camera.mjpg \\
        python -m posprintweb.relay
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse

from .camera import RelayCamera
from .config import Config

log = logging.getLogger("posprintweb.relay")

cfg = Config.from_env()

camera = RelayCamera(
    cfg.relay_upstream,
    idle_timeout=cfg.relay_idle_timeout,
    read_timeout=15.0,
)

app = FastAPI(title="posprint camera relay", docs_url=None, redoc_url=None)

_NO_CACHE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}
BOUNDARY = "posprintframe"


@app.on_event("shutdown")
async def _shutdown() -> None:
    await camera.stop()


def _available() -> None:
    if not camera.configured:
        raise HTTPException(status_code=404, detail="not found")
    # The container said the feed is switched off. 404 rather than 503: whether
    # a camera exists at all is not something a closed feed should confirm, and
    # that judgement belongs upstream, not here.
    if camera.upstream_live is False:
        raise HTTPException(status_code=404, detail="not found")


@app.get("/api/camera.mjpg", include_in_schema=False)
async def stream() -> StreamingResponse:
    _available()
    if camera.viewers >= cfg.relay_max_viewers:
        raise HTTPException(
            status_code=503, detail="too many people are watching")

    # Start the pull and wait for the upstream's answer before committing to a
    # response. Without this the first viewer after a cold start gets a 200
    # with an empty body - the pull has not reported back yet - and only learns
    # the feed is off on their next attempt. This costs nothing: a viewer waits
    # for the first frame either way.
    picture = await camera.frame()
    _available()
    if picture is None:
        # Reached when the container cannot be reached at all, as opposed to
        # answering that the feed is off. Saying 200 here and streaming nothing
        # made a dead tunnel look like a working camera, which cost real time
        # during an outage: the site was 502 while this endpoint said 200.
        log.warning("relay cannot reach the upstream: %s", camera.last_error)
        raise HTTPException(
            status_code=503, detail="the camera is not available")

    async def frames():
        async for frame in camera.stream():
            yield (
                f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                f"Content-Length: {len(frame)}\r\n\r\n"
            ).encode() + frame + b"\r\n"

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers=_NO_CACHE,
    )


@app.get("/api/camera.jpg", include_in_schema=False)
async def frame() -> Response:
    _available()
    picture = await camera.frame()
    if picture is None:
        log.warning("relay has no frame: %s", camera.last_error)
        raise HTTPException(status_code=503, detail="the camera is not available")
    return Response(picture, media_type="image/jpeg", headers=_NO_CACHE)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    """What to look at after a deploy.

    The number that matters is not here but upstream: the container's viewer
    count should stay at 1 while this reports many. That difference is the
    whole point of the relay.
    """
    return {"ok": True, "camera": camera.status(),
            "max_viewers": cfg.relay_max_viewers}


def main() -> None:
    import uvicorn

    if not cfg.relay_upstream:
        raise SystemExit(
            "POSPRINTWEB_RELAY_UPSTREAM is unset. Point it at the container's "
            "feed, e.g. http://100.x.y.z:8000/api/camera.mjpg")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("relaying %s to at most %d viewers",
             camera.status()["upstream"], cfg.relay_max_viewers)
    uvicorn.run(app, host=cfg.relay_host, port=cfg.relay_port, log_level="info")


if __name__ == "__main__":
    main()
