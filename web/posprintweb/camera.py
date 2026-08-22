"""A live view of the printer, proxied so the camera itself stays private.

The same rule as the printer API key: the browser never learns where the camera
is or how to talk to it. It asks this service for a JPEG; this service holds the
RTSP credentials and hands back a frame. A visitor who reads the page source
learns nothing they could point VLC at.

Two properties are deliberate and worth keeping.

**The producer only runs while someone is actually watching.** A request starts
it, and an idle timeout stops it. Nobody is looking most of the time, and a
camera in a flat that is not being read from is meaningfully different from one
that is.

**Frames are shared, not per-viewer.** One producer, one latest frame, handed to
everyone who asks. Ten people watching costs the same upstream bandwidth as one,
and the camera sees exactly one client no matter how popular the page gets.

That second property is what FrameHub is, and it is deliberately separate from
where the frames come from. Two things produce them:

    Camera       ffmpeg decoding RTSP, in the flat, next to the printer
    RelayCamera  one HTTP stream pulled from another posprintweb

The relay exists because a reverse proxy opens a *separate* connection per
viewer, so ten people watching used to mean ten MJPEG streams leaving a
domestic uplink. Pulling the feed once into a relay on a VPS and fanning it out
there makes the flat's cost independent of the audience: one decode, one
connection out of the house, however many people are looking.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Callable

log = logging.getLogger("posprintweb.camera")

SOI = b"\xff\xd8"      # JPEG start of image
EOI = b"\xff\xd9"      # end of image

# A frame this large is not a frame; it means the marker scan lost sync and the
# buffer is growing without bound.
_MAX_FRAME = 4 * 1024 * 1024


class FrameHub:
    """The latest frame, and every viewer waiting for the next one.

    Knows nothing about where frames come from. Subclasses supply a producer by
    implementing `configured`, `_running`, `_start` and `_teardown`.
    """

    def __init__(
        self,
        *,
        idle_timeout: float = 15.0,
        start_timeout: float = 12.0,
        retry_after: float = 5.0,
    ) -> None:
        self._idle_timeout = idle_timeout
        self._start_timeout = start_timeout
        # A wrong URL fails in about 200ms, so without a cooldown every page
        # load spawns another doomed producer against the camera.
        self._retry_after = retry_after
        self._failed_at: float = 0.0

        self._frame: bytes | None = None
        self._frame_at: float = 0.0
        self._last_request: float = 0.0
        self._idler: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

        # Viewers wait to be handed the next frame rather than polling for one.
        # At the camera's native rate a poll loop would either busy-wait or add
        # latency, and neither is necessary when the producer can just push.
        self._waiters: set[asyncio.Future] = set()
        self.viewers = 0

    # -- what a producer must provide -------------------------------------

    @property
    def configured(self) -> bool:
        raise NotImplementedError

    def _running(self) -> bool:
        raise NotImplementedError

    async def _start(self) -> None:
        raise NotImplementedError

    async def _teardown(self) -> None:
        raise NotImplementedError

    async def _note_failure(self) -> None:
        self._failed_at = time.monotonic()
        log.error("camera failed: %s", self.last_error)

    # -- public -----------------------------------------------------------

    async def frame(self) -> bytes | None:
        """The most recent frame, starting the producer if it is not running."""
        self._last_request = time.monotonic()
        if not self.configured:
            return None
        await self._ensure_running()
        return self._frame

    async def stream(
        self,
        max_wait: float = 10.0,
        allowed: Callable[[], bool] | None = None,
    ):
        """Yield frames as they arrive, for multipart/x-mixed-replace.

        One producer feeds every viewer, so a crowd costs the camera and the
        uplink exactly one decode. What does scale per viewer is the bytes
        leaving the server, which is what camera_max_viewers is for.

        `allowed` is re-checked on every frame, not once at the start. Without
        it, touching the killswitch stopped *new* viewers and left everyone
        already watching with a live picture of the flat - which is not what
        anybody means by a killswitch, and mattered more once a relay could
        hold one long connection on behalf of a whole audience.
        """
        self.viewers += 1
        try:
            last_sent = 0.0
            while True:
                if allowed is not None and not allowed():
                    return
                self._last_request = time.monotonic()
                await self._ensure_running()
                if not self._running():
                    return
                frame, at = self._frame, self._frame_at
                if frame is not None and at != last_sent:
                    last_sent = at
                    yield frame
                if not await self._next_frame(max_wait):
                    return                  # producer stalled; let the client retry
        finally:
            self.viewers -= 1

    async def stop(self) -> None:
        async with self._lock:
            await self._teardown()

    def status(self) -> dict:
        return {
            "configured": self.configured,
            "running": self._running(),
            "viewers": self.viewers,
            "frame_age": round(time.monotonic() - self._frame_at, 1) if self._frame else None,
            "last_error": self.last_error,
        }

    # -- internals --------------------------------------------------------

    async def _next_frame(self, timeout: float) -> bool:
        fut = asyncio.get_running_loop().create_future()
        self._waiters.add(fut)
        try:
            await asyncio.wait_for(fut, timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._waiters.discard(fut)

    def _publish(self, frame: bytes) -> None:
        self._frame = frame
        self._frame_at = time.monotonic()
        for fut in self._waiters:
            if not fut.done():
                fut.set_result(None)
        self._waiters.clear()

    async def _ensure_running(self) -> None:
        # Warm and running: the overwhelming majority of calls, and stream()
        # makes one per frame, so this stays a couple of attribute reads.
        if self._frame is not None and self._running():
            return

        if not self._running():
            if time.monotonic() - self._failed_at < self._retry_after:
                return                      # still cooling off from a failure
            async with self._lock:
                if not self._running():
                    await self._teardown()
                    await self._start()
                    if self._running():
                        self._idler = asyncio.create_task(self._idle_watch())

        # Wait for the first frame - whoever started the producer. Returning
        # early just because someone else got there first is what made a group
        # arriving together after an idle period see 503s: one request started
        # the capture and the other nine were handed a None while it warmed up.
        deadline = time.monotonic() + self._start_timeout
        while self._frame is None and time.monotonic() < deadline:
            if not self._running():
                break
            await asyncio.sleep(0.2)

        if self._frame is None:
            await self._note_failure()

    async def _idle_watch(self) -> None:
        """Stop the producer once nobody has asked for a while."""
        try:
            while True:
                await asyncio.sleep(1.0)
                if time.monotonic() - self._last_request > self._idle_timeout:
                    log.info("camera idle; stopping capture")
                    asyncio.create_task(self.stop())
                    return
        except asyncio.CancelledError:
            raise


class Camera(FrameHub):
    """ffmpeg decoding RTSP into a stream of JPEGs."""

    def __init__(
        self,
        rtsp_url: str,
        *,
        fps: int = 2,
        width: int = 640,
        quality: int = 6,
        idle_timeout: float = 15.0,
        start_timeout: float = 12.0,
    ) -> None:
        super().__init__(idle_timeout=idle_timeout, start_timeout=start_timeout)
        self._url = rtsp_url
        self._fps = fps
        self._width = width
        self._quality = quality

        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._draining: asyncio.Task | None = None
        # ffmpeg explains itself on stderr and then exits. Reading that only
        # after stdout closes loses the race against the request that is
        # waiting to report why there is no picture, so it is drained
        # continuously from the moment the process starts.
        self._stderr_tail: list[str] = []

        # ffmpeg's URL parser splits userinfo from host on the FIRST '@', so a
        # username or password containing one silently redirects the whole
        # connection at a host that does not exist. The resulting error talks
        # about DNS, which sends you looking anywhere but at your credentials.
        if rtsp_url.count("@") > 1:
            log.warning(
                "POSPRINTWEB_CAMERA_URL contains more than one '@'. If it is in "
                "the username or password it must be percent-encoded as %%40, "
                "or ffmpeg will treat part of the credentials as the hostname."
            )

    @property
    def configured(self) -> bool:
        return bool(self._url)

    def _running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _note_failure(self) -> None:
        """Turn a dead ffmpeg into something readable in the journal."""
        self._failed_at = time.monotonic()
        code = self._proc.returncode if self._proc is not None else None

        # Give the drain a moment to catch what ffmpeg said on its way out.
        for _ in range(10):
            if self._stderr_tail:
                break
            await asyncio.sleep(0.05)

        detail = " / ".join(self._stderr_tail[-3:]) or "no output from ffmpeg"
        self.last_error = f"ffmpeg exited ({code}): {detail}"[:400]
        log.error("camera failed: %s", self.last_error)

    async def _start(self) -> None:
        # -rtsp_transport tcp: UDP drops frames on wifi and ffmpeg then spends
        # its time complaining rather than decoding.
        # -an: no audio. A printer makes a noise; nobody needs to hear the flat.
        # -fflags nobuffer / -flags low_delay: this is a live view, so a frame
        # that arrives late is worth less than one that arrives now.
        filters = []
        if self._fps:                       # 0 means "whatever the camera sends"
            filters.append(f"fps={self._fps}")
        if self._width:                     # 0 means "do not rescale"
            filters.append(f"scale={self._width}:-2")

        args = [
            "ffmpeg",
            "-nostdin", "-loglevel", "error",
            "-fflags", "nobuffer", "-flags", "low_delay",
            "-rtsp_transport", "tcp",
            "-i", self._url,
            "-an",
        ]
        if filters:
            args += ["-vf", ",".join(filters)]
        args += ["-q:v", str(self._quality), "-f", "mjpeg", "-"]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "AV_LOG_FORCE_NOCOLOR": "1"},
            )
        except FileNotFoundError:
            self.last_error = "ffmpeg is not installed"
            log.error("ffmpeg not found; the camera needs it to decode RTSP")
            return
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            log.error("could not start ffmpeg: %s", exc)
            return

        self.last_error = None
        self._stderr_tail.clear()
        self._reader = asyncio.create_task(self._read_frames())
        self._draining = asyncio.create_task(self._drain_stderr())
        log.info("camera capture started")

    async def _drain_stderr(self) -> None:
        """Keep the last few lines ffmpeg complained about, as it complains."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self._stderr_tail.append(text)
                    del self._stderr_tail[:-5]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - diagnostics must not take the app down
            return

    async def _teardown(self) -> None:
        for task in (self._reader, self._idler, self._draining):
            if task is not None:
                task.cancel()
        self._reader = self._idler = self._draining = None

        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError):
                with_kill = getattr(proc, "kill", None)
                if with_kill:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
        self._frame = None

    async def _read_frames(self) -> None:
        """Split ffmpeg's MJPEG stdout into whole JPEGs.

        mjpeg over a pipe is a bare concatenation of images with no length
        prefix, so frames are found by their markers.
        """
        assert self._proc is not None and self._proc.stdout is not None
        buf = bytearray()
        try:
            while True:
                chunk = await self._proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    end = buf.find(EOI)
                    if end == -1:
                        break
                    start = buf.find(SOI)
                    if start == -1 or start > end:
                        del buf[: end + 2]      # junk before a usable frame
                        continue
                    self._publish(bytes(buf[start : end + 2]))
                    del buf[: end + 2]
                if len(buf) > _MAX_FRAME:
                    log.warning("frame buffer out of sync; dropping %d bytes", len(buf))
                    buf.clear()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            log.warning("camera read failed: %s", exc)
        finally:
            if self._proc is not None and self._proc.stderr is not None:
                try:
                    err = await asyncio.wait_for(self._proc.stderr.read(2000), timeout=1)
                    if err:
                        # RTSP failures are almost always credentials or a typo
                        # in the URL, and both are ours to fix, not a visitor's.
                        self.last_error = err.decode("utf-8", "replace").strip()[:300]
                        log.error("ffmpeg: %s", self.last_error)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    pass


# -- pulling a feed from another posprintweb ------------------------------


def boundary_of(content_type: str) -> bytes | None:
    """The multipart boundary marker, as it appears in the body."""
    match = re.search(r"boundary=([^;]+)", content_type or "", re.I)
    return f"--{match.group(1).strip()}".encode() if match else None


def take_frame(buf: bytearray, marker: bytes) -> bytes | None:
    """One frame off the front of `buf`, consuming it. None if more is needed.

    Reads the exact number of bytes the part declares rather than scanning for
    the next boundary. A JPEG can contain any byte sequence, including one that
    looks like the boundary, so scanning for it corrupts frames at random and
    only on some pictures - which is the worst kind of bug to be handed.
    """
    start = buf.find(marker)
    if start == -1:
        return None
    head_end = buf.find(b"\r\n\r\n", start + len(marker))
    if head_end == -1:
        return None

    head = bytes(buf[start + len(marker):head_end]).decode("latin-1")
    declared = re.search(r"content-length:\s*(\d+)", head, re.I)
    if not declared:
        raise ValueError("frame with no length")

    body = head_end + 4
    end = body + int(declared.group(1))
    if len(buf) < end:
        return None
    frame = bytes(buf[body:end])
    del buf[:end]
    return frame


class RelayCamera(FrameHub):
    """One HTTP stream pulled from another posprintweb, fanned out to many.

    A reverse proxy opens a separate connection per viewer, so ten people
    watching means ten MJPEG streams out of the flat. This pulls the feed once
    and hands it to everyone, which makes the flat's cost independent of the
    audience.

    Three things carry over from doing the same job in the browser, where an
    <img> could not be told the stream had stopped:

    - **A body that ends is a dead feed, not a completed download.** Nothing
      here is finished until someone stops watching.
    - **A silent stall needs a timeout.** The read timeout is set above the
      upstream's own producer-stall limit, so in the normal case the body ends
      first and this only catches a connection that is open but unfed.
    - **Frames are taken by declared length**, never by scanning for the next
      boundary. See take_frame.
    """

    def __init__(
        self,
        upstream_url: str,
        *,
        idle_timeout: float = 30.0,
        start_timeout: float = 15.0,
        read_timeout: float = 15.0,
    ) -> None:
        super().__init__(idle_timeout=idle_timeout, start_timeout=start_timeout)
        self._url = upstream_url
        self._read_timeout = read_timeout
        self._pump: asyncio.Task | None = None
        # None until we have asked. False means the upstream says the feed is
        # switched off, which is a different thing from being unable to reach
        # it, and is not worth retrying hard.
        self.upstream_live: bool | None = None

    @property
    def configured(self) -> bool:
        return bool(self._url)

    def _running(self) -> bool:
        return self._pump is not None and not self._pump.done()

    async def _start(self) -> None:
        self.last_error = None
        self._pump = asyncio.create_task(self._read_upstream())

    async def _teardown(self) -> None:
        for task in (self._pump, self._idler):
            if task is not None:
                task.cancel()
        self._pump = self._idler = None
        self._frame = None

    async def _read_upstream(self) -> None:
        import httpx

        timeout = httpx.Timeout(
            connect=5.0, read=self._read_timeout, write=5.0, pool=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("GET", self._url) as response:
                    if response.status_code == 404:
                        # Not an error: the upstream's own gates say the feed is
                        # off. Says so plainly so the relay does not report a
                        # switched-off camera as a broken one.
                        self.upstream_live = False
                        self.last_error = "the upstream feed is switched off"
                        return
                    if response.status_code != 200:
                        self.upstream_live = False
                        self.last_error = f"upstream returned {response.status_code}"
                        return

                    marker = boundary_of(response.headers.get("content-type", ""))
                    if marker is None:
                        self.last_error = "upstream sent no multipart boundary"
                        return

                    self.upstream_live = True
                    buf = bytearray()
                    async for chunk in response.aiter_bytes():
                        buf += chunk
                        while (frame := take_frame(buf, marker)) is not None:
                            self._publish(frame)
                        if len(buf) > _MAX_FRAME:
                            raise ValueError("upstream feed lost sync")

            # Falling out of the loop means the body ended, which for a feed
            # meant to outlive the page is a death rather than a completion.
            self.last_error = "the upstream feed ended"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("relay read failed: %s", self.last_error)

    def status(self) -> dict:
        return {**super().status(), "upstream_live": self.upstream_live,
                "upstream": _redacted(self._url)}


def _redacted(url: str) -> str:
    """The upstream address without any credentials, for /healthz."""
    return re.sub(r"//[^/@]*@", "//", url)
