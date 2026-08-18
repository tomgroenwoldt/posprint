"""Run the site locally.

    python web/scripts/dev.py            # talks to a real posprint at :8080
    python web/scripts/dev.py --fake     # pretends the printer is there
    python web/scripts/dev.py --camera   # adds a synthetic camera feed

`--camera-drop=8` ends every camera stream after 8 seconds, the way a real one
ends when its producer stalls. That ending is invisible to an <img>, so it is
the case worth being able to reproduce.

Sets defaults that make sense on a laptop (a local database file, no quiet
hours, generous quotas) so you can poke at the page without a printer, a
tunnel, or root. Nothing in here runs in production; the service is started by
`python -m posprintweb` under systemd.
"""

from __future__ import annotations

import asyncio
import io
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The fake printer echoes messages to this console, and messages contain
# whatever a visitor typed: accents, braille art, box drawing. A Windows
# terminal defaults to cp1252 and raises UnicodeEncodeError on all of it,
# turning a harmless echo into a 500 from the dev server.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - not a real tty
        pass

FAKE = "--fake" in sys.argv
CAMERA = "--camera" in sys.argv

# Seconds before the synthetic feed ends a stream, as a stalled producer makes
# the real one do. 0 leaves it running.
DROP_AFTER = float(
    next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--camera-drop=")),
         os.environ.get("POSPRINTWEB_DEV_CAMERA_DROP", "0") or 0))

if CAMERA:
    # Config is frozen and read at import time, so this has to be set
    # before posprintweb is imported rather than poked in afterwards.
    os.environ.setdefault("POSPRINTWEB_CAMERA_MODE", "always")

os.environ.setdefault("POSPRINTWEB_DB", str(ROOT / "dev-prints.db"))
os.environ.setdefault("POSPRINTWEB_KILLSWITCH", str(ROOT / "dev-disabled"))
os.environ.setdefault("POSPRINTWEB_UPSTREAM", "http://127.0.0.1:8080")
os.environ.setdefault("POSPRINTWEB_HOST", "127.0.0.1")
os.environ.setdefault("POSPRINTWEB_PORT", "8000")
os.environ.setdefault("POSPRINTWEB_QUIET_START", "0")
os.environ.setdefault("POSPRINTWEB_QUIET_END", "0")
os.environ.setdefault("POSPRINTWEB_COOLDOWN_SECONDS", "5")
os.environ.setdefault("POSPRINTWEB_PER_IP_DAILY", "50")
os.environ.setdefault("POSPRINTWEB_ADMIN_KEYS", "dev-admin")

from posprintweb import app as appmod  # noqa: E402

if FAKE:

    class FakePrinter:
        async def start(self):
            print("  [fake printer] no real hardware; jobs go to stdout")

        async def stop(self):
            pass

        async def health(self):
            return {"ok": True, "state": "ready", "device_present": True, "paper": 80}

        async def print_message(self, *, message, name, columns, when, note="",
                                image_png=None):
            bar = "=" * columns
            print(f"\n{bar}\n{'INCOMING'.center(columns)}")
            print(f"{when.strftime('%Y-%m-%d %H:%M').center(columns)}\n{bar}")
            if image_png is not None:
                # Braille art travels as a decoded bitmap. Show the shape of
                # what would print rather than dumping PNG bytes to a terminal.
                print(f"[image: {len(image_png)} bytes of PNG]")
                print(message)
            else:
                print(message)
            print("-" * columns)
            print(f"from: {name or 'someone on the internet'}".rjust(columns))
            print(f"{bar}\n")
            return {"job_id": "fake-1", "state": "done"}

    appmod.upstream = FakePrinter()

if CAMERA:

    class FakeCamera:
        """A synthetic feed, so the page can be exercised without a camera.

        The point of it is the failure, not the picture. A real stream ends on
        its own - the producer stalls and the server closes the response - and
        that ending is invisible to an <img>, which is what made "the camera
        needs a page refresh" so hard to pin down. POSPRINTWEB_DEV_CAMERA_DROP
        makes it happen on a schedule instead of by luck.
        """

        configured = True

        def __init__(self) -> None:
            self.viewers = 0
            self.last_error: str | None = None
            self.drop_after = DROP_AFTER
            self._n = 0

        def _jpeg(self) -> bytes:
            from PIL import Image, ImageDraw

            self._n += 1
            im = Image.new("RGB", (640, 360), (18, 18, 22))
            draw = ImageDraw.Draw(im)
            draw.text((20, 20), f"fake camera - frame {self._n}",
                      fill=(235, 235, 235))
            # A moving element, so a frozen picture is obvious at a glance.
            x = 20 + (self._n * 17) % 560
            draw.rectangle([x, 180, x + 60, 240], fill=(200, 90, 60))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=70)
            return buf.getvalue()

        async def frame(self) -> bytes:
            return self._jpeg()

        async def stream(self):
            self.viewers += 1
            started = time.monotonic()
            try:
                while True:
                    if self.drop_after and (
                            time.monotonic() - started) > self.drop_after:
                        print("  [fake camera] ending the stream, as a stalled "
                              "producer does")
                        return
                    yield self._jpeg()
                    await asyncio.sleep(0.5)
            finally:
                self.viewers -= 1

        async def stop(self) -> None:
            pass

        def health(self) -> dict:
            return {"configured": True, "running": True, "viewers": self.viewers}

    appmod.camera = FakeCamera()
    print("  [fake camera] synthetic feed at /api/camera.mjpg")

if __name__ == "__main__":
    import uvicorn

    print(f"  http://127.0.0.1:{appmod.cfg.port}")
    uvicorn.run(appmod.app, host=appmod.cfg.host, port=appmod.cfg.port, log_level="warning")
