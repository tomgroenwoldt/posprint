"""End-to-end tests against the HTTP surface, with a fake printer upstream."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# Config is read at import time, so the environment has to be set first.
os.environ.update(
    POSPRINTWEB_DB=":memory:",
    POSPRINTWEB_UPSTREAM_KEY="test-key",
    POSPRINTWEB_TRUST_PROXY="true",   # lets each test present its own IP
    POSPRINTWEB_COOLDOWN_SECONDS="60",
    POSPRINTWEB_PER_IP_DAILY="3",
    POSPRINTWEB_GLOBAL_DAILY="100",
    POSPRINTWEB_MAX_CHARS="200",
    POSPRINTWEB_MAX_LINES="10",
    POSPRINTWEB_QUIET_START="0",      # 0==0 disables quiet hours
    POSPRINTWEB_QUIET_END="0",
    POSPRINTWEB_KILLSWITCH="",
    POSPRINTWEB_ADMIN_KEYS="admin-secret",
    POSPRINTWEB_TZ="UTC",
    # Off by default here so the quota tests can send the same message twice
    # and still be testing quotas. Both are exercised directly in
    # test_shadow.py, and through the API by the tests that switch them on.
    POSPRINTWEB_REPEAT_HOURS="0",
    POSPRINTWEB_GLOBAL_HOURLY="0",
)

from posprintweb import app as appmod  # noqa: E402
from posprintweb.upstream import UpstreamError  # noqa: E402


class FakeUpstream:
    """Stands in for posprint. Records what would have been printed."""

    def __init__(self):
        self.jobs = []
        self.fail = False
        self.online = True
        self.state = "ready"

    async def start(self):
        pass

    async def stop(self):
        pass

    async def health(self):
        return {
            "ok": self.online,
            "state": self.state if self.online else "offline",
            "device_present": self.online,
        }

    async def print_message(self, *, message, name, columns, when, note="",
                            image_png=None):
        if self.fail:
            raise UpstreamError("the printer is offline", "offline")
        if self.state == "out_of_paper":
            raise UpstreamError("the printer is out of paper", "out_of_paper")
        self.jobs.append({"message": message, "name": name, "image_png": image_png})
        return {"job_id": f"job-{len(self.jobs)}", "state": "done"}


@pytest.fixture()
def fake():
    f = FakeUpstream()
    appmod.upstream = f
    return f


@pytest.fixture()
def override():
    """Temporarily change a setting.

    Config is a frozen dataclass, so monkeypatch.setattr cannot touch it.
    """
    saved: dict[str, object] = {}

    def _set(**kw):
        for key, value in kw.items():
            saved.setdefault(key, getattr(appmod.cfg, key))
            object.__setattr__(appmod.cfg, key, value)

    yield _set
    for key, value in saved.items():
        object.__setattr__(appmod.cfg, key, value)


@pytest.fixture()
def client(fake):
    # The context manager is required: without it the lifespan never runs.
    with TestClient(appmod.app) as c:
        yield c
    # Each test gets a clean quota ledger.
    appmod.store._db.execute("DELETE FROM prints")
    appmod.store._db.commit()


def send(client, message="hello printer", name="tom", ip="1.2.3.4", key=None):
    headers = {"X-Forwarded-For": ip}
    if key:
        headers["X-Admin-Key"] = key
    return client.post("/api/print", json={"message": message, "name": name},
                       headers=headers)


# -- happy path -----------------------------------------------------------


def test_index_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_status_reports_limits(client):
    s = client.get("/api/status", headers={"X-Forwarded-For": "9.9.9.9"}).json()
    assert s["limits"]["max_chars"] == 200
    assert s["you"]["remaining_today"] == 3
    assert s["online"] is True


def test_print_succeeds(client, fake):
    r = send(client)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert fake.jobs == [
        {"message": "hello printer", "name": "tom", "image_png": None}
    ]


def test_print_records_remaining_quota(client):
    assert send(client).json()["remaining_today"] == 2


# -- the public surface stays narrow --------------------------------------


def test_control_bytes_never_reach_the_printer(client, fake):
    """A visitor must not be able to smuggle ESC/POS commands through."""
    send(client, message="hi\x1b\x70\x00\x01 there")
    assert "\x1b" not in fake.jobs[0]["message"]
    assert "\x00" not in fake.jobs[0]["message"]


def test_extra_fields_are_ignored(client, fake):
    """No block passthrough: raw/drawer are not reachable from the web form."""
    r = client.post(
        "/api/print",
        json={"message": "hi", "name": "x", "blocks": [{"type": "drawer"}]},
        headers={"X-Forwarded-For": "1.2.3.4"},
    )
    assert r.status_code == 200
    assert len(fake.jobs) == 1


def test_asset_urls_carry_a_build_stamp(client):
    """A changed URL is the only thing that reaches an already-poisoned cache.

    no-cache fixes future deploys, but only after one revalidation, and a
    browser holding an asset from before that header existed will sit on it for
    a heuristic interval nobody can shorten.
    """
    html = client.get("/").text
    assert "/static/app.js?v=" in html
    assert "/static/style.css?v=" in html
    assert client.get("/").headers["cache-control"] == "no-store"


def test_static_assets_must_be_revalidated(client):
    """A deploy has to actually reach people.

    Without this header browsers cache app.js heuristically and keep running
    old JavaScript against a new API for days.
    """
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert "no-cache" in r.headers.get("cache-control", "")
    assert r.headers.get("etag")


def test_admin_log_is_hidden_without_a_key(client):
    assert client.get("/admin/log").status_code == 404


def test_admin_log_works_with_a_key(client):
    send(client)
    r = client.get("/admin/log", headers={"X-Admin-Key": "admin-secret"})
    assert r.status_code == 200
    assert r.json()["prints"][0]["message"] == "hello printer"


# -- validation -----------------------------------------------------------


def test_empty_message_is_rejected(client):
    assert send(client, message="   ").status_code == 422


def test_overlong_message_is_rejected(client):
    r = send(client, message="x " * 300)
    assert r.status_code == 422
    assert "Too long" in r.json()["detail"]


def test_absurd_payload_is_rejected_by_the_schema(client):
    r = send(client, message="x" * 6000)
    assert r.status_code == 422


def test_overlong_name_is_rejected(client):
    assert send(client, name="n" * 40).status_code == 422


def test_korean_is_refused_rather_than_printed_as_question_marks(client, fake):
    r = send(client, message="안녕하세요")
    assert r.status_code == 422
    assert "no glyph" in r.json()["detail"]
    assert fake.jobs == []


def test_braille_prints_as_a_picture(client, fake):
    """The one script with no glyphs that still prints perfectly.

    It bypasses max_chars and the codepage check entirely: what reaches the
    printer is a decoded bitmap, not text.
    """
    art = "\n".join("⠿" * 20 for _ in range(10))
    r = send(client, message=art)
    assert r.status_code == 200
    job = fake.jobs[0]
    assert job["image_png"], "braille must be sent as an image, not as text"
    assert job["message"] == art


def test_braille_may_exceed_the_text_character_limit(client, fake):
    """1200 cells is far past max_chars=200, and entirely legitimate."""
    art = "\n".join("⠿" * 60 for _ in range(20))
    assert len(art) > 200
    assert send(client, message=art).status_code == 200


def test_braille_mixed_with_text_is_refused(client, fake):
    r = send(client, message="look at this ⠿⠿⠿")
    assert r.status_code == 422
    assert "on its own" in r.json()["detail"]
    assert fake.jobs == []


def test_oversized_braille_is_refused(client):
    r = send(client, message="⠿" * 400)
    assert r.status_code == 422
    assert "wide" in r.json()["detail"]


def test_status_publishes_braille_limits(client):
    b = client.get("/api/status").json()["braille"]
    assert b["enabled"] is True
    assert b["max_cols"] > 48        # wider than the text column count
    assert b["printer_dots"] == 576


def test_emoji_is_refused(client):
    assert send(client, message="thanks 🙏").status_code == 422


def test_unprintable_name_is_refused(client):
    assert send(client, name="Ольга").status_code == 422


MWEOL = " /\\ /\\\n((ovo))\n():::()\n  VVV"


def test_ascii_art_keeps_its_indentation(client, fake):
    """The first line used to lose its leading spaces and nothing else did.

    clean() finished with .strip(), which trims the whole message rather than
    each line, so row one of a drawing arrived shifted left while rows two
    onward were untouched. On paper that reads as a printer fault - mweol's
    ears came out crooked.
    """
    assert send(client, message=MWEOL).status_code == 200
    assert fake.jobs[0]["message"] == MWEOL


def test_a_message_of_only_spaces_is_still_rejected(client):
    """clean() no longer strips indentation, so emptiness needs its own check."""
    assert send(client, message="    ").status_code == 422


def test_accents_are_allowed(client, fake):
    """The printer degrades these rather than failing, so they must get through.

    Refusing them would be worse than the bug being fixed: 'Grönwoldt' prints
    perfectly well in cp858, and even where it does not it arrives readable.
    """
    assert send(client, message="Grönwoldt café naïve").status_code == 200
    assert fake.jobs[0]["message"] == "Grönwoldt café naïve"


def test_smart_quotes_and_dashes_are_allowed(client):
    """Phone keyboards produce these constantly; posprint maps them to ASCII."""
    assert send(client, message="“hi” — it’s fine…").status_code == 200


def test_fallbacks_match_the_printer():
    """The web service copies posprint's replacement table; keep them equal.

    They cannot share the module: the two services deploy to separate
    containers and this one has no posprint checkout. Nothing but this test
    stops the copies drifting, at which point the page would promise a
    character the paper renders as '?'.
    """
    from posprint.escpos import _FALLBACK

    from posprintweb.filters import FALLBACK

    assert FALLBACK == _FALLBACK


def test_charset_is_published_for_the_preview(client):
    charset = client.get("/api/status").json()["charset"]
    assert "é" in charset["printable"]
    assert "안" not in charset["printable"]
    assert charset["replacements"]["—"] == "-"


def test_repeats_are_refused_across_addresses(client, fake, override):
    """The defence that does not care about the sender's IP."""
    override(repeat_hours=24)
    assert send(client, message="spam art", ip="1.1.1.1").status_code == 200
    r = send(client, message="spam art", ip="2.2.2.2")
    assert r.status_code == 429
    assert "already been printed" in r.json()["detail"]
    assert len(fake.jobs) == 1


def test_shadowed_message_looks_exactly_like_a_success(client, fake, override):
    """The sender must not be able to tell. Only the log knows."""
    override(shadowlist=("badword",), shadow_delay_ms=0)
    ok = send(client, message="hello there", ip="5.5.5.5")
    bad = send(client, message="badword here", ip="6.6.6.6")

    assert bad.status_code == ok.status_code == 200
    assert bad.json().keys() == ok.json().keys()
    assert bad.json()["ok"] is True and bad.json()["state"] == "printed"
    # ...but nothing reached the printer.
    assert [j["message"] for j in fake.jobs] == ["hello there"]


def test_a_shadowed_message_still_costs_the_sender_their_quota(client, override):
    override(shadowlist=("badword",), shadow_delay_ms=0)
    first = send(client, message="badword one", ip="7.7.7.7")
    assert first.json()["remaining_today"] == 2      # per_ip_daily is 3 here
    # And the cooldown applies, exactly as if it had printed.
    assert send(client, message="something else", ip="7.7.7.7").status_code == 429


def test_shadowed_messages_are_visible_to_the_owner(client, override):
    override(shadowlist=("badword",), shadow_delay_ms=0)
    send(client, message="badword here", ip="8.8.8.8")
    log = client.get("/admin/log", headers={"X-Admin-Key": "admin-secret"}).json()
    row = log["prints"][0]
    assert row["state"] == "shadowed"
    assert row["message"] == "badword here"


def test_the_filter_is_invisible_in_the_public_api(client, override):
    """Nothing may hint that a quiet filter exists."""
    override(shadowlist=("badword",))
    body = client.get("/api/status").text.lower()
    for leak in ("shadow", "blocklist", "badword", "filter"):
        assert leak not in body


def test_admin_key_bypasses_the_quiet_filter(client, fake, override):
    override(shadowlist=("badword",), shadow_delay_ms=0)
    send(client, message="badword here", key="admin-secret")
    assert fake.jobs and "badword" in fake.jobs[0]["message"]


def test_only_the_configured_header_is_trusted(client):
    """Behind Caddy, nothing strips CF-Connecting-IP.

    If the app read whichever forwarding header happened to be present, a
    visitor could send the one the proxy does not overwrite and hand themselves
    a fresh quota every request. Only POSPRINTWEB_CLIENT_IP_HEADER counts.
    """
    body = {"message": "hi", "name": ""}
    first = client.post(
        "/api/print", json=body,
        headers={"X-Forwarded-For": "1.1.1.1", "CF-Connecting-IP": "8.8.8.8"},
    )
    assert first.status_code == 200

    second = client.post(
        "/api/print", json=body,
        headers={"X-Forwarded-For": "1.1.1.1", "CF-Connecting-IP": "9.9.9.9"},
    )
    assert second.status_code == 429


# -- quotas ---------------------------------------------------------------


def test_cooldown_returns_429_with_retry_after(client):
    send(client)
    r = send(client)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0


def test_quota_is_per_ip(client):
    assert send(client, ip="1.1.1.1").status_code == 200
    assert send(client, ip="2.2.2.2").status_code == 200


def test_spoofed_forwarded_for_is_ignored_without_trust_proxy(client, override):
    """The setting exists because XFF is attacker-controlled by default."""
    override(trust_proxy=False)
    send(client, ip="1.1.1.1")
    r = send(client, ip="2.2.2.2")          # a fresh header, but the same peer
    assert r.status_code == 429


# -- failure handling -----------------------------------------------------


def test_printer_failure_returns_502(client, fake):
    fake.fail = True
    r = send(client)
    assert r.status_code == 502
    assert "offline" in r.json()["detail"]


def test_printer_failure_gives_the_quota_back(client, fake):
    fake.fail = True
    send(client, ip="7.7.7.7")
    fake.fail = False
    # Not 429: the failed attempt must not have consumed the cooldown.
    assert send(client, ip="7.7.7.7").status_code == 200


def test_offline_printer_shows_in_status(client, fake):
    fake.online = False
    assert client.get("/api/status").json()["online"] is False


def test_status_distinguishes_out_of_paper_from_offline(client, fake):
    """Two different errands for whoever has to fix it."""
    fake.state = "out_of_paper"
    assert client.get("/api/status").json()["printer_state"] == "out_of_paper"

    fake.online = False
    assert client.get("/api/status").json()["printer_state"] == "offline"


def test_out_of_paper_says_so_and_refunds_the_quota(client, fake):
    fake.state = "out_of_paper"
    r = send(client, ip="4.4.4.4")
    assert r.status_code == 502
    assert "out of paper" in r.json()["detail"]

    # The visitor did nothing wrong: no cooldown, no lost daily print.
    fake.state = "ready"
    assert send(client, ip="4.4.4.4").status_code == 200


# -- gates ----------------------------------------------------------------


def test_killswitch_blocks_printing(client, fake, tmp_path, override):
    flag = tmp_path / "disabled"
    flag.write_text("")
    override(killswitch_path=str(flag))
    r = send(client)
    assert r.status_code == 503
    assert fake.jobs == []


def test_quiet_hours_block_printing(client, override):
    override(quiet_start_hour=0, quiet_end_hour=24)
    assert send(client).status_code == 503


def test_admin_key_bypasses_the_cooldown(client):
    send(client, key="admin-secret")
    assert send(client, key="admin-secret").status_code == 200


def test_admin_key_bypasses_quiet_hours(client, override):
    override(quiet_start_hour=0, quiet_end_hour=24)
    assert send(client, key="admin-secret").status_code == 200


def test_wrong_admin_key_does_not_bypass(client):
    send(client)
    assert send(client, key="not-the-key").status_code == 429


# -- quiet-hours arithmetic ----------------------------------------------


@pytest.mark.parametrize(
    "hour,expected",
    [(23, True), (2, True), (7, True), (8, False), (12, False), (21, False), (22, True)],
)
def test_quiet_window_wraps_midnight(override, hour, expected):
    from datetime import datetime

    override(quiet_start_hour=22, quiet_end_hour=8)
    when = datetime(2026, 8, 12, hour, 30)
    assert appmod.in_quiet_hours(when) is expected
