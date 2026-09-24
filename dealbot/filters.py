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

import re
import sqlite3
from functools import lru_cache
from typing import Iterable, Mapping, Sequence

from .geo import approx_distance_miles
from .models import Candidate, GateResult, Hunt, Listing, Location, UpsertResult

# How far a price must fall before a listing we already judged is worth
# re-judging. Below this it is noise -- sellers nudge prices constantly.
PRICE_DROP_THRESHOLD = 0.15

# A decision you made, which is never re-judged. `grabbed` is the strongest
# case of it: the thing is in the user's house.
TRIAGED = ("saved", "dismissed", "grabbed")

# Post-enrichment rejections that cannot come untrue. A listing does not grow a
# photograph and it does not get younger, so re-deciding either one costs a
# detail fetch to learn a fact we already hold.
#
# This list is short on purpose, and what is left off it is left off for a
# reason:
#
# * `excluded_kw` -- those words are typed on a phone and deleted from a
#   phone, so a term you remove has to let its listings back.
# * `over_price`, `too_far`, `too_far_by_city` -- the gate below re-decides
#   all three from the current price and the CURRENT radius, which is a
#   number on the settings page. Sticking them would buy nothing and could
#   only go stale.
# * `duplicate_of:` -- the fingerprint is the weakest thing here. It has
#   already collapsed four different "Curb alert" posts into one, and making
#   a wrong merge permanent is exactly the confidently-wrong failure the
#   relist detection was left inert to avoid. So it is re-decided every run,
#   but from the STORE (see `REDECIDED_FROM_STORE`), never from a detail
#   fetch.
#
# `too_old` cannot be undone from the dashboard either: `max_age_days` lives
# in config.yaml, so raising it will not bring these back. That is the one
# case where the reason genuinely outlives the decision, and it is the right
# trade for a listing already past the age the hunt asked for.
#
# `is_ad` joins them, and is the least arguable of the four: it is not our
# inference at all, it is the source stating what kind of thing this is, and
# no edit by anybody turns a retail advertisement into a neighbour selling a
# planter.
PERMANENT_REJECTIONS = ("too_old", "no_photo", "nothing_to_judge", "is_ad")

# Rejections re-decided every run from what the store already holds, at no
# request cost. The gate admits them as `was_duplicate` and the pipeline
# re-runs the check on the stored, enriched row before the batch cap.
#
# Re-fetching was the old way, and it bought nothing: an unchanged listing
# re-hashes to the same keys and reproduces the same merge. What CAN undo one
# is a new price or title, which the search feed has already written to the
# store, or a fix to the key code -- and re-deciding from the store sees both.
#
# The fetch was not only wasted, it starved. An enriched duplicate carries a
# real posting date while a never-enriched Craigslist listing has only
# `first_seen`, so the duplicates won the batch cap on every run: five of
# them held all five of `want:bookshelf`'s slots for nine days, and the 21
# live listings behind them were never fetched, never judged, and `n_deferred`
# sat flat at 21.
REDECIDED_FROM_STORE = ("duplicate_of:",)


@lru_cache(maxsize=512)
def _term_pattern(term: str) -> re.Pattern[str]:
    """An exclude term, matched at word starts and tolerant of a plural.

    Plain substring matching was wrong in both directions once these became
    something you type on a phone rather than a line in a reviewed file.
    "bed" matched *bedroom set*, and this is the one rule here that fails
    CLOSED -- an over-broad term silently drops the thing you wanted, with only
    a reject-reason count to show for it. Anchoring to a word start fixes that.

    The trailing `(?:e?s)?` is the other half: an exact-word match would let
    "mattress" through every listing selling *mattresses*, which is the common
    case rather than the edge one.
    """
    return re.compile(r"(?<!\w)" + re.escape(term.lower().strip())
                      + r"(?:e?s)?(?!\w)")


def matches_any(listing: Listing, terms: Sequence[str]) -> str | None:
    """The first excluded term found in the title or description, if any.

    Public because the pipeline runs it AGAIN after enrichment. The gate only
    ever sees the search feed, where Facebook supplies no description and
    Craigslist hardcodes `description=None` -- so an exclude term that appears
    only in the body cannot fire here, and the listing gets paid for twice.
    """
    haystack = f"{listing.title}\n{listing.description or ''}".lower()
    for term in terms:
        if term.strip() and _term_pattern(term).search(haystack):
            return term
    return None


def gate(
    hunt: Hunt,
    listings: Iterable[Listing],
    location: Location,
    statuses: Mapping[str, str],
    last_scores: Mapping[str, sqlite3.Row],
    upserts: Mapping[str, UpsertResult],
    filtered: Mapping[str, str] | None = None,
) -> GateResult:
    """Decide who is worth spending money on. Pure: state comes in as dicts.

    Order matters -- cheapest checks first, and every one that fires is a
    listing never paid for. `unchanged` is the load-bearing rule: in steady
    state almost everything hits it, which is what makes a 15-minute poll
    interval affordable rather than ruinous.

    Rejections are RETURNED, not discarded, so the caller can record why. An
    empty result you cannot explain is indistinguishable from a broken scraper.

    `filtered` is listing id -> the reason this hunt last filtered it on, and
    it is what stops a permanent rejection being re-learned every run.
    """
    candidates: list[Candidate] = []
    rejected: list[tuple[str, str]] = []

    for listing in listings:
        status = statuses.get(listing.id)

        # Cheapest checks first; each one is a listing we never pay to think about.
        if status in TRIAGED:
            rejected.append((listing.id, "triaged"))
            continue

        # A retail advertisement, not a neighbour with a thing to get rid of.
        # This bot exists to find local pickups, and an ad is the one category
        # that can never be one however good the price looks: it ships from a
        # warehouse, there is nothing to drive to, and no price history of
        # ours means anything about it.
        #
        # 76 of 1,737 Facebook listings collected carry the flag, NONE of them
        # carry coordinates, and 29 had already been appraised -- pouf covers,
        # "Open Box" ottoman slipcovers, a Poshmark planter that reached
        # /skipped at 6.0 and is what sent me looking.
        if listing.is_ad:
            rejected.append((listing.id, "is_ad"))
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

        # A post-enrichment rejection for a reason that cannot come untrue is
        # a decision, not a gap -- but it leaves no score, so without this it
        # reads as "never judged" and comes back as a candidate on every run,
        # is fetched over HTTP again, and is dropped again. Forever.
        #
        # That is not theoretical. Three photoless Craigslist posts held three
        # of the free sweep's five candidate slots for four days, so the 73
        # listings queued behind them were never judged at all while
        # `n_deferred` sat at a flat 78 and every individual run looked fine.
        was_filtered = (filtered or {}).get(listing.id)
        if was_filtered and was_filtered.startswith(PERMANENT_REJECTIONS):
            rejected.append((listing.id, was_filtered))
            continue
        if was_filtered and was_filtered.startswith(REDECIDED_FROM_STORE):
            candidates.append(Candidate(listing, "was_duplicate"))
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
            candidates.append(Candidate(listing, "price_drop"))
            continue

        if upserts.get(listing.id) and upserts[listing.id].is_relist:
            candidates.append(Candidate(listing, "relist"))
            continue

        rejected.append((listing.id, "unchanged"))

    return GateResult(candidates=candidates, rejected=rejected)
