import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dealbot.models import Hunt, Listing, Location, Want  # noqa: E402

ABQ = Location(lat=35.0844, lng=-106.6504, radius_miles=25)


@pytest.fixture
def want():
    return Want(name="tv-stand", description="70in+ media console",
                max_price_cents=25000, queries=("tv stand", "media console"))


@pytest.fixture
def hunt(want):
    return Hunt(id="want:tv-stand", name="tv-stand", kind="want",
                queries=want.queries, max_price_cents=25000,
                exclude=("wall mount",), wants=(want,), min_deal_score=7.0,
                free_find_min_score=5.0, interval_minutes=60, max_results=60)


def make_listing(lid="fixture:1", title="TV stand 72 inch", price_cents=10000,
                 **kw):
    base = dict(
        id=lid, source="fixture", source_id=lid.split(":")[-1], title=title,
        description="desc", price_cents=price_cents, currency="USD",
        url=f"https://example/{lid}", city="Albuquerque",
        lat=35.08, lng=-106.65, distance_mi=1.0, seller_id="s1",
        seller_name="S", images=("a.jpg",), category="furniture",
        # RELATIVE, like FixtureSource slides a recording forward. A fixed
        # date rots against `max_age_days`: this one read as fresh until the
        # 15th, when two tests about photoless listings began failing with
        # `too_old` for reasons that had nothing to do with photographs.
        posted_at=datetime.now(timezone.utc) - timedelta(days=1), raw={},
    )
    base.update(kw)
    return Listing(**base)
