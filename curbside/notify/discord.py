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

log = logging.getLogger("curbside.notify.discord")

# Colour tracks the score, so the left edge of the card reads at a glance in a
# scrolling channel.
HOT, GOOD, FAIR, DIM = 0x22C55E, 0x16A34A, 0xD97706, 0x6B7280
MAX_TITLE, MAX_DESC, MAX_FIELD = 250, 3900, 1000

MET_MARK = {"yes": "\u2705", "no": "\u274c", "unknown": "\u2753"}


def _money(cents: int | None) -> str:
    if cents is None:
        return "no price"
    return "**FREE**" if cents == 0 else f"${cents / 100:,.0f}"


def _age_days(posted_at: datetime | None) -> int | None:
    if posted_at is None:
        return None
    return max(0, (datetime.now(timezone.utc) - posted_at).days)


def _colour(score: float) -> int:
    if score >= 9:
        return HOT
    if score >= 7:
        return GOOD
    return FAIR if score >= 5 else DIM


def _badge(score: float) -> str:
    if score >= 9:
        return "\U0001f525 "          # fire
    if score >= 8:
        return "\u2b50 "              # star
    return ""


def _requirement_lines(score: Score) -> str:
    """The per-requirement verdicts with their evidence.

    This is the most useful thing we know and it was never shown: "at least 70
    inches wide -- 'six feet long' = 72 inches" is what makes an alert
    actionable without opening anything."""
    out = []
    for req in score.requirements[:5]:
        if not isinstance(req, dict):
            continue
        mark = MET_MARK.get(str(req.get("met")), "\u2753")
        line = f"{mark} {req.get('req', '')}"
        if (ev := req.get("evidence")):
            line += f"\n\u2003*{str(ev)[:110]}*"
        out.append(line)
    return "\n".join(out)[:MAX_FIELD]


def build_embed(listing: Listing, score: Score, dashboard_url: str) -> dict:
    """One listing as a Discord embed."""
    # A $0 the seller contradicted in the description is not a price. `_money`
    # itself is left alone: it also renders the OLD price in a drop alert,
    # which is a real figure whatever the new one turns out to be.
    price = "price unclear" if score.price_unclear else _money(listing.price_cents)
    if (listing.previous_price_cents is not None
            and listing.price_cents is not None
            and listing.previous_price_cents > listing.price_cents):
        drop = 100 - (listing.price_cents / listing.previous_price_cents * 100)
        price = (f"~~${listing.previous_price_cents / 100:,.0f}~~ \u2192 {price}"
                 f"\n`-{drop:.0f}%`")

    verdict = f"**{score.deal_score:.0f}**/10"
    if score.est_value_cents:
        verdict += f"\nworth ~${score.est_value_cents / 100:,.0f}"
    if score.match == "unknown":
        verdict += "\n*unverified*"

    fields = [
        {"name": "Price", "value": price, "inline": True},
        {"name": "Distance", "value":
            (f"{listing.distance_mi:.0f} mi" if listing.distance_mi is not None
             else "unknown"), "inline": True},
        {"name": "Verdict", "value": verdict, "inline": True},
    ]

    if (reqs := _requirement_lines(score)):
        fields.append({"name": "Requirements", "value": reqs, "inline": False})
    if score.match == "unknown" and score.unknowns:
        fields.append({"name": "Worth checking",
                       "value": "\n".join(f"\u2022 {u}" for u in score.unknowns[:4])[:MAX_FIELD],
                       "inline": False})
    if score.red_flags:
        fields.append({"name": "\u26a0\ufe0f Red flags",
                       "value": "\n".join(f"\u2022 {f}" for f in score.red_flags[:4])[:MAX_FIELD],
                       "inline": False})

    description = (score.reasoning or "").strip()
    if dashboard_url:
        description += f"\n\n[\u2192 open in Curbside]({dashboard_url}/listing/{listing.id})"

    age = _age_days(listing.posted_at)
    footer = [listing.source]
    if age is not None:
        footer.append("listed today" if age == 0 else f"listed {age}d ago")
    if age is not None and age >= 14 and listing.previous_price_cents is not None:
        footer.append("\U0001f4c9 motivated seller")
    if score.images_checked:
        footer.append("\U0001f4f7 photos checked")

    embed = {
        "author": {"name": (listing.city or listing.source).strip()},
        "title": (_badge(score.deal_score) + listing.title)[:MAX_TITLE],
        "url": listing.url,
        "description": description[:MAX_DESC],
        "color": _colour(score.deal_score),
        "fields": fields,
        "footer": {"text": " \u00b7 ".join(footer)},
    }
    if listing.posted_at:
        embed["timestamp"] = listing.posted_at.isoformat()
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

    def notify_price_drop(self, hunt: Hunt, listing: Listing, score: Score,
                          was_cents: int) -> bool:
        """A thing you already care about just got cheaper.

        This is what the append-only price history was FOR. Every observation
        needed to notice it was already on disk, and nothing read them: alerts
        only ever fired when a listing first reached a bin, so a saved $200
        credenza falling to $120 said nothing at all.

        Always the wants channel, muted or not: you decided this one mattered
        before the price moved, which is exactly what makes the drop worth an
        interruption.
        """
        if not self.wants_webhook:
            return False
        embed = build_embed(listing, score, self.dashboard_url)
        drop = 100 - (listing.price_cents / was_cents * 100) if was_cents else 0
        embed["title"] = f"\u2193 {int(round(drop))}% \u00b7 {embed.get('title', listing.title)}"
        embed["color"] = 0x0F7040                       # the good-news green
        # Spelled out rather than `_money`, which bolds: the whole line is
        # already inside asterisks.
        now = ("price unclear" if score.price_unclear
               else "FREE" if not listing.price_cents
               else _money(listing.price_cents))
        payload = {
            "content": f"**{_money(was_cents)} \u2192 {now}** on something you saved",
            "embeds": [embed],
        }
        if self.mention_user_id:
            payload["content"] = f"<@{self.mention_user_id}> " + payload["content"]
        return self._post(self.wants_webhook, payload)

    def notify(self, hunt: Hunt, surfaced: Sequence[tuple[Listing, Score]]) -> None:
        # Work from everything in a bin that has never been announced, not just
        # what this run produced. Otherwise the per-run cap DROPS rather than
        # defers, and anything that landed in a bin while notifications were off
        # is never revisited -- next run it is `unchanged` and never surfaces.
        # Per RUN. Set only in __init__, this was really per process: one
        # `curbside run` daemon builds a single notifier and loops forever, so
        # after ten messages it went permanently silent with nothing but an INFO
        # line to show for it. In `once` the budget was shared across every
        # hunt, so a busy free sweep left the want hunts nothing.
        self._sent_this_run = 0

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
