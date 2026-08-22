"""What the camera feed actually costs, measured rather than guessed.

    python web/scripts/camera-bitrate.py https://print.example.com

Camera quality is three knobs - CAMERA_WIDTH, CAMERA_FPS, CAMERA_QUALITY -
whose combined effect on bandwidth is hard to predict and easy to measure.
Guessing tends to go one of two ways: too timid, and the picture stays worse
than it needs to be; too bold, and a domestic uplink saturates for everyone in
the flat.

Two numbers matter and they are not the same:

    Mbit/s          out of the flat, ONCE, however many people are watching -
                    the relay is what makes that true
    GB/viewer-hour  off the VPS, which does scale with the audience and eats a
                    monthly traffic allowance
"""

from __future__ import annotations

import io
import sys
import time
import urllib.request

BOUNDARY = b"--posprintframe"


def measure(base: str, seconds: float = 10.0) -> None:
    url = base.rstrip("/") + "/api/camera.mjpg"
    print(f"sampling {url} for {seconds:.0f}s ...\n")

    data = bytearray()
    with urllib.request.urlopen(url, timeout=seconds + 30) as response:
        if "multipart" not in response.headers.get("Content-Type", ""):
            raise SystemExit("that did not answer with a multipart stream")
        # The clock starts here, not before the request. Nothing is sent until
        # the first frame exists, and a cold start means spawning ffmpeg at the
        # far end - which can take longer than the whole sampling window and
        # made this report zero frames on a perfectly healthy feed.
        started = time.monotonic()
        while time.monotonic() - started < seconds:
            chunk = response.read(4096)
            if not chunk:
                break
            data += chunk
    elapsed = time.monotonic() - started

    frames = data.count(BOUNDARY)
    if not frames:
        raise SystemExit("no frames arrived - is the feed live?")

    size = "unknown"
    try:
        from PIL import Image

        start = data.find(b"\xff\xd8")
        end = data.find(b"\xff\xd9", start)
        with Image.open(io.BytesIO(bytes(data[start:end + 2]))) as im:
            size = f"{im.size[0]}x{im.size[1]}"
    except Exception:  # noqa: BLE001 - Pillow is optional for this
        pass

    per_second = len(data) / elapsed
    print(f"  resolution     : {size}")
    print(f"  frame rate     : {frames / elapsed:.1f} fps")
    print(f"  per frame      : {len(data) / frames / 1024:.1f} KiB")
    print(f"  bitrate        : {per_second * 8 / 1_000_000:.2f} Mbit/s"
          "   <- out of the flat, once")
    print(f"  per viewer-hour: {per_second * 3600 / 1e9:.2f} GB"
          "        <- off the VPS, times the audience")
    monthly = 20_000 / (per_second * 3600 / 1e9) if per_second else 0
    print(f"  20 TB of VPS traffic is {monthly:,.0f} viewer-hours")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    measure(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 10.0)
