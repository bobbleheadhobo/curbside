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
from .thumbs import ThumbnailStore

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

    # Saved, and then it sold. Saved things are kept when they go, so this is
    # the one state a designer meets weeks after everything else on the page.
    (dict(lid="demo:7", title="Electric sit-stand desk, 60in", price=0,
          sold="sold"),
     dict(deal_score=8.5, match="no", matched_want=None, worth_grabbing=True,
          reasoning="A working sit-stand desk for nothing is worth the drive."),
     "saved"),

    # And the end of the story: one you went and got. /saved counts it apart
    # from both the things still to act on and the ones that sold without you,
    # and it is the only row on the page carrying a figure that is not a guess.
    # Priced at $250 and grabbed for $180, so /stats has an estimate to check
    # itself against and the difference is visible rather than flattering.
    (dict(lid="demo:9", title="Walnut media console, 72in", price=25000),
     dict(matched_want="tv-stand", deal_score=8.0, est_value_cents=40000,
          reasoning="Solid walnut at the width you need. Worth the drive."),
     "grabbed"),

    # $0 in the price box, money asked for in the words. Scored as if it really
    # were free -- a box of wrenches IS worth the drive -- and kept out of the
    # free bin anyway, because a price nobody knows is not a price. Scored high
    # and binned nowhere is exactly the state /skipped exists to show.
    (dict(lid="demo:8", title="Sockets and wrenches", price=0,
          description="Send me offers please over 50 wrenches"),
     dict(hunt_id="sweep:free-nearby", deal_score=7.0, match="no",
          matched_want=None, worth_grabbing=True, price_unclear=True,
          est_value_cents=8000,
          red_flags=("Listed as free, but the description asks for offers",),
          unknowns=("what the seller actually wants for it",),
          reasoning="Worth the drive if it really is free, and it may well not "
                    "be -- the description asks for offers."),
     "scored"),

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
        sold = listing_kw.pop("sold", None)
        listing = _listing(lid, title, price, **listing_kw)
        store.upsert_listing(listing)
        hunt_id = score_kw.pop("hunt_id", tv)
        store.mark_matches(hunt_id, [listing])
        store.save_score(_score(lid, hunt_id, **score_kw),
                         priced_at_cents=listing.previous_price_cents)
        if status == "grabbed":
            # Through `mark_grabbed`, not `set_status`: it also stamps the
            # listing and clears it out of the other bins, and a demo database
            # that skipped that would show a state the live one cannot reach.
            store.set_status(hunt_id, lid, "saved")
            store.mark_grabbed(hunt_id, lid, 18000)
        else:
            store.set_status(hunt_id, lid, status)
        if sold:
            store.mark_sold(lid, sold)
        # A price history that spans weeks, not one instant. Two observations
        # sharing a timestamp draw a sparkline that says nothing.
        if listing.previous_price_cents:
            store.record_price(lid, listing.previous_price_cents,
                               _stamp(NOW - timedelta(days=24)))
            store.record_price(lid, (listing.previous_price_cents
                                     + listing.price_cents) // 2,
                               _stamp(NOW - timedelta(days=11)))
        store.record_price(lid, listing.price_cents, _stamp(NOW))

    _seed_thumbnails(store, Path(path).parent / "thumbs")
    return store


def _stamp(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


# Palettes standing in for a photographed thing: enough to tell the cards apart
# and to judge the layout, obviously not real photographs.
SWATCHES = [((214, 199, 176), (120, 96, 68)), ((186, 199, 205), (72, 92, 104)),
            ((205, 186, 186), (110, 74, 74)), ((190, 202, 186), (78, 100, 76)),
            ((208, 196, 214), (96, 80, 110)), ((214, 205, 182), (128, 112, 72))]


def _seed_thumbnails(store: Store, root: Path) -> None:
    """Fill the thumbnail cache so the demo dashboard has pictures.

    The real store downloads them; this one draws them, because every test and
    every demo build has to work with no network. Without this, almost every
    demo card renders the "photo expired" state and the photo -- which is the
    first thing you judge a listing on -- cannot be designed against at all.
    """
    from PIL import Image, ImageDraw

    thumbs = ThumbnailStore(root)
    thumbs.root.mkdir(parents=True, exist_ok=True)
    rows = store.conn.execute(
        "SELECT id, images FROM listings WHERE images IS NOT NULL AND images != '[]'")
    for n, row in enumerate(rows):
        target = thumbs.path_for(row["id"])
        if target.exists():
            continue
        ground, mark = SWATCHES[n % len(SWATCHES)]
        img = Image.new("RGB", (512, 512), ground)
        d = ImageDraw.Draw(img)
        # a lit ground and one solid mass on it -- the shape of a photographed
        # object, without pretending to be one
        d.rectangle((0, 340, 512, 512), fill=tuple(int(c * 0.92) for c in ground))
        d.rounded_rectangle((96 + (n % 3) * 24, 150, 400 + (n % 3) * 16, 380),
                            radius=14, fill=mark)
        img.save(target, "JPEG", quality=82)
