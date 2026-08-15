"""The camera feed, and the gates in front of it.

ffmpeg is never invoked here: the frame producer is replaced so the tests
exercise the parts that can be wrong in a way that matters - who is allowed to
see the feed, and whether frames reach a viewer.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

os.environ.update(
    POSPRINTWEB_DB=":memory:",
    POSPRINTWEB_UPSTREAM_KEY="test-key",
    POSPRINTWEB_QUIET_START="0",
    POSPRINTWEB_QUIET_END="0",
    POSPRINTWEB_KILLSWITCH="",
    POSPRINTWEB_ADMIN_KEYS="admin-secret",
    POSPRINTWEB_TZ="UTC",
    POSPRINTWEB_CAMERA_URL="rtsp://example.invalid/stream2",
)

from posprintweb import app as appmod  # noqa: E402
from posprintweb.camera import Camera  # noqa: E402

JPEG = b"\xff\xd8fake-frame\xff\xd9"


@pytest.fixture()
def client():
    with TestClient(appmod.app) as c:
        yield c


@pytest.fixture(autouse=True)
def settings():
    """Config is frozen, so changes go through object.__setattr__."""
    saved: dict[str, object] = {}

    def _set(**kw):
        for key, value in kw.items():
            saved.setdefault(key, getattr(appmod.cfg, key))
            object.__setattr__(appmod.cfg, key, value)

    yield _set
    for key, value in saved.items():
        object.__setattr__(appmod.cfg, key, value)


@pytest.fixture(autouse=True)
def fake_frames(monkeypatch):
    """Stand in for ffmpeg without pretending to be a subprocess.

    The URL is set here rather than trusted from the environment: posprintweb.app
    builds its Config at import, and whichever test module imports it first wins
    for the whole process.
    """
    async def frame(self):
        return JPEG

    monkeypatch.setattr(Camera, "frame", frame)
    monkeypatch.setattr(appmod.camera, "_url", "rtsp://example.invalid/stream2")


# -- the gates ------------------------------------------------------------


def test_feed_is_served_when_live(client, settings):
    settings(camera_mode="always")
    r = client.get("/api/camera.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == JPEG


def test_feed_is_not_cached(client, settings):
    settings(camera_mode="always")
    assert "no-store" in client.get("/api/camera.jpg").headers["cache-control"]


def test_mode_off_hides_the_feed_entirely(client, settings):
    """404 rather than 403: a closed feed should not confirm a camera exists."""
    settings(camera_mode="off")
    assert client.get("/api/camera.jpg").status_code == 404
    assert client.get("/api/camera.mjpg").status_code == 404


def test_killswitch_cuts_the_picture(client, settings, tmp_path):
    flag = tmp_path / "camera-off"
    flag.write_text("")
    settings(camera_mode="always", camera_killswitch=str(flag))
    assert client.get("/api/camera.jpg").status_code == 404


def test_camera_killswitch_does_not_stop_printing(client, settings, tmp_path):
    """Cutting the picture and cutting the printer are separate decisions."""
    flag = tmp_path / "camera-off"
    flag.write_text("")
    settings(camera_mode="always", camera_killswitch=str(flag))
    assert client.get("/api/status").json()["disabled"] is False


def test_printing_killswitch_also_cuts_the_picture(client, settings, tmp_path):
    """The reverse does not hold: no printing means no live view of a printer."""
    flag = tmp_path / "all-off"
    flag.write_text("")
    settings(camera_mode="always", killswitch_path=str(flag))
    assert client.get("/api/camera.jpg").status_code == 404


def test_after_print_mode_is_dark_until_something_prints(client, settings):
    settings(camera_mode="after_print", camera_window_seconds=90)
    appmod._last_print = 0.0
    assert client.get("/api/camera.jpg").status_code == 404
    assert client.get("/api/status").json()["camera"]["live"] is False


def test_after_print_mode_opens_a_window(client, settings, monkeypatch):
    import time

    settings(camera_mode="after_print", camera_window_seconds=90)
    monkeypatch.setattr(appmod, "_last_print", time.monotonic())
    assert client.get("/api/camera.jpg").status_code == 200


def test_viewer_cap_turns_people_away(client, settings, monkeypatch):
    """The flat's upstream bandwidth is the scarce resource, not the VPS."""
    settings(camera_mode="always", camera_max_viewers=2)
    monkeypatch.setattr(appmod.camera, "viewers", 2)
    r = client.get("/api/camera.mjpg")
    assert r.status_code == 503
    assert "watching" in r.json()["detail"]


def test_status_reports_liveness(client, settings):
    settings(camera_mode="always")
    cam = client.get("/api/status").json()["camera"]
    assert cam["live"] is True
    assert cam["mode"] == "always"


# -- frame splitting ------------------------------------------------------


def test_frames_are_split_on_jpeg_markers():
    """ffmpeg's mjpeg output is a bare concatenation with no length prefix."""
    cam = Camera("rtsp://example.invalid/stream2")
    seen: list[bytes] = []
    cam._publish = seen.append               # type: ignore[method-assign]

    class FakeStdout:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        async def read(self, _n):
            return self._chunks.pop(0) if self._chunks else b""

    class FakeProc:
        returncode = None
        stdout = FakeStdout([b"\xff\xd8one\xff\xd9\xff\xd8t", b"wo\xff\xd9", b""])
        stderr = None

    cam._proc = FakeProc()                   # type: ignore[assignment]
    asyncio.run(cam._read_frames())

    assert seen == [b"\xff\xd8one\xff\xd9", b"\xff\xd8two\xff\xd9"]
