"""The gate decides who costs money, so every rule gets a test."""
from conftest import ABQ, make_listing

from dealbot.filters import gate
from dealbot.models import UpsertResult


def _gate(hunt, listings, statuses=None, last_scores=None, upserts=None):
    return gate(hunt, listings, ABQ, statuses or {}, last_scores or {},
                upserts or {})


def test_new_listing_is_admitted(hunt):
    gr = _gate(hunt, [make_listing()])
    assert [c.reason for c in gr.candidates] == ["new"]
    assert gr.rejected == []


def test_over_price_rejected(hunt):
    gr = _gate(hunt, [make_listing(price_cents=30000)])
    assert gr.candidates == []
    assert gr.rejected == [("fixture:1", "over_price")]


def test_too_far_rejected(hunt):
    gr = _gate(hunt, [make_listing(distance_mi=90.0)])
    assert gr.rejected == [("fixture:1", "too_far")]


def test_excluded_keyword_rejected(hunt):
    gr = _gate(hunt, [make_listing(title="TV wall mount bracket")])
    assert gr.rejected[0][1].startswith("excluded_kw:")


def test_triaged_listing_is_never_reconsidered(hunt):
    for status in ("saved", "dismissed", "contacted"):
        gr = _gate(hunt, [make_listing()], statuses={"fixture:1": status})
        assert gr.rejected == [("fixture:1", "triaged")], status


def test_free_listing_is_not_over_price(hunt):
    """price 0 must not be confused with a missing price."""
    gr = _gate(hunt, [make_listing(price_cents=0)])
    assert len(gr.candidates) == 1


def test_already_scored_and_unchanged_costs_nothing(hunt):
    scores = {"fixture:1": {"priced_at_cents": 10000}}
    gr = _gate(hunt, [make_listing(price_cents=10000)], last_scores=scores)
    assert gr.candidates == []
    assert gr.rejected == [("fixture:1", "unchanged")]


def test_material_price_drop_reopens_a_scored_listing(hunt):
    scores = {"fixture:1": {"priced_at_cents": 10000}}
    gr = _gate(hunt, [make_listing(price_cents=8000)], last_scores=scores)
    assert [c.reason for c in gr.candidates] == ["price_drop"]


def test_trivial_price_drop_is_noise(hunt):
    scores = {"fixture:1": {"priced_at_cents": 10000}}
    gr = _gate(hunt, [make_listing(price_cents=9500)], last_scores=scores)
    assert gr.rejected == [("fixture:1", "unchanged")]


def test_relist_reopens_a_scored_listing(hunt):
    scores = {"fixture:1": {"priced_at_cents": 10000}}
    ups = {"fixture:1": UpsertResult("fixture:1", True, False, None, True)}
    gr = _gate(hunt, [make_listing()], last_scores=scores, upserts=ups)
    assert [c.reason for c in gr.candidates] == ["relist"]


def test_rejections_are_returned_not_dropped(hunt):
    """An empty result must be explainable, or a broken scraper looks like a
    quiet day."""
    listings = [make_listing("fixture:1", price_cents=30000),
                make_listing("fixture:2", distance_mi=90.0)]
    gr = _gate(hunt, listings)
    assert dict(gr.rejected) == {"fixture:1": "over_price", "fixture:2": "too_far"}
