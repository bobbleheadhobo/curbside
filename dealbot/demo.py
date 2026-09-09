"""Build a realistic database to develop the dashboard against.

Offline, deterministic, free. The live database is a poor thing to design
against: a timer writes to it every fifteen minutes, its bins hold a handful of
rows, and nothing has ever been saved, so half the views are empty and the other
half never show their awkward cases.

This runs the fixture source through the real pipeline with the stub scorer, so
the shape is genuine, then adds the cases a designer needs to see and would
otherwise wait weeks for: a long title, a listing with no photo, a big price
drop, a motivated seller, an unverified match with requirement evidence, red
flags, and something already saved.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .db import Store
from .models import Hunt, Listing, Score
from .notify.dashboard import DashboardNotifier
from .pipeline import run_hunt
from .scoring.stub import StubScorer
from .sources.fixture import FixtureSource

NOW = datetime.now(timezone.utc)
PHOTO = ("https://images.craigslist.org/00E0E_dOPa29SsGT_0n90t2_600x450.jpg",)


def _listing(lid, title, price, **kw):
    base = dict(
        id=lid, source="craigslist", source_id=lid.split(":")[-1], title=title,
        description="A demonstration listing, generated locally.",
        price_cents=price, currency="USD",
        url="https://www.craigslist.org/view/d/demo/" + lid.split(":")[-1],
        city="Albuquerque, NM", lat=35.09, lng=-106.62, distance_mi=4.0,
        images=PHOTO, posted_at=NOW - timedelta(days=2))
    base.update(kw)
    return Listing(**base)


def _score(lid, hunt_id, **kw):
    base = dict(
        listing_id=lid, hunt_id=hunt_id, model="sonnet", scored_at=NOW,
        match="yes", deal_score=8.0, est_value_cents=25000, condition="good",
        matched_want="tv-stand", worth_grabbing=True, unknowns=(),
        requirements=(), red_flags=(), reasoning="A demonstration verdict.")
    base.update(kw)
    return Score(**base)


# (listing kwargs, score kwargs, status) -- one per awkward case a designer
# needs on screen and would otherwise wait weeks to encounter naturally.
CASES = [
    (dict(lid="demo:1", title="Solid oak media console, 78in wide, mid century",
          price=19900, previous_price_cents=45000,
          posted_at=NOW - timedelta(days=21)),
     dict(deal_score=9.0, est_value_cents=45000, images_checked=True,
          requirements=(
              {"req": "at least 70 inches wide", "met": "yes",
               "evidence": '"78in wide" stated in the title'},
              {"req": "not a corner unit", "met": "yes",
               "evidence": "photos show a straight flat front"}),
          reasoning=("Solid oak, 78 inches, and the price has come down from "
                     "$450 over three weeks. The seller wants it gone.")),
     "wanted"),

    (dict(lid="demo:2", title="TV stand", price=6000, images=()),
     dict(deal_score=7.0, match="unknown", est_value_cents=None,
          unknowns=("width is not stated anywhere",
                    "no photo, so the style is unknown"),
          requirements=({"req": "at least 70 inches wide", "met": "unknown",
                         "evidence": "no dimensions given"},),
          reasoning="Could be right, but the listing says almost nothing."),
     "wanted"),

    (dict(lid="demo:3",
          title=("Enormous solid walnut mid-century modern credenza sideboard "
                 "media console entertainment unit, excellent condition, must "
                 "collect this weekend from the north valley"),
          price=0, previous_price_cents=12000),
     dict(deal_score=8.0, match="no", matched_want=None, worth_grabbing=True,
          red_flags=("photos look like stock images",),
          reasoning="Free, and worth real money. The photos worry me slightly."),
     "free_find"),

    (dict(lid="demo:4", title="Chest freezer, works", price=0,
          posted_at=NOW - timedelta(days=30)),
     dict(deal_score=6.0, match="no", matched_want=None, worth_grabbing=True,
          reasoning="A working freezer for nothing is worth the van hire."),
     "free_find"),

    (dict(lid="demo:5", title="Teal three-tier stacked pouf ottoman", price=8500),
     dict(hunt_id="want:stacked-ottoman", matched_want="stacked-ottoman",
          deal_score=9.0, images_checked=True,
          requirements=(
              {"req": "teal or blue in colour", "met": "yes",
               "evidence": "photos show solid teal velvet"},
              {"req": "body is three stacked round tiers", "met": "yes",
               "evidence": "three distinct rounds visible"}),
          reasoning="Exactly the thing. Photos confirm colour and tier count."),
     "saved"),

    (dict(lid="demo:6", title="Entertainment center, 44 inches", price=4000),
     dict(deal_score=5.0, match="no", matched_want=None, worth_grabbing=False,
          requirements=({"req": "at least 70 inches wide", "met": "no",
                         "evidence": '"44 inches" -- well under the minimum'},),
          reasoning="Too narrow, and nothing else recommends it."),
     "scored"),
]


def build(cfg: Config, path: str | Path) -> Store:
    """Create (or refresh) a demo database and return the open Store."""
    path = Path(path)
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)

    store = Store(path)
    source = FixtureSource(cfg.location, "fixtures/listings")
    scorer, notifiers = StubScorer(), [DashboardNotifier(store)]

    # Real pipeline, real gate, real rejection reasons -- so the hunt views and
    # the runs table look like the genuine article rather than a mock.
    for hunt in cfg.hunts:
        run_hunt(store, hunt, source, scorer, notifiers, cfg.location)

    tv = next((h.id for h in cfg.hunts if h.name == "tv-stand"), "want:tv-stand")
    for listing_kw, score_kw, status in CASES:
        lid = listing_kw.pop("lid")
        title = listing_kw.pop("title")
        price = listing_kw.pop("price")
        listing = _listing(lid, title, price, **listing_kw)
        store.upsert_listing(listing)
        hunt_id = score_kw.pop("hunt_id", tv)
        store.mark_matches(hunt_id, [listing])
        store.save_score(_score(lid, hunt_id, **score_kw),
                         priced_at_cents=listing.previous_price_cents)
        store.set_status(hunt_id, lid, status)
        # A couple of price observations so the sparkline has something to draw.
        if listing.previous_price_cents:
            store.record_price(lid, listing.previous_price_cents)
        store.record_price(lid, listing.price_cents)
    return store
