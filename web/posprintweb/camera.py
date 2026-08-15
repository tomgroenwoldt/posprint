"""A live view of the printer, proxied so the camera itself stays private.

The same rule as the printer API key: the browser never learns where the camera
is or how to talk to it. It asks this service for a JPEG; this service holds the
RTSP credentials and hands back a frame. A visitor who reads the page source
learns nothing they could point VLC at.

Two properties are deliberate and worth keeping.

**ffmpeg only runs while someone is actually watching.** A request starts it, and
an idle timeout stops it. Nobody is looking most of the time, and a camera in a
flat that is not being read from is meaningfully different from one that is.

**Frames are shared, not per-viewer.** One ffmpeg, one latest frame, handed to
everyone who asks. Ten people watching costs the same upstream bandwidth as one,
and the camera sees exactly one client no matter how popular the page gets.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

log = logging.getLogger("posprintweb.camera")

SOI = b"\xff\xd8"      # JPEG start of image
EOI = b"\xff\xd9"      # end of image

# A frame this large is not a frame; it means the marker scan lost sync and the
# buffer is growing without bound.
_MAX_FRAME = 4 * 1024 * 1024


class Camera:
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
        self._url = rtsp_url
        self._fps = fps
        self._width = width
        self._quality = quality
        self._idle_timeout = idle_timeout
        self._start_timeout = start_timeout

        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._idler: asyncio.Task | None = None
        self._frame: bytes | None = None
        self._frame_at: float = 0.0
        self._last_request: float = 0.0
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

        # Viewers wait to be handed the next frame rather than polling for one.
        # At the camera's native rate a poll loop would either busy-wait or add
        # latency, and neither is necessary when the producer can just push.
        self._waiters: set[asyncio.Future] = set()
        self.viewers = 0

    # -- public -----------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self._url)

    async def frame(self) -> bytes | None:
        """The most recent frame, starting the capture if it is not running."""
        self._last_request = time.monotonic()
        if not self.configured:
            return None
        await self._ensure_running()
        return self._frame

    async def stream(self, max_wait: float = 10.0):
        """Yield frames as they arrive, for multipart/x-mixed-replace.

        One ffmpeg feeds every viewer, so a crowd costs the camera and the home
        uplink exactly one decode. What does scale per viewer is the bytes
        leaving the VPS, which is what camera_max_viewers is for.
        """
        self.viewers += 1
        try:
            last_sent = 0.0
            while True:
                self._last_request = time.monotonic()
                await self._ensure_running()
                if self._proc is None or self._proc.returncode is not None:
                    return
                frame, at = self._frame, self._frame_at
                if frame is not None and at != last_sent:
                    last_sent = at
                    yield frame
                if not await self._next_frame(max_wait):
                    return                      # producer stalled; let the client retry
        finally:
            self.viewers -= 1

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

    async def stop(self) -> None:
        async with self._lock:
            await self._teardown()

    def status(self) -> dict:
        return {
            "configured": self.configured,
            "running": self._proc is not None and self._proc.returncode is None,
            "viewers": self.viewers,
            "frame_age": round(time.monotonic() - self._frame_at, 1) if self._frame else None,
            "last_error": self.last_error,
        }

    # -- lifecycle --------------------------------------------------------

    async def _ensure_running(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        async with self._lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            await self._teardown()
            await self._spawn()

        # First viewer waits for a frame rather than getting a 503 that would
        # make a working camera look broken.
        deadline = time.monotonic() + self._start_timeout
        while self._frame is None and time.monotonic() < deadline:
            if self._proc is None or self._proc.returncode is not None:
                break
            await asyncio.sleep(0.2)

    async def _spawn(self) -> None:
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
        self._reader = asyncio.create_task(self._read_frames())
        self._idler = asyncio.create_task(self._idle_watch())
        log.info("camera capture started")

    async def _teardown(self) -> None:
        for task in (self._reader, self._idler):
            if task is not None:
                task.cancel()
        self._reader = self._idler = None

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

    async def _idle_watch(self) -> None:
        """Stop capturing once nobody has asked for a while."""
        try:
            while True:
                await asyncio.sleep(1.0)
                if time.monotonic() - self._last_request > self._idle_timeout:
                    log.info("camera idle; stopping capture")
                    asyncio.create_task(self.stop())
                    return
        except asyncio.CancelledError:
            raise

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
