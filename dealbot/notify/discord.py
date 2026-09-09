"""Discord notifications, two channels.

Wants go to a channel you keep notifications ON for -- a 70" console at $20 is
worth interrupting you. Free finds go to a channel you keep muted and browse,
with an @mention reserved for the exceptional ones, so the ping still means
something.

Three things stop it becoming noise:

  * **Notified once.** `hunt_matches.notified_at` is the record. Re-running a
    hunt, or a listing re-entering a bin, never re-pings.
  * **Cold-start suppression.** There are hundreds of backlogged listings; the
    first run after enabling must not dump them all. Anything older than
    `max_age_days` is marked as notified WITHOUT sending, so it is caught up
    silently rather than announced.
  * **Caps and pacing.** A per-run ceiling, and a pause between messages so
    Discord's rate limiter is never the thing that discovers the problem.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Sequence

import requests

from ..db import Store
from ..models import Hunt, Listing, Score

log = logging.getLogger("dealbot.notify.discord")

GREEN, AMBER, GREY = 0x2E9E5B, 0xC08A2E, 0x6B6B6B
MAX_TITLE, MAX_DESC = 250, 3900


def _money(cents: int | None) -> str:
    if cents is None:
        return "no price"
    return "**FREE**" if cents == 0 else f"${cents / 100:,.0f}"


def _age_days(posted_at: datetime | None) -> int | None:
    if posted_at is None:
        return None
    return max(0, (datetime.now(timezone.utc) - posted_at).days)


def build_embed(listing: Listing, score: Score, dashboard_url: str) -> dict:
    """One listing as a Discord embed: photo, price, distance, verdict."""
    price = _money(listing.price_cents)
    if (listing.previous_price_cents is not None
            and listing.price_cents is not None
            and listing.previous_price_cents > listing.price_cents):
        price = f"~~${listing.previous_price_cents / 100:,.0f}~~ → {price}"

    verdict = f"{score.deal_score:.0f}/10"
    if score.est_value_cents:
        verdict += f" · worth ~${score.est_value_cents / 100:,.0f}"

    fields = [
        {"name": "Price", "value": price, "inline": True},
        {"name": "Distance", "value":
            f"{listing.distance_mi:.0f} mi" if listing.distance_mi is not None else "?",
         "inline": True},
        {"name": "Verdict", "value": verdict, "inline": True},
    ]

    notes = [score.reasoning or ""]
    if score.match == "unknown" and score.unknowns:
        notes.append("**Needs checking:** " + "; ".join(score.unknowns[:3]))
    if score.red_flags:
        notes.append("⚠ " + "; ".join(score.red_flags[:3]))
    notes.append(f"[open on the dashboard]({dashboard_url}/listing/{listing.id})")

    age = _age_days(listing.posted_at)
    footer = [listing.source]
    if age is not None:
        footer.append(f"listed {age}d ago")
    if age is not None and age >= 14 and listing.previous_price_cents is not None:
        footer.append("motivated seller")
    if score.images_checked:
        footer.append("photos checked")

    embed = {
        "title": listing.title[:MAX_TITLE],
        "url": listing.url,
        "description": "\n\n".join(n for n in notes if n)[:MAX_DESC],
        "color": GREEN if score.match == "yes" else (
            AMBER if score.match == "unknown" else GREY),
        "fields": fields,
        "footer": {"text": " · ".join(footer)},
    }
    if listing.images:
        embed["image"] = {"url": listing.images[0]}
    return embed


class DiscordNotifier:
    name = "discord"

    def __init__(self, store: Store, *, wants_webhook: str | None = None,
                 free_webhook: str | None = None, dashboard_url: str = "",
                 mention_user_id: str = "", mention_score: float = 8.0,
                 max_per_run: int = 10, max_age_days: int = 7,
                 pause_seconds: float = 1.2, timeout: float = 20.0):
        self.store = store
        self.wants_webhook = wants_webhook or os.environ.get("DISCORD_WANTS_WEBHOOK")
        self.free_webhook = free_webhook or os.environ.get("DISCORD_FREE_WEBHOOK")
        self.dashboard_url = dashboard_url.rstrip("/")
        self.mention_user_id = mention_user_id or os.environ.get(
            "DISCORD_MENTION_USER_ID", "")
        self.mention_score = mention_score
        self.max_per_run = max_per_run
        self.max_age_days = max_age_days
        self.pause = pause_seconds
        self.timeout = timeout
        self._sent_this_run = 0

    # --- sending ------------------------------------------------------------

    def _post(self, webhook: str, payload: dict) -> bool:
        for attempt in range(3):
            try:
                r = requests.post(webhook, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("discord post failed: %s", exc)
                return False
            if r.status_code == 429:
                # Discord tells us exactly how long to wait; obey it rather
                # than guessing, and never hammer.
                wait = float(r.json().get("retry_after", 2)) if r.content else 2.0
                log.info("discord rate limited, waiting %.1fs", wait)
                time.sleep(min(wait + 0.2, 30))
                continue
            if r.status_code >= 400:
                log.warning("discord %s: %s", r.status_code, r.text[:200])
                return False
            return True
        return False

    # --- the Notifier interface --------------------------------------------

    def notify(self, hunt: Hunt, surfaced: Sequence[tuple[Listing, Score]]) -> None:
        # Work from everything in a bin that has never been announced, not just
        # what this run produced. Otherwise the per-run cap DROPS rather than
        # defers, and anything that landed in a bin while notifications were off
        # is never revisited -- next run it is `unchanged` and never surfaces.
        pending = self.store.pending_notifications(hunt.id)
        seen = {l.id for l, _ in pending}
        queue = list(pending) + [(l, s) for l, s in surfaced if l.id not in seen]

        for listing, score in queue:
            if self.store.was_notified(hunt.id, listing.id):
                continue

            wanted = score.match in ("yes", "unknown")
            webhook = self.wants_webhook if wanted else self.free_webhook
            if not webhook:
                continue

            # Cold start: catch the backlog up silently instead of announcing
            # hundreds of listings, most of which are long gone.
            age = _age_days(listing.posted_at)
            if age is not None and age > self.max_age_days:
                self.store.mark_notified(hunt.id, listing.id)
                continue

            if self._sent_this_run >= self.max_per_run:
                log.info("per-run notification cap reached (%d)", self.max_per_run)
                return

            payload = {"embeds": [build_embed(listing, score, self.dashboard_url)]}
            # The free channel is muted, so a mention is the only thing that
            # reaches you there -- reserve it for the genuinely exceptional.
            if (not wanted and self.mention_user_id
                    and score.deal_score >= self.mention_score):
                payload["content"] = f"<@{self.mention_user_id}> worth a look"

            if self._post(webhook, payload):
                self.store.mark_notified(hunt.id, listing.id)
                self._sent_this_run += 1
                time.sleep(self.pause)
