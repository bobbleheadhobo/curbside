"""Notifier protocol.

The dashboard implementation is nearly a no-op -- it just flips status to
`surfaced` and lets the web app read the database. It exists so that adding ntfy
later is a drop-in rather than a refactor.
"""
from __future__ import annotations

from typing import Protocol, Sequence

from ..models import Hunt, Listing, Score


class Notifier(Protocol):
    name: str

    def notify(self, hunt: Hunt, surfaced: Sequence[tuple[Listing, Score]]) -> None: ...
