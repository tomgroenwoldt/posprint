"""End-to-end tests against the HTTP surface, with a fake printer upstream."""

from __future__ import annotations

import os
import re

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
    POSPRINTWEB_GLOBAL_BURST="0",
    # Off here so every other test is not a proof-of-work benchmark. The check
    # itself is covered in test_challenge.py and switched on deliberately below.
    POSPRINTWEB_POW_BITS="0",
)

from posprintweb import app as appmod  # noqa: E402
from posprintweb.store import Store  # noqa: E402
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


# -- gallery over HTTP ----------------------------------------------------


def test_admin_endpoints_are_hidden_without_a_key(client):
    """404, not 401: a stranger should not learn the endpoint exists."""
    assert client.get("/api/admin/queue").status_code == 404
    assert client.post("/api/admin/gallery",
                       json={"id": 1, "action": "approve"}).status_code == 404


def test_gallery_is_empty_until_approved(client):
    send(client, message="hello gallery")
    assert client.get("/api/gallery").json()["entries"] == []

    queue = client.get("/api/admin/queue",
                       headers={"X-Admin-Key": "admin-secret"}).json()
    assert len(queue["queue"]) == 1
    assert queue["counts"]["new"] == 1

    row_id = queue["queue"][0]["id"]
    r = client.post("/api/admin/gallery", json={"id": row_id, "action": "approve"},
                    headers={"X-Admin-Key": "admin-secret"})
    assert r.status_code == 200

    entries = client.get("/api/gallery").json()["entries"]
    assert [e["message"] for e in entries] == ["hello gallery"]


def test_the_public_gallery_leaks_no_addresses(client):
    send(client, message="hello gallery", ip="203.0.113.9")
    queue = client.get("/api/admin/queue",
                       headers={"X-Admin-Key": "admin-secret"}).json()
    client.post("/api/admin/gallery",
                json={"id": queue["queue"][0]["id"], "action": "approve"},
                headers={"X-Admin-Key": "admin-secret"})

    body = client.get("/api/gallery").text
    assert "203.0.113.9" not in body
    assert '"ip"' not in body


def test_an_approved_entry_can_be_taken_down(client):
    send(client, message="regrettable in hindsight")
    queue = client.get("/api/admin/queue",
                       headers={"X-Admin-Key": "admin-secret"}).json()
    row_id = queue["queue"][0]["id"]
    key = {"X-Admin-Key": "admin-secret"}

    client.post("/api/admin/gallery", json={"id": row_id, "action": "approve"},
                headers=key)
    assert len(client.get("/api/gallery").json()["entries"]) == 1

    # It is listed under `approved`, which is how the page offers a way back.
    approved = client.get("/api/admin/queue?gallery=approved", headers=key).json()
    assert [e["id"] for e in approved["queue"]] == [row_id]

    client.post("/api/admin/gallery", json={"id": row_id, "action": "hide"},
                headers=key)
    assert client.get("/api/gallery").json()["entries"] == []


def test_pages_carry_what_they_need_to_draw_a_receipt(client):
    """Both surfaces render with the print preview's code, which needs these."""
    for path in ("/api/gallery", "/api/admin/queue"):
        body = client.get(path, headers={"X-Admin-Key": "admin-secret"}).json()
        assert body["columns"] == 48
        assert "é" in body["charset"]["printable"]
        assert body["charset"]["replacements"]["—"] == "-"


def test_an_unknown_gallery_list_is_rejected(client):
    r = client.get("/api/admin/queue?gallery=featured",
                   headers={"X-Admin-Key": "admin-secret"})
    assert r.status_code == 422


def test_a_short_page_offers_no_cursor(client):
    """Otherwise the page shows a 'Show older' button that fetches nothing."""
    send(client, message="only one")
    queue = client.get("/api/admin/queue",
                       headers={"X-Admin-Key": "admin-secret"}).json()
    client.post("/api/admin/gallery",
                json={"id": queue["queue"][0]["id"], "action": "approve"},
                headers={"X-Admin-Key": "admin-secret"})

    body = client.get("/api/gallery").json()
    assert len(body["entries"]) == 1
    assert body["next"] is None

    # A full page does offer one.
    full = client.get("/api/gallery?limit=1").json()
    assert full["next"] == full["entries"][0]["id"]


def test_approving_something_unprintable_is_a_404(client):
    r = client.post("/api/admin/gallery", json={"id": 4242, "action": "approve"},
                    headers={"X-Admin-Key": "admin-secret"})
    assert r.status_code == 404


def test_the_pages_are_served(client):
    for path in ("/", "/gallery", "/admin"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "text/html" in r.headers["content-type"]
        assert r.headers["cache-control"] == "no-store"


def test_the_page_normalises_the_message_in_exactly_one_place():
    """Indentation is content, and the send must not reach for .trim() again.

    The server has preserved leading spaces since the clean() fix, and the
    preview since the same commit - but the submit handler kept its own
    .trim(), so a drawing looked right on screen and arrived with its first
    line shoved left. Two copies of one rule is what allowed that gap.

    Checked from Python because there is no JS test runner, and this class of
    bug is invisible from the server: the request that arrives is well-formed,
    just missing a space nobody can see.
    """
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1]
          / "posprintweb" / "static" / "app.js").read_text(encoding="utf-8")

    assert "el.message.value.trim()" not in js, "the send is trimming again"
    # Once in the preview, once in the send - the same function, so they
    # cannot drift apart.
    assert js.count("asTyped(el.message.value)") == 2


def test_every_page_links_to_the_source(client):
    """It is a public service running in someone's flat; the code should be
    one click away from all of it, not just the front page."""
    for path in ("/", "/gallery", "/admin"):
        body = client.get(path).text
        assert "github.com/tomgroenwoldt/posprint" in body, path
        # Opening a new tab without this hands the target a window handle.
        assert 'rel="noopener noreferrer"' in body, path


def test_the_admin_shell_contains_no_data_and_no_key(client):
    """It is served unauthenticated, so it had better be inert."""
    body = client.get("/admin").text
    assert "admin-secret" not in body
    assert "X-Admin-Key" not in body


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
    art = "\n".join("⠃" * 20 for _ in range(10))
    r = send(client, message=art)
    assert r.status_code == 200
    job = fake.jobs[0]
    assert job["image_png"], "braille must be sent as an image, not as text"
    assert job["message"] == art


def test_braille_may_exceed_the_text_character_limit(client, fake):
    """1200 cells is far past max_chars=200, and entirely legitimate."""
    art = "\n".join("⠃" * 60 for _ in range(20))
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


def test_the_gallery_can_be_narrowed_to_one_day(client):
    """Every row the harness makes lands on the same day, so this checks the
    plumbing - the store tests cover the filtering across days."""
    key = {"X-Admin-Key": "admin-secret"}
    send(client, message="one")
    send(client, message="two", ip="203.0.113.2")
    for row in client.get("/api/admin/queue", headers=key).json()["queue"]:
        client.post("/api/admin/gallery",
                    json={"id": row["id"], "action": "approve"}, headers=key)

    body = client.get("/api/gallery").json()
    assert body["day"] is None
    assert [d["count"] for d in body["days"]] == [2]
    today = body["days"][0]["day"]

    same = client.get(f"/api/gallery?day={today}").json()
    assert same["day"] == today
    assert len(same["entries"]) == 2

    other = client.get("/api/gallery?day=1999-12-31").json()
    assert other["entries"] == []
    # The list still comes back, or the page could not offer a way out.
    assert other["days"] == body["days"]


def test_the_day_list_rides_only_with_the_first_page(client):
    """It cannot change while paging, so sending it again would be waste."""
    key = {"X-Admin-Key": "admin-secret"}
    send(client, message="one")
    send(client, message="two", ip="203.0.113.2")
    for row in client.get("/api/admin/queue", headers=key).json()["queue"]:
        client.post("/api/admin/gallery",
                    json={"id": row["id"], "action": "approve"}, headers=key)

    first = client.get("/api/gallery?limit=1").json()
    assert "days" in first
    later = client.get(f"/api/gallery?limit=1&before={first['next']}").json()
    assert "days" not in later
    assert len(later["entries"]) == 1


def test_a_malformed_day_never_reaches_the_store(client):
    for bad in ("lol", "1999-1-1", "' OR 1=1 --", "2026-08-18 OR 1"):
        assert client.get("/api/gallery", params={"day": bad}).status_code == 422


def test_every_script_and_stylesheet_carries_a_build_stamp(client):
    """A hand-kept list of filenames drifts; receipt.js fell off one. It is the
    renderer both the preview and the gallery use, so a stale copy would have
    the two disagree about the same message."""
    for path in ("/", "/gallery", "/admin"):
        html = client.get(path).text
        assert "/static/" in html
        for ref in re.findall(r'/static/[^"\']+', html):
            assert "?v=" in ref, f"{ref} on {path} is not stamped"


def test_the_camera_feed_is_read_rather_than_pointed_at():
    """An <img> cannot report that a stream has stopped.

    Measured in Chrome: when a multipart response ends - cleanly, by reset, or
    by going silent - the element fires no event at all, keeps complete ===
    true, and goes on showing its last frame. So the error handler meant to
    reconnect could only fire before the first frame arrived, and any failure
    after that froze the picture until someone reloaded the page. Reading the
    body instead is what makes the end of a stream observable.

    Structural, like the .trim() check above: there is no JS test runner, and
    nothing on the server can see this go wrong.
    """
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1]
          / "posprintweb" / "static" / "app.js").read_text(encoding="utf-8")

    assert 'fetch("/api/camera.mjpg"' in js
    # The old shape, and the one to keep out: handing the URL to the element.
    assert ".src = \"/api/camera.mjpg" not in js
    # The two ways a dead feed shows itself, both of which must stay handled.
    assert "CAMERA_TIMEOUT" in js
    assert "feed ended" in js


def test_the_burst_cap_holds_across_addresses(monkeypatch):
    """The API-level check: a flood from many addresses is still capped.

    TRUST_PROXY is on in this harness, so each request presents its own
    X-Forwarded-For - which is exactly the attacker's position when they have
    a pool of real addresses to rotate through, and the position in which every
    other limit here is worthless.
    """
    from dataclasses import replace

    monkeypatch.setattr(appmod, "cfg", replace(
        appmod.cfg, global_burst=3, global_burst_seconds=60))
    monkeypatch.setattr(appmod, "store", Store(":memory:", appmod.TZ))

    with TestClient(appmod.app) as client:
        codes = [
            send(client, message=f"flood {i}", ip=f"172.59.{i}.{i}").status_code
            for i in range(8)
        ]
        assert codes[:3] == [200, 200, 200]
        assert set(codes[3:]) == {429}

        # And it says when, rather than "later".
        blocked = send(client, message="one more", ip="203.0.113.7")
        assert blocked.status_code == 429
        assert 0 < int(blocked.headers["Retry-After"]) <= 60


def _solve(challenge, bits):
    from posprintweb.challenge import solved
    counter = 0
    while not solved(challenge, counter, bits):
        counter += 1
    return counter


def test_printing_needs_proof_of_work(monkeypatch):
    """The flood posted straight here, with no page and so no button to click.
    What it cannot skip is arriving with the work already done."""
    from dataclasses import replace

    from posprintweb.challenge import Challenges

    monkeypatch.setattr(appmod, "cfg", replace(appmod.cfg, pow_bits=8))
    monkeypatch.setattr(appmod, "challenges", Challenges(bits=8, ttl=300.0))
    monkeypatch.setattr(appmod, "store", Store(":memory:", appmod.TZ))

    with TestClient(appmod.app) as client:
        # What curl does: no proof at all.
        bare = client.post("/api/print", json={"message": "no work done"})
        assert bare.status_code == 428

        # What the page does.
        issued = client.get("/api/challenge").json()
        assert issued["bits"] == 8
        counter = _solve(issued["challenge"], issued["bits"])
        ok = client.post("/api/print", json={
            "message": "work done", "challenge": issued["challenge"],
            "counter": counter,
        })
        assert ok.status_code == 200

        # And that proof is spent - replaying it is worth nothing.
        again = client.post("/api/print", json={
            "message": "replayed", "challenge": issued["challenge"],
            "counter": counter,
        })
        assert again.status_code == 428


def test_the_admin_key_skips_the_proof(monkeypatch):
    """So a flood cannot lock you out of your own printer."""
    from dataclasses import replace

    from posprintweb.challenge import Challenges

    monkeypatch.setattr(appmod, "cfg", replace(appmod.cfg, pow_bits=8))
    monkeypatch.setattr(appmod, "challenges", Challenges(bits=8, ttl=300.0))
    monkeypatch.setattr(appmod, "store", Store(":memory:", appmod.TZ))

    with TestClient(appmod.app) as client:
        r = client.post("/api/print", json={"message": "mine"},
                        headers={"X-Admin-Key": "admin-secret"})
    assert r.status_code == 200


def test_zero_bits_disables_the_check(monkeypatch):
    monkeypatch.setattr(appmod, "store", Store(":memory:", appmod.TZ))
    with TestClient(appmod.app) as client:      # harness runs with pow_bits=0
        assert client.post("/api/print", json={"message": "no proof"}).status_code == 200


def _under_siege(monkeypatch, **overrides):
    """An app configured to hold, with a fresh store and siege state."""
    from dataclasses import replace

    from posprintweb.siege import Siege

    settings = dict(global_burst=2, global_burst_seconds=60,
                    hold_threshold=2, cooldown_seconds=0, per_ip_daily=0)
    settings.update(overrides)
    monkeypatch.setattr(appmod, "cfg", replace(appmod.cfg, **settings))
    monkeypatch.setattr(appmod, "siege", Siege(
        threshold=settings["hold_threshold"], window_seconds=300.0,
        hold_for_seconds=1800.0))
    monkeypatch.setattr(appmod, "store", Store(":memory:", appmod.TZ))


def test_a_flood_ends_up_printing_nothing(monkeypatch):
    """The whole point. Every other control prices abuse and hopes the price is
    enough; this one removes the outcome.

    Note what the burst cap turns into here. Refusals arrive before the hold
    does, so while a siege is on, the cap stops admitting messages to *paper*
    and starts admitting them to the *queue* - at the same 8 a minute, with the
    queue ceiling behind it. Either way the number of receipts is zero.
    """
    _under_siege(monkeypatch)

    with TestClient(appmod.app) as client:
        printed = refused = 0
        for i in range(40):                      # the flood, addresses and all
            r = send(client, message=f"flood {i}", ip=f"172.59.{i}.{i}")
            if r.status_code == 200:
                printed += 1
            elif r.status_code != 202:
                refused += 1

        assert refused > 0                       # the burst cap, doing its job
        # Two got through before the siege triggered, and nothing after: the
        # printer is no longer something the sender can reach.
        assert printed == 2
        assert appmod.siege.active() is True


def test_messages_are_held_rather_than_printed_during_a_siege(monkeypatch):
    """With the paper cap out of the way, so this is about the hold itself."""
    _under_siege(monkeypatch, global_burst=0)
    appmod.siege.refused()
    appmod.siege.refused()
    assert appmod.siege.active() is True

    with TestClient(appmod.app) as client:
        codes = [send(client, message=f"m{i}", ip=f"10.2.0.{i}").status_code
                 for i in range(6)]
        queue = client.get("/api/admin/held",
                           headers={"X-Admin-Key": "admin-secret"}).json()

    assert codes == [202] * 6                    # accepted, none printed
    assert queue["held"] == 6
    assert queue["siege"]["active"] is True
    # Oldest first: a queue to work through, not a feed to browse.
    assert [e["message"] for e in queue["queue"]][:2] == ["m0", "m1"]


def test_a_held_message_tells_the_sender_the_truth(monkeypatch):
    """Unlike the shadow filter, which lies on purpose. A held message is a
    real one that arrived at a bad moment, and its sender should know."""
    _under_siege(monkeypatch)

    with TestClient(appmod.app) as client:
        for i in range(12):
            r = send(client, message=f"flood {i}", ip=f"10.0.0.{i}")

        assert r.status_code in (202, 429)
        if r.status_code == 202:
            body = r.json()
            assert body["state"] == "held"
            assert "queue" in body["detail"]


def test_the_owner_can_release_or_discard(monkeypatch):
    _under_siege(monkeypatch, global_burst=0)
    appmod.siege.refused()
    appmod.siege.refused()
    key = {"X-Admin-Key": "admin-secret"}

    with TestClient(appmod.app) as client:
        for i in range(6):
            send(client, message=f"m{i}", ip=f"10.3.0.{i}")

        queue = client.get("/api/admin/held", headers=key).json()["queue"]
        assert len(queue) == 6

        # Releasing one actually prints it.
        r = client.post("/api/admin/held",
                        json={"id": queue[0]["id"], "action": "print"}, headers=key)
        assert r.status_code == 200
        assert r.json()["held"] == 5

        # And it cannot be printed twice, however many times the button is hit.
        again = client.post("/api/admin/held",
                            json={"id": queue[0]["id"], "action": "print"}, headers=key)
        assert again.status_code == 404

        # One discarded stays in the log rather than vanishing.
        client.post("/api/admin/held",
                    json={"id": queue[1]["id"], "action": "discard"}, headers=key)

        # The rest go in one sweep, because after a flood there are hundreds.
        emptied = client.post("/api/admin/held",
                              json={"id": 1, "action": "empty"}, headers=key).json()
        assert emptied["held"] == 0


def test_the_siege_can_be_lifted_by_hand(monkeypatch):
    _under_siege(monkeypatch)
    key = {"X-Admin-Key": "admin-secret"}

    with TestClient(appmod.app) as client:
        for i in range(12):
            send(client, message=f"flood {i}", ip=f"10.0.0.{i}")
        assert appmod.siege.active() is True

        r = client.post("/api/admin/held", json={"id": 1, "action": "lift"},
                        headers=key)
        assert r.status_code == 200
        assert r.json()["siege"]["active"] is False


def test_the_hold_queue_has_a_ceiling(monkeypatch):
    """A long siege must not be a way to grow the database without bound."""
    _under_siege(monkeypatch, hold_max_queue=3, global_burst=0)

    with TestClient(appmod.app) as client:
        appmod.siege.refused(); appmod.siege.refused()
        assert appmod.siege.active() is True

        codes = [send(client, message=f"m{i}", ip=f"10.1.0.{i}").status_code
                 for i in range(6)]

    assert codes.count(202) == 3
    assert codes.count(503) == 3


def test_the_admin_still_prints_during_a_siege(monkeypatch):
    """Being locked out of your own printer by an attacker would be its own
    kind of win for them."""
    _under_siege(monkeypatch)

    with TestClient(appmod.app) as client:
        for i in range(12):
            send(client, message=f"flood {i}", ip=f"10.0.0.{i}")
        assert appmod.siege.active() is True

        r = client.post("/api/print", json={"message": "mine"},
                        headers={"X-Admin-Key": "admin-secret"})
    assert r.status_code == 200
