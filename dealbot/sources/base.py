"""Source protocol.

`parse` lives on the source because parsing is inherently source-specific, but it
returns the shared `Listing` type and `validate` checks the result before anything
is written. A broken adapter therefore yields garbage that gets rejected and
logged -- it cannot corrupt the store.

Rate limiting belongs *inside* an adapter, never in the caller, so it cannot be
bypassed by accident.
"""
from __future__ import annotations

import random
import time
from typing import Iterator, Protocol

from ..models import Hunt, Listing, RawListing


class SourceBlocked(RuntimeError):
    """The site answered, but withheld the data. Distinct from an empty result:
    this must fail the run loudly rather than look like a quiet day.

    Shared by every adapter on purpose. The pipeline has to tell "the site is
    gating us, stop asking" apart from "this one payload would not parse", and
    it cannot do that while each source raises its own unrelated class.
    """


class BudgetExhausted(SourceBlocked):
    """Our own politeness limit, not the site's. Trying another surface cannot
    help, and reporting it as "all surfaces gated" blames the wrong thing."""


class StaleCopy(Exception):
    """The source answered with an OLDER copy than the one already stored.

    Not a `SourceBlocked`: the site answered fine, and the next listing will
    probably get a current copy. And not a `None`, which means "there is
    nothing here" -- this means "we already hold something newer", and the two
    want opposite handling. Deferring on it left a listing waiting for a cache
    that, for four Craigslist postings, served the older copy on every fetch
    for two days.
    """


class Throttled:
    """Politeness, shared by the adapters that make requests.

    Both carried their own copy of this -- the same budget check, the same
    jittered wait against a monotonic clock, the same two counters, the same
    `reset_budget`. The copies had already drifted, and in the way that
    mattered: Facebook raised `BudgetExhausted` when it ran out of its own
    budget, Craigslist raised a plain `SourceBlocked`. That distinction is
    load-bearing -- `facebook.search` tries another surface when the SITE gates
    it and gives up when OUR budget is spent -- and Craigslist could not make
    it, because its copy predated the refinement.

    Rate limiting still lives INSIDE the adapter, which is the invariant that
    matters. It is simply no longer retyped per adapter, so a third one cannot
    get a subtly different version of it.
    """

    def _init_budget(self, *, min_interval: float, jitter: float,
                     max_requests: int, timeout: float) -> None:
        self.min_interval = min_interval
        self.jitter = jitter
        self.max_requests = max_requests
        self.timeout = timeout
        self._last_request = 0.0
        self._requests_made = 0

    def reset_budget(self) -> None:
        """One budget per PASS, not per hunt. The caller resets between passes."""
        self._requests_made = 0

    def _await_slot(self) -> None:
        """Block until this adapter may make its next request.

        Silent throttling is the failure mode both sites exhibit, so the
        interval is deliberately generous and jittered.
        """
        if self._requests_made >= self.max_requests:
            raise BudgetExhausted(
                f"request budget exhausted ({self.max_requests} this run)")
        wait = self.min_interval * (1 + random.uniform(-self.jitter, self.jitter))
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < wait:
            time.sleep(wait - elapsed)

    def _spend_slot(self) -> None:
        """Count a request that has just gone out."""
        self._last_request = time.monotonic()
        self._requests_made += 1


class Source(Protocol):
    """A marketplace adapter. To add one, implement these three things.

    `name` is stored on every listing and every run, and `mark_gone` is scoped
    by it -- so two sources sharing a name would retire each other's listings.

    `search` yields whatever the cheap index gives you, and MUST raise rather
    than return an empty list when the site withholds data. Facebook answers a
    throttled request with HTTP 200 and a full-size page containing nothing;
    read as "no results" that looks exactly like a quiet day, forever.

    `parse` turns one raw payload into a Listing. Return None for anything
    unusable rather than raising -- one malformed row should not lose the batch.

    `detail(listing) -> Listing | None` is OPTIONAL. Implement it when the index
    omits things worth paying for (descriptions, coordinates), and the pipeline
    will call it only for listings that survive the gate. Return None when the
    detail is unavailable; the listing is deferred rather than judged thin.

    `liveness(listing) -> str` is OPTIONAL too, and answers `listed`,
    `removed` or `unknown` for the re-check pass. Implement it when the
    detail payload is not trustworthy evidence that a listing still exists --
    Craigslist keeps serving a cached copy of a posting its author deleted, so
    "the payload arrived" says nothing there. Asked FIRST, and a `removed`
    answer skips the detail fetch entirely. Return `unknown`, never `removed`,
    for anything that might be the site gating us: retiring is irreversible and
    it empties the list of things the user saved.

    Rate limiting belongs INSIDE the adapter, never in the caller, so it cannot
    be bypassed by accident.
    """

    name: str

    def search(self, hunt: Hunt) -> Iterator[RawListing]: ...

    def parse(self, raw: RawListing) -> Listing | None: ...


def validate(listing: Listing | None) -> Listing | None:
    """Reject anything that would poison the store. Deliberately minimal: a
    listing missing a title or url is useless, everything else is allowed to be
    absent because Marketplace routinely omits it."""
    if listing is None:
        return None
    if not listing.id or not listing.title.strip() or not listing.url:
        return None
    if listing.price_cents is not None and listing.price_cents < 0:
        return None
    return listing
