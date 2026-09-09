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
        for item in json.loads(path.read_text()):
            yield RawListing(
                source=self.name,
                source_id=str(item["id"]),
                payload=item,
                fetched_at=now,
            )

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
