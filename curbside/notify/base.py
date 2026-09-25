"""Notifier protocol.

The dashboard implementation is nearly a no-op -- it just flips status to
`surfaced` and lets the web app read the database. It exists so that adding ntfy
later is a drop-in rather than a refactor.
"""
from __future__ import annotations

from typing import Protocol, Sequence

from ..models import Hunt, Listing, Score


class Notifier(Protocol):
    """Somewhere to send a find.

    `surfaced` is only what THIS run produced. Do not treat it as the whole job:
    a listing that enters a bin while a notifier is failing, or past a per-run
    cap, never surfaces again -- next run it is `unchanged`. Consult
    `store.pending_notifications(hunt.id)` as well, and record delivery with
    `store.mark_notified`, or finds are silently lost. DiscordNotifier is the
    worked example.

    A notifier must never raise: the pipeline catches it, but a failure here
    should cost you an alert, not a run.
    """

    name: str

    def notify(self, hunt: Hunt, surfaced: Sequence[tuple[Listing, Score]]) -> None: ...
