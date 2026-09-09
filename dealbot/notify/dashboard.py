"""Dashboard notifier: the surfacing IS the status change. The web app reads the
same SQLite file, so there is nothing to push."""
from __future__ import annotations

from typing import Sequence

from ..db import Store
from ..models import Hunt, Listing, Score


class DashboardNotifier:
    """A genuine no-op: the pipeline has already routed each listing into its
    bin, and the web app reads the same database.

    It previously wrote `surfaced` here, which silently clobbered the `wanted` /
    `free_find` status assigned moments earlier and collapsed both bins back
    into one. It exists only so that adding NtfyNotifier later is a drop-in."""
    name = "dashboard"

    def __init__(self, store: Store):
        self.store = store

    def notify(self, hunt: Hunt, surfaced: Sequence[tuple[Listing, Score]]) -> None:
        return None
