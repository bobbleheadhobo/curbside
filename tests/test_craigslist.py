"""Craigslist adapter, tested against real captured API payloads.

fixtures/craigslist/ holds a live free-stuff search for Albuquerque (areaId 50)
and one detail response, both captured 2026-09-08.
"""
import json
from dataclasses import replace
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


@pytest.fixture
def detail_payload():
    return json.loads((FIX / "detail-couch.json").read_text())


@pytest.fixture
def stub_listing(src, search_payload):
    """The index-level record a detail fetch is handed to fill in."""
    return src.parse(src.parse_search(search_payload)[1])


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


def test_detail_supplies_body_condition_and_canonical_url(src, detail_payload,
                                                          stub_listing):
    full = src.parse_detail(detail_payload, stub_listing)
    assert "grey couch set" in full.description
    assert "[condition: good]" in full.description   # attribute folded into text
    assert full.url.startswith("https://www.craigslist.org/view/d/")
    assert full.posted_at is not None
    assert full.price_cents == 25000
    assert len(full.images) > 1
    assert full.id == stub_listing.id                 # identity preserved


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


def test_the_description_arrives_as_text_not_html(src, detail_payload, stub_listing):
    """Craigslist's `body` is HTML. Every description collected carried a
    `<br>`, and the ones with a phone number carried a `<showcontactinfo>`
    element the site fills in client-side. Both used to reach the prompt and
    the card verbatim."""
    full = src.parse_detail(detail_payload, stub_listing)
    assert "<" not in full.description
    assert "grey couch set" in full.description


@pytest.mark.parametrize("body,want", [
    ("one<br>two", "one\ntwo"),
    ("one<br/>two", "one\ntwo"),
    ("one<BR />two", "one\ntwo"),
    # the element the site replaces with a phone number, and nothing else
    ('Call <showcontactinfo postingid="7" title="show contact info">'
     "</showcontactinfo>", "Call"),
    ("<b>FREE</b> pallets", "FREE pallets"),
    ("caf&eacute; table &amp; chairs", "café table & chairs"),
    # a seller who typed the characters keeps them, rather than getting a
    # line break they never asked for
    ("a &lt;br&gt; b", "a <br> b"),
    ("one<br>\n\n\n\n\ntwo", "one\n\ntwo"),
    ("", ""),
    (None, None),
])
def test_markup_is_taken_out_but_the_words_are_kept(body, want):
    from dealbot.sources.craigslist import _plain_text
    assert _plain_text(body) == want


def test_an_all_markup_body_reads_as_no_description(src, detail_payload,
                                                    stub_listing):
    """`<br><br>` is not a description. It has to come back None rather than
    as whitespace, or the card's "no description" chip never fires."""
    payload = json.loads(json.dumps(detail_payload))
    payload["data"]["items"][0]["body"] = "<br><br>\n  "
    payload["data"]["items"][0]["attributes"] = []
    assert src.parse_detail(payload, stub_listing).description is None


# --- liveness ----------------------------------------------------------------
#
# No fixture file here on purpose: `liveness` reads the STATUS CODE and nothing
# else, so a recorded body would be decoration. The 410 is real -- it is what
# www.craigslist.org answered for the Onkyo receiver that prompted this, while
# sapi was still serving that same posting in full.

class _Head:
    def __init__(self, code): self.status_code = code


@pytest.mark.parametrize("code, verdict", [
    (410, "removed"),      # "deleted by its author" -- the only sale signal
    (404, "removed"),
    (200, "listed"),
    (403, "unknown"),      # being gated is not evidence of a sale
    (429, "unknown"),
    (500, "unknown"),
])
def test_liveness_reads_the_status_code(src, stub_listing, monkeypatch, code,
                                        verdict):
    monkeypatch.setattr(src._session, "head", lambda *a, **k: _Head(code))
    assert src.liveness(stub_listing) == verdict


def test_liveness_asks_the_posting_page_not_the_api(src, stub_listing,
                                                    monkeypatch):
    """The API is the thing that lied. Asking it again cannot help."""
    seen = []
    monkeypatch.setattr(src._session, "head",
                        lambda url, **k: (seen.append(url), _Head(200))[1])
    src.liveness(stub_listing)
    assert seen == [stub_listing.url]
    assert "sapi" not in seen[0]


def test_liveness_spends_a_request_slot(src, stub_listing, monkeypatch):
    """Rate limiting lives inside the adapter. A surface that skipped it would
    be a way to make unpaced requests by accident."""
    monkeypatch.setattr(src._session, "head", lambda *a, **k: _Head(200))
    before = src._requests_made
    src.liveness(stub_listing)
    assert src._requests_made == before + 1


# --- stale cache copies ------------------------------------------------------

def _payload(updated, price):
    return {"data": {"items": [{"postingUuid": "u", "title": "t",
                                "updatedDate": updated, "price": price}]}}


def test_a_stale_detail_copy_is_ignored_rather_than_stored(src, stub_listing,
                                                           monkeypatch):
    """Craigslist's item endpoint serves a cache that does not converge: two
    fetches minutes apart returned the seller's pre-edit and post-edit copies.
    Stored blind, the price ping-pongs -- which reads as a price drop, and buys
    an appraisal, every time it swings down."""
    held = dict(stub_listing.raw or {})
    held["detail"] = {"updatedDate": 2000}
    listing = replace(stub_listing, raw=held)

    monkeypatch.setattr(src, "_get", lambda *a, **k: _payload(1000, 150))
    assert src.detail(listing) is None


def test_a_newer_copy_is_taken(src, stub_listing, monkeypatch):
    held = dict(stub_listing.raw or {})
    held["detail"] = {"updatedDate": 1000}
    listing = replace(stub_listing, raw=held)

    monkeypatch.setattr(src, "_get", lambda *a, **k: _payload(2000, 125))
    full = src.detail(listing)
    assert full is not None and full.price_cents == 12500


def test_the_same_copy_again_is_not_stale(src, stub_listing, monkeypatch):
    """Equal is not older. Every unchanged re-fetch would otherwise be thrown
    away, and with it the price refresh that `recheck` exists for."""
    held = dict(stub_listing.raw or {})
    held["detail"] = {"updatedDate": 2000}
    listing = replace(stub_listing, raw=held)

    monkeypatch.setattr(src, "_get", lambda *a, **k: _payload(2000, 150))
    assert src.detail(listing) is not None


def test_no_stamp_on_either_side_lets_the_payload_through(src, stub_listing,
                                                          monkeypatch):
    """FAIL OPEN, like every other filter here. The first detail fetch of a
    listing has nothing to compare against."""
    monkeypatch.setattr(src, "_get", lambda *a, **k: _payload(None, 150))
    assert src.detail(stub_listing) is not None

    monkeypatch.setattr(src, "_get", lambda *a, **k: _payload(2000, 150))
    assert src.detail(stub_listing) is not None
