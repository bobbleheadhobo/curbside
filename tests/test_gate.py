"""The gate decides who costs money, so every rule gets a test."""
from conftest import ABQ, make_listing

from dealbot.filters import gate
from dealbot.models import UpsertResult


def _gate(hunt, listings, statuses=None, last_scores=None, upserts=None,
          filtered=None):
    return gate(hunt, listings, ABQ, statuses or {}, last_scores or {},
                upserts or {}, filtered or {})


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
    for status in ("saved", "dismissed", "grabbed"):
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


def test_out_of_state_is_rejected_without_coordinates(hunt):
    """Facebook's "free" search returned listings from Kansas City, Amarillo,
    Sacramento and Findlay OH. Coordinates only arrive with a 15-second detail
    fetch, so the state has to do the work first."""
    far = make_listing("fb:1", distance_mi=None, city="Kansas City, KS")
    gr = _gate(hunt, [far])
    assert gr.rejected == [("fb:1", "too_far_by_city")]


def test_a_distant_in_state_city_is_also_rejected(hunt):
    gr = _gate(hunt, [make_listing("fb:2", distance_mi=None, city="Santa Fe, NM")])
    assert gr.rejected == [("fb:2", "too_far_by_city")]


def test_a_nearby_city_still_gets_through(hunt):
    for city in ("Albuquerque, NM", "Rio Rancho, NM", "Los Lunas, NM"):
        gr = _gate(hunt, [make_listing("fb:3", distance_mi=None, city=city)])
        assert len(gr.candidates) == 1, city


def test_an_unrecognised_place_fails_OPEN(hunt):
    """Failing closed silently drops a listing that might be the one you wanted;
    failing open costs one detail fetch."""
    for city in ("Somewhere, NM", "This, GES", None):
        gr = _gate(hunt, [make_listing("fb:4", distance_mi=None, city=city)])
        assert len(gr.candidates) == 1, city


def test_a_real_distance_always_beats_the_city_guess(hunt):
    """Once the detail fetch has run we have the seller's actual pin; the city
    centroid must not override it in either direction."""
    near = make_listing("fb:5", distance_mi=3.0, city="Santa Fe, NM")
    assert len(_gate(hunt, [near]).candidates) == 1

    far = make_listing("fb:6", distance_mi=90.0, city="Albuquerque, NM")
    assert _gate(hunt, [far]).rejected == [("fb:6", "too_far")]


def test_a_permanent_rejection_is_not_re_decided(hunt):
    """A photoless listing does not grow a photograph, so asking again costs a
    detail fetch to learn what we already hold.

    Without this the listing has no score, reads as never-judged, and is
    admitted on every run forever. Three of them held three of the free
    sweep's five candidate slots for four days, and the 73 listings queued
    behind them were never judged at all."""
    gr = _gate(hunt, [make_listing()], filtered={"fixture:1": "no_photo"})
    assert gr.candidates == []
    assert gr.rejected == [("fixture:1", "no_photo")]


def test_every_permanent_reason_sticks(hunt):
    from dealbot.filters import PERMANENT_REJECTIONS
    for reason in PERMANENT_REJECTIONS:
        gr = _gate(hunt, [make_listing()], filtered={"fixture:1": reason})
        assert gr.candidates == [], reason


def test_a_duplicate_is_re_decided_every_run(hunt):
    """The fingerprint has collapsed four different "Curb alert" posts into
    one before now. Making a wrong merge permanent is the confidently-wrong
    failure relist detection was left inert to avoid."""
    gr = _gate(hunt, [make_listing()],
               filtered={"fixture:1": "duplicate_of:craigslist:abc"})
    assert [c.reason for c in gr.candidates] == ["new"]


def test_a_blocked_word_is_re_decided_every_run(hunt):
    """The one rejection that must NOT stick. Blocked words are typed on a
    phone and deleted from a phone, so a term you remove has to let its
    listings back -- and the gate above re-checks the current list for free."""
    gr = _gate(hunt, [make_listing()], filtered={"fixture:1": "excluded_kw:bed"})
    assert [c.reason for c in gr.candidates] == ["new"]


def test_a_price_rejection_is_re_decided_every_run(hunt):
    """`over_price` and `too_far` are re-decided from the current price and the
    current radius, so stickiness would buy nothing and could only go stale."""
    for reason in ("over_price", "too_far", "too_far_by_city"):
        gr = _gate(hunt, [make_listing()], filtered={"fixture:1": reason})
        assert [c.reason for c in gr.candidates] == ["new"], reason
