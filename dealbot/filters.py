"""The gate: cheap deterministic checks that decide who gets to cost money.

This is the cost-control layer. Everything here is arithmetic and string
matching -- no judgement, no API calls. Judgement is the model's job, and it only
sees what survives.

A pure function on purpose: the pipeline reads the state it needs and passes it
in, so the whole gate is testable with dicts and no database.

Rejections are RETURNED, not discarded. The caller records them with their
reason, which is what makes an empty result debuggable -- "fetched 200, rejected
all on over_price" and "fetched 0" look identical otherwise, and only one of them
means the scraper is broken.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Mapping, Sequence

from .geo import approx_distance_miles
from .models import Candidate, GateResult, Hunt, Listing, Location, UpsertResult

# How far a price must fall before a listing we already judged is worth
# re-judging. Below this it is noise -- sellers nudge prices constantly.
PRICE_DROP_THRESHOLD = 0.15

TRIAGED = ("saved", "dismissed", "contacted")


def matches_any(listing: Listing, terms: Sequence[str]) -> str | None:
    """The first excluded term found in the title or description, if any.

    Public because the pipeline runs it AGAIN after enrichment. The gate only
    ever sees the search feed, where Facebook supplies no description and
    Craigslist hardcodes `description=None` -- so an exclude term that appears
    only in the body cannot fire here, and the listing gets paid for twice.
    """
    haystack = f"{listing.title}\n{listing.description or ''}".lower()
    for term in terms:
        if term.lower() in haystack:
            return term
    return None


def gate(
    hunt: Hunt,
    listings: Iterable[Listing],
    location: Location,
    statuses: Mapping[str, str],
    last_scores: Mapping[str, sqlite3.Row],
    upserts: Mapping[str, UpsertResult],
    blocked_sellers: Sequence[str] = (),
) -> GateResult:
    """Decide who is worth spending money on. Pure: state comes in as dicts.

    Order matters -- cheapest checks first, and every one that fires is a
    listing never paid for. `unchanged` is the load-bearing rule: in steady
    state almost everything hits it, which is what makes a 15-minute poll
    interval affordable rather than ruinous.

    Rejections are RETURNED, not discarded, so the caller can record why. An
    empty result you cannot explain is indistinguishable from a broken scraper.
    """
    candidates: list[Candidate] = []
    rejected: list[tuple[str, str]] = []

    for listing in listings:
        status = statuses.get(listing.id)

        # Cheapest checks first; each one is a listing we never pay to think about.
        if status in TRIAGED:
            rejected.append((listing.id, "triaged"))
            continue

        if listing.seller_id and listing.seller_id in blocked_sellers:
            rejected.append((listing.id, "blocked_seller"))
            continue

        if (hunt.max_price_cents is not None
                and listing.price_cents is not None
                and listing.price_cents > hunt.max_price_cents):
            rejected.append((listing.id, "over_price"))
            continue

        # Facebook has no coordinates until the item page is fetched, so fall
        # back to the city name. That turns a 15-second detail request for
        # something in Santa Fe into a string comparison.
        distance = listing.distance_mi
        approx = distance is None
        if approx:
            distance = approx_distance_miles(listing.city, location.lat,
                                             location.lng)
        if distance is not None and distance > location.radius_miles:
            rejected.append((listing.id,
                             "too_far_by_city" if approx else "too_far"))
            continue

        if hunt.exclude and (hit := matches_any(listing, hunt.exclude)):
            rejected.append((listing.id, f"excluded_kw:{hit}"))
            continue

        prior = last_scores.get(listing.id)
        if prior is None:
            candidates.append(Candidate(listing, "new"))
            continue

        # Already judged. Only a material change earns a second opinion.
        was = prior["priced_at_cents"]
        now = listing.price_cents
        if (was is not None and now is not None and was > 0
                and (was - now) / was >= PRICE_DROP_THRESHOLD):
            candidates.append(Candidate(listing, "price_drop", None))
            continue

        if upserts.get(listing.id) and upserts[listing.id].is_relist:
            candidates.append(Candidate(listing, "relist", None))
            continue

        rejected.append((listing.id, "unchanged"))

    return GateResult(candidates=candidates, rejected=rejected)
