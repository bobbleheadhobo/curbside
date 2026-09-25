"""Fixture source: replays recorded JSON instead of hitting the network.

This is what makes P0 possible -- the whole pipeline runs end to end with no
network and no quota -- and it stays useful forever after, because captured real
responses become the regression suite. When the live adapter breaks (and it
will), the pipeline is still provably fine and the repair is confined to one file.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..geo import haversine_miles
from ..models import Hunt, Listing, Location, RawListing


class FixtureSource:
    name = "fixture"

    def __init__(self, location: Location, root: str | Path = "fixtures/listings"):
        self.location = location
        self.root = Path(root)

    def search(self, hunt: Hunt) -> Iterator[RawListing]:
        path = self.root / f"{hunt.name}.json"
        if not path.exists():
            return
        now = datetime.now(timezone.utc)
        items = json.loads(path.read_text())
        shift = self._anchor(items, now)
        for item in items:
            if shift is not None and item.get("posted_at"):
                item = dict(item, posted_at=(
                    datetime.fromisoformat(item["posted_at"]) + shift).isoformat())
            yield RawListing(
                source=self.name,
                source_id=str(item["id"]),
                payload=item,
                fetched_at=now,
            )

    @staticmethod
    def _anchor(items: list[dict], now: datetime):
        """Slide the whole recording forward so its NEWEST listing is "now".

        The dates in these files are absolute, and hunts filter on
        `max_age_days`. So the suite rotted with the calendar: a fixture
        recorded on the 8th passed the sweep's 7-day age filter until the
        13th, and then one listing crossed the line and a test that had
        nothing to do with ages started failing. Every later day would have
        taken another one.

        Shifting rather than stamping them all "now" keeps what the recording
        actually encodes -- one listing three days older than another, one old
        enough to be dropped -- which is the part the tests are about.
        """
        dates = [datetime.fromisoformat(i["posted_at"])
                 for i in items if i.get("posted_at")]
        if not dates:
            return None
        newest = max(dates)
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        return now - newest

    def parse(self, raw: RawListing) -> Listing | None:
        p = raw.payload
        loc = p.get("location") or {}
        lat, lng = loc.get("lat"), loc.get("lng")
        distance = None
        if lat is not None and lng is not None:
            distance = round(
                haversine_miles(self.location.lat, self.location.lng, lat, lng), 1)

        price = p.get("price")
        posted = p.get("posted_at")
        seller = p.get("seller") or {}

        return Listing(
            id=f"{raw.source}:{raw.source_id}",
            source=raw.source,
            source_id=raw.source_id,
            title=p.get("title", ""),
            description=p.get("description"),
            price_cents=None if price is None else int(round(float(price) * 100)),
            currency=p.get("currency", "USD"),
            url=p.get("url", ""),
            city=loc.get("city"),
            lat=lat,
            lng=lng,
            distance_mi=distance,
            seller_id=seller.get("id"),
            seller_name=seller.get("name"),
            images=tuple(p.get("images") or ()),
            category=p.get("category"),
            posted_at=datetime.fromisoformat(posted) if posted else None,
            raw=p,
        )
