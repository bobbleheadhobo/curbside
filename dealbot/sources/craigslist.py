"""Craigslist, via its own JSON API.

Much kinder than Facebook: no HTML parsing anywhere. `sapi.craigslist.org`
serves both the search and the detail endpoints as JSON, so the whole adapter is
decoding rather than scraping.

Two things had to be worked out:

**Search items are positional arrays, not objects.** The shape is

    [internalId, postingIdOffset, categoryId, price, "1:locIdx~lat~lon",
     imgPrefix, [code, ...], ..., title]

where `price` is whole dollars and `-1` means none, position 4 carries the
coordinates inline, and the trailing `[code, ...]` pairs are typed fields:
4 = image ids, 6 = url slug, 10 = formatted price, 13 = the posting uuid.

**The detail endpoint wants the uuid, not the numeric posting id.** Field 13.
Passing the posting id returns `parameter uuid=... failed`. Detail then hands
back `body` (the description), all images, exact lat/lon, `postedDate`, a
canonical `url`, and sometimes a condition attribute -- everything the search
feed omits.

The `?format=rss` endpoint is blocked outright (403), so this is the path.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from html import unescape
from typing import Any, Iterator

import requests

from ..geo import haversine_miles
from ..models import Hunt, Listing, Location, RawListing
# Defined in sources/base so the pipeline can tell "Craigslist is gating us"
# from "this one payload would not parse" without importing every adapter.
from .base import (BudgetExhausted, SourceBlocked,    # noqa: F401  re-exported
                   StaleCopy, Throttled)

log = logging.getLogger("dealbot.sources.craigslist")

SEARCH_URL = "https://sapi.craigslist.org/web/v8/postings/search/full"
DETAIL_URL = "https://sapi.craigslist.org/web/v8/postings/{uuid}"
IMAGE_URL = "https://images.craigslist.org/{img}_600x450.jpg"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

# Field codes inside a search item's trailing typed pairs.
F_IMAGES, F_SLUG, F_PRICE_STR, F_UUID = 4, 6, 10, 13

# "zip" is free stuff; "sss" is everything for sale.
PATH_FREE, PATH_ALL = "zip", "sss"


def _updated_at(item: dict) -> datetime | None:
    """The posting's own version stamp, off a detail item."""
    stamp = item.get("updatedDate")
    if not isinstance(stamp, int):
        return None
    return datetime.fromtimestamp(stamp, timezone.utc)


def _is_stale(now: datetime | None, was: datetime | None) -> bool:
    """Is this detail payload OLDER than the one already stored?

    Craigslist serves the item endpoint from a cache that does not converge:
    two fetches of the same posting eight minutes apart returned the seller's
    pre-edit copy ($150, the original body) and their post-edit copy ($125,
    reworded). Stored blind, the listing's price ping-ponged between the two
    for a day and a half -- nine price observations recording a change that
    never happened, and, because each downward swing clears
    `PRICE_DROP_THRESHOLD`, an appraisal bought every time the gate read one
    as a price drop.

    `updatedDate` tells the two copies apart without guessing, and it is
    carried on `Listing.source_updated_at` rather than dug out of `raw`,
    because `row_to_listing` does not rebuild `raw` -- a version of this that
    read it there was dead code that its own tests could not see, since they
    were the only thing that ever built such a listing.

    Absent on either side means "cannot tell", which lets the payload through:
    the first detail fetch of a listing has nothing to compare against, and
    fail-open is the rule everywhere else here.
    """
    return now is not None and was is not None and now < was


def _fields(item: list) -> dict[int, list]:
    return {e[0]: e[1:] for e in item
            if isinstance(e, list) and e and isinstance(e[0], int)}


# Craigslist's `body` is HTML, not text. Every one of the 310 descriptions
# collected carries a `<br>`, and the ones with a phone number carry a
# `<showcontactinfo>` element the site swaps for the digits in a browser.
# Left in, that markup reaches the two places it does not belong: the prompt,
# where the model reads tags as if the seller had typed them, and the card,
# which escapes them and shows the reader a literal "<br>".
_BR = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]*>")
_BLANK_RUN = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")


def _plain_text(body: str | None) -> str | None:
    """The seller's words, with Craigslist's markup taken back out.

    Tags are dropped rather than escaped, and their text content is kept: a
    `<b>free</b>` keeps the word and loses the emphasis, and an empty element
    like `<showcontactinfo>` simply disappears. Entities are unescaped LAST,
    so a seller who typed "&lt;br&gt;" ends up with those four characters
    rather than a line break they never asked for.
    """
    if not body:
        return body
    text = _TAG.sub("", _BR.sub("\n", body))
    text = unescape(text)
    text = _TRAILING_SPACE.sub("\n", text)
    return _BLANK_RUN.sub("\n\n", text).strip() or None


def _latlon(encoded: Any) -> tuple[float | None, float | None]:
    """Position 4 looks like "1:0~34.9419~-106.7083"."""
    if not isinstance(encoded, str) or "~" not in encoded:
        return None, None
    parts = encoded.split("~")
    try:
        return float(parts[1]), float(parts[2])
    except (IndexError, ValueError):
        return None, None


class CraigslistSource(Throttled):
    name = "craigslist"

    def __init__(self, location: Location, area_id: int = 50, *,
                 min_interval_seconds: float = 4.0, jitter: float = 0.3,
                 max_requests_per_run: int = 40, timeout: float = 30.0):
        self.location = location
        self.area_id = area_id
        self._init_budget(min_interval=min_interval_seconds, jitter=jitter,
                          max_requests=max_requests_per_run, timeout=timeout)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": UA,
                                      "Accept": "application/json"})

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict:
        """Rate limiting lives HERE, not in the caller. Exhausting OUR budget
        now raises `BudgetExhausted` like Facebook's does, rather than the
        plain `SourceBlocked` this copy used to raise -- the caller can finally
        tell "we stopped asking" from "the site stopped answering"."""
        self._await_slot()
        resp = self._session.get(url, params=params, timeout=self.timeout)
        self._spend_slot()
        if resp.status_code != 200:
            raise SourceBlocked(f"HTTP {resp.status_code} for {url}")
        try:
            return resp.json()
        except ValueError as exc:
            # A block page or an interstitial arrives as HTML with a 200. Same
            # class of failure as Facebook's silent throttling, and it must read
            # as blocked rather than crash with a decode error.
            raise SourceBlocked(
                f"non-JSON response ({len(resp.content)} bytes) from {url}: {exc}"
            ) from None

    # --- search -------------------------------------------------------------

    def search(self, hunt: Hunt) -> Iterator[RawListing]:
        # A free sweep is a category browse; a want hunt is a keyword search.
        free_only = hunt.max_price_cents == 0
        path = PATH_FREE if free_only else PATH_ALL
        queries = [""] if free_only else list(hunt.queries or [""])

        seen: set[str] = set()
        for query in queries:
            params = {"batch": f"{self.area_id}-0-360-0-0", "cc": "US",
                      "lang": "en", "searchPath": path}
            if query:
                params["query"] = query
            payload = self._get(SEARCH_URL, params)
            yield from self.parse_search(payload, seen)

    def parse_search(self, payload: dict, seen: set[str] | None = None,
                     now: datetime | None = None) -> list[RawListing]:
        """Pure: API payload in, RawListings out."""
        data = payload.get("data") or {}
        if "items" not in data:
            raise SourceBlocked(f"no items key in response; errors="
                                f"{payload.get('errors')}")
        seen = seen if seen is not None else set()
        now = now or datetime.now(timezone.utc)
        out: list[RawListing] = []
        for item in data["items"]:
            if not isinstance(item, list) or len(item) < 6:
                continue
            fields = _fields(item)
            uuid = (fields.get(F_UUID) or [None])[0]
            if not uuid or uuid in seen:
                continue
            seen.add(uuid)
            out.append(RawListing(source=self.name, source_id=str(uuid),
                                  payload={"item": item, "fields":
                                           {str(k): v for k, v in fields.items()}},
                                  fetched_at=now))
        return out

    def parse(self, raw: RawListing) -> Listing | None:
        item = raw.payload["item"]
        fields = {int(k): v for k, v in raw.payload["fields"].items()}
        title = item[-1] if isinstance(item[-1], str) else None
        if not title:
            return None

        price = item[3] if len(item) > 3 and isinstance(item[3], int) else -1
        lat, lng = _latlon(item[4] if len(item) > 4 else None)
        distance = None
        if lat is not None and lng is not None:
            distance = round(haversine_miles(self.location.lat, self.location.lng,
                                             lat, lng), 1)
        slug = (fields.get(F_SLUG) or [""])[0]
        images = tuple(IMAGE_URL.format(img=i.split(":", 1)[-1])
                       for i in (fields.get(F_IMAGES) or []))

        return Listing(
            id=f"{self.name}:{raw.source_id}",
            source=self.name, source_id=raw.source_id,
            title=title, description=None,
            # -1 means no price shown, which is NOT the same as free.
            price_cents=None if price < 0 else price * 100,
            currency="USD",
            url=f"https://www.craigslist.org/view/d/{slug}/{raw.source_id}",
            city=None, lat=lat, lng=lng, distance_mi=distance,
            seller_id=None, seller_name=None,
            images=images, category=None, posted_at=None,
            raw=raw.payload,
        )

    # --- detail -------------------------------------------------------------

    def detail(self, listing: Listing) -> Listing | None:
        payload = self._get(DETAIL_URL.format(uuid=listing.source_id),
                            {"lang": "en", "cc": "US"})
        full = self.parse_detail(payload, listing)
        if full is not None and _is_stale(full.source_updated_at,
                                          listing.source_updated_at):
            # A cached copy older than the one we already hold. Not an error,
            # not evidence of anything -- just nothing new. Raised rather than
            # returned as None, because None means "nothing there" and this
            # means the opposite: the pipeline judges the copy it already
            # holds instead of deferring, and `recheck` has already had its
            # availability answer from the page.
            log.info("stale detail payload for %s; ignored", listing.id)
            raise StaleCopy(listing.id)
        return full

    # --- liveness ------------------------------------------------------------

    def liveness(self, listing: Listing) -> str:
        """`removed`, `listed` or `unknown`, from the posting's own page.

        **The API is not evidence here.** `sapi` serves cached snapshots and
        keeps serving them after a posting is deleted: the Onkyo receiver that
        prompted this returned a full HTTP 200 payload, with price and body and
        photographs, for more than a day after its author took it down -- and
        two fetches eight minutes apart returned two DIFFERENT versions of it.
        Meanwhile the public page answered `410 Gone` the whole time.

        So availability is asked of the page, where the status code IS the
        answer, and a HEAD pays for no body at all. `410` is Craigslist's word
        for "deleted by its author", which is the only sale signal this source
        has -- it never states `is_sold` the way Facebook does.

        Anything else is `unknown` and retires nothing. A 403 or a 429 is the
        site gating us, and reading that as a sale would quietly empty the list
        of things the user saved.
        """
        self._await_slot()
        resp = self._session.head(listing.url, timeout=self.timeout,
                                  allow_redirects=True)
        self._spend_slot()
        if resp.status_code in (404, 410):
            return "removed"
        if resp.status_code == 200:
            return "listed"
        log.info("liveness for %s inconclusive: HTTP %d", listing.id,
                 resp.status_code)
        return "unknown"

    def parse_detail(self, payload: dict, listing: Listing) -> Listing | None:
        items = (payload.get("data") or {}).get("items") or []
        if not items or not isinstance(items[0], dict):
            return None
        d = items[0]

        loc = d.get("location") or {}
        lat, lng = loc.get("lat"), loc.get("lon")
        distance = listing.distance_mi
        if lat is not None and lng is not None:
            distance = round(haversine_miles(self.location.lat, self.location.lng,
                                             lat, lng), 1)

        body = _plain_text(d.get("body"))
        # Condition arrives as a structured attribute; fold it into the text the
        # model reads rather than inventing a new field for one source.
        for attr in d.get("attributes") or []:
            if attr.get("label") == "condition" and attr.get("value"):
                body = f"{body or ''}\n[condition: {attr['value']}]".strip()

        images = tuple(IMAGE_URL.format(img=i.split(":", 1)[-1])
                       for i in (d.get("images") or [])) or listing.images
        posted = d.get("postedDate")
        price = d.get("price")
        updated = _updated_at(d)

        return Listing(
            id=listing.id, source=listing.source, source_id=listing.source_id,
            title=d.get("title") or listing.title,
            description=body or listing.description,
            price_cents=(price * 100 if isinstance(price, int) and price >= 0
                         else listing.price_cents),
            currency=listing.currency,
            url=d.get("url") or listing.url,
            city=loc.get("area") or listing.city,
            lat=lat if lat is not None else listing.lat,
            lng=lng if lng is not None else listing.lng,
            distance_mi=distance,
            seller_id=None, seller_name=None,
            images=images[:6],
            category=d.get("categoryAbbr") or listing.category,
            posted_at=(datetime.fromtimestamp(posted, timezone.utc)
                       if posted else listing.posted_at),
            source_updated_at=updated or listing.source_updated_at,
            raw={**listing.raw, "detail": d},
        )
