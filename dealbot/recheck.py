"""Confirming that a find is still there.

`mark_gone` retires a listing that stops appearing in search results, which is
a statement about visibility rather than about sale: three misses on the
15-minute free sweep is 45 minutes off page one, and in the live data 32
listings were retired within an hour of first being seen. Falling off page one
and being sold look identical from the outside.

So ask the source directly, for the handful of listings you might actually act
on. Facebook's item payload carries `is_sold` and `is_live` outright.
Craigslist states nothing, and its item endpoint cannot even be trusted to
stop answering -- it served a deleted posting in full for over a day -- so
that source is asked through `liveness` instead, which reads the status code
of the posting's own page. Neither answer needs the model, so this costs
requests and no quota at all -- the same principle as the rest of the
pipeline: fetching is free, judgement is not.

Three things keep it cheap and safe:

  * **Only what is in a bin.** Re-checking the whole store would be hundreds of
    requests to learn something about listings nobody will look at.
  * **Paced per listing, at two speeds.** One detail fetch per listing per
    interval, capped per run, and the source's own rate limit still applies
    underneath. The list you curated is asked about on every pass; the
    candidate bins are paced far slower. They do not cost the same: a saved
    listing is one you might be about to drive to, while `wanted` and
    `free_find` are things you have not decided on -- and they are the half
    that grows to dozens, against sources that throttle silently. Anything
    that simply vanishes from search is retired by `mark_gone` regardless.
  * **Fails open, and means it.** A request that errors is not evidence a
    listing is gone, and neither is a page that came back without one. On
    Facebook a missing payload is `unknown` rather than `removed`, because a
    silent throttle looks exactly like a deleted listing and retiring on it is
    irreversible. A gated source stops being asked; the others carry on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from .db import Store
from .models import Listing
from .sources.base import Source, SourceBlocked, StaleCopy

log = logging.getLogger("dealbot.recheck")

# What is worth a request: things you might drive to. `scored` is the vast
# middle and nobody is going to look at it.
BIN_STATUSES = ("wanted", "free_find", "saved")

# A decision the user made about a listing is theirs. Something they saved is
# still theirs after it sells -- it gets marked, not taken off their list.
KEEP_STATUS = ("saved",)

# `grabbed` is deliberately in NEITHER list. `mark_grabbed` stamps `sold_at`,
# and `due_for_recheck` already filters on that, so asking Facebook whether a
# thing in your garage is still for sale cannot happen. Adding it here would
# spend a request per pass to be told what you already know.


@dataclass
class RecheckResult:
    n_checked: int = 0
    n_sold: int = 0
    n_removed: int = 0
    n_listed: int = 0
    # Asked, and could not tell. Retires nothing.
    n_unknown: int = 0
    error: str | None = None


def availability(full: Listing | None, source: str = "") -> str:
    """`sold`, `removed`, `listed`, or `unknown`, read off a detail payload.

    The fallback, for a source with no `liveness` of its own. Craigslist has
    one now, so in practice this speaks for Facebook.

    `removed` is the weaker claim: the item page no longer resolves, which is
    usually a sale but the source did not say so, and a listing can also be
    deleted or hidden. Recorded separately so a "sold" badge never overstates
    what we actually know.

    **A missing payload is never `removed` on Facebook.** There, "the page did
    not contain this listing" and "we are being throttled" look identical, and
    retiring on it is irreversible: `mark_sold` COALESCEs so the stamp never
    moves, `due_for_recheck` skips anything stamped, and `mark_seen` will not
    un-`gone` it. One throttled window would have quietly emptied a bin of
    things the user had saved. Facebook states `is_sold` and `is_live`
    outright when the page does resolve, so nothing is lost by insisting on
    that positive evidence; a listing that really is gone still stops
    appearing in search and is retired by `mark_gone`.
    """
    if full is None:
        return "unknown" if source == "facebook" else "removed"
    raw = full.raw or {}
    if raw.get("is_sold"):
        return "sold"
    if raw.get("is_live") is False:
        return "removed"
    return "listed"


def recheck(store: Store, sources: Sequence[tuple[str, Source]], *,
            every_hours: float = 6.0, saved_every_hours: float = 0.25,
            max_per_run: int = 10,
            statuses: Sequence[str] = BIN_STATUSES) -> RecheckResult:
    """Re-confirm the availability of listings sitting in a bin.

    Two speeds, and the faster one goes FIRST so a full candidate bin can never
    crowd your own list out of the per-run cap.
    """
    result = RecheckResult()
    if max_per_run <= 0 or not statuses:
        return result

    def cutoff(hours: float) -> str | None:
        # Zero or less means "all of them", not "none of them": that is what
        # the manual `recheck --all` asks for.
        if hours <= 0:
            return None
        return (datetime.now(timezone.utc)
                - timedelta(hours=hours)).isoformat(timespec="seconds")

    # Never LESS often than the candidates, and `--all` (0) still means all.
    keep_hours = 0.0 if every_hours <= 0 else min(every_hours, saved_every_hours)
    keep = [s for s in statuses if s in KEEP_STATUS]
    rest = [s for s in statuses if s not in KEEP_STATUS]
    due: list[tuple[str, str, Listing]] = []
    if keep:
        due += store.due_for_recheck(keep, cutoff(keep_hours), max_per_run)
    if rest and len(due) < max_per_run:
        due += store.due_for_recheck(rest, cutoff(every_hours),
                                     max_per_run - len(due))
    by_name = {name: src for name, src in sources}
    blocked: set[str] = set()
    asked: set[str] = set()

    for hunt_id, status, listing in due:
        source = by_name.get(listing.source)
        if source is None or not hasattr(source, "detail"):
            continue                      # fixture runs, or a source dropped
        if listing.source in blocked:
            continue
        # A listing matched by two hunts is two rows here. Asking twice spends
        # a second request on an answer we already have, against the very
        # budget that makes this pass stop early.
        if listing.id in asked:
            store.mark_rechecked(hunt_id, listing.id)
            continue
        asked.add(listing.id)

        # Ask the cheap, direct question first where the source has one. A
        # `removed` answer settles it without a detail fetch at all.
        verdict: str | None = None
        if hasattr(source, "liveness"):
            try:
                verdict = source.liveness(listing)
            except SourceBlocked as exc:
                _stop(result, blocked, listing, exc)
                continue
            except Exception as exc:                      # noqa: BLE001
                log.warning("liveness failed for %s: %s", listing.id, exc)
                continue

        # Both answers that are not "listed" are settled here, before any
        # detail fetch. A deleted posting has nothing worth refreshing, and a
        # source that is gating us will not answer the second request either.
        if verdict in ("removed", "sold", "unknown"):
            result.n_checked += 1
            store.mark_rechecked(hunt_id, listing.id)
            if verdict == "unknown":
                result.n_unknown += 1
                log.info("%s: no answer either way; left alone", listing.id)
            else:
                _retire(store, result, listing, status, verdict)
            continue

        try:
            full = source.detail(listing)
        except StaleCopy:
            # An older copy than the one held. Nothing to refresh, and no
            # evidence of anything -- the same as no payload at all, which is
            # what the verdict logic below already knows how to read.
            full = None
        except SourceBlocked as exc:
            # Gated, or out of request budget -- for THIS source. Stop asking
            # it and carry on with the others: Facebook's budget is spent by
            # the sweep that runs before this, and breaking outright meant one
            # exhausted source starved every Craigslist listing behind it.
            _stop(result, blocked, listing, exc)
            continue
        except Exception as exc:                          # noqa: BLE001
            # FAIL OPEN. An unreadable answer is not evidence that a listing is
            # gone, and retiring on one would delete the find the user wanted.
            # No stamp either, so it is retried rather than skipped for hours.
            log.warning("recheck failed for %s: %s", listing.id, exc)
            continue

        result.n_checked += 1
        store.mark_rechecked(hunt_id, listing.id)
        # A source that answered `listed` has ALREADY said the posting is
        # there, and said it from the surface that can tell. A missing or
        # stale payload after that is a failure to refresh, never a sale --
        # which is the whole bug: Craigslist's item endpoint kept serving a
        # deleted posting, and `availability` read the absence of a payload,
        # when it finally came, as the only sale signal the source had.
        if verdict is None:
            verdict = availability(full, listing.source)

        if verdict == "unknown":
            # Asked, could not tell. Stamped so it is paced rather than asked
            # again every pass, and retired from nothing.
            result.n_unknown += 1
            log.info("%s: no answer either way; left alone", listing.id)
            continue

        if verdict == "listed":
            # Still there. Refresh it while we have it: this is the only place
            # a price drop on something already judged gets noticed, since the
            # gate stops such a listing ever being fetched again.
            result.n_listed += 1
            if full is not None:
                store.upsert_listing(full)
                store.record_price(full.id, full.price_cents)
            continue

        _retire(store, result, listing, status, verdict)

    return result


def _stop(result: RecheckResult, blocked: set[str], listing: Listing,
          exc: Exception) -> None:
    """This SOURCE is gated or out of budget. Stop asking it, keep the others."""
    log.warning("recheck stopped for %s at %s: %s", listing.source, listing.id, exc)
    blocked.add(listing.source)
    result.error = (f"recheck stopped for {listing.source}: "
                    f"{type(exc).__name__}: {exc}")


def _retire(store: Store, result: RecheckResult, listing: Listing,
            status: str, verdict: str) -> None:
    store.mark_sold(listing.id, verdict)
    result.n_sold += verdict == "sold"
    result.n_removed += verdict == "removed"
    retired = store.retire_sold(listing.id)
    log.info("%s is %s; retired from %d bin(s)%s", listing.id, verdict,
             retired, " (kept on your list)" if status in KEEP_STATUS else "")
