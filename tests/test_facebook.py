"""Parser tests against REAL recorded Facebook responses.

fixtures/html/ holds pages captured on 2026-09-08: a good search, a throttled
search, and one item page. No network is touched here -- which is the point,
because the live adapter is the part guaranteed to break, and when it does these
tests say whether the parser or Facebook changed.
"""
from pathlib import Path

import pytest

from dealbot.models import Location
from dealbot.sources.facebook import FacebookSource, SourceBlocked

HTML = Path(__file__).resolve().parents[1] / "fixtures/html"
ABQ = Location(lat=35.0844, lng=-106.6504, radius_miles=25)


@pytest.fixture
def src():
    return FacebookSource(ABQ, city="albuquerque", min_interval_seconds=0)


def test_parses_a_real_search_page(src):
    raws = src.parse_search_html((HTML / "search-free-abq.html").read_text(errors="replace"))
    assert len(raws) == 15
    listings = [src.parse(r) for r in raws]
    assert all(l is not None for l in listings)
    assert all(l.url.startswith("https://www.facebook.com/marketplace/item/")
               for l in listings)
    titles = {l.title for l in listings}
    assert "Free Firewood" in titles


def test_free_is_zero_not_missing(src):
    raws = src.parse_search_html((HTML / "search-free-abq.html").read_text(errors="replace"))
    prices = [src.parse(r).price_cents for r in raws]
    assert 0 in prices                     # genuinely free
    assert None not in prices              # and never confused with "no price"


def test_search_feed_has_no_descriptions(src):
    """Why the two-stage fetch exists: the index is title-only, and real titles
    are frequently just 'Free'."""
    raws = src.parse_search_html((HTML / "search-free-abq.html").read_text(errors="replace"))
    listings = [src.parse(r) for r in raws]
    assert all(l.description is None for l in listings)
    assert any(l.title.strip().lower() == "free" for l in listings)


def test_silent_throttling_is_an_error_not_an_empty_result(src):
    """THE critical failure mode: Facebook keeps answering 200 with ~590KB of
    valid HTML containing no listings at all. Read as 'quiet day', the bot dies
    silently and looks healthy."""
    html = (HTML / "search-throttled.html").read_text(errors="replace")
    assert len(html) > 500_000            # a big, entirely plausible page
    assert "marketplace_listing_title" not in html
    with pytest.raises(SourceBlocked, match="throttled"):
        src.parse_search_html(html)


def test_detail_page_supplies_description_and_coordinates(src):
    raws = src.parse_search_html((HTML / "search-free-abq.html").read_text(errors="replace"))
    stub = next(src.parse(r) for r in raws
                if r.source_id == "1354512002236671")
    assert stub.description is None and stub.lat is None

    full = src.parse_detail_html(
        (HTML / "item-1354512002236671.html").read_text(errors="replace"), stub)
    assert "Pottery Barn Desk" in full.description
    assert full.lat == pytest.approx(35.0876, abs=0.01)
    assert full.distance_mi is not None and full.distance_mi < 5
    assert len(full.images) > 1
    assert full.id == stub.id              # identity is preserved by enrichment


def test_enrichment_keeps_the_raw_payload(src):
    raws = src.parse_search_html((HTML / "search-free-abq.html").read_text(errors="replace"))
    stub = next(src.parse(r) for r in raws if r.source_id == "1354512002236671")
    full = src.parse_detail_html(
        (HTML / "item-1354512002236671.html").read_text(errors="replace"), stub)
    assert full.raw                        # kept, so a parser fix can be replayed


def test_request_budget_is_enforced_by_the_adapter(src):
    """Rate limiting lives inside the source so a caller cannot bypass it."""
    src.max_requests = 0
    with pytest.raises(SourceBlocked, match="budget"):
        src._get("https://www.facebook.com/marketplace/albuquerque/search")


def test_category_surface_is_tried_before_search(src):
    """Both surfaces carry the same feed, and for a browse query the category
    page yields more: 24 listings vs 15 on the day these were captured."""
    assert src._surfaces("free") == [
        "https://www.facebook.com/marketplace/albuquerque/free",
        "https://www.facebook.com/marketplace/albuquerque/search?query=free",
    ]
    # A query with no category equivalent has only the search surface.
    assert len(src._surfaces("dewalt drill")) == 1


def test_a_gated_surface_falls_through_to_the_next(src, monkeypatch):
    """One gated surface must not fail the run while another still works."""
    good = (HTML / "category-free-abq.html").read_text(errors="replace")
    bad = (HTML / "search-throttled.html").read_text(errors="replace")
    calls = []

    def fake_get(url):
        calls.append(url)
        return bad if len(calls) == 1 else good

    monkeypatch.setattr(src, "_get", fake_get)
    hunt = type("H", (), {"queries": ("free",)})()
    raws = list(src.search(hunt))
    assert len(calls) == 2                 # first gated, second used
    assert len(raws) > 15                  # the category page is the richer one


def test_all_surfaces_gated_is_still_an_error(src, monkeypatch):
    bad = (HTML / "search-throttled.html").read_text(errors="replace")
    monkeypatch.setattr(src, "_get", lambda url: bad)
    hunt = type("H", (), {"queries": ("free",)})()
    with pytest.raises(SourceBlocked, match="all 2 surfaces gated"):
        list(src.search(hunt))


def test_category_page_parses(src):
    raws = src.parse_search_html((HTML / "category-free-abq.html").read_text(errors="replace"))
    assert len(raws) >= 20
    assert all(src.parse(r) is not None for r in raws)


def test_our_own_budget_is_not_reported_as_facebook_gating(src, monkeypatch):
    """Blaming Facebook for our politeness limit sends you debugging the wrong
    thing, and retrying surfaces cannot help."""
    from dealbot.sources.facebook import BudgetExhausted
    src.max_requests = 0
    hunt = type("H", (), {"queries": ("free",)})()
    with pytest.raises(BudgetExhausted, match="budget"):
        list(src.search(hunt))


def test_a_price_drop_to_free_is_not_lost_in_enrichment(src):
    """REGRESSION: `detail_price or search_price` treats 0 as falsy, so a seller
    dropping to free kept showing the old price -- defeating the free sweep and
    the price-drop signal for the transition that matters most."""
    from dealbot.models import Listing
    stub = Listing(id="facebook:9", source="facebook", source_id="9",
                   title="Couch", description=None, price_cents=50000,
                   currency="USD", url="u")
    html = ('<script type="application/json">'
            '{"x":{"id":"9","marketplace_listing_title":"Couch",'
            '"listing_price":{"amount":"0.00"}}}</script>')
    full = src.parse_detail_html(html, stub)
    assert full.price_cents == 0


def test_the_strikethrough_price_is_captured(src):
    """40 of 144 live listings carried an old price we were discarding --
    including "$500 -> free". It is the strongest buy signal in the dataset."""
    raws = src.parse_search_html((HTML / "search-free-abq.html").read_text(errors="replace"))
    drops = [l for r in raws if (l := src.parse(r)) and l.previous_price_cents]
    assert drops
    desk = next(l for l in drops if "Pottery Barn" in l.title)
    assert desk.previous_price_cents == 15000 and desk.price_cents == 0


def test_a_price_drop_is_put_in_front_of_the_model(src):
    """A thing that was $500 and is now free is a different proposition from a
    thing that was always free, and that cannot be inferred from price alone."""
    from dealbot.scoring.base import render_listing
    raws = src.parse_search_html((HTML / "search-free-abq.html").read_text(errors="replace"))
    desk = next(l for r in raws if (l := src.parse(r))
                and "Pottery Barn" in l.title)
    rendered = render_listing(desk)
    assert "previously: $150" in rendered
    assert "dropped the price" in rendered


def test_the_state_is_kept_on_the_city_label(src):
    """"Albuquerque, NM" not "Albuquerque" -- the state is what lets the gate
    reject Kansas City without knowing where Kansas City is."""
    raws = src.parse_search_html((HTML / "search-free-abq.html").read_text(errors="replace"))
    cities = {l.city for r in raws if (l := src.parse(r)) and l.city}
    assert any(c.endswith(", NM") for c in cities)


# --- photos ------------------------------------------------------------------

def test_photos_come_from_the_listing_not_from_the_page_around_it(src):
    """REGRESSION: photos were regex-scraped from the whole item page -- every
    `scontent` URI on it, first six in document order. An item page carries
    ~32 of those (recommendation carousels, "more like this", the seller's
    other items), so other people's listings were stored as this one's photos:
    one file ended up filed against 71 different listings, and the image pass
    was shown a mini bike as a TV stand's second photo. On this very page, all
    six stored photos belonged to something else."""
    raws = src.parse_search_html((HTML / "search-free-abq.html").read_text(errors="replace"))
    stub = next(src.parse(r) for r in raws if r.source_id == "1354512002236671")
    full = src.parse_detail_html(
        (HTML / "item-1354512002236671.html").read_text(errors="replace"), stub)

    assert len(full.images) == 6
    # The seller's own six, in their order, from `listing_photos`.
    assert "589577616_1370775094546670" in full.images[0]
    # ...and the carousel image this page shares with every other item page.
    assert not any("789580349_1950363825919734" in u for u in full.images)
    # Full size, not the 260px crop the carousel uses: the downscale to 512
    # was a no-op on what we used to send.
    assert all("s960x960" in u for u in full.images)


def test_photos_fall_back_to_the_primary_one_rather_than_to_the_page(src):
    """One right photo beats six that may belong to somebody else. A listing
    with no photo set simply gets no image pass."""
    from dealbot.models import Listing
    stub = Listing(id="facebook:9", source="facebook", source_id="9",
                   title="Couch", description=None, price_cents=0,
                   currency="USD", url="u")
    html = ('<script type="application/json">'
            '{"x":{"id":"9","marketplace_listing_title":"Couch",'
            '"primary_listing_photo":{"image":{"uri":"https://scontent/mine.jpg"}}}}'
            '</script>')
    assert src.parse_detail_html(html, stub).images == ("https://scontent/mine.jpg",)


def test_another_listings_photo_set_on_the_same_page_is_ignored(src):
    """The id check is the whole safeguard: a product-details target for some
    OTHER listing is exactly what the carousel is made of."""
    from dealbot.models import Listing
    stub = Listing(id="facebook:9", source="facebook", source_id="9",
                   title="Couch", description=None, price_cents=0,
                   currency="USD", url="u")
    html = ('<script type="application/json">'
            '{"a":{"id":"9","marketplace_listing_title":"Couch",'
            '"primary_listing_photo":{"image":{"uri":"https://scontent/mine.jpg"}}},'
            '"b":{"id":"77","listing_photos":['
            '{"image":{"uri":"https://scontent/somebody-elses.jpg"}}]}}'
            '</script>')
    assert src.parse_detail_html(html, stub).images == ("https://scontent/mine.jpg",)
