"""The availability re-check. Nothing here touches the network: the sources are
hand-built and return what a real one would.

`mark_gone` only ever knew that a listing had stopped APPEARING, which for the
15-minute free sweep is 45 minutes off page one. These cover the difference
between that and a listing that actually sold.
"""
from dataclasses import replace

import pytest
from conftest import make_listing

from curbside.db import Store
from curbside.recheck import availability, recheck
from curbside.sources.base import SourceBlocked


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def register(store, listing, status, hunt_id="h"):
    store.upsert_listing(listing)
    store.record_price(listing.id, listing.price_cents)
    store.mark_matches(hunt_id, [listing])
    store.set_status(hunt_id, listing.id, status)
    return listing


class Says:
    """A source that answers `detail` with whatever it was handed."""
    name = "fixture"

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def search(self, hunt): return iter(())
    def parse(self, raw): return None

    def detail(self, listing):
        self.calls.append(listing.id)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer(listing) if callable(self.answer) else self.answer


def _sold_row(store, lid):
    return store.conn.execute(
        "SELECT sold_at, sold_reason, is_active FROM listings WHERE id=?",
        (lid,)).fetchone()


# --- reading the answer ------------------------------------------------------

def test_availability_reads_what_the_source_actually_said():
    assert availability(None) == "removed"
    assert availability(make_listing(raw={"is_sold": True})) == "sold"
    assert availability(make_listing(raw={"is_live": False})) == "removed"
    assert availability(make_listing(raw={"is_sold": False, "is_live": True})) == "listed"
    assert availability(make_listing(raw={})) == "listed"


# --- the three outcomes ------------------------------------------------------

def test_a_sold_listing_is_recorded_and_leaves_the_bin(store):
    l = register(store, make_listing("fb:1"), "wanted")
    src = Says(lambda x: replace(x, raw={"is_sold": True}))

    r = recheck(store, [("fixture", src)])
    assert (r.n_checked, r.n_sold) == (1, 1)
    row = _sold_row(store, l.id)
    assert row["sold_at"] and row["sold_reason"] == "sold" and row["is_active"] == 0
    assert store.statuses("h")[l.id] == "gone"


def test_a_removed_posting_is_the_weaker_claim(store):
    """Craigslist just stops returning a detail payload. That is usually a sale,
    but the source did not say so and the badge must not pretend it did."""
    l = register(store, make_listing("cl:1"), "free_find")
    r = recheck(store, [("fixture", Says(None))])
    assert (r.n_checked, r.n_removed) == (1, 1)
    assert _sold_row(store, l.id)["sold_reason"] == "removed"


def test_something_still_for_sale_is_refreshed_not_retired(store):
    """The only place a price drop on an already-judged listing is ever seen:
    the gate stops such a listing being fetched again at all."""
    l = register(store, make_listing("fb:2", price_cents=20000), "wanted")
    src = Says(lambda x: replace(x, price_cents=12000, raw={"is_live": True}))

    r = recheck(store, [("fixture", src)])
    assert (r.n_checked, r.n_listed) == (1, 1)
    assert store.statuses("h")[l.id] == "wanted"
    assert _sold_row(store, l.id)["sold_at"] is None
    assert store.conn.execute(
        "SELECT price_cents FROM listings WHERE id=?", (l.id,)).fetchone()[0] == 12000
    assert store.conn.execute(
        "SELECT COUNT(*) FROM price_observations WHERE listing_id=?",
        (l.id,)).fetchone()[0] == 2


# --- what it must never do ---------------------------------------------------

def test_an_unreadable_answer_retires_nothing(store):
    """FAIL OPEN. A parse error is not evidence a listing is gone, and there is
    no stamp either, so it is retried rather than skipped for six hours."""
    l = register(store, make_listing("fb:3"), "wanted")
    r = recheck(store, [("fixture", Says(ValueError("bad payload")))])

    assert (r.n_checked, r.n_sold, r.n_removed) == (0, 0, 0)
    assert store.statuses("h")[l.id] == "wanted"
    assert _sold_row(store, l.id)["sold_at"] is None
    assert store.due_for_recheck(["wanted"], "9999", 10)      # still due


def test_a_gated_source_stops_the_pass_instead_of_retiring_the_queue(store):
    for i in range(4):
        register(store, make_listing(f"fb:{i}"), "wanted")
    src = Says(SourceBlocked("request budget exhausted"))

    r = recheck(store, [("fixture", src)], max_per_run=4)
    assert len(src.calls) == 1                     # stopped, did not work through
    assert "recheck stopped" in r.error
    assert all(s == "wanted" for s in store.statuses("h").values())


def test_a_users_own_decision_is_marked_not_undone(store):
    """Something they saved is still theirs after it sells."""
    l = register(store, make_listing("fb:4"), "saved")
    recheck(store, [("fixture", Says(lambda x: replace(x, raw={"is_sold": True})))])
    assert _sold_row(store, l.id)["sold_reason"] == "sold"
    assert store.statuses("h")[l.id] == "saved"


def test_a_sold_listing_leaves_every_bin_it_is_in(store):
    """One listing can match several hunts, and marking only the hunt that
    happened to re-check it leaves the same sold couch sitting in another tab."""
    l = make_listing("fb:5")
    register(store, l, "wanted", hunt_id="want:tv-stand")
    register(store, l, "free_find", hunt_id="sweep:free")

    recheck(store, [("fixture", Says(lambda x: replace(x, raw={"is_sold": True})))])
    assert store.statuses("want:tv-stand")[l.id] == "gone"
    assert store.statuses("sweep:free")[l.id] == "gone"


def test_a_sold_listing_is_not_resurrected_by_seeing_it_again(store):
    """Facebook keeps showing sold items in search results. Both paths that
    write `is_active`/`status` used to take that as evidence it was back."""
    l = register(store, make_listing("fb:6"), "wanted")
    recheck(store, [("fixture", Says(lambda x: replace(x, raw={"is_sold": True})))])

    store.upsert_listing(l)                       # ...and there it is again
    store.mark_gone("h", "fixture", [l.id])
    row = _sold_row(store, l.id)
    assert row["is_active"] == 0 and row["sold_at"]
    assert store.statuses("h")[l.id] == "gone"


# --- pacing ------------------------------------------------------------------

def test_a_listing_is_not_rechecked_twice_in_an_interval(store):
    """A bin of forty things must not become forty requests every run."""
    l = register(store, make_listing("fb:7"), "wanted")
    src = Says(lambda x: replace(x, raw={"is_live": True}))

    assert recheck(store, [("fixture", src)], every_hours=6).n_checked == 1
    assert recheck(store, [("fixture", src)], every_hours=6).n_checked == 0
    assert recheck(store, [("fixture", src)], every_hours=0).n_checked == 1
    assert len(src.calls) == 2


def test_the_list_you_curated_is_asked_about_far_more_often(store):
    """Both bins cost requests and no quota, but they are not worth the same.

    A saved listing is one you might be about to drive to, and there are only
    ever a handful. `wanted` and `free_find` are candidates nobody has decided
    on, and they are the half that grows to dozens -- at the saved pace those
    would be a few hundred item-page fetches a day at a site that throttles
    silently, to learn something `mark_gone` already half-answers for free.
    """
    from datetime import datetime, timedelta, timezone
    register(store, make_listing("fb:mine"), "saved")
    register(store, make_listing("fb:maybe"), "wanted")
    src = Says(lambda x: replace(x, raw={"is_live": True}))

    assert recheck(store, [("fixture", src)]).n_checked == 2   # never checked
    hour_ago = (datetime.now(timezone.utc)
                - timedelta(hours=1)).isoformat(timespec="seconds")
    store.conn.execute("UPDATE hunt_matches SET rechecked_at=?", (hour_ago,))

    r = recheck(store, [("fixture", src)])
    assert r.n_checked == 1
    assert src.calls[-1] == "fb:mine"
    # ...and `--all` still means all of them, saved pacing included.
    assert recheck(store, [("fixture", src)], every_hours=0).n_checked == 2


def test_a_full_candidate_bin_cannot_crowd_out_your_saved_list(store):
    """The per-run cap is what keeps this inside the source request budget, so
    the faster half has to be asked FIRST. Ordered the other way, six free
    finds would spend the whole cap and the thing you are driving to would go
    unconfirmed for as long as the bin stayed full."""
    for i in range(6):
        register(store, make_listing(f"fb:maybe{i}"), "free_find")
    register(store, make_listing("fb:mine"), "saved")
    src = Says(lambda x: replace(x, raw={"is_live": True}))

    assert recheck(store, [("fixture", src)], max_per_run=2).n_checked == 2
    assert "fb:mine" in src.calls


def test_only_listings_in_a_bin_are_worth_a_request(store):
    for status in ("new", "scored", "filtered", "dismissed", "gone"):
        register(store, make_listing(f"fb:{status}"), status)
    register(store, make_listing("fb:keep"), "wanted")

    src = Says(lambda x: replace(x, raw={"is_live": True}))
    assert recheck(store, [("fixture", src)]).n_checked == 1
    assert src.calls == ["fb:keep"]


def test_the_per_run_cap_is_obeyed(store):
    for i in range(6):
        register(store, make_listing(f"fb:cap{i}"), "free_find")
    src = Says(lambda x: replace(x, raw={"is_live": True}))
    assert recheck(store, [("fixture", src)], max_per_run=2).n_checked == 2


def test_a_missing_facebook_payload_never_retires_anything():
    """The irreversible one. `mark_sold` COALESCEs so the stamp never moves,
    `due_for_recheck` skips anything stamped, and `mark_seen` will not un-gone
    it -- so reading a throttle as "removed" quietly emptied a bin of things
    the user had saved, permanently."""
    from curbside.recheck import availability
    assert availability(None, "facebook") == "unknown"
    assert availability(None, "craigslist") == "removed"


def test_positive_evidence_still_retires():
    from curbside.recheck import availability
    from conftest import make_listing
    assert availability(make_listing(raw={"is_sold": True}), "facebook") == "sold"
    assert availability(make_listing(raw={"is_live": False}), "facebook") == "removed"
    assert availability(make_listing(raw={"is_live": True}), "facebook") == "listed"


class _Blocked:
    """A source that is out of request budget."""
    name = "facebook"
    def detail(self, listing):
        from curbside.sources.base import SourceBlocked
        raise SourceBlocked("budget exhausted")


class _Fine:
    name = "craigslist"
    def __init__(self): self.asked = []
    def detail(self, listing):
        self.asked.append(listing.id)
        from dataclasses import replace
        return replace(listing, raw={"is_live": True})


def _bin_listing(store, lid, source, hunts):
    from conftest import make_listing
    from dataclasses import replace
    lst = replace(make_listing(lid=lid), source=source,
                  source_id=lid.split(":")[-1])
    store.upsert_listing(lst)
    for h in hunts:
        store.mark_matches(h, [lst])
        store.set_status(h, lst.id, "saved")
    return lst


def test_one_exhausted_source_does_not_starve_the_others(tmp_path):
    """recheck runs last and shares the pass's single request budget, so
    Facebook is routinely spent by the sweep before this. Breaking outright
    skipped every Craigslist listing queued behind it."""
    from curbside.db import Store
    from curbside.recheck import recheck
    store = Store(tmp_path / "t.db")
    _bin_listing(store, "facebook:1", "facebook", ["want:tv-stand"])
    _bin_listing(store, "craigslist:2", "craigslist", ["want:tv-stand"])
    fine = _Fine()
    r = recheck(store, [("facebook", _Blocked()), ("craigslist", fine)],
                every_hours=0, max_per_run=10)
    assert fine.asked == ["craigslist:2"]
    assert r.n_listed == 1
    assert "facebook" in (r.error or "")


def test_a_listing_in_two_bins_is_only_asked_about_once(tmp_path):
    """One row per (hunt, listing), so a listing matched by two hunts was two
    requests for one answer -- against the very budget above."""
    from curbside.db import Store
    from curbside.recheck import recheck
    store = Store(tmp_path / "t.db")
    _bin_listing(store, "craigslist:9", "craigslist",
                 ["want:tv-stand", "sweep:free-nearby"])
    fine = _Fine()
    r = recheck(store, [("craigslist", fine)], every_hours=0, max_per_run=10)
    assert fine.asked == ["craigslist:9"]
    assert r.n_checked == 1


# --- something you own is not something to ask the seller about -------------


def test_a_grabbed_listing_is_never_rechecked(store):
    """`mark_grabbed` stamps `sold_at`, and `due_for_recheck` already filters on
    that, so the guard costs no new code. Worth pinning anyway: a grabbed
    listing is `saved`'s successor and `saved` is re-checked on EVERY pass, so
    the wrong status list here would spend a request per tick asking Facebook
    whether the bookshelf in your hallway is still for sale."""
    l = register(store, make_listing(), "saved")
    store.mark_grabbed("h", l.id, 1800)
    src = Says(l)

    result = recheck(store, [("fixture", src)], every_hours=0, max_per_run=10)

    assert src.calls == [], "asked the source about a thing you already own"
    assert result.n_checked == 0
    assert store.due_for_recheck(["saved", "grabbed"], None, 10) == []


def test_grabbing_does_not_retire_it_from_your_own_list(store):
    """It leaves every OTHER bin, because it is off the market -- and stays on
    yours, because that is the decision you made. Same rule `retire_sold`
    already applies to a sold listing that you saved."""
    l = register(store, make_listing(), "saved", hunt_id="mine")
    store.mark_matches("sweep", [l])
    store.set_status("sweep", l.id, "free_find")

    store.mark_grabbed("mine", l.id, 0)

    assert store.statuses("mine")[l.id] == "grabbed"
    assert store.statuses("sweep")[l.id] == "gone"


# --- a source that can answer directly ---------------------------------------
#
# Craigslist's detail payload is not evidence a listing exists: sapi kept
# serving a deleted posting, in full, for over a day. `liveness` reads the
# posting page's status code instead, and it is asked FIRST.

class SaysLive(Says):
    """A source with a `liveness` opinion as well as a detail payload."""

    def __init__(self, verdict, answer=None):
        super().__init__(answer)
        self.verdict, self.liveness_calls = verdict, []

    def liveness(self, listing):
        self.liveness_calls.append(listing.id)
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


def test_liveness_is_asked_first_and_settles_it_alone(store):
    """A deleted posting costs ONE request, not two: there is nothing worth
    refreshing about a listing that is gone."""
    l = register(store, make_listing("cl:10"), "saved")
    src = SaysLive("removed", answer=make_listing("cl:10"))

    r = recheck(store, [("fixture", src)])
    assert (r.n_checked, r.n_removed) == (1, 1)
    assert src.liveness_calls == [l.id]
    assert src.calls == []                       # no detail fetch at all
    assert _sold_row(store, l.id)["sold_reason"] == "removed"


def test_a_source_that_says_listed_is_believed_over_a_missing_payload(store):
    """THE BUG. Craigslist's item endpoint answering with nothing used to be
    the source's only sale signal, so a posting that was merely not in the
    cache read as sold -- and, the other way round, a deleted posting that the
    cache still held read as for sale. The page's status code outranks both."""
    l = register(store, make_listing("cl:11"), "saved")
    src = SaysLive("listed", answer=None)

    r = recheck(store, [("fixture", src)])
    assert (r.n_checked, r.n_listed) == (1, 1)
    assert (r.n_sold, r.n_removed) == (0, 0)
    assert _sold_row(store, l.id)["sold_at"] is None
    assert store.statuses("h")[l.id] == "saved"


def test_a_listed_answer_still_refreshes_the_price(store):
    l = register(store, make_listing("cl:12", price_cents=15000), "saved")
    src = SaysLive("listed", answer=lambda x: replace(x, price_cents=12500))

    r = recheck(store, [("fixture", src)])
    assert r.n_listed == 1
    assert store.conn.execute(
        "SELECT price_cents FROM listings WHERE id=?", (l.id,)).fetchone()[0] == 12500


def test_an_inconclusive_liveness_retires_nothing(store):
    """A 403 is the site gating us. Reading that as a sale would quietly empty
    the list of things the user saved, and `mark_sold` does not come undone."""
    l = register(store, make_listing("cl:13"), "saved")
    src = SaysLive("unknown", answer=None)

    r = recheck(store, [("fixture", src)])
    assert (r.n_unknown, r.n_sold, r.n_removed) == (1, 0, 0)
    assert _sold_row(store, l.id)["sold_at"] is None


def test_a_liveness_failure_fails_open(store):
    l = register(store, make_listing("cl:14"), "saved")
    src = SaysLive(ValueError("nonsense"), answer=None)

    r = recheck(store, [("fixture", src)])
    assert (r.n_checked, r.n_sold, r.n_removed) == (0, 0, 0)
    assert _sold_row(store, l.id)["sold_at"] is None
    assert src.calls == []


def test_a_gated_source_stops_the_pass_from_the_liveness_call_too(store):
    for i in range(3):
        register(store, make_listing(f"cl:2{i}"), "saved")
    src = SaysLive(SourceBlocked("HTTP 429"), answer=None)

    r = recheck(store, [("fixture", src)])
    assert (r.n_checked, r.n_removed) == (0, 0)
    assert "recheck stopped for fixture" in r.error
    assert len(src.liveness_calls) == 1           # stopped asking after one


def test_a_saved_craigslist_listing_can_finally_be_marked(store):
    """Both halves of why the receiver sat there. `mark_gone` deliberately
    never retires a saved row -- the user's own decision is marked, not undone
    -- so vanishing from search said nothing, and the detail payload was the
    only other evidence. It was wrong."""
    l = register(store, make_listing("cl:15"), "saved")
    store.mark_gone("h", "fixture", ["cl:other"], threshold=1)
    assert store.statuses("h")[l.id] == "saved"   # unchanged, as designed

    recheck(store, [("fixture", SaysLive("removed"))])
    row = _sold_row(store, l.id)
    assert row["sold_reason"] == "removed" and row["is_active"] == 0
    assert store.statuses("h")[l.id] == "saved"   # still on your list, marked
