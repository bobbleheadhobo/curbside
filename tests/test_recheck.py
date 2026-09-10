"""The availability re-check. Nothing here touches the network: the sources are
hand-built and return what a real one would.

`mark_gone` only ever knew that a listing had stopped APPEARING, which for the
15-minute free sweep is 45 minutes off page one. These cover the difference
between that and a listing that actually sold.
"""
from dataclasses import replace

import pytest
from conftest import make_listing

from dealbot.db import Store
from dealbot.recheck import availability, recheck
from dealbot.sources.base import SourceBlocked


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
