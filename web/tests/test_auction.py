"""The auction page: a listing that cannot be embedded, so it is described.

eBay serves item pages with X-Frame-Options: SAMEORIGIN, so there is no iframe
to test. What there is instead is a page built at import time out of a static
file and three settings, and the thing worth pinning is that the substitution
actually happens - a page that renders beautifully with a dead button is the
failure this feature is prone to, and was shipping with until it was caught in
a browser.
"""

from __future__ import annotations

import dataclasses

import pytest

from posprintweb import app as appmod
from posprintweb.config import _env_url


@pytest.fixture
def selling(monkeypatch):
    """A config with something for sale."""
    cfg = dataclasses.replace(
        appmod.cfg,
        auction_url="https://www.ebay.de/itm/318796407274",
        auction_label="Auction",
        auction_note="Ends Sunday",
    )
    monkeypatch.setattr(appmod, "cfg", cfg)
    return cfg


@pytest.fixture
def not_selling(monkeypatch):
    cfg = dataclasses.replace(appmod.cfg, auction_url="", auction_note="")
    monkeypatch.setattr(appmod, "cfg", cfg)
    return cfg


# -- the nav ---------------------------------------------------------------


def test_the_slot_disappears_entirely_when_there_is_no_auction(not_selling):
    """Not hidden with CSS - absent. There is nothing to hide."""
    out = appmod._fill_auction(f"<nav>{appmod.NAV_SLOT}</nav>", "index")
    assert out == "<nav></nav>"


def test_the_nav_link_appears_on_the_other_pages(selling):
    out = appmod._fill_auction(f"<nav>{appmod.NAV_SLOT}</nav>", "gallery")
    assert 'href="/auction"' in out
    assert ">Auction</a>" in out
    # Not the current page, so it must not claim to be.
    assert "nav__link--current" not in out
    assert "aria-current" not in out


def test_the_auction_page_marks_its_own_nav_entry(selling):
    out = appmod._fill_auction(f"<nav>{appmod.NAV_SLOT}</nav>", "auction")
    assert "nav__link--current" in out
    assert 'aria-current="page"' in out


def test_the_label_is_configurable_and_escaped(monkeypatch):
    cfg = dataclasses.replace(
        appmod.cfg, auction_url="https://example.com/x",
        auction_label='Bid "now" & <b>win</b>', auction_note="")
    monkeypatch.setattr(appmod, "cfg", cfg)
    out = appmod._fill_auction(appmod.NAV_SLOT, "index")
    assert "<b>" not in out
    assert "&lt;b&gt;" in out and "&amp;" in out


# -- the page --------------------------------------------------------------


def test_the_note_is_dropped_when_unset(monkeypatch):
    cfg = dataclasses.replace(
        appmod.cfg, auction_url="https://example.com/x", auction_note="")
    monkeypatch.setattr(appmod, "cfg", cfg)
    out = appmod._fill_auction("<!--auction:note-->", "auction")
    assert out == ""


def test_the_note_renders_when_set(selling):
    out = appmod._fill_auction("<!--auction:note-->", "auction")
    assert out == '<p class="auction__note">Ends Sunday</p>'


def test_page_only_substitutions_do_not_leak_onto_other_pages(selling):
    """The url and note markers only exist on auction.html, but a stray one
    elsewhere must not be filled - the nav slot is the shared surface."""
    out = appmod._fill_auction("<!--auction:url--><!--auction:note-->", "index")
    assert out == "<!--auction:url--><!--auction:note-->"


# -- the regression this feature actually shipped with ---------------------


def test_the_real_page_gets_a_real_href(selling):
    """The bug: _versioned_page is called with "auction.html" while the pages
    are keyed on "auction", so comparing the wrong one left every auction-page
    substitution as a no-op. The page rendered perfectly and the bid button's
    href was the literal string "<!--auction:url-->".

    Built from the real file on purpose. A test against a stub string would
    have passed while the shipped page was broken.
    """
    html = appmod._versioned_page("auction.html")
    assert "<!--auction:url-->" not in html
    assert "<!--auction:note-->" not in html
    assert appmod.NAV_SLOT not in html
    assert 'href="https://www.ebay.de/itm/318796407274"' in html


def test_the_real_page_still_opens_the_listing_safely(selling):
    html = appmod._versioned_page("auction.html")
    assert 'rel="noopener noreferrer"' in html
    assert 'target="_blank"' in html


# -- the URL guard ---------------------------------------------------------


def test_env_url_accepts_what_belongs_in_an_href(monkeypatch):
    monkeypatch.setenv("X_URL", "https://www.ebay.de/itm/1")
    assert _env_url("X_URL") == "https://www.ebay.de/itm/1"
    monkeypatch.setenv("X_URL", "/static/auction/frame.jpg")
    assert _env_url("X_URL") == "/static/auction/frame.jpg"
    monkeypatch.delenv("X_URL")
    assert _env_url("X_URL") == ""


def test_env_url_refuses_a_scheme_that_would_run(monkeypatch):
    """Escaping makes javascript: inert as text and does nothing about it in an
    href, so it is refused at startup rather than rendered."""
    monkeypatch.setenv("X_URL", "javascript:alert(1)")
    with pytest.raises(SystemExit):
        _env_url("X_URL")
