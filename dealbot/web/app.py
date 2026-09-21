"""Dashboard. Read-mostly over the same SQLite file the pipeline writes; the only
writes are triage actions.

The Runs view is not decoration. It is what distinguishes "nothing good posted
today" from "the scraper broke last Tuesday", and an empty result you cannot
explain is a tool you stop opening.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path

import logging

from fastapi import FastAPI, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import config as config_mod
from .. import schedule as schedule_mod
from ..scoring.claude_code import (OVERRIDE_UNTIL, PAUSE_REASON, PAUSE_UNTIL,
                                   PlanUsage, read_plan_usage)
from ..config import Config
from ..db import Store
from ..filters import matches_any
from ..models import WANT_NAME_RE, Listing, Want, slugify_want
from ..pipeline import route
from ..thumbs import ThumbnailStore

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STATIC = Path(__file__).parent / "static"


def asset_version() -> int:
    """Newest mtime in /static, used to bust three caches at once: the browser's
    (a ?v= on the stylesheet and script), the service worker's own name, and
    therefore everything the worker holds cache-first. A hardcoded version meant
    a regenerated icon never reached a phone that had already installed this."""
    return max((int(f.stat().st_mtime) for f in STATIC.iterdir() if f.is_file()),
               default=0)
log = logging.getLogger("dealbot.web")

# The bin views are one query with the WHERE clause swapped. They used to be
# built by `str.replace` on each other -- QUEUE_SQL rebound to a longer string,
# then SAVED_SQL and NEAR_MISS_SQL doing textual surgery on the rebound value.
# A non-matching needle in str.replace is a SILENT no-op, so editing the text
# "WHERE m.status = ?" produced a perfectly valid query against the wrong rows,
# with nothing raised anywhere. The clause is a parameter now.
def _queue_sql(where: str) -> str:
    return f"""
SELECT m.hunt_id, m.status, l.*,
       l.previous_price_cents,
       CAST(julianday('now') - julianday(l.posted_at) AS INTEGER) AS age_days,
       (SELECT COUNT(DISTINCT price_cents) - 1 FROM price_observations
         WHERE listing_id = l.id) AS price_moves,
       s.deal_score, s.reasoning, s.red_flags,
       s.matched_want, s.est_value_cents, s.priced_at_cents, s.match,
       s.unknowns, s.requirements, s.worth_grabbing, s.images_checked,
       s.price_unclear
FROM hunt_matches m
JOIN listings l ON l.id = m.listing_id
JOIN scores  s ON s.id = (SELECT MAX(id) FROM scores
                          WHERE hunt_id = m.hunt_id AND listing_id = m.listing_id)
{where}
ORDER BY s.deal_score DESC, l.last_seen DESC
LIMIT ?
"""

HUNT_SQL = """
SELECT m.hunt_id, m.status, m.filter_reason, l.*,
       l.previous_price_cents,
       CAST(julianday('now') - julianday(l.posted_at) AS INTEGER) AS age_days,
       (SELECT COUNT(DISTINCT price_cents) - 1 FROM price_observations
         WHERE listing_id = l.id) AS price_moves,
       s.deal_score, s.reasoning, s.red_flags, s.matched_want, s.priced_at_cents,
       s.match, s.unknowns, s.requirements, s.worth_grabbing, s.images_checked,
       s.price_unclear
FROM hunt_matches m
JOIN listings l ON l.id = m.listing_id
LEFT JOIN scores s ON s.id = (SELECT MAX(id) FROM scores
                              WHERE hunt_id = m.hunt_id AND listing_id = m.listing_id)
WHERE m.hunt_id = ? AND (? = '' OR m.status = ?)
ORDER BY COALESCE(s.deal_score, -1) DESC, l.last_seen DESC
LIMIT ?
"""

# Counting is a separate GROUP BY rather than a second pass over the full
# result set: the hunt view used to run the whole query TWICE per page load and
# then filter status in Python, which is ~25x the cost of asking SQLite.
HUNT_COUNTS_SQL = """
SELECT status, COUNT(*) AS n FROM hunt_matches WHERE hunt_id = ? GROUP BY status
"""

# Every rejection is recorded with its reason and none of it was visible. This
# is the tuning view: "118 rejected on over_price" says your cap is too low far
# faster than reading listings one at a time.
# One row per want, not one query per want. This list moved onto `/` in the
# same change, which turned its COUNT(*) into N queries on the busiest page in
# the site -- paid on every load, panel unfolded or not.
WANT_COUNTS_SQL = """
SELECT hunt_id, COUNT(*) AS n FROM hunt_matches
WHERE hunt_id IN (%s) GROUP BY hunt_id
"""

REJECT_REASONS_SQL = """
SELECT filter_reason, COUNT(*) AS n FROM hunt_matches
WHERE hunt_id = ? AND filter_reason IS NOT NULL
GROUP BY filter_reason ORDER BY n DESC
"""

PRICE_HISTORY_SQL = """
SELECT observed_at, price_cents FROM price_observations
WHERE listing_id = ? ORDER BY id
"""

PAGE_LIMIT = 200

# Where "manage my wants" lives, in ONE place. It moved off /settings: the list
# you edit is the list the front page is the result of, and reaching it through
# a gear read as configuration. `manage=1` renders the panel open; the hash
# scrolls to it, so a save lands on the list rather than at the top of a page.
MANAGE_URL = "/?manage=1#manage"

# One listing appears in exactly ONE bin.
#
# `hunt_matches` is per (hunt, listing) on purpose -- a listing matched by two
# hunts keeps independent triage state, and that is worth keeping. But it meant
# the same physical thing could be a card in Wants and a card in Free finds at
# the same time, which reads as a bug however well justified it is.
#
# So the bin views pick one row per listing, by what you have already decided:
# something you saved outranks something merely wanted, which outranks a free
# find. Ties inside a bin go to the first hunt by name, so the choice is stable
# between page loads rather than moving around.
ONE_BIN = """
  AND m.rowid = (SELECT x.rowid FROM hunt_matches x
                 WHERE x.listing_id = m.listing_id
                   AND x.status IN ('saved','grabbed','wanted','free_find')
                 ORDER BY CASE x.status WHEN 'grabbed' THEN 0 WHEN 'saved' THEN 1
                                        WHEN 'wanted' THEN 2 ELSE 3 END,
                          x.hunt_id
                 LIMIT 1)
"""

QUEUE_SQL = _queue_sql("WHERE m.status = ?" + ONE_BIN)

# Things you decided to act on. Clicking "saved" used to make a listing vanish:
# it left the bin and was only findable by digging through a hunt view.
# `grabbed` sits here rather than on a page of its own: it is what a saved
# thing BECOMES, this page already keeps a sold listing rather than dropping it,
# and a fourth bin in the nav for something that happens a few times a month
# would be a page you visit to confirm it is still empty.
SAVED_SQL = _queue_sql("WHERE m.status IN ('saved', 'grabbed')" + ONE_BIN)

# The band just under the bar. `deal_score` is judged as if unknowns resolve
# favourably, so a 6 means "even if it is what it looks like, it is mediocre" --
# but you cannot calibrate a threshold you can never see over.
# /skipped is not a bin -- it is the band under the bar -- so it keeps its own
# rows and takes no ONE_BIN clause.
#
# It is also where a contradicted $0 lands. `pipeline.route` keeps a
# `price_unclear` listing out of the free bin -- a price nobody knows is not a
# price, so there is nothing to weigh the trip against -- and it stays `scored`,
# which brings it here with the card saying "price unclear" over it. A folded
# group on /free used to carry them instead: it rendered a second copy of any
# that reached a bin anyway, and scanned `scores` unindexed on every load.
NEAR_MISS_SQL = _queue_sql("WHERE m.status = 'scored' AND s.deal_score >= ?")


def _rows(store: Store, sql: str, args=()) -> list[dict]:
    out = []
    for r in store.conn.execute(sql, args):
        d = dict(r)
        d["images"] = json.loads(d.get("images") or "[]")
        d["red_flags"] = json.loads(d.get("red_flags") or "[]")
        d["unknowns"] = json.loads(d.get("unknowns") or "[]")
        d["requirements"] = json.loads(d.get("requirements") or "[]")
        out.append(d)
    return out


def _sparkline(history: list[tuple[str, int | None]], w: int = 160,
               h: int = 28) -> str | None:
    """Inline SVG of the price over time. We have kept every observation since
    day one and never drawn it; a flat line versus a staircase down is the
    difference between a firm seller and a motivated one."""
    pts = [(i, p) for i, (_, p) in enumerate(history) if p is not None]
    if len(pts) < 2:
        return None
    lo = min(p for _, p in pts)
    hi = max(p for _, p in pts)
    span = (hi - lo) or 1
    last_x = pts[-1][0] or 1
    coords = " ".join(
        f"{x / last_x * (w - 2) + 1:.1f},{h - 1 - (p - lo) / span * (h - 2):.1f}"
        for x, p in pts)
    colour = "var(--good)" if pts[-1][1] < pts[0][1] else "var(--dim)"
    # Spans whatever it is given rather than a fixed 160px, which left the plot
    # stopping mid-air in a full-width panel. `preserveAspectRatio="none"` is
    # what stretches it; `vector-effect` keeps the stroke an even weight while
    # it does.
    return (f'<svg width="100%" height="{h}" viewBox="0 0 {w} {h}" '
            f'preserveAspectRatio="none" role="img" aria-label="price history">'
            f'<polyline fill="none" stroke="{colour}" stroke-width="1.5" '
            f'vector-effect="non-scaling-stroke" points="{coords}"/></svg>')


def _daybars(days: list[dict], ceiling: float, w: int = 300, h: int = 44) -> str:
    """Inline SVG of daily spend, drawn the way `_sparkline` is: by hand, in
    house colours, with no library to load on a phone.

    A day the bot did not run is a GAP, not a zero. The caller fills the
    calendar so the bars are evenly spaced in time -- a run of five bars means
    five days whether or not one of them was silent -- but a silent day gets
    no bar at all, because a day off and a day that found nothing are
    different facts.
    """
    if not days:
        return ""
    top = max([d["usd"] or 0 for d in days] + [ceiling if ceiling > 0 else 0]) or 1
    slot = w / len(days)
    bar = max(2.0, slot - 2)
    out = []
    if ceiling > 0:
        y = h - (ceiling / top) * (h - 2)
        out.append(f'<line x1="0" y1="{y:.1f}" x2="{w}" y2="{y:.1f}" '
                   f'stroke="var(--line)" stroke-width="1" stroke-dasharray="3 3"/>')
    for i, d in enumerate(days):
        usd = d["usd"]
        if usd is None:
            continue
        bh = max(1.0, (usd / top) * (h - 2))
        # Amber where MOST of that day's runs stood aside, not where any did.
        # Flagging any standdown painted all five days amber on the live box,
        # which is a legend that describes every bar and therefore tells you
        # nothing. Half is the point where the day's bill stops meaning what
        # the bot would normally spend.
        runs = d.get("runs") or 0
        fill = ("var(--warn)" if runs and (d.get("degraded") or 0) / runs >= 0.5
                else "var(--accent)")
        out.append(f'<rect x="{i * slot + 1:.1f}" y="{h - bh:.1f}" '
                   f'width="{bar:.1f}" height="{bh:.1f}" rx="1.5" fill="{fill}"/>')
    return (f'<svg width="100%" height="{h}" viewBox="0 0 {w} {h}" '
            f'preserveAspectRatio="none" role="img" '
            f'aria-label="daily spend">{"".join(out)}</svg>')


def _fill_days(rows: list[dict], days: int, offset_minutes: int = 0) -> list[dict]:
    """Every date in the window, in order, whether or not it has a run.

    `offset_minutes` has to match the one the rows were bucketed with, or the
    newest bar is labelled with a date no row carries and renders as a gap.
    """
    have = {r["day"]: r for r in rows}
    today = (datetime.now(timezone.utc)
             + timedelta(minutes=offset_minutes)).date()
    return [have.get((today - timedelta(days=n)).isoformat(),
                     {"day": (today - timedelta(days=n)).isoformat(),
                      "usd": None, "runs": 0, "scored": 0, "degraded": 0})
            for n in range(days - 1, -1, -1)]


# `pipeline` writes a standdown as "<what happened>: <why>", and joins it to
# any other warning from the same run with "; " -- so this is a clause inside
# the column, not necessarily the whole of it.
STANDDOWNS = ("scoring skipped", "scoring interrupted")


def standdown_reason(warning: str | None) -> str | None:
    """The why out of a run's standdown warning, or None if there is not one.

    A warning may be two things at once ("2 detail fetches failed; scoring
    skipped: ..."), which is why this looks at every clause rather than the
    start of the string. Matching only the start meant a run that lost a
    detail fetch AND stood aside reported neither pause.
    """
    for clause in str(warning or "").split("; "):
        clause = clause.strip()
        for prefix in STANDDOWNS:
            if not clause.startswith(prefix):
                continue
            # Cut the PREFIX off, not "everything before the first colon".
            # Splitting on ": " returns the whole clause when there is no
            # separator, so a bare "scoring skipped" came back as its own
            # reason and the pill read "Judging paused: scoring skipped".
            reason = clause[len(prefix):].lstrip(": ").split(" -- ")[0].strip()
            # The pause is real even when nothing explains it, and saying so
            # beats returning None and letting the pill fall through to some
            # other state -- which would hide a stopped scorer completely.
            return reason or "reason not recorded"
    return None


def hunts_including_archived(cfg: Config, store: Store) -> dict:
    """Every hunt, plus one rebuilt for each want that has been deleted.

    A deleted want is archived rather than dropped and everything it matched
    stays readable, but its hunt is gone from `cfg.hunts` -- so anything that
    re-decides `route` for an old score had nothing to judge against. That is
    not a rare corner: 254 of 1,378 listings here carry a newest score from
    `want:tv-stand`, deleted weeks ago, and 66 of those could only be told the
    photographs "did not get" looked at, which is the least useful of the three
    answers the page can give.

    Rebuilt by handing the archived wants back to `Config.hunts` rather than
    constructing a `Hunt` here, so every threshold and per-want override
    resolves exactly the way a live one's does. A want with no queries never
    had its own hunt and still does not get one.
    """
    hunts = {h.id: h for h in cfg.hunts}
    gone = [w.want for w in store.wants(include_archived=True)
            if f"want:{w.want.name}" not in hunts]
    if gone:
        for h in replace(cfg, wants=tuple(gone)).hunts:
            hunts.setdefault(h.id, h)
    return hunts


def headed_for_a_bin(store: Store, listing: dict, scores: list[dict],
                     hunts: dict) -> bool | None:
    """Whether the newest score would have put this listing in a bin.

    TWO different things stop a requested image pass and the page used to blame
    the budget for both. Stage 5b buys photographs only for a listing that
    `route` would ALREADY bin on its text score, so one that scored poorly and
    asked to be seen was declined by that test and never reached the budget at
    all. In the live data that is 162 of the 169 unmet requests: the sentence
    "the image budget ran out" was wrong on 96% of the listings it appeared on,
    and it named the one number a reader might go and raise in response.

    `route` is CALLED, not re-derived. Re-deriving it here is precisely the
    mistake its own docstring records -- the rule was written twice once
    before, and the copies agreed only as long as someone remembered.

    `None` means cannot say, which after `hunts_including_archived` is rare:
    a want that never had its own hunt, or rows that have since gone. The
    template then claims nothing about why.
    """
    if not scores:
        return None
    hunt = hunts.get(scores[0]["hunt_id"])
    if hunt is None:
        return None
    # Raw rows: `_rows` has already decoded the JSON columns that the Store
    # converters decode, and handing them back a list would raise. Two indexed
    # single-row reads on a page that renders one listing.
    srow = store.conn.execute("SELECT * FROM scores WHERE id=?",
                              (scores[0]["id"],)).fetchone()
    lrow = store.conn.execute("SELECT * FROM listings WHERE id=?",
                              (listing["id"],)).fetchone()
    if srow is None or lrow is None:
        return None
    return route(store.row_to_score(srow), hunt,
                 store.row_to_listing(lrow)) is not None


def photo_verdict(scores: list[dict], binned: bool | None = None) -> dict | None:
    """Whether the model looked at the photographs, and what it got for it.

    FOUR states, and the interface once admitted to one. A chip appeared when
    the photos had been checked and nothing appeared otherwise, so "judged on
    the text alone" and "asked for a look and never got one" were the same
    blank space. Then that second state was given a sentence which blamed the
    image budget -- true seven times in the life of the bot, against 162
    listings for which the real answer is that they were not going to be shown
    to anyone, so no photograph was worth buying. `binned` separates them; see
    `headed_for_a_bin`, and `None` there means neither claim is made.

    Where it did look, the score BEFORE is worth as much as the verdict. Both
    rows are kept for exactly this reason -- the text judgement stays next to
    the one that looked -- and every sampled pair moved: 5.0 to 6.0, 1.0 to
    3.0, 3.0 to 5.0. That is the image pass earning its ~15x cost, stated
    rather than assumed.
    """
    if not scores:
        return None
    latest = scores[0]
    checked = bool(latest["images_checked"])
    # The text pass on the same hunt, which is the "before". Matched on the
    # hunt because one listing can be judged by several, and their scores are
    # answers to different questions.
    prior = next((s for s in scores[1:]
                  if s["hunt_id"] == latest["hunt_id"] and not s["images_checked"]),
                 None)
    return {
        "checked": checked,
        "asked": bool(latest["needs_images"]) and not checked,
        "binned": binned,
        "question": (latest["image_question"] or "").strip() or None,
        "before": prior["deal_score"] if checked and prior else None,
        "after": latest["deal_score"],
    }


def local_stamp(raw, tz=None) -> str:
    """A stored timestamp, in the reader's clock and the reader's hours.

    Two things were wrong with `raw[:16].replace("T", " ")`, and the second is
    the one that matters. It rendered 24-hour time, which reads like a log
    line rather than like a person telling you when something happened. And it
    rendered it in UTC with nothing saying so: a run at 19:50 in the table
    happened at 1:50pm where the user is, so every timestamp on the site was
    six hours wrong.

    Hand-rolled rather than `strftime("%-I:%M%p")`, matching
    `schedule.fmt_clock`: the `%-` flag is a glibc extension, and the lowercase
    "pm" with no space is the convention the rest of the interface uses
    ("Asleep till 12pm", "11am to 8pm").
    """
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:                      # not a timestamp: show it as-is
        return str(raw)
    if dt.tzinfo is None:                   # stored naive means stored UTC
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(tz) if tz else dt.astimezone()
    h12 = local.hour % 12 or 12
    suffix = "am" if local.hour < 12 else "pm"
    return f"{local.day} {local:%b}, {h12}:{local.minute:02d}{suffix}"


def took(started_at: str | None, finished_at: str | None) -> str | None:
    """How long one run took, from the two stamps it already carries.

    DERIVED, not stored. A `duration_secs` column would be a third copy of a
    fact `started_at` and `finished_at` state between them, and the copies
    would be free to disagree -- the same trap as the `runs` column whitelist
    that went stale while looking correct.

    `None` where there is nothing honest to say: a run still in flight and a
    run whose process was killed mid-pass are the same row, both with no
    finish stamp, and neither has a duration yet.
    """
    if not started_at or not finished_at:
        return None
    try:
        secs = (datetime.fromisoformat(finished_at)
                - datetime.fromisoformat(started_at)).total_seconds()
    except (TypeError, ValueError):
        return None
    if secs < 0:
        return None
    if secs < 60:
        return f"{secs:.0f}s"
    mins, secs = divmod(int(secs), 60)
    if mins < 60:
        return f"{mins}m {secs:02d}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins:02d}m"


def until_words(seconds: float | None) -> str:
    """A countdown to a reset, in the units a person would say it in.

    Days matter: the seven-day window is routinely more than a day out, and
    "in 114h 9m" is correct and unreadable. Empty once it has passed, because
    a negative countdown reads as a broken page rather than a rolled window.
    """
    if not seconds or seconds <= 0:
        return ""
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {mins}m"
    return f"in {mins}m" if mins else "in under a minute"


def ago_words(minutes: float | None) -> str:
    """How old a reading is, phrased the way the health pill phrases it."""
    if minutes is None:
        return ""
    mins = int(minutes)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    if mins < 2880:
        return f"{mins // 60}h ago"
    return f"{mins // 1440}d ago"


def plan_usage_view(usage: PlanUsage, now: float | None = None) -> dict:
    """The plan windows, shaped for the meters on /runs.

    Pure, and it decides nothing: `read_plan_usage` already settled which
    windows count and which have expired, so this only turns fractions into
    percents and instants into words. The pill sends you here to find out why
    judging stopped, so what the page draws has to be what the gate enforced.

    A percent is clamped to 0-100 because it is written straight into a CSS
    width, and a reading of 1.01 -- which the wire really does carry once a
    window is spent -- would push the fill out of its own track.
    """
    now = time.time() if now is None else now
    windows = []
    for w in usage.windows:
        windows.append({
            "label": w.label,
            # An expired reading has no number to show: the window it measured
            # has rolled, so drawing its bar would assert a fact about a window
            # that no longer exists.
            "pct": None if w.expired else max(0, min(100, int(round(w.used * 100)))),
            "ceiling_pct": int(round(w.ceiling * 100)) if w.ceiling > 0 else None,
            "over": w.over,
            "expired": w.expired,
            "resets_in": "" if w.expired else until_words((w.resets_at or 0) - now),
        })
    age = (now - usage.recorded_at.timestamp()) / 60 if usage.recorded_at else None
    return {
        "known": bool(windows), "windows": windows, "as_of": ago_words(age),
        # Judging is standing aside on these numbers right now.
        "holding": any(w["over"] for w in windows),
        # The reading gates nothing: too old to trust, and it named no reset to
        # date it by. Said out loud because a page showing 82% over a bot that
        # is judging away reads as broken. An old reading that DID name a reset
        # is not this: it is enforcing, and saying otherwise would be the lie
        # in the other direction.
        "unenforced": usage.stale and all(w.resets_at is None
                                          for w in usage.windows),
    }


def _safe_back(back: str) -> str:
    """Where a triage button returns to, once we have checked it is here.

    `back` is a form field and now also a query parameter on the detail page,
    so it is reader-supplied and ends up in a Location header. Anything that is
    not a path on this dashboard becomes "/".

    Checking `startswith("//")` is not enough, and both holes are the same
    mistake -- judging the string we were handed rather than the URL a browser
    will resolve:

    * **Tab, newline and carriage return are STRIPPED before parsing.** So
      "/\t/evil.test" is not a path beginning "/t", it is "//evil.test", a
      protocol-relative URL to somebody else's host.
    * **A backslash is normalised to a forward slash.** So "/\\evil.test" is
      "//evil.test" by the time it is resolved.

    Strip the first, then treat both characters as off-site in second place.
    """
    cleaned = back.translate({9: None, 10: None, 13: None})
    if not cleaned.startswith("/") or cleaned[1:2] in ("/", "\\"):
        return "/"
    return cleaned


def health(row, paused, hunts, sched, activity) -> dict:
    """The state of the bot itself, on every page, in one pill.

    Pure: `row` is the latest `runs` row (or None) and `activity` is
    `Store.run_activity()`. It lives out here
    because the ORDER below has been wrong twice, and a precedence
    ladder that can only be exercised through HTTP is one nobody tests
    every branch of.

    This is the ONLY place a pause is announced. The banner that used to
    sit on every page said the same thing, took a block of every screen to
    say it, and pushed the first listing below the fold.

    So the order below is the whole design: the most actionable true fact
    wins, and the pill links to /runs where the switch to undo it lives.
    Several of these are true at once most of the time -- asleep AND
    paused AND quiet -- and picking the wrong one is how the pill starts
    lying. It reported "Asleep till 12pm" over a bot with every hunt
    switched off, which is the exact failure it exists to prevent.
    """
    window = "" if sched.always_on else f"Awake {sched.window_label}. "
    detail = f"Last run {row['started_at']}" if row else "Nothing has fetched yet."

    # 1. Everything is off. Nothing is running, so nothing else here is the
    #    reason nothing is happening -- not the hours, not a stale error.
    if hunts and len(paused) == len(hunts):
        return {"state": "warn", "label": "Paused",
                "detail": f"Every hunt is off. Nothing is being collected. "
                          f"{window}{detail}"}

    # 2. A fetch that died. Louder than anything below it -- but a run
    #    that fetched fine and then stood aside from the plan quota writes
    #    that to the SAME column, and 108 of the 112 errors on the live box
    #    were exactly that. Reporting a routine quota pause as a red
    #    "Fetch failing" is the same lie as the last one.
    if row is not None and row["error"]:
        return {"state": "bad", "label": "Fetch failing",
                "detail": f"{detail} — {row['error']}"}

    # 2b. Fetched fine, stood aside from the plan quota. This used to be
    #     written to `error` and special-cased back out again here -- which
    #     fixed the pill while leaving `last_success_at` reading it as a
    #     failure, so the cadence collapsed to the timer period. It is a
    #     warning now, at the source.
    reason = standdown_reason(row["warning"]) if row is not None else None
    if reason:
        # The label says the STATE and nothing else. "Judging paused: plan
        # 72%" is 24 characters in a pill that has to share a phone's top bar
        # with the brand and the settings gear, and it was being cut off
        # mid-word -- a truncated reason is worse than no reason, because it
        # looks like the interface is broken rather than terse.
        #
        # The why is one tap away and stated in full: that is what the pill
        # links to, and /runs used to contradict it by reading "Running" while
        # this went amber. Fixing that is what makes a short label honest.
        return {"state": "warn", "label": "Judging paused",
                "detail": f"{reason}. Collecting normally. {detail}"}

    # 3. Some hunts off. Indefinite, and only you can undo it.
    if paused:
        n = len(paused)
        return {"state": "warn",
                "label": f"{n} hunt{'' if n == 1 else 's'} off",
                "detail": f"{', '.join(h.name for h in paused)} paused. "
                          f"{window}{detail}"}

    if row is None:
        return {"state": "idle", "label": "No runs yet",
                "detail": f"{window}Nothing has fetched yet."}

    mins = int(row["mins"] or 0)
    when = "just now" if mins < 1 else (
        f"{mins}m ago" if mins < 60 else
        f"{mins // 60}h ago" if mins < 2880 else f"{mins // 1440}d ago")

    # 4. Asleep on purpose is not quiet and is not broken. A schedule the
    #    interface does not admit to is the same trap as a pause switch
    #    nobody can see: the bot looks dead for eight hours a day and you
    #    stop trusting the page.
    if not sched.is_open():
        return {"state": "idle",
                "label": f"Asleep till {schedule_mod.fmt_clock(sched.start_minute)}",
                "detail": f"{window}{detail}"}

    # 5. The timer fires every 15 minutes, so an hour of silence is the
    #    timer -- unless it only just woke, when the last run is
    #    legitimately as old as the night.
    opened = sched.opened_at()
    just_woke = opened is not None and (
        sched.now() - opened).total_seconds() < 30 * 60
    if mins > 60 and just_woke:
        return {"state": "ok", "label": "Just woke",
                "detail": f"{window}{detail}"}
    if mins > 60:
        return {"state": "warn", "label": f"Quiet {when}", "detail": detail}

    # 6. Actually working, right now. This is the ONLY state that claims
    #    activity, and it is true for as long as a pass is in flight.
    if activity["in_flight"]:
        return {"state": "ok", "label": "Looking now",
                "detail": f"A pass is running right now. {detail}"}

    # 7. Between ticks: the clock, and no claim about what it is doing.
    #    "Judged · 15m" and "Collecting · 15m" both described a bot that was
    #    sitting still waiting for the timer, and a state word next to a
    #    growing number reads as work in progress. What it HAS judged is a
    #    fact about the last hour, not a state, so it lives in the detail --
    #    and in full on /runs, where the listings themselves are.
    n = activity["scored"]
    return {"state": "ok", "label": when,
            "detail": (f"{n} listing{'' if n == 1 else 's'} judged in the "
                       f"last hour. {detail}" if n else
                       f"Nothing new to judge in the last hour. {detail}")}


def create_app(base_cfg: Config, scorer=None) -> FastAPI:
    """`scorer` is optional and is used for ONE thing: drafting a want's search
    terms when its author left them blank. Without it that field simply stays
    empty, which is what the whole path falls back to anyway -- so the dashboard
    still runs, and every test that does not care can keep omitting it."""
    app = FastAPI(title="Curbside")
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    store = Store(base_cfg.db_path)
    thumbs = ThumbnailStore(base_cfg.db_path.parent / "thumbs")

    def _answer(request: Request, back: str, payload: dict | None = None):
        """303 to a form post, 204 (or JSON) to a fetch.

        Every mutating endpoint here answers both ways, so the same form works
        with the script absent -- it just reloads and drops you at the top of
        the page, which on a settings screen means hunting for where you were.
        """
        if request.headers.get("x-requested-with") == "fetch":
            return payload if payload is not None else Response(status_code=204)
        return RedirectResponse(_safe_back(back), status_code=303)

    def _live() -> Config:
        """The config as it stands right now, wants and cadences included.

        Recomputed per request rather than captured at startup: this process
        stays up for weeks, and a want added on the phone has to become a hunt
        without an ssh session and a restart.

        It is a handful of SELECTs and, as of the seed-marker removal, no
        writes -- the claim here used to be "two SELECTs" while it was in fact
        nine reads and two writes, and the writes meant a GET could block on
        the poller's lock.
        """
        return config_mod.with_store(base_cfg, store)

    def _schedule():
        return schedule_mod.load(store, base_cfg.schedule)

    def _health(paused, hunts, sched, activity) -> dict:
        return health(store.conn.execute(
            "SELECT started_at, error, warning,"
            " (julianday('now') - julianday(started_at))"
            " * 1440 AS mins FROM runs ORDER BY id DESC LIMIT 1").fetchone(),
            paused, hunts, sched, activity)

    def ctx(request: Request, **kw):
        # Every page carries the paused state. A bot that has been switched off
        # and forgotten looks exactly like a broken one.
        cfg = kw.pop("cfg", None) or _live()
        sched = kw.pop("sched", None) or _schedule()
        off = store.disabled_hunts()
        # `cfg.hunts` is a computed property -- bound once here because this
        # block used to read it seven times and rebuild the whole tuple each.
        hunts = cfg.hunts
        paused = [h for h in hunts if h.id in off]
        all_paused = bool(hunts) and len(paused) == len(hunts)
        sweeps = [h for h in hunts if h.kind == "sweep"]
        # Read once and passed on: the pill needs it on every page, and /runs
        # states the same figures underneath the list they describe. Two calls
        # would be two answers to one question, a second apart.
        activity = store.run_activity()
        return {"request": request, "hunts": hunts, "activity": activity,
                # Passed rather than registered as a Jinja filter: the filter
                # table lives on a module-level Environment, so binding a
                # timezone into it would make one app's clock another's.
                "when": lambda raw: local_stamp(raw, base_cfg.schedule.tz),
                "assets": asset_version(),
                # Every template that points at the wants list points at the
                # same string, so moving it again is one edit.
                "manage_url": MANAGE_URL,
                "paused_hunts": paused,
                "all_paused": all_paused,
                "bin_counts": _counts(),
                "health": _health(paused, hunts, sched, activity),
                "schedule": sched,
                "sweeps_paused": bool(sweeps)
                                 and all(h.id in off for h in sweeps),
                **kw}

    def _counts():
        """Bin counts, deduplicated the same way the bins are -- otherwise the
        tab pip says 12 over a list of ten cards."""
        out = {r["status"]: r["n"] for r in store.conn.execute(
            "SELECT status, COUNT(*) n FROM hunt_matches GROUP BY status")}
        for st in ("wanted", "free_find"):
            out[st] = store.conn.execute(
                "SELECT COUNT(*) c FROM hunt_matches m WHERE m.status=?" + ONE_BIN,
                (st,)).fetchone()["c"]
        return out

    def _bin(request: Request, template: str, status: str, **extra):
        items = _rows(store, QUEUE_SQL, (status, PAGE_LIMIT))
        total = store.conn.execute(
            "SELECT COUNT(*) c FROM hunt_matches m WHERE m.status=?" + ONE_BIN,
            (status,)).fetchone()["c"]
        return TEMPLATES.TemplateResponse(
            request, template,
            ctx(request, items=items, total=total,
                truncated=total > len(items), **extra))

    @app.get("/")
    def wants(request: Request, manage: int = 0):
        """Bin one: things that match something you asked for. Unverified
        matches sit here too, flagged, rather than in a bin of their own -- a
        9.0 unconfirmed TV stand belongs next to a 9.0 confirmed one.

        The list itself is edited here as well, in a panel folded away above
        the cards. It used to be a panel on /settings, which is the wrong
        place twice over: this page IS that list's output, and the gear reads
        as configuration rather than as "the things I am looking for".
        """
        cfg = _live()
        return _bin(request, "wants.html", "wanted", cfg=cfg,
                    wants=_want_rows(cfg), ilabels=dict(INTERVAL_CHOICES),
                    manage_open=bool(manage))

    @app.get("/saved")
    def saved(request: Request, show: str = ""):
        """Two lists behind one tab, defaulting to the one with work in it.

        Things you own accumulate and things to act on do not, so within a few
        months the page would have been mostly archive -- and a thing you
        already have is not a thing to decide about. The split is made HERE
        rather than with a second query, because one `SAVED_SQL` already
        returns both and dedupes to one row per listing: partitioning what it
        returns gives both counts exactly, while two queries would each need
        their own copy of that dedupe and could disagree about a listing that
        is grabbed for one hunt and saved for another.
        """
        rows = _rows(store, SAVED_SQL, (PAGE_LIMIT,))
        got = [r for r in rows if r["grabbed_at"]]
        act = [r for r in rows if not r["grabbed_at"]]
        showing = "grabbed" if show == "grabbed" else "saved"
        items = got if showing == "grabbed" else act
        return TEMPLATES.TemplateResponse(
            request, "saved.html",
            ctx(request, items=items, total=len(items), showing=showing,
                back="/saved?show=grabbed" if showing == "grabbed" else "/saved",
                n_act=len(act), n_got=len(got),
                paid=sum(r["paid_cents"] or 0 for r in got),
                truncated=False))

    @app.get("/near")
    def near_misses_legacy(floor: float = 4.0):
        """The old name. "Near" read as "near me", which is the one thing it
        never meant. Kept as a redirect so old bookmarks still land."""
        return RedirectResponse(f"/skipped?floor={floor}", status_code=308)

    @app.get("/skipped")
    def skipped(request: Request, floor: float = 4.0):
        """Judged, then passed over. This is how you tell whether the bar is
        in the right place."""
        items = _rows(store, NEAR_MISS_SQL, (floor, PAGE_LIMIT))
        total = store.conn.execute(
            "SELECT COUNT(*) c FROM hunt_matches m JOIN scores s ON s.id = "
            "(SELECT MAX(id) FROM scores WHERE hunt_id=m.hunt_id AND "
            "listing_id=m.listing_id) WHERE m.status='scored' AND s.deal_score>=?",
            (floor,)).fetchone()["c"]
        return TEMPLATES.TemplateResponse(
            request, "skipped.html",
            ctx(request, items=items, total=total,
                floor=floor, truncated=total > len(items)))

    # --- installable ------------------------------------------------------
    # Android will offer to add this to a home screen given a manifest, an icon
    # and a service worker at the root. It is worth having: the dashboard is
    # read on a phone in spare moments, and a tab among forty tabs is not.

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest",
                            media_type="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():
        """Served from the root, not /static, because a worker's scope cannot
        rise above its own path -- at /static/sw.js it could only ever see
        /static. `no-cache` so a fixed worker is picked up on the next load
        rather than in a day's time."""
        stamp = asset_version()
        return Response(
            (STATIC / "sw.js").read_text().replace("__VERSION__",
                                                   f"curbside-{stamp}"),
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache",
                     "Service-Worker-Allowed": "/"})

    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(STATIC / "favicon-32.png", media_type="image/png")

    @app.get("/thumb/{listing_id:path}")
    def thumb(listing_id: str):
        """Local copy if we have one, otherwise fall back to the source URL --
        which for Facebook stops working after about four days."""
        path = thumbs.path_for(listing_id)
        if path.exists():
            return FileResponse(path, media_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=86400"})
        row = store.conn.execute(
            "SELECT images FROM listings WHERE id=?", (listing_id,)).fetchone()
        urls = json.loads(row["images"]) if row and row["images"] else []
        if urls:
            return RedirectResponse(urls[0], status_code=307)
        return Response(status_code=404)

    @app.get("/free")
    def free_finds(request: Request):
        """Bin two: worth grabbing regardless of the wants list. This is the
        half of the sweep that finds things you never thought to search for."""
        # The blocked-word count belongs on this page: it is where blocking
        # happens, so it is where you look for what you have blocked.
        cfg = _live()
        blocked = sum(len(h.exclude) for h in cfg.hunts if h.kind == "sweep")
        return _bin(request, "free.html", "free_find", cfg=cfg, blocked=blocked)

    @app.get("/hunt/{hunt_id:path}")
    def hunt_view(request: Request, hunt_id: str, status: str | None = None):
        st = status or ""
        hunts = {h.id: h for h in _live().hunts}
        items = _rows(store, HUNT_SQL, (hunt_id, st, st, PAGE_LIMIT))
        counts = {r["status"]: r["n"] for r in
                  store.conn.execute(HUNT_COUNTS_SQL, (hunt_id,))}
        reasons = [(r["filter_reason"], r["n"]) for r in
                   store.conn.execute(REJECT_REASONS_SQL, (hunt_id,))]
        total = counts.get(st, sum(counts.values())) if st else sum(counts.values())
        return TEMPLATES.TemplateResponse(request, "hunt.html", ctx(
            request, items=items, hunt=hunts.get(hunt_id), hunt_id=hunt_id,
            counts=counts, active=status, total=total, reasons=reasons,
            truncated=total > len(items)))

    @app.get("/listing/{listing_id:path}")
    def listing_view(request: Request, listing_id: str, back: str = "/"):
        # `_rows` is the one JSON-column decoder. This page used to hand-decode
        # its own, which is how it ended up showing only red_flags while the
        # card beside it showed unknowns and requirements too -- the deepest
        # view of a listing carrying less of the model's output than the list.
        listings = _rows(store, "SELECT * FROM listings WHERE id=?", (listing_id,))
        if not listings:
            return RedirectResponse("/", status_code=303)
        listing = listings[0]
        scores = _rows(
            store, "SELECT * FROM scores WHERE listing_id=? ORDER BY id DESC",
            (listing_id,))
        matches = [dict(r) for r in store.conn.execute(
            "SELECT * FROM hunt_matches WHERE listing_id=?", (listing_id,))]
        history = store.price_history(listing_id)
        # Blocked words belong to a sweep: they are what stops the free trawl
        # dragging the same category back every 30 minutes, and a want hunt
        # searches its own terms instead. So the control appears only when this
        # listing actually reached a sweep, and acts on that sweep's list.
        cfg = _live()
        hunts = hunts_including_archived(cfg, store)
        sweeps = {h.id for h in cfg.hunts if h.kind == "sweep"}
        blockable = next((m for m in matches if m["hunt_id"] in sweeps), None)
        return TEMPLATES.TemplateResponse(request, "listing.html", ctx(
            request, listing=listing, scores=scores, matches=matches,
            # The newest score decides: it is the one the card is showing.
            price_unclear=bool(scores and scores[0].get("price_unclear")),
            history=history, sparkline=_sparkline(history),
            photos=photo_verdict(
                scores, headed_for_a_bin(store, listing, scores, hunts)),
            blockable=blockable,
            back=_safe_back(back)))

    def _error_page(request: Request, status: int, heading: str,
                    detail: str, recovery: str) -> Response:
        """Errors get the same shell as everything else. The nav still works,
        so a wrong URL is a wrong turn rather than a dead end."""
        try:
            return TEMPLATES.TemplateResponse(
                request, "error.html",
                ctx(request, heading=heading, detail=detail, recovery=recovery),
                status_code=status)
        except Exception:            # the database itself may be the problem
            log.exception("error page failed to render")
            return PlainTextResponse(f"{status}: {detail}", status_code=status)

    @app.exception_handler(StarletteHTTPException)
    def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            return _error_page(
                request, 404, "Not found",
                "There is no page at that address.",
                "If you followed a bookmark, the view may have been renamed.")
        return _error_page(request, exc.status_code, "Something went wrong",
                           str(exc.detail), "Try again, or head back.")

    @app.exception_handler(RequestValidationError)
    def _bad_query(request: Request, exc: RequestValidationError) -> Response:
        return _error_page(
            request, 400, "Bad link",
            "That address has a value this page cannot read.",
            "Drop the query string and try again.")

    @app.exception_handler(Exception)
    def _unhandled(request: Request, exc: Exception) -> Response:
        log.exception("unhandled error rendering %s", request.url.path)
        return _error_page(
            request, 500, "Something went wrong",
            "The dashboard hit an error rendering this page.",
            "It is logged. The other views should still work.")

    @app.post("/hunts/toggle")
    def toggle_hunts(request: Request, enable: str = Form(...),
                     kind: str = Form(""), hunt_id: str = Form(""),
                     back: str = Form("/")):
        """Switch a whole class of hunt on or off.

        Pausing a sweep stops the fetching as well as the judging, so nothing is
        collected for it while it is off -- unlike the quota and spend pauses,
        which deliberately keep collecting. That is the point: this exists to
        stop spending on free stuff, not to quieten it.
        """
        want_on = enable == "1"
        for h in _live().hunts:
            if h.id == hunt_id or (kind and kind in ("all", h.kind)):
                store.set_hunt_enabled(h.id, want_on)
        return _answer(request, back)

    @app.post("/triage")
    def triage(request: Request, hunt_id: str = Form(...),
               listing_id: str = Form(...), status: str = Form(...),
               note: str = Form(""), back: str = Form("/")):
        """The only mutation that happens dozens of times in a sitting.

        Answers 204 to a fetch and 303 to a form post, so the page can act on
        one card without reloading while the same endpoint still works with
        JavaScript off. The buttons are real forms; the script intercepts them.
        """
        # `scored` is here for undo, not for the buttons: a card on /skipped
        # is `scored`, and undoing a dismiss there has to put it back exactly
        # where it was or the list lies about what the database holds.
        # `grabbed` is NOT here. It carries a figure and clears the listing
        # out of every other bin, so it gets its own endpoint rather than
        # riding a form field this one would have to special-case.
        if status not in ("saved", "dismissed", "wanted",
                          "free_find", "scored"):
            raise StarletteHTTPException(400, f"unknown status {status!r}")
        if not store.set_status(hunt_id, listing_id, status, note or None):
            # Nothing matched. Say so: the card is already folding away and the
            # toast is about to claim it worked, and a triage that silently
            # does nothing is the one failure this page cannot show you.
            raise StarletteHTTPException(
                404, f"no listing {listing_id!r} in hunt {hunt_id!r}")
        return _answer(request, back)

    @app.post("/grabbed")
    def grabbed(request: Request, hunt_id: str = Form(...),
                listing_id: str = Form(...), paid: str = Form(""),
                undo: str = Form(""), back: str = Form("/saved")):
        """You went and got it, and this is what you paid.

        Its own endpoint rather than a `/triage` status, because it carries a
        figure and because it does more than move one row: the listing is off
        the market now, so it leaves every other hunt's bin too.

        **A blank price stores NULL, not zero.** Those are different answers --
        0 means free, which is most of what this bot finds, and NULL means you
        did not write it down. Recording a forgotten figure as free would put a
        lie into the one table that exists to check what the model claims a
        thing is worth. An unparseable figure is treated as blank rather than
        raised on, matching the tuning form: a typo on a phone must not cost
        the record of the purchase itself.
        """
        if undo:
            if not store.ungrab(hunt_id, listing_id):
                raise StarletteHTTPException(
                    404, f"no listing {listing_id!r} in hunt {hunt_id!r}")
            return _answer(request, back)
        cents: int | None = None
        if (raw := paid.strip().lstrip("$").replace(",", "")):
            try:
                cents = max(0, int(round(float(raw) * 100)))
            except (TypeError, ValueError):
                cents = None
        if not store.mark_grabbed(hunt_id, listing_id, cents):
            # Same reasoning as `/triage`: the card is already folding away and
            # the toast is about to say it worked.
            raise StarletteHTTPException(
                404, f"no listing {listing_id!r} in hunt {hunt_id!r}")
        return _answer(request, back)

    # --- settings: waking hours, cadence, and the wants list ---------------
    #
    # PRODUCT.md said no settings screens, and meant it: a personal tool with
    # one user does not need a preferences pane. What this is instead is the two
    # things that used to require an ssh session and a service restart -- when
    # the bot is awake, and what it is looking for. Both were already editable,
    # just not from the device the dashboard is read on.

    INTERVAL_CHOICES = ((15, "Every 15 minutes"), (30, "Every 30 minutes"),
                        (60, "Hourly"), (120, "Every 2 hours"),
                        (240, "Every 4 hours"), (480, "Every 8 hours"),
                        (720, "Twice a day"), (1440, "Once a day"))

    def _clean_interval(minutes: int) -> int:
        """The timer only fires every 15 minutes, so anything under that is a
        number with no effect -- and anything much under it would be a way to
        hammer two sources that throttle silently."""
        return max(15, min(int(minutes), 7 * 24 * 60))

    def _lines(text: str) -> tuple[str, ...]:
        return tuple(ln.strip() for ln in (text or "").splitlines() if ln.strip())

    def _want_rows(cfg: Config) -> list[dict]:
        """One row per want, archived ones included, for the manage panel.

        Takes the config it is given rather than calling `_live()` itself: the
        page that renders these already has one, and recomputing it is a
        handful of SELECTs spent to arrive at the same answer.
        """
        off = store.disabled_hunts()
        # Both lookups indexed once. `cfg.hunts` is a computed property, so
        # the linear scan that was here rebuilt every hunt for every want; the
        # match count was a query per want. Each was quadratic in the one number
        # on this page a person is expected to grow.
        by_id = {h.id: h for h in cfg.hunts}
        wants = list(store.wants(include_archived=True))
        counts = {}
        if wants:
            ids = [sw.hunt_id for sw in wants]
            counts = {r["hunt_id"]: r["n"] for r in store.conn.execute(
                WANT_COUNTS_SQL % ",".join("?" * len(ids)), ids)}
        rows = []
        for sw in wants:
            rows.append({
                "w": sw.want, "stored": sw, "hunt": by_id.get(sw.hunt_id),
                "hunt_id": sw.hunt_id,
                "paused": sw.hunt_id in off,
                "matched": counts.get(sw.hunt_id, 0),
            })
        return rows

    @app.get("/settings")
    def settings_view(request: Request, err: str = ""):
        cfg, sched = _live(), _schedule()
        off = store.disabled_hunts()
        sweeps = []
        for h in (x for x in cfg.hunts if x.kind == "sweep"):
            counts = store.exclude_counts(h.id)
            sweeps.append({
                "hunt": h, "paused": h.id in off,
                # Every term is removable. config.yaml seeded them once and is
                # not consulted again, so this is the whole list.
                "terms": [{"term": t, "n": counts.get(t, 0)} for t in h.exclude],
            })
        # Each lever shows what it is currently costing, so it is tuned against
        # evidence rather than adjusted and forgotten.
        bar = cfg.defaults.min_deal_score
        near = store.conn.execute(
            """SELECT COUNT(*) c FROM hunt_matches m JOIN scores s
                 ON s.id=(SELECT MAX(id) FROM scores WHERE hunt_id=m.hunt_id
                          AND listing_id=m.listing_id)
               WHERE m.status='scored' AND s.deal_score >= ? AND s.deal_score < ?""",
            (bar - 2, bar)).fetchone()["c"]
        far = {r["filter_reason"]: r["n"] for r in store.conn.execute(
            "SELECT filter_reason, COUNT(*) n FROM hunt_matches "
            "WHERE filter_reason IN ('too_far','too_far_by_city') "
            "GROUP BY filter_reason")}
        # Image passes actually spent. Not "times the budget refused one":
        # that is only knowable by re-deciding `route`, and the raw count of
        # unmet requests is dominated by listings the pipeline declined to buy
        # photos for because they were not headed for a bin anyway. A settings
        # hint that conflated the two would invite raising a number that
        # changes nothing.
        looked = store.conn.execute(
            "SELECT COUNT(*) c FROM scores WHERE images_checked=1").fetchone()["c"]
        tuning = {
            "min_deal_score": cfg.defaults.min_deal_score,
            "free_find_min_score": cfg.defaults.free_find_min_score,
            "max_results": cfg.defaults.max_results,
            "radius_miles": cfg.location.radius_miles,
            "max_image_checks": cfg.scorer.max_image_checks,
            "images_per_check": cfg.scorer.images_per_check,
            "looked": looked,
            "near_miss": near,
            "too_far": sum(far.values()),
            "backlog": sum(store.unjudged_counts().values()),
            # The cap is per hunt per source, so what it means in practice
            # depends on how many of each there are. Computed, because a typed
            # number here goes stale the moment a want is added.
            "combos": len([h for h in cfg.hunts if h.id not in off]) * len(cfg.sources),
        }
        return TEMPLATES.TemplateResponse(request, "settings.html", ctx(
            request, cfg=cfg, sched=sched, n_wants=len(store.wants()),
            sweeps=sweeps,
            intervals=INTERVAL_CHOICES, ilabels=dict(INTERVAL_CHOICES),
            err=err, tuning=tuning,
            start=schedule_mod.fmt_hhmm(sched.start_minute),
            end=schedule_mod.fmt_hhmm(sched.end_minute)))

    @app.post("/settings/hours")
    def save_hours(request: Request, enabled: str = Form("0"),
                   start: str = Form(""), end: str = Form(""),
                   back: str = Form("/settings")):
        """The window the bot is awake. Stored in `settings`, so a hand edit of
        config.yaml and a tap on the phone are never fighting over one file."""
        current = _schedule()
        start_m = schedule_mod.parse_hhmm(start)
        end_m = schedule_mod.parse_hhmm(end)
        schedule_mod.save(
            store, enabled=enabled == "1",
            start_minute=current.start_minute if start_m is None else start_m,
            end_minute=current.end_minute if end_m is None else end_m)
        return _answer(request, back)

    @app.post("/settings/tuning")
    def save_tuning(request: Request, min_deal_score: str = Form(""),
                    free_find_min_score: str = Form(""),
                    max_results: str = Form(""), radius_miles: str = Form(""),
                    max_image_checks: str = Form(""),
                    back: str = Form("/settings")):
        """The numbers worth a thumb. Everything else in config.yaml stays in
        config.yaml -- the source rate limits especially, which exist to keep
        Facebook from blocking you and live inside the adapter precisely so a
        caller cannot bypass them."""
        # The form fields have to be declared for FastAPI, but the LOOP is
        # driven by Store.TUNING, so the set of numbers this endpoint accepts
        # cannot drift from the set it knows how to clamp.
        submitted = {"min_deal_score": min_deal_score,
                     "free_find_min_score": free_find_min_score,
                     "max_results": max_results,
                     "radius_miles": radius_miles,
                     "max_image_checks": max_image_checks}
        for key in store.TUNING:
            raw = submitted.get(key, "")
            if str(raw).strip():
                try:
                    store.set_tuning(key, raw)
                except (TypeError, ValueError):
                    continue
        return _answer(request, back)

    @app.post("/settings/interval")
    def save_interval(request: Request, hunt_id: str = Form(...),
                      minutes: int = Form(...), back: str = Form("/settings")):
        store.set_hunt_interval(hunt_id, _clean_interval(minutes))
        return _answer(request, back)

    # --- never show me this again ------------------------------------------
    #
    # Dismissing teaches by example and cannot be counted. A blocked word is
    # the opposite: it is deterministic, it is listed on this page, and every
    # listing it ever dropped is countable as `excluded_kw:<term>` on the hunt
    # view. That is the difference between a preference you can audit and one
    # you have to trust.
    #
    # It is also the only rule here that fails CLOSED -- a blocked listing is
    # gone without being read -- so it is guarded twice: word-start matching in
    # `filters._term_pattern`, and a refusal to block a word that appears in
    # something you are hunting for.

    MIN_TERM = 3

    def _would_block_a_want(cfg: Config, term: str) -> str | None:
        """The guard that matters. Blocking "console" on the sweep would drop
        the free media console the tv-stand hunt exists to find, and nothing in
        the interface would ever say so."""
        probe = Listing(id="probe", source="probe", source_id="probe",
                        title=term, description=None, price_cents=None,
                        currency="USD", url="")
        for want in cfg.wants:
            for phrase in (want.name.replace("-", " "),) + want.queries:
                if matches_any(replace(probe, title=phrase), [term]):
                    return want.name
        return None

    @app.post("/settings/exclude")
    def save_exclude(request: Request, hunt_id: str = Form(...),
                     term: str = Form(""), remove: str = Form("0"),
                     back: str = Form("/settings")):
        """Add or drop one blocked word. Answers JSON to a fetch and a redirect
        to a form post, so the settings panel and the card both use it."""
        term = " ".join(term.lower().split())
        wants_json = request.headers.get("x-requested-with") == "fetch"

        def refuse(code: str, message: str):
            if wants_json:
                return {"ok": False, "error": message}
            return RedirectResponse(f"{back}?err={code}", status_code=303)

        if remove == "1":
            store.remove_hunt_exclude(hunt_id, term)
            return _answer(request, back, {"ok": True, "term": term})
        if len(term) < MIN_TERM:
            return refuse("short", "Too short to block safely.")
        if (clash := _would_block_a_want(_live(), term)):
            return refuse(f"wanted:{clash}",
                          f"You are hunting for {clash}. Not blocking that.")
        store.add_hunt_exclude(hunt_id, term)
        return _answer(request, back, {"ok": True, "term": term})

    # --- wants -------------------------------------------------------------

    def _draft_queries(name: str, description: str,
                       requires: tuple[str, ...]) -> tuple[str, ...]:
        """Draft search terms for a want from its description.

        Describing what you want and naming it the way a seller would are two
        different skills, and the second is the one people are bad at: "tv
        stand" and "media console" are the same object and share no word.

        Reached only from the "Suggest terms" button, never from a plain save,
        so nothing is ever spent that was not asked for.

        FAILS OPEN, always. A want with no terms is a perfectly good want --
        the free sweep still matches every want, and its own hunt simply has
        nothing to search yet -- whereas refusing the save would throw away a
        paragraph of prose someone just typed on a phone. So every way this can
        go wrong (no scorer wired, quota paused, the model unreachable, output
        that will not parse) ends in the same place: no terms, and the form
        handed back with everything still in it.
        """
        if scorer is None or not hasattr(scorer, "suggest_queries"):
            return ()
        try:
            return tuple(scorer.suggest_queries(name, description, requires))
        except Exception as exc:                           # noqa: BLE001
            # Includes ScoringUnavailable, which is the EXPECTED failure here:
            # the ceilings and pauses that protect quota shared with otter
            # apply to this call exactly as they do to an appraisal.
            log.info("could not draft search terms for %r: %s", name, exc)
            return ()

    def _want_form(request: Request, *, stored=None, values=None,
                   error: str | None = None, status: int = 200,
                   interval: int | None = None):
        """One template for new and edit. On a validation error it comes back
        with what was typed still in it -- a description is a paragraph of
        prose, and losing it to a bad price would be unforgivable on a phone."""
        cfg = _live()
        hunt_id = stored.hunt_id if stored else None
        hunt = next((h for h in cfg.hunts if h.id == hunt_id), None)
        return TEMPLATES.TemplateResponse(request, "want_form.html", ctx(
            request, cfg=cfg, stored=stored, error=error,
            intervals=INTERVAL_CHOICES,
            # What was just posted wins over the stored hunt: pressing
            # "Suggest terms" is a round trip through this form, and it used to
            # quietly reset a cadence you had just chosen.
            interval=(interval if interval is not None
                      else (hunt.interval_minutes if hunt else 60)),
            v=values or {}), status_code=status)

    @app.get("/wants/new")
    def new_want(request: Request):
        return _want_form(request)

    @app.get("/wants/{name}")
    def edit_want(request: Request, name: str):
        stored = store.get_want(name)
        if stored is None:
            return RedirectResponse(MANAGE_URL, status_code=303)
        w = stored.want
        return _want_form(request, stored=stored, values={
            "name": w.name, "description": w.description,
            "max_price": f"{w.max_price_cents / 100:.0f}",
            "queries": "\n".join(w.queries),
            "requires": "\n".join(w.requires)})

    @app.post("/wants/save")
    def save_want(request: Request, description: str = Form(""),
                  max_price: str = Form(""), queries: str = Form(""),
                  requires: str = Form(""), name: str = Form(""),
                  existing: str = Form(""), interval: int = Form(60),
                  action: str = Form("")):
        """Create or edit one want.

        The name is derived once and then frozen. It is the hunt id, the URL of
        that hunt's view, and the key every score and every triage decision is
        filed under -- renaming would orphan the lot, silently.
        """
        values = {"name": name, "description": description,
                  "max_price": max_price, "queries": queries,
                  "requires": requires}
        stored = store.get_want(existing) if existing else None
        # "Suggest terms" posts here in the background now, so every way this
        # can answer has a JSON twin. Same shape as /settings/exclude: ok:false
        # and a sentence, never a status the script has to decode.
        wants_json = request.headers.get("x-requested-with") == "fetch"

        def fail(msg):
            if wants_json:
                return {"ok": False, "error": msg}
            return _want_form(request, stored=stored, values=values,
                              error=msg, status=400)

        slug = stored.want.name if stored else slugify_want(name)
        if not slug or not WANT_NAME_RE.match(slug):
            return fail("Give it a short name, letters and numbers.")
        if not description.strip():
            return fail("Say what you are looking for. This is what gets judged.")

        # "Suggest terms" -- draft them INTO the form and hand it back unsaved.
        # A button rather than something that happens silently on save: the
        # terms become two searches per tick for as long as the want exists, so
        # they should be read and edited by the person who will live with them,
        # before they are committed to. Nothing is spent unless this is pressed.
        if action == "suggest":
            drafted = _draft_queries(slug, description, _lines(requires))
            if not drafted:
                msg = ("Could not draft search terms just now. "
                       "Type a few, or try again in a moment.")
                if wants_json:
                    return {"ok": False, "error": msg}
                return _want_form(
                    request, stored=stored, values=values, interval=interval,
                    error=msg)
            # ADDED to what is already there, never swapped for it. Replacing
            # would throw away a term typed and not yet committed to a pill --
            # and this form's whole rule is that a round trip loses nothing.
            merged = list(_lines(queries))
            seen = {t.lower() for t in merged}
            for t in drafted:
                if t.lower() not in seen:
                    seen.add(t.lower())
                    merged.append(t)
            # The script asks for the merged list and rewrites the pills in
            # place. The merge stays here rather than being done twice: a
            # browser with no script gets the same list in the same order.
            if wants_json:
                return {"ok": True, "queries": merged}
            values["queries"] = "\n".join(merged)
            return _want_form(request, stored=stored, values=values,
                              interval=interval)

        try:
            dollars = float((max_price or "").replace("$", "").replace(",", ""))
        except ValueError:
            return fail("Set a price cap in dollars, or 0 for free things only.")
        # Zero is a real answer, not a mistake. `over_price` drops anything
        # dearer than the cap, so a cap of 0 keeps free listings and nothing
        # else -- which is how you say "I want one of these, but only if
        # someone is giving it away". This used to be rejected outright, which
        # left "free only" with no way to say it except leaving the search
        # terms blank, and that now means something different.
        if dollars < 0:
            return fail("A price cap cannot be negative.")
        if stored is None and (clash := store.get_want(slug)) and not clash.archived:
            return fail(f"There is already a want called {slug}.")
        # A want with no search terms only ever rides the free sweep, which
        # searches "free" rather than the thing you asked for -- so it quietly
        # does much less than it looks like it does. Requiring one makes that
        # impossible to create by accident; "Suggest terms" is right there, and
        # pausing the hunt is how you say "do not search for this".
        if not _lines(queries):
            return fail("Give it at least one search term, or press "
                        "Suggest terms.")

        store.save_want(Want(
            name=slug, description=description.strip(),
            max_price_cents=int(round(dollars * 100)),
            queries=_lines(queries), requires=_lines(requires)))
        # Re-adding a name that was deleted brings it back rather than
        # colliding with it, and its old matches come back with it.
        store.restore_want(slug)
        store.set_hunt_interval(f"want:{slug}", _clean_interval(interval))
        # Back to the list you just changed, open, rather than to the top of a
        # settings page with the change somewhere below the fold.
        return RedirectResponse(MANAGE_URL, status_code=303)

    @app.post("/wants/archive")
    def archive_want(request: Request, name: str = Form(...),
                     restore: str = Form("0"), back: str = Form(MANAGE_URL)):
        """Deleting a want stops its hunt. It does NOT delete anything: every
        listing it matched, every score and every dismissal stays where it is,
        readable at /hunt/want:<name>, because nothing in this project is ever
        deleted."""
        if restore == "1":
            store.restore_want(name)
        else:
            store.archive_want(name)
        return _answer(request, back)

    def _quota_state() -> dict:
        """What is stopping the judging, and whether the user has overridden it.

        Fetching is unaffected by any of this -- it costs no quota -- so this
        panel is only ever about the judging half.
        """
        now = time.time()
        def _ts(key):
            try:
                return float(store.get_setting(key) or 0)
            except (TypeError, ValueError):
                return 0.0
        until, override = _ts(PAUSE_UNTIL), _ts(OVERRIDE_UNTIL)
        # The pill links here to explain itself, so this row has to know about
        # BOTH ways the judging stops. It only knew about the rate-limit pause
        # in `settings`, so a run that stood aside from the plan ceiling lit
        # the pill amber and then told you, on the page it sent you to, that
        # judging was "Running".
        last = store.conn.execute(
            "SELECT warning FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "paused": until > now,
            "reason": store.get_setting(PAUSE_REASON, "rate limit"),
            "mins": int((until - now) / 60) if until > now else 0,
            "override": override > now,
            "override_mins": int((override - now) / 60) if override > now else 0,
            "standdown": standdown_reason(last["warning"]) if last else None,
        }

    @app.post("/quota/override")
    def quota_override(request: Request, minutes: int = Form(120),
                       back: str = Form("/runs")):
        """Spend anyway, for a bounded while.

        Lifts the three things this project chose to stop at -- the daily spend
        ceiling, the plan-utilisation ceilings, and a rate-limit pause it set
        itself. It cannot lift the real one: if the plan refuses the call, the
        call is refused.
        """
        minutes = max(0, min(int(minutes), 12 * 60))
        if minutes:
            store.set_setting(OVERRIDE_UNTIL, str(time.time() + minutes * 60))
        else:
            store.set_setting(OVERRIDE_UNTIL, "0")
        return _answer(request, back)

    @app.get("/stats")
    def stats(request: Request):
        """What it costs, and what it found for the money.

        A separate page from /runs on purpose. /runs answers "is it working
        right now"; this answers "is it worth running", which is a question
        you ask monthly and act on by rewording a want or deleting it. The two
        want different time horizons and different numbers.

        Every figure comes from `runs` -- see the note above `Store.spend_since`
        for why summing `scores` would lose a third of the money.
        """
        cfg, sched = _live(), _schedule()
        now = datetime.now(timezone.utc)
        # The SAME boundary the spend ceiling uses, and it is the user's
        # midnight rather than UTC's -- see `schedule.local_day_start`. Read at
        # 10:25 on a Monday morning, "Today" used to cover everything since 6pm
        # on the Sunday, because UTC midnight is 6pm in Albuquerque.
        day_start = schedule_mod.local_day_start(sched.tz, now)
        day_iso = day_start.isoformat()
        windows = {
            "today": day_iso,
            "week": (now - timedelta(days=7)).isoformat(),
            "month": (now - timedelta(days=30)).isoformat(),
            "all": None,
        }
        spend = {k: store.spend_since(v) for k, v in windows.items()}

        first = store.first_run_at()
        history_days = ((now - datetime.fromisoformat(first)).days + 1
                        if first else 0)
        ceiling = cfg.scorer.daily_cost_limit_usd
        # Bars are the user's days too, or the chart would disagree with the
        # figure above it on which day is which.
        offset = int((now.astimezone(sched.tz) if sched.tz
                      else now.astimezone()).utcoffset().total_seconds() // 60)
        by_day = store.spend_by_day(30, offset)
        days = _fill_days(by_day, min(30, max(history_days, 1)), offset)
        # The same week the bars draw, said in figures. A bar chart answers
        # "is this normal"; it cannot answer "what did Tuesday cost", which is
        # the question you have when today's number looks high. Newest first:
        # today is the row you came for.
        week_days = []
        for d in reversed(_fill_days(by_day, 7, offset)):
            day = date.fromisoformat(d["day"])
            week_days.append({**d, "label": f"{day:%a} {day.day} {day:%b}",
                              "today": d["day"] == days[-1]["day"]})

        # A run rate, from the shorter of a week and what we actually have.
        # Annualising four days of a new bot would be a made-up number stated
        # to the cent.
        rate_days = min(7, history_days) or 1
        per_day = spend["week"]["usd"] / rate_days

        hunts = {h.id: h for h in cfg.hunts}
        by_hunt = []
        for row in store.spend_by_hunt():
            hunt = hunts.get(row["hunt_id"])
            by_hunt.append({
                **row,
                "name": hunt.name if hunt else row["hunt_id"].split(":", 1)[-1],
                # A want deleted from /settings stops running and keeps its
                # history. What it spent is still your money and still belongs
                # in the total.
                "live": hunt is not None,
                "per_scored": (row["usd"] / row["scored"]) if row["scored"] else None,
                "per_save": (row["usd"] / row["saved"]) if row["saved"] else None,
            })

        # What today's money actually went on. Same shape as the all-time
        # table, scoped to the day, plus the individual listings that cost the
        # most -- an image pass runs about 15x a text appraisal, so one
        # photo-checked junk post shows up immediately.
        today = {
            "hunts": [{**r,
                       "name": (hunts[r["hunt_id"]].name if r["hunt_id"] in hunts
                                else r["hunt_id"].split(":", 1)[-1])}
                      for r in store.spend_by_hunt(day_iso)],
            "stages": store.spend_by_stage(day_iso),
            "sources": store.spend_by_source(day_iso),
            "dearest": store.dearest_since(day_iso, 5),
            "started": day_start,
        }

        tokens = store.token_totals()
        billed = tokens["input"] + tokens["cached"]
        return TEMPLATES.TemplateResponse(request, "stats.html", ctx(
            request, spend=spend, days=days, week_days=week_days,
            bars=_daybars(days, ceiling),
            ceiling=ceiling, per_day=per_day, history_days=history_days,
            by_hunt=by_hunt, by_source=store.spend_by_source(),
            stages=store.spend_by_stage(), funnel=store.funnel(),
            calibration=store.calibration(),
            today=today, asleep=not sched.is_open(), tz_name=sched.tz_name,
            tokens=tokens,
            cache_pct=(tokens["cached"] / billed * 100) if billed else None))

    @app.get("/runs")
    def runs(request: Request):
        rows = [dict(r) for r in store.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 200")]
        for r in rows:
            r["took"] = took(r["started_at"], r["finished_at"])
        # What the pill is talking about. It has room for "Judging" and a
        # clock, and nothing else -- so the listings themselves, and which
        # hunt and which site each came from, live on the page it links to.
        judged = []
        for j in store.judged_recently(12):
            judged.append({
                **j,
                "hunt": j["hunt_id"].split(":", 1)[-1],
                # A first-pass drop is a score of 0.0 with a `:triage` model.
                # Shown as a drop rather than as a verdict of zero, which is
                # what the number alone would read as.
                "dropped": (j["model"] or "").endswith(":triage"),
            })
        return TEMPLATES.TemplateResponse(request, "runs.html", ctx(
            request, runs=rows, quota=_quota_state(), judged=judged,
            # What the plan has left, next to the switch that overrides it.
            # `base_cfg` rather than `_live()`: the ceilings are config, not
            # one of the things the dashboard is allowed to tune.
            usage=plan_usage_view(read_plan_usage(store, base_cfg.scorer)),
            backlog=store.unjudged_counts()))

    return app
