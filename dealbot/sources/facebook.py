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

3. **Photos must be read from `listing_photos`, never scraped off the page.**
   An item page carries ~32 `scontent` image URIs -- recommendation carousels,
   "more like this", the seller's other items. Taking the first six in document
   order stored other people's listings as this one's photos: one file ended up
   filed against 71 different listings, and the image pass was shown a mini
   bike as a TV stand's second photo. The item's OWN photos live in
   `listing_photos` on the product-details target, whose `id` is the listing
   id -- and at 960px rather than the 260px crop the carousel uses.

4. **Throttling is SILENT.** After roughly five rapid requests the server keeps
   answering 200 with ~590KB of perfectly valid HTML that simply contains no
   listing data at all. A naive adapter reads that as "quiet day" forever. The
   `feed_units` key is the discriminator: present means a real feed (possibly
   legitimately empty), absent means we were cut off -- which is an ERROR and has
   to be raised so it lands in the runs table.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterator

import requests

from ..geo import haversine_miles
from ..models import Hunt, Listing, Location, RawListing
# Defined in sources/base so the pipeline can tell "Facebook is gating us" from
# "this one payload would not parse" without importing every adapter.
from .base import (BudgetExhausted, SourceBlocked,    # noqa: F401  re-exported
                   Throttled)

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


def _json_blocks(html: str) -> Iterator[dict[str, Any]]:
    for raw in SCRIPT_JSON.findall(html):
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def _nodes_with(obj: Any, *keys: str) -> Iterator[dict[str, Any]]:
    """Depth-first walk for dicts carrying any of `keys`.

    Deliberately STRUCTURAL rather than path-based, which is the whole point:
    Facebook reshapes the wrapper constantly -- the photo node lives under
    `viewer.marketplace_product_details_page.target` today and somewhere else
    next month -- but a listing always has a `marketplace_listing_title` and a
    photo set always has `listing_photos`. The walk was written out twice, once
    per key set; the keys are an argument now.
    """
    if isinstance(obj, dict):
        if any(k in obj for k in keys):
            yield obj
        for v in obj.values():
            yield from _nodes_with(v, *keys)
    elif isinstance(obj, list):
        for v in obj:
            yield from _nodes_with(v, *keys)


def _photo_uris(node: dict[str, Any]) -> tuple[str, ...]:
    """The photo URIs from a `listing_photos` array, in the seller's order."""
    out: list[str] = []
    for photo in node.get("listing_photos") or []:
        if not isinstance(photo, dict):
            continue
        uri = (photo.get("image") or {}).get("uri")
        if uri and uri not in out:
            out.append(uri)
    return tuple(out[:6])


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


class FacebookSource(Throttled):
    name = "facebook"

    def __init__(self, location: Location, city: str = "albuquerque", *,
                 min_interval_seconds: float = 15.0, jitter: float = 0.4,
                 max_requests_per_run: int = 25, timeout: float = 30.0):
        self.location = location
        self.city = city
        self._init_budget(min_interval=min_interval_seconds, jitter=jitter,
                          max_requests=max_requests_per_run, timeout=timeout)
        self._session = requests.Session()
        self._session.headers.update(HEADERS)

    # --- politeness ---------------------------------------------------------

    def _get(self, url: str) -> str:
        """Rate limiting lives HERE, not in the caller, so it cannot be bypassed
        by accident. The waiting and counting are `Throttled`'s."""
        self._await_slot()
        resp = self._session.get(url, timeout=self.timeout, allow_redirects=True)
        self._spend_slot()
        if resp.status_code != 200:
            raise SourceBlocked(f"HTTP {resp.status_code} for {url}")
        return resp.text

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
            for node in _nodes_with(block, "marketplace_listing_title",
                                    "redacted_description"):
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
        queries = hunt.queries or ("",)
        self._reserve(len(queries))
        for query in queries:
            surfaces = self._surfaces(query)
            results, failures = None, []
            # Deduplicated per TERM, not across them: a listing two terms both
            # find comes back twice, tagged, and the pipeline keeps one. The
            # repeat is how it learns which terms find nothing the others miss.
            seen: set[str] = set()
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
            yield from (replace(r, query=query or None) for r in results)

    def parse(self, raw: RawListing) -> Listing | None:
        p = raw.payload
        title = p.get("marketplace_listing_title") or p.get("custom_title")
        if not title:
            return None

        geo = ((p.get("location") or {}).get("reverse_geocode") or {})
        # "Albuquerque, NM" rather than "Albuquerque": the state is what lets
        # the gate reject Kansas City without knowing where Kansas City is.
        city_name = geo.get("city")
        city_state = geo.get("state")
        city_label = (f"{city_name}, {city_state}" if city_name and city_state
                      else city_name)
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
            city=city_label or (p.get("location_text") or {}).get("text"),
            lat=lat, lng=lng, distance_mi=distance,
            seller_id=None, seller_name=None,
            images=(photo,) if photo else (),
            category=str(p.get("marketplace_listing_category_id") or "") or None,
            # Stated in the SEARCH feed, which is what makes it free to act on:
            # these never reach a detail fetch, let alone the model.
            is_ad=bool(p.get("is_partner_listing")),
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

    # The item page's equivalent of `feed_units`. A throttled response is ~590KB
    # of valid HTML carrying none of the marketplace product structure, and it
    # is indistinguishable from a deleted listing unless you look for this.
    # All three are absent from a throttled response and present on any page
    # that carries a real listing payload, synthetic test ones included.
    DETAIL_MARKERS = ("marketplace_product_details", "MarketplacePDP",
                      "marketplace_listing_title")

    def parse_detail_html(self, html: str, listing: Listing) -> Listing | None:
        """None means "this page did not carry THIS listing". It does not mean
        the listing is gone, and the re-check pass must not read it that way.

        A page with no product structure at all is the silent throttle, and it
        raises for the same reason the search path does: absorbing it turns a
        rate limit into "your saved listings were removed"."""
        if not any(m in html for m in self.DETAIL_MARKERS):
            raise SourceBlocked(
                f"no product data in {len(html)} bytes -- almost certainly throttled")
        best: dict[str, Any] | None = None
        photos: tuple[str, ...] = ()
        for block in _json_blocks(html):
            for node in _nodes_with(block, "marketplace_listing_title",
                                    "redacted_description"):
                if str(node.get("id") or "") != listing.source_id:
                    continue
                if best is None or len(node) > len(best):
                    best = node
            # The id check is what makes these safe to trust as THIS listing's
            # photos. Without it we are back to picking up the carousel.
            for node in _nodes_with(block, "listing_photos"):
                if not photos and str(node.get("id") or "") == listing.source_id:
                    photos = _photo_uris(node)
        if best is None:
            return None

        desc = (best.get("redacted_description") or {}).get("text")
        loc = best.get("location") or {}
        lat, lng = loc.get("latitude"), loc.get("longitude")
        distance = listing.distance_mi
        if lat is not None and lng is not None:
            distance = round(haversine_miles(self.location.lat, self.location.lng,
                                             lat, lng), 1)

        # Fall back to the primary photo alone rather than to anything scraped
        # off the page: one right photo beats six that may belong to somebody
        # else, and a listing with no photo simply gets no image pass.
        primary = ((best.get("primary_listing_photo") or {}).get("image")
                   or {}).get("uri")
        if not photos and primary:
            photos = (primary,)
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
            images=photos or listing.images,
            category=listing.category,
            posted_at=listing.posted_at, raw=merged,
        )
