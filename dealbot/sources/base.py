"""Source protocol.

`parse` lives on the source because parsing is inherently source-specific, but it
returns the shared `Listing` type and `validate` checks the result before anything
is written. A broken adapter therefore yields garbage that gets rejected and
logged -- it cannot corrupt the store.

Rate limiting belongs *inside* an adapter, never in the caller, so it cannot be
bypassed by accident.
"""
from __future__ import annotations

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
