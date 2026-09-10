"""Confirming that a find is still there.

`mark_gone` retires a listing that stops appearing in search results, which is
a statement about visibility rather than about sale: three misses on the
15-minute free sweep is 45 minutes off page one, and in the live data 32
listings were retired within an hour of first being seen. Falling off page one
and being sold look identical from the outside.

So ask the source directly, for the handful of listings you might actually act
on. Facebook's item payload carries `is_sold` and `is_live` outright; a
Craigslist posting that has been taken down simply stops returning a detail
payload. Neither answer needs the model, so this costs requests and no quota
at all -- the same principle as the rest of the pipeline: fetching is free,
judgement is not.

Three things keep it cheap and safe:

  * **Only what is in a bin.** Re-checking the whole store would be hundreds of
    requests to learn something about listings nobody will look at.
  * **Paced per listing.** One detail fetch per listing per interval, capped
    per run, and the source's own rate limit still applies underneath.
  * **Fails open.** A request that errors is not evidence a listing is gone.
    Nothing is retired on an unreadable answer, and a gated source stops the
    pass rather than quietly retiring everything behind it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from .db import Store
from .models import Listing
from .sources.base import Source, SourceBlocked

log = logging.getLogger("dealbot.recheck")

# What is worth a request: things you might drive to. `scored` is the vast
# middle and nobody is going to look at it.
BIN_STATUSES = ("wanted", "free_find", "saved", "contacted")

# A decision the user made about a listing is theirs. Something they saved is
# still theirs after it sells -- it gets marked, not taken off their list.
KEEP_STATUS = ("saved", "contacted")


@dataclass
class RecheckResult:
    n_checked: int = 0
    n_sold: int = 0
    n_removed: int = 0
    n_listed: int = 0
    error: str | None = None


def availability(full: Listing | None) -> str:
    """`sold`, `removed` or `listed`, from whatever the source gave back.

    `removed` is the weaker claim: the item page no longer resolves, which is
    usually a sale but the source did not say so, and a listing can also be
    deleted or hidden. Recorded separately so a "sold" badge never overstates
    what we actually know.
    """
    if full is None:
        return "removed"
    raw = full.raw or {}
    if raw.get("is_sold"):
        return "sold"
    if raw.get("is_live") is False:
        return "removed"
    return "listed"


def recheck(store: Store, sources: Sequence[tuple[str, Source]], *,
            every_hours: float = 6.0, max_per_run: int = 10,
            statuses: Sequence[str] = BIN_STATUSES) -> RecheckResult:
    """Re-confirm the availability of listings sitting in a bin."""
    result = RecheckResult()
    if max_per_run <= 0 or not statuses:
        return result

    # Zero or less means "all of them", not "none of them": that is what the
    # manual `recheck --all` asks for.
    cutoff = None if every_hours <= 0 else (
        datetime.now(timezone.utc) - timedelta(hours=every_hours)
    ).isoformat(timespec="seconds")
    due = store.due_for_recheck(list(statuses), cutoff, max_per_run)
    by_name = {name: src for name, src in sources}

    for hunt_id, status, listing in due:
        source = by_name.get(listing.source)
        if source is None or not hasattr(source, "detail"):
            continue                      # fixture runs, or a source dropped
        try:
            full = source.detail(listing)
        except SourceBlocked as exc:
            # Gated, or out of request budget. Stop asking; the rest keep their
            # place in the queue and are checked next time.
            log.warning("recheck stopped at %s: %s", listing.id, exc)
            result.error = f"recheck stopped: {type(exc).__name__}: {exc}"
            break
        except Exception as exc:                          # noqa: BLE001
            # FAIL OPEN. An unreadable answer is not evidence that a listing is
            # gone, and retiring on one would delete the find the user wanted.
            # No stamp either, so it is retried rather than skipped for hours.
            log.warning("recheck failed for %s: %s", listing.id, exc)
            continue

        result.n_checked += 1
        store.mark_rechecked(hunt_id, listing.id)
        verdict = availability(full)

        if verdict == "listed":
            # Still there. Refresh it while we have it: this is the only place
            # a price drop on something already judged gets noticed, since the
            # gate stops such a listing ever being fetched again.
            result.n_listed += 1
            store.upsert_listing(full)
            store.record_price(full.id, full.price_cents)
            continue

        store.mark_sold(listing.id, verdict)
        result.n_sold += verdict == "sold"
        result.n_removed += verdict == "removed"
        retired = store.retire_sold(listing.id)
        log.info("%s is %s; retired from %d bin(s)%s", listing.id, verdict,
                 retired, " (kept on your list)" if status in KEEP_STATUS else "")

    return result
