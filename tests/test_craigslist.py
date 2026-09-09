"""Craigslist adapter, tested against real captured API payloads.

fixtures/craigslist/ holds a live free-stuff search for Albuquerque (areaId 50)
and one detail response, both captured 2026-09-08.
"""
import json
from pathlib import Path

import pytest

from dealbot.models import Location
from dealbot.sources.craigslist import CraigslistSource, SourceBlocked

FIX = Path(__file__).resolve().parents[1] / "fixtures/craigslist"
ABQ = Location(lat=35.0844, lng=-106.6504, radius_miles=25)


@pytest.fixture
def src():
    return CraigslistSource(ABQ, area_id=50, min_interval_seconds=0)


@pytest.fixture
def search_payload():
    return json.loads((FIX / "search-free-abq.json").read_text())


def test_decodes_positional_items(src, search_payload):
    raws = src.parse_search(search_payload)
    assert len(raws) == 192
    listings = [src.parse(r) for r in raws]
    assert all(l is not None for l in listings)
    assert all(l.title for l in listings)


def test_coordinates_come_from_the_search_feed(src, search_payload):
    """Unlike Facebook, Craigslist gives coordinates without a detail fetch, so
    the radius filter works on the first pass."""
    listings = [src.parse(r) for r in src.parse_search(search_payload)]
    located = [l for l in listings if l.lat is not None]
    assert len(located) > 150
    assert all(l.distance_mi is not None for l in located)
    assert min(l.distance_mi for l in located) < 5


def test_no_price_is_not_free(src, search_payload):
    """`-1` in the price slot means no price shown; conflating that with 0 would
    put every unpriced listing into a free sweep."""
    listings = [src.parse(r) for r in src.parse_search(search_payload)]
    assert any(l.price_cents is None for l in listings)


def test_urls_are_built_from_slug_and_uuid(src, search_payload):
    listings = [src.parse(r) for r in src.parse_search(search_payload)]
    assert all(l.url.startswith("https://www.craigslist.org/view/d/")
               for l in listings)


def test_search_has_no_descriptions(src, search_payload):
    """Same two-stage shape as Facebook: search is an index, detail has the body."""
    listings = [src.parse(r) for r in src.parse_search(search_payload)]
    assert all(l.description is None for l in listings)


def test_detail_supplies_body_condition_and_canonical_url(src, search_payload):
    stub = src.parse(src.parse_search(search_payload)[1])
    detail = json.loads((FIX / "detail-couch.json").read_text())
    full = src.parse_detail(detail, stub)
    assert "grey couch set" in full.description
    assert "[condition: good]" in full.description   # attribute folded into text
    assert full.url.startswith("https://www.craigslist.org/view/d/")
    assert full.posted_at is not None
    assert full.price_cents == 25000
    assert len(full.images) > 1
    assert full.id == stub.id                         # identity preserved


def test_an_error_payload_raises_rather_than_looking_empty(src):
    with pytest.raises(SourceBlocked):
        src.parse_search({"data": {}, "errors": [{"message": "nope"}]})


def test_budget_is_enforced(src):
    src.max_requests = 0
    with pytest.raises(SourceBlocked, match="budget"):
        src._get("https://sapi.craigslist.org/web/v8/postings/search/full")


def test_an_html_interstitial_reads_as_blocked_not_a_decode_crash(src, monkeypatch):
    """Craigslist's equivalent of Facebook's silent throttle: a block page
    served as HTML with a 200."""
    class Resp:
        status_code = 200
        content = b"<html>blocked</html>"
        def json(self): raise ValueError("Expecting value")
    monkeypatch.setattr(src._session, "get", lambda *a, **k: Resp())
    with pytest.raises(SourceBlocked, match="non-JSON"):
        src._get("https://sapi.craigslist.org/x")
