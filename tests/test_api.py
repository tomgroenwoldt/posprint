"""End-to-end tests against the HTTP API.

The printer is stood in for by an ordinary file: `discover_device` only checks
that the path exists and `write_bytes` only needs something openable, so the
full spooler -> device path is exercised for real rather than mocked out.
"""

from __future__ import annotations

import base64
import io
import os
import tempfile
import threading

import pytest

# Config is read from the environment at import time, so this must precede the
# app import.
_TMP = tempfile.mkdtemp(prefix="posprint-test-")
FAKE_DEVICE = os.path.join(_TMP, "lp0")
open(FAKE_DEVICE, "wb").close()

os.environ["POSPRINT_DEVICE"] = FAKE_DEVICE
os.environ["POSPRINT_API_KEY"] = "test-key"
os.environ["POSPRINT_PAPER_MM"] = "80"
os.environ["POSPRINT_CODEPAGE"] = "cp858"

from fastapi.testclient import TestClient  # noqa: E402

from posprint.app import app  # noqa: E402
from posprint.escpos import ESC, GS  # noqa: E402

KEY = {"X-API-Key": "test-key"}


@pytest.fixture
def client():
    # The context manager is required: it runs the lifespan hook that starts the
    # spooler thread. Without it every job sits in the queue forever.
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_device():
    with open(FAKE_DEVICE, "wb"):
        pass
    yield


def spooled() -> bytes:
    with open(FAKE_DEVICE, "rb") as fh:
        return fh.read()


# -- auth -----------------------------------------------------------------


def test_health_needs_no_key(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["device_present"] is True
    assert body["worker_alive"] is True
    assert body["config"]["columns"] == 48


def test_print_rejected_without_key(client):
    r = client.post("/print/text", json={"text": "nope"})
    assert r.status_code == 401


def test_print_rejected_with_wrong_key(client):
    r = client.post("/print/text", json={"text": "nope"}, headers={"X-API-Key": "bad"})
    assert r.status_code == 401


def test_bearer_token_is_accepted(client):
    r = client.post(
        "/print/text",
        json={"text": "hi"},
        headers={"Authorization": "Bearer test-key"},
    )
    assert r.status_code == 200


# -- printing -------------------------------------------------------------


def test_text_reaches_the_device(client):
    r = client.post("/print/text", json={"text": "Hello printer"}, headers=KEY)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "done"

    data = spooled()
    assert data.startswith(ESC + b"@")       # init
    assert b"Hello printer" in data
    assert data.endswith(GS + b"V" + bytes([66, 0]))  # partial cut


def test_auto_cut_can_be_disabled_per_job(client):
    r = client.post("/print/text", json={"text": "no cut", "cut": False}, headers=KEY)
    assert r.status_code == 200
    assert not spooled().endswith(GS + b"V" + bytes([66, 0]))


def test_accented_text_uses_the_codepage_not_ascii(client):
    r = client.post("/print/text", json={"text": "Grönwoldt café €5"}, headers=KEY)
    assert r.status_code == 200
    data = spooled()
    assert "Grönwoldt café €5".encode("cp858") in data
    assert b"EUR5" not in data


def test_block_document(client):
    payload = {
        "blocks": [
            {"type": "text", "text": "Header", "bold": True, "align": "center",
             "width": 2, "height": 2},
            {"type": "rule", "char": "="},
            {"type": "columns", "left": "Coffee", "right": "3.50"},
            {"type": "barcode", "data": "ABC123", "symbology": "code128"},
            {"type": "qr", "data": "https://example.com", "size": 5},
            {"type": "feed", "lines": 2},
            {"type": "cut", "partial": True},
        ],
        "label": "doc-test",
    }
    r = client.post("/print", json=payload, headers=KEY)
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "doc-test"

    data = spooled()
    assert b"Header" in data
    assert b"=" * 48 in data
    assert GS + b"k" + bytes([73, 8]) + b"{BABC123" in data
    assert GS + b"(k" in data
    # An explicit cut block must not be doubled by the auto-cut default.
    assert data.count(GS + b"V" + bytes([66, 0])) == 1


def test_receipt_renders_totals_and_items(client):
    payload = {
        "title": "Test Cafe",
        "items": [
            {"name": "Espresso", "qty": 2, "unit_price": 2.5},
            {"name": "Croissant", "qty": 1, "total": 3.0},
        ],
        "subtotal": 8.0,
        "tax": 1.68,
        "total": 9.68,
        "currency": "EUR",
        "footer_lines": ["Thank you"],
    }
    r = client.post("/print/receipt", json=payload, headers=KEY)
    assert r.status_code == 200, r.text

    text = spooled().decode("cp858")
    assert "Test Cafe" in text
    assert "2 x Espresso" in text
    assert "5.00 EUR" in text     # 2 x 2.50 computed
    assert "Croissant" in text
    assert "TOTAL" in text
    assert "9.68 EUR" in text
    assert "Thank you" in text


def test_raw_passthrough_is_byte_exact(client):
    raw = ESC + b"@" + b"raw bytes" + b"\n"
    r = client.post(
        "/print/raw",
        json={"data_base64": base64.b64encode(raw).decode()},
        headers=KEY,
    )
    assert r.status_code == 200
    assert spooled() == raw


def test_image_upload(client):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("L", (128, 8), 0).save(buf, format="PNG")
    r = client.post(
        "/print/image",
        files={"file": ("logo.png", buf.getvalue(), "image/png")},
        data={"dither": "false", "max_width": "128"},
        headers=KEY,
    )
    assert r.status_code == 200, r.text
    data = spooled()
    assert GS + b"v0" in data
    assert b"\xff" * 16 in data  # solid black row


def test_drawer_kick(client):
    r = client.post("/drawer", json={"pin": 0, "on_ms": 100, "off_ms": 200}, headers=KEY)
    assert r.status_code == 200
    assert spooled() == ESC + b"p" + bytes([0, 50, 100])


def test_self_test_page(client):
    r = client.post("/print/test", headers=KEY)
    assert r.status_code == 200, r.text
    assert b"POSPRINT" in spooled()


# -- validation -----------------------------------------------------------


def test_empty_document_rejected(client):
    r = client.post("/print", json={"blocks": []}, headers=KEY)
    assert r.status_code == 422


def test_unknown_block_type_rejected(client):
    r = client.post("/print", json={"blocks": [{"type": "hologram"}]}, headers=KEY)
    assert r.status_code == 422


def test_bad_base64_rejected(client):
    r = client.post("/print/raw", json={"data_base64": "not!base64!"}, headers=KEY)
    assert r.status_code == 422


def test_non_ascii_barcode_rejected_as_422_not_500(client):
    r = client.post(
        "/print",
        json={"blocks": [{"type": "barcode", "data": "café"}]},
        headers=KEY,
    )
    assert r.status_code == 422


def test_oversized_magnification_rejected(client):
    r = client.post("/print/text", json={"text": "x", "width": 99}, headers=KEY)
    assert r.status_code == 422


# -- jobs and failure handling -------------------------------------------


def test_job_is_retrievable_by_id(client):
    r = client.post("/print/text", json={"text": "trackme"}, headers=KEY)
    job_id = r.json()["id"]

    detail = client.get(f"/jobs/{job_id}", headers=KEY)
    assert detail.status_code == 200
    assert detail.json()["state"] == "done"
    assert detail.json()["bytes_written"] > 0

    listing = client.get("/jobs", headers=KEY).json()["jobs"]
    assert any(j["id"] == job_id for j in listing)


def test_unknown_job_id_is_404(client):
    assert client.get("/jobs/deadbeef", headers=KEY).status_code == 404


def test_missing_device_reports_502_not_500(client):
    os.rename(FAKE_DEVICE, FAKE_DEVICE + ".away")
    try:
        r = client.post("/print/text", json={"text": "gone"}, headers=KEY)
        assert r.status_code == 502, r.text
        body = r.json()
        assert body["state"] == "failed"
        assert "does not exist" in body["error"]
    finally:
        os.rename(FAKE_DEVICE + ".away", FAKE_DEVICE)


def test_health_reports_unhealthy_when_device_missing(client):
    os.rename(FAKE_DEVICE, FAKE_DEVICE + ".away")
    try:
        r = client.get("/health")
        assert r.status_code == 503
        assert r.json()["device_present"] is False
    finally:
        os.rename(FAKE_DEVICE + ".away", FAKE_DEVICE)


# -- out of paper ---------------------------------------------------------
#
# This is the failure that used to be invisible. A thermal printer with an empty
# roll still accepts bytes over USB, so the write succeeds, the job is marked
# done, and nothing is printed. Only the status byte knows.


def _paper(monkeypatch, paper_ok):
    from posprint import device

    def fake_status(path):
        return device.PrinterStatus(
            online=True, paper_ok=paper_ok, error=False, raw=None, source="test"
        )

    monkeypatch.setattr(device, "read_status", fake_status)


def test_out_of_paper_fails_the_job_instead_of_pretending(client, monkeypatch):
    _paper(monkeypatch, False)
    r = client.post("/print/text", json={"text": "into the void"}, headers=KEY)
    assert r.status_code == 502
    body = r.json()
    assert body["state"] == "failed"
    assert body["reason"] == "out_of_paper"
    # Nothing must reach the device: the bytes would sit in the printer's
    # buffer and surface later as a mystery receipt.
    assert spooled() == b""


def test_out_of_paper_shows_in_health(client, monkeypatch):
    _paper(monkeypatch, False)
    r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["state"] == "out_of_paper"
    assert body["paper_ok"] is False
    # Still present and working - that is the whole distinction.
    assert body["device_present"] is True


def test_unknown_paper_status_still_prints(client, monkeypatch):
    """Clones that do not implement LPGETSTATUS report None, not False.

    Treating None as empty would refuse every job forever on exactly the
    printers with no way to prove themselves.
    """
    _paper(monkeypatch, None)
    r = client.post("/print/text", json={"text": "hello"}, headers=KEY)
    assert r.status_code == 200
    assert spooled() != b""


def test_health_is_ready_when_paper_is_present(client, monkeypatch):
    _paper(monkeypatch, True)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["state"] == "ready"


def test_fire_and_forget_returns_202(client):
    r = client.post("/print/text", json={"text": "async", "wait": False}, headers=KEY)
    assert r.status_code == 202
    assert r.json()["state"] in ("queued", "printing", "done")


def test_concurrent_jobs_do_not_interleave(client):
    """The whole reason for the single-writer spooler.

    Twenty clients print simultaneously; every job must complete, and each must
    have written its own full byte count rather than a shredded mix.
    """
    results: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def fire(n: int) -> None:
        r = client.post(
            "/print/text",
            json={"text": f"job-{n:03d}", "timeout": 30},
            headers=KEY,
        )
        with lock:
            results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(results) == 20
    for status, body in results:
        assert status == 200, body
        assert body["state"] == "done"
        assert body["bytes_written"] == body["bytes"]
