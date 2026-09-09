"""Facebook Marketplace, logged out.

No cookies, no login, no browser. The public search page still embeds its
GraphQL payload in `<script type="application/json">` blocks, and so does the
item page, so a plain HTTPS GET is enough.

Three things had to be learned by trying:

1. **`Sec-Fetch-*` headers are mandatory.** Without `Sec-Fetch-Site: none` and
   friends the response is a bodyless HTTP 400. The response even says so:
   `vary: Sec-Fetch-Site, Sec-Fetch-Mode`.

2. **The search feed has no descriptions and no coordinates.** It carries title,
   price, city, one thumbnail and the id -- and real titles are frequently just
   "Free". The item page has `redacted_description` and exact lat/lng. So this is
   a TWO-STAGE fetch: search is a cheap index, detail is fetched only for
   listings that survive the gate. Same funnel shape as the scoring.

3. **Throttling is SILENT.** After roughly five rapid requests the server keeps
   answering 200 with ~590KB of perfectly valid HTML that simply contains no
   listing data at all. A naive adapter reads that as "quiet day" forever. The
   `feed_units` key is the discriminator: present means a real feed (possibly
   legitimately empty), absent means we were cut off -- which is an ERROR and has
   to be raised so it lands in the runs table.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterator

import requests

from ..geo import haversine_miles
from ..models import Hunt, Listing, Location, RawListing

log = logging.getLogger("dealbot.sources.facebook")

BASE = "https://www.facebook.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # Without these the response is a bodyless 400.
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua-platform": '"Linux"',
}

# Facebook exposes the same feed through more than one page. When one is gated
# the other frequently is not, and for a browse-style query the category page is
# simply better: /marketplace/albuquerque/free returned 24 listings where
# /search?query=free returned 15. Category first, search as the fallback.
CATEGORY_SLUGS = {
    "free": "free",
    "furniture": "furniture",
    "appliances": "appliances",
    "tools": "tools",
    "electronics": "electronics",
    "home goods": "household",
}

SCRIPT_JSON = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)
PHOTO_URI = re.compile(r'"uri":"(https:\\?/\\?/scontent[^"]{40,}?)"')


class SourceBlocked(RuntimeError):
    """Facebook answered, but withheld the data. Distinct from an empty result:
    this must fail the run loudly rather than look like a quiet day."""


class BudgetExhausted(SourceBlocked):
    """Our own politeness limit, not Facebook's. Trying another surface cannot
    help, and reporting it as "all surfaces gated" blames the wrong thing."""


def _json_blocks(html: str) -> Iterator[dict[str, Any]]:
    for raw in SCRIPT_JSON.findall(html):
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def _find_listings(obj: Any) -> Iterator[dict[str, Any]]:
    """Depth-first walk for listing-shaped dicts. Deliberately structural rather
    than path-based -- Facebook reshapes the wrapper constantly, but a listing
    always has a `marketplace_listing_title`."""
    if isinstance(obj, dict):
        if "marketplace_listing_title" in obj or "redacted_description" in obj:
            yield obj
        for v in obj.values():
            yield from _find_listings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_listings(v)


def _cents(price: dict[str, Any] | None) -> int | None:
    if not price:
        return None
    raw = price.get("amount")
    if raw is None:
        return None
    try:
        return int(round(float(raw) * 100))
    except (TypeError, ValueError):
        return None


class FacebookSource:
    name = "facebook"

    def __init__(self, location: Location, city: str = "albuquerque", *,
                 min_interval_seconds: float = 15.0, jitter: float = 0.4,
                 max_requests_per_run: int = 25, timeout: float = 30.0):
        self.location = location
        self.city = city
        self.min_interval = min_interval_seconds
        self.jitter = jitter
        self.max_requests = max_requests_per_run
        self.timeout = timeout
        self._last_request = 0.0
        self._requests_made = 0
        self._session = requests.Session()
        self._session.headers.update(HEADERS)

    # --- politeness ---------------------------------------------------------

    def _get(self, url: str) -> str:
        """Rate limiting lives HERE, not in the caller, so it cannot be bypassed
        by accident. Silent throttling is the failure mode, so the interval is
        deliberately generous."""
        if self._requests_made >= self.max_requests:
            raise BudgetExhausted(
                f"request budget exhausted ({self.max_requests} this run)")

        wait = self.min_interval * (1 + random.uniform(-self.jitter, self.jitter))
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < wait:
            time.sleep(wait - elapsed)

        resp = self._session.get(url, timeout=self.timeout, allow_redirects=True)
        self._last_request = time.monotonic()
        self._requests_made += 1
        if resp.status_code != 200:
            raise SourceBlocked(f"HTTP {resp.status_code} for {url}")
        return resp.text

    def reset_budget(self) -> None:
        self._requests_made = 0

    # --- search (cheap index) -----------------------------------------------

    def parse_search_html(self, html: str, seen: set[str] | None = None,
                          now: datetime | None = None) -> list[RawListing]:
        """Pure: HTML in, RawListings out. Split from the fetch so the whole
        parser can be tested against recorded pages with no network."""
        # The discriminator. Absent feed structure means we were cut off, not
        # that nothing matched -- see the module docstring on silent throttling.
        if "feed_units" not in html and "marketplace_search" not in html:
            raise SourceBlocked(
                f"no feed data in {len(html)} bytes -- almost certainly throttled")

        seen = seen if seen is not None else set()
        now = now or datetime.now(timezone.utc)
        out: list[RawListing] = []
        for block in _json_blocks(html):
            for node in _find_listings(block):
                lid = str(node.get("id") or "")
                if not lid or lid in seen or node.get("is_sold"):
                    continue
                seen.add(lid)
                out.append(RawListing(source=self.name, source_id=lid,
                                      payload=node, fetched_at=now))
        return out

    def _surfaces(self, query: str) -> list[str]:
        """Candidate URLs for one query, best first."""
        urls = []
        slug = CATEGORY_SLUGS.get(query.strip().lower())
        if slug:
            urls.append(f"{BASE}/marketplace/{self.city}/{slug}")
        urls.append(f"{BASE}/marketplace/{self.city}/search"
                    f"?query={requests.utils.quote(query)}")
        return urls

    def search(self, hunt: Hunt) -> Iterator[RawListing]:
        seen: set[str] = set()
        for query in (hunt.queries or ("",)):
            surfaces = self._surfaces(query)
            results, failures = None, []
            for url in surfaces:
                try:
                    results = self.parse_search_html(self._get(url), seen)
                    break
                except BudgetExhausted:
                    raise                 # our own limit; another surface won't help
                except SourceBlocked as exc:
                    # Gated on this surface. Try the next one before concluding
                    # we are cut off -- that is the whole point of having two.
                    failures.append(f"{url.rsplit('/', 1)[-1]}: {exc}")
                    continue
            if results is None:
                raise SourceBlocked(
                    f"all {len(surfaces)} surfaces gated for {query!r} "
                    f"({'; '.join(failures)})")
            yield from results

    def parse(self, raw: RawListing) -> Listing | None:
        p = raw.payload
        title = p.get("marketplace_listing_title") or p.get("custom_title")
        if not title:
            return None

        geo = ((p.get("location") or {}).get("reverse_geocode") or {})
        loc = p.get("location") or {}
        lat, lng = loc.get("latitude"), loc.get("longitude")
        distance = None
        if lat is not None and lng is not None:
            distance = round(haversine_miles(self.location.lat, self.location.lng,
                                             lat, lng), 1)

        photo = (((p.get("primary_listing_photo") or {}).get("image") or {})
                 .get("uri"))
        created = p.get("creation_time")
        desc = (p.get("redacted_description") or {}).get("text")

        return Listing(
            id=f"{self.name}:{raw.source_id}",
            source=self.name, source_id=raw.source_id,
            title=title,
            description=desc,
            price_cents=_cents(p.get("listing_price")),
            previous_price_cents=_cents(p.get("strikethrough_price")),
            currency="USD",
            url=f"{BASE}/marketplace/item/{raw.source_id}/",
            city=geo.get("city") or (p.get("location_text") or {}).get("text"),
            lat=lat, lng=lng, distance_mi=distance,
            seller_id=None, seller_name=None,
            images=(photo,) if photo else (),
            category=str(p.get("marketplace_listing_category_id") or "") or None,
            posted_at=(datetime.fromtimestamp(created, timezone.utc)
                       if created else None),
            raw=p,
        )

    # --- detail (only for gate survivors) -----------------------------------

    def detail(self, listing: Listing) -> Listing | None:
        """Fetch the item page to fill in what the search feed omits: the
        description and the exact coordinates. Called only for candidates, which
        is what keeps the request count survivable."""
        return self.parse_detail_html(
            self._get(f"{BASE}/marketplace/item/{listing.source_id}/"), listing)

    def parse_detail_html(self, html: str, listing: Listing) -> Listing | None:
        best: dict[str, Any] | None = None
        for block in _json_blocks(html):
            for node in _find_listings(block):
                if str(node.get("id") or "") != listing.source_id:
                    continue
                if best is None or len(node) > len(best):
                    best = node
        if best is None:
            return None

        desc = (best.get("redacted_description") or {}).get("text")
        loc = best.get("location") or {}
        lat, lng = loc.get("latitude"), loc.get("longitude")
        distance = listing.distance_mi
        if lat is not None and lng is not None:
            distance = round(haversine_miles(self.location.lat, self.location.lng,
                                             lat, lng), 1)

        photos = []
        for m in PHOTO_URI.findall(html):
            u = m.replace("\\/", "/")
            if u.split("?")[0] not in {p.split("?")[0] for p in photos}:
                photos.append(u)
        merged = {**listing.raw, **best}

        return Listing(
            id=listing.id, source=listing.source, source_id=listing.source_id,
            title=best.get("marketplace_listing_title") or listing.title,
            description=desc or listing.description,
            # NOT `or`: a free item is 0, which is falsy. `0 or 500` keeps the
            # stale $500, so a seller dropping their price to free would still
            # show the old price -- defeating the free sweep and the price-drop
            # signal for exactly the transition that matters most.
            price_cents=(detail_price if (detail_price := _cents(best.get("listing_price"))) is not None
                         else listing.price_cents),
            previous_price_cents=(_cents(best.get("strikethrough_price"))
                                  or listing.previous_price_cents),
            currency=listing.currency, url=listing.url,
            city=(best.get("location_text") or {}).get("text") or listing.city,
            lat=lat if lat is not None else listing.lat,
            lng=lng if lng is not None else listing.lng,
            distance_mi=distance,
            seller_id=listing.seller_id, seller_name=listing.seller_name,
            images=tuple(photos[:6]) or listing.images,
            category=listing.category,
            posted_at=listing.posted_at, raw=merged,
        )
