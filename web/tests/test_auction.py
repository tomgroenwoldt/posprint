"""The auction pages: a manifest of listings that cannot be embedded.

eBay serves item pages with X-Frame-Options: SAMEORIGIN, so there is no iframe
to test. What there is instead is a JSON manifest turned into HTML at startup,
and the thing worth pinning is that the substitution actually happens - a page
that renders beautifully with a dead button is the failure this feature is
prone to, and shipped with once already.
"""

from __future__ import annotations

import json

import pytest

from posprintweb import app as appmod
from posprintweb import listings
from posprintweb.listings import BadManifest, Listing, Photo

FRAME = Listing(
    title="The first frame",
    url="https://www.ebay.com/itm/318796407274",
    note="Ends Tuesday",
    blurb="Around a hundred receipts.",
    photos=(Photo(src="/static/auction/frame.jpg", alt="The frame"),
            Photo(src="/static/auction/detail-1.jpg", alt="A detail",
                  caption="Top left.")),
)
SECOND = Listing(title="The second frame", url="https://www.ebay.com/itm/2",
                 photos=(Photo(src="/static/auction/two.jpg", alt="Another"),))
GONE = Listing(title="Already sold", url="https://www.ebay.com/itm/3", sold=True)


@pytest.fixture
def selling(monkeypatch):
    monkeypatch.setattr(appmod, "LISTINGS", (FRAME,))


@pytest.fixture
def selling_several(monkeypatch):
    monkeypatch.setattr(appmod, "LISTINGS", (FRAME, SECOND, GONE))


@pytest.fixture
def not_selling(monkeypatch):
    monkeypatch.setattr(appmod, "LISTINGS", ())


# -- the manifest ----------------------------------------------------------


def write(tmp_path, data):
    p = tmp_path / "auctions.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_a_missing_manifest_means_nothing_is_for_sale(tmp_path):
    """The ordinary state of a deployment that sells nothing. Not an error."""
    assert listings.load(tmp_path / "nope.json") == ()


def test_the_shipped_manifest_is_valid():
    """The one in the repository has to parse, or the service will not boot."""
    from posprintweb.config import _HERE
    loaded = listings.load(_HERE / "auctions.json")
    assert loaded
    assert all(x.title and x.url for x in loaded)


def test_an_empty_env_var_falls_back_to_the_default_path(monkeypatch):
    """An env file line reading `POSPRINTWEB_AUCTIONS=` with nothing after it
    sets the key to "", which os.environ.get returns in preference to its
    default. That became Path(""), which is ".", which exists - and the
    service died reading a directory. install.sh writes exactly that shape of
    line for the other optional paths, so it was one paste away."""
    from posprintweb.config import Config
    monkeypatch.setenv("POSPRINTWEB_AUCTIONS", "")
    assert Config.from_env().auctions_path.endswith("auctions.json")
    monkeypatch.setenv("POSPRINTWEB_AUCTIONS", "   ")
    assert Config.from_env().auctions_path.endswith("auctions.json")


def test_a_path_that_is_not_a_file_is_refused_by_name(tmp_path):
    """Reading a directory raises a bare PermissionError naming neither the
    setting nor the mistake."""
    with pytest.raises(BadManifest) as exc:
        listings.load(tmp_path)
    assert "not a file" in str(exc.value)


def test_either_shape_of_file_is_accepted(tmp_path):
    one = [{"title": "x", "url": "https://e.com/1"}]
    assert len(listings.load(write(tmp_path, one))) == 1
    assert len(listings.load(write(tmp_path, {"listings": one}))) == 1


@pytest.mark.parametrize("data,expected", [
    ([{"title": "x"}], "url"),
    ([{"url": "https://e.com/1"}], "title"),
    ([{"title": "x", "url": "ftp://e.com/1"}], "http"),
    ([{"title": "x", "url": "https://e.com/1",
       "photos": [{"src": "/a.jpg"}]}], "alt"),
])
def test_a_wrong_manifest_is_refused_with_something_actionable(
        tmp_path, data, expected):
    """Loudly, at startup. A page nobody can buy from is worse than a service
    that refused to start and said which field was wrong."""
    with pytest.raises(BadManifest) as exc:
        listings.load(write(tmp_path, data))
    assert expected in str(exc.value)


def test_a_url_that_would_run_is_refused(tmp_path):
    """Escaping renders javascript: inert as text and does nothing about it in
    an href."""
    with pytest.raises(BadManifest):
        listings.load(write(tmp_path, [
            {"title": "x", "url": "javascript:alert(1)"}]))


def test_hero_is_the_first_photo_and_rest_is_everything_after():
    assert FRAME.hero.src.endswith("frame.jpg")
    assert len(FRAME.rest) == 1
    assert Listing(title="x", url="https://e.com/1").hero is None


def test_live_drops_the_sold_ones():
    assert listings.live((FRAME, SECOND, GONE)) == (FRAME, SECOND)


# -- the nav ---------------------------------------------------------------


def test_every_slot_disappears_when_nothing_is_for_sale(not_selling):
    markup = f"{appmod.NAV_SLOT}{appmod.CTA_SLOT}{appmod.LOTS_SLOT}"
    assert appmod._fill_auction(markup, "index") == ""


def test_the_nav_link_appears_on_the_other_pages(selling):
    out = appmod._fill_auction(appmod.NAV_SLOT, "gallery")
    assert 'href="/auction"' in out and ">Auction</a>" in out
    assert "nav__link--current" not in out


def test_the_auction_page_marks_its_own_nav_entry(selling):
    out = appmod._fill_auction(appmod.NAV_SLOT, "auction")
    assert "nav__link--current" in out and 'aria-current="page"' in out


# -- the button under the camera -------------------------------------------


def test_one_listing_is_named(selling):
    out = appmod._fill_auction(appmod.CTA_SLOT, "index")
    assert "The first frame is up for auction" in out


def test_several_listings_are_counted(selling_several):
    """"The first frame is up for auction" stops being true the moment a
    second lot exists, and this feature has already shipped one claim that
    was not."""
    out = appmod._fill_auction(appmod.CTA_SLOT, "index")
    assert "2 things are up for auction" in out   # the sold one does not count


def test_everything_sold_still_offers_the_page(monkeypatch):
    monkeypatch.setattr(appmod, "LISTINGS", (GONE,))
    out = appmod._fill_auction(appmod.CTA_SLOT, "index")
    assert "was up for auction" in out


# -- the lots --------------------------------------------------------------


def test_a_lot_carries_its_title_note_blurb_photos_and_button(selling):
    out = appmod._fill_auction(appmod.LOTS_SLOT, "auction")
    assert "The first frame" in out
    assert "Ends Tuesday" in out
    assert "Around a hundred receipts." in out
    assert 'src="/static/auction/frame.jpg"' in out
    assert 'href="https://www.ebay.com/itm/318796407274"' in out
    assert 'rel="noopener noreferrer"' in out
    assert "Top left." in out                      # the caption on photo two


def test_the_first_photo_is_the_hero_and_loads_eagerly(selling):
    out = appmod._fill_auction(appmod.LOTS_SLOT, "auction")
    hero = out[out.index("auction__hero"):out.index("auction__cta")]
    assert 'loading="eager"' in hero
    assert out.count('loading="lazy"') == 1        # the one remaining photo


def test_a_sold_lot_keeps_its_pictures_and_loses_its_button(selling_several):
    out = appmod._fill_auction(appmod.LOTS_SLOT, "auction")
    sold = out[out.index("Already sold"):]
    assert "lot__sold" in out
    assert "itm/3" not in sold                     # no way to bid on it
    assert "Already sold" in out                   # but still on the page


def test_sold_lots_sort_last(selling_several):
    out = appmod._fill_auction(appmod.LOTS_SLOT, "auction")
    assert out.index("The first frame") < out.index("Already sold")
    assert out.index("The second frame") < out.index("Already sold")


def test_listing_text_is_escaped(monkeypatch):
    nasty = Listing(title='<script>alert(1)</script>',
                    url="https://e.com/1", note='" onload="x')
    monkeypatch.setattr(appmod, "LISTINGS", (nasty,))
    out = appmod._fill_auction(appmod.LOTS_SLOT, "auction")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert ' onload="' not in out


# -- the real pages --------------------------------------------------------


def test_the_real_auction_page_has_no_leftover_slots(selling):
    """The bug this feature shipped with: _versioned_page is called with
    "auction.html" while pages are keyed on "auction", so every substitution
    silently no-opped and the bid button's href was the literal placeholder.
    Built from the real file on purpose."""
    html = appmod._versioned_page("auction.html")
    for slot in (appmod.NAV_SLOT, appmod.CTA_SLOT, appmod.LOTS_SLOT):
        assert slot not in html
    assert 'href="https://www.ebay.com/itm/318796407274"' in html


def test_the_real_print_page_links_to_the_auction(selling):
    html = appmod._versioned_page("index.html")
    assert appmod.NAV_SLOT not in html and appmod.CTA_SLOT not in html
    assert 'href="/auction"' in html


def test_the_print_page_is_clean_with_nothing_for_sale(not_selling):
    html = appmod._versioned_page("index.html")
    assert "/auction" not in html
