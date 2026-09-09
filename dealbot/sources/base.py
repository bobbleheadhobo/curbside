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


class Source(Protocol):
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
