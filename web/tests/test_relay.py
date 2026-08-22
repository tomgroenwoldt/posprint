"""The camera relay: pulling one feed and handing it to many.

Most of this is the multipart parser, because that is where a bug would be
quiet. A frame boundary read wrongly does not raise - it produces a picture
that is subtly wrong, on some images and not others, which is the worst kind of
failure to be handed.
"""

from __future__ import annotations

import asyncio

import pytest

from posprintweb.camera import FrameHub, RelayCamera, boundary_of, take_frame

MARKER = b"--posprintframe"


def part(payload: bytes, marker: bytes = MARKER) -> bytes:
    return (marker + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(payload)).encode() + b"\r\n\r\n" + payload + b"\r\n")


# -- the boundary ---------------------------------------------------------


def test_the_boundary_is_read_from_the_content_type():
    assert boundary_of("multipart/x-mixed-replace; boundary=posprintframe") == MARKER
    assert boundary_of("multipart/x-mixed-replace;boundary=abc ") == b"--abc"
    assert boundary_of("MULTIPART/X-MIXED-REPLACE; BOUNDARY=abc") == b"--abc"
    assert boundary_of("image/jpeg") is None
    assert boundary_of("") is None


# -- taking frames --------------------------------------------------------


def test_one_whole_frame():
    buf = bytearray(part(b"a-picture"))
    assert take_frame(buf, MARKER) == b"a-picture"


def test_several_frames_in_one_chunk():
    buf = bytearray(part(b"one") + part(b"two") + part(b"three"))
    assert [take_frame(buf, MARKER) for _ in range(3)] == [b"one", b"two", b"three"]
    assert take_frame(buf, MARKER) is None


def test_a_frame_split_across_chunks_waits_for_the_rest():
    whole = part(b"0123456789")
    for cut in range(1, len(whole)):
        buf = bytearray(whole[:cut])
        # Incomplete is not an error and not a short frame - it is "ask again".
        first = take_frame(buf, MARKER)
        if first is None:
            buf += whole[cut:]
            assert take_frame(buf, MARKER) == b"0123456789", cut
        else:
            assert first == b"0123456789", cut


def test_a_header_split_midway_waits_too():
    whole = part(b"xyz")
    header_end = whole.index(b"\r\n\r\n")
    buf = bytearray(whole[:header_end + 2])       # mid-way through the blank line
    assert take_frame(buf, MARKER) is None
    buf += whole[header_end + 2:]
    assert take_frame(buf, MARKER) == b"xyz"


def test_a_payload_containing_the_boundary_survives():
    """The reason frames are taken by declared length rather than by scanning.

    A JPEG is arbitrary bytes and may contain anything, including the boundary
    string. A scanner would cut the frame in half here, on some pictures and
    not others.
    """
    nasty = b"before" + MARKER + b"after" + MARKER + b"\r\nend"
    buf = bytearray(part(nasty) + part(b"next"))
    assert take_frame(buf, MARKER) == nasty
    assert take_frame(buf, MARKER) == b"next"


def test_leading_junk_before_the_first_boundary_is_skipped():
    buf = bytearray(b"preamble nobody asked for\r\n" + part(b"pic"))
    assert take_frame(buf, MARKER) == b"pic"


def test_a_part_with_no_length_is_an_error_not_a_guess():
    """Without a length there is nothing to do but scan, and scanning is the
    thing being avoided. Better to fail loudly and reconnect."""
    buf = bytearray(MARKER + b"\r\nContent-Type: image/jpeg\r\n\r\nbody\r\n")
    with pytest.raises(ValueError):
        take_frame(buf, MARKER)


def test_an_empty_buffer_is_not_an_error():
    assert take_frame(bytearray(), MARKER) is None


# -- the gate on a live stream --------------------------------------------


class FakeHub(FrameHub):
    """A hub with a producer that is always running and never fed."""

    def __init__(self) -> None:
        super().__init__(idle_timeout=999.0, start_timeout=0.1)
        self.alive = True

    @property
    def configured(self) -> bool:
        return True

    def _running(self) -> bool:
        return self.alive

    async def _start(self) -> None:
        pass

    async def _teardown(self) -> None:
        pass


def test_a_stream_stops_when_it_stops_being_allowed():
    """The killswitch has to reach people already watching.

    Checked once at the start, touching it stopped new viewers and left
    everyone else looking at a live picture of the flat - which is not what
    anybody means by a killswitch, and matters more once a relay holds one
    connection on behalf of a whole audience.
    """
    hub = FakeHub()
    live = {"ok": True}

    async def run():
        seen = []
        hub._publish(b"frame-1")
        async for frame in hub.stream(max_wait=0.05,
                                      allowed=lambda: live["ok"]):
            seen.append(frame)
            live["ok"] = False              # the killswitch, mid-stream
            hub._publish(b"frame-2")
        return seen

    assert asyncio.run(asyncio.wait_for(run(), timeout=5)) == [b"frame-1"]


def test_without_a_gate_a_stream_is_not_cut():
    hub = FakeHub()

    async def run():
        seen = []
        hub._publish(b"only-frame")
        async for frame in hub.stream(max_wait=0.05):
            seen.append(frame)
            hub.alive = False               # producer died; stream should end
        return seen

    assert asyncio.run(asyncio.wait_for(run(), timeout=5)) == [b"only-frame"]


# -- what the relay reports ----------------------------------------------


def test_the_upstream_address_is_redacted_in_status():
    """/healthz is the thing you curl during an incident, possibly over
    someone's shoulder."""
    relay = RelayCamera("http://user:hunter2@10.0.0.5:8000/api/camera.mjpg")
    reported = relay.status()["upstream"]
    assert "hunter2" not in reported
    assert reported == "http://10.0.0.5:8000/api/camera.mjpg"


def test_an_unconfigured_relay_is_not_running():
    relay = RelayCamera("")
    assert relay.configured is False
    assert relay.status()["running"] is False
    assert relay.upstream_live is None


def test_an_unreachable_upstream_is_503_not_an_empty_200():
    """A dead tunnel must not look like a working camera.

    upstream_live stays None when the pull fails to connect - as opposed to
    False, which means the container answered and said the feed is off - so
    nothing refused the request and the endpoint streamed nothing with a 200.
    During an outage that read as "the camera is fine, the site is broken",
    which sent the search in the wrong direction.
    """
    from fastapi.testclient import TestClient

    from posprintweb import relay

    relay.camera = RelayCamera("http://127.0.0.1:9/api/camera.mjpg")  # discard port
    with TestClient(relay.app) as client:
        assert client.get("/api/camera.mjpg").status_code == 503
        assert client.get("/api/camera.jpg").status_code == 503
        health = client.get("/healthz").json()["camera"]
    assert health["upstream_live"] is None
    assert health["last_error"]


def test_a_switched_off_feed_is_re_checked_rather_than_latched():
    """A "switched off" answer has to expire.

    The gate that reads it refuses the request before anything would re-ask, so
    a permanent answer means a feed switched back on - the killswitch lifted,
    quiet hours ending - stays dark until someone restarts the relay by hand.
    Which is exactly what happened: the printing killswitch also closes the
    camera, and lifting it left the relay serving 404s to a live feed.
    """
    relay = RelayCamera("http://127.0.0.1:9/api/camera.mjpg")
    assert relay.believed_off is False           # never asked, so not refusing

    relay._note_live(False)
    assert relay.believed_off is True            # asked just now, believe it

    relay._asked_at -= relay._recheck_after + 1  # ...and later
    assert relay.believed_off is False           # ask again rather than assume
    assert relay.upstream_live is False          # the last answer is still on record

    relay._note_live(True)
    assert relay.believed_off is False
