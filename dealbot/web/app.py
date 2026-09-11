"""Dashboard. Read-mostly over the same SQLite file the pipeline writes; the only
writes are triage actions.

The Runs view is not decoration. It is what distinguishes "nothing good posted
today" from "the scraper broke last Tuesday", and an empty result you cannot
explain is a tool you stop opening.
"""
from __future__ import annotations

import json
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
from ..config import Config
from ..db import Store
from ..filters import matches_any
from ..models import WANT_NAME_RE, Listing, Want, slugify_want
from ..thumbs import ThumbnailStore

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STATIC = Path(__file__).parent / "static"
log = logging.getLogger("dealbot.web")

QUEUE_SQL = """
SELECT m.hunt_id, m.status, l.*,
       l.previous_price_cents,
       CAST(julianday('now') - julianday(l.posted_at) AS INTEGER) AS age_days,
       (SELECT COUNT(DISTINCT price_cents) - 1 FROM price_observations
         WHERE listing_id = l.id) AS price_moves,
       s.deal_score, s.reasoning, s.red_flags,
       s.matched_want, s.est_value_cents, s.priced_at_cents, s.match,
       s.unknowns, s.requirements, s.worth_grabbing, s.images_checked
FROM hunt_matches m
JOIN listings l ON l.id = m.listing_id
JOIN scores  s ON s.id = (SELECT MAX(id) FROM scores
                          WHERE hunt_id = m.hunt_id AND listing_id = m.listing_id)
WHERE m.status = ?
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
       s.match, s.unknowns, s.requirements, s.worth_grabbing, s.images_checked
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

# Things you decided to act on. Clicking "saved" used to make a listing vanish:
# it left the bin and was only findable by digging through a hunt view.
SAVED_SQL = QUEUE_SQL.replace("WHERE m.status = ?",
                              "WHERE m.status IN ('saved', 'contacted')")

# The band just under the bar. `deal_score` is judged as if unknowns resolve
# favourably, so a 6 means "even if it is what it looks like, it is mediocre" --
# but you cannot calibrate a threshold you can never see over.
NEAR_MISS_SQL = QUEUE_SQL.replace(
    "WHERE m.status = ?", "WHERE m.status = 'scored' AND s.deal_score >= ?")


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


def create_app(base_cfg: Config) -> FastAPI:
    app = FastAPI(title="Curbside")
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    store = Store(base_cfg.db_path)
    thumbs = ThumbnailStore(base_cfg.db_path.parent / "thumbs")

    def _live() -> Config:
        """The config as it stands right now, wants and cadences included.

        Recomputed per request rather than captured at startup: this process
        stays up for weeks, and a want added on the phone has to become a hunt
        without an ssh session and a restart. It is two SELECTs.
        """
        return config_mod.with_store(base_cfg, store)

    def _schedule():
        return schedule_mod.load(store, base_cfg.schedule)

    def _health(paused, hunts, sched) -> dict:
        """The state of the bot itself, on every page, in one pill.

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
        row = store.conn.execute(
            "SELECT started_at, error, (julianday('now') - julianday(started_at))"
            " * 1440 AS mins FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        window = "" if sched.always_on else f"Awake {sched.window_label}. "
        detail = f"Last run {row['started_at']}" if row else "Nothing has fetched yet."

        # 1. Everything is off. Nothing is running, so nothing else here is the
        #    reason nothing is happening -- not the hours, not a stale error.
        if hunts and len(paused) == len(hunts):
            return {"state": "warn", "label": "Paused",
                    "detail": f"Every hunt is off. Nothing is being collected. "
                              f"{window}{detail}"}

        # 2. A fetch that died. Louder than anything below it.
        if row is not None and row["error"]:
            return {"state": "bad", "label": "Fetch failing",
                    "detail": f"{detail} — {row['error']}"}

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
        return {"state": "ok", "label": when, "detail": detail}

    def ctx(request: Request, **kw):
        # Every page carries the paused state. A bot that has been switched off
        # and forgotten looks exactly like a broken one.
        cfg = kw.pop("cfg", None) or _live()
        sched = kw.pop("sched", None) or _schedule()
        off = store.disabled_hunts()
        paused = [h for h in cfg.hunts if h.id in off]
        all_paused = bool(cfg.hunts) and len(paused) == len(cfg.hunts)
        return {"request": request, "hunts": cfg.hunts,
                "paused_hunts": paused,
                "all_paused": all_paused,
                "bin_counts": _counts(),
                "health": _health(paused, cfg.hunts, sched),
                "schedule": sched,
                "sweeps_paused": all(h.id in off for h in cfg.hunts
                                     if h.kind == "sweep")
                                 and any(h.kind == "sweep" for h in cfg.hunts),
                **kw}

    def _counts():
        return {r["status"]: r["n"] for r in store.conn.execute(
            "SELECT status, COUNT(*) n FROM hunt_matches GROUP BY status")}

    def _bin(request: Request, template: str, status: str):
        items = _rows(store, QUEUE_SQL, (status, PAGE_LIMIT))
        total = store.conn.execute(
            "SELECT COUNT(*) c FROM hunt_matches WHERE status=?",
            (status,)).fetchone()["c"]
        return TEMPLATES.TemplateResponse(
            request, template,
            ctx(request, items=items, counts=_counts(), total=total,
                truncated=total > len(items)))

    @app.get("/")
    def wants(request: Request):
        """Bin one: things that match something you asked for. Unverified
        matches sit here too, flagged, rather than in a bin of their own -- a
        9.0 unconfirmed TV stand belongs next to a 9.0 confirmed one."""
        return _bin(request, "wants.html", "wanted")

    @app.get("/saved")
    def saved(request: Request):
        items = _rows(store, SAVED_SQL, (PAGE_LIMIT,))
        return TEMPLATES.TemplateResponse(
            request, "saved.html",
            ctx(request, items=items, counts=_counts(), total=len(items),
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
            ctx(request, items=items, counts=_counts(), total=total,
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
        return FileResponse(
            STATIC / "sw.js", media_type="text/javascript",
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
        return _bin(request, "free.html", "free_find")

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
    def listing_view(request: Request, listing_id: str):
        row = store.conn.execute(
            "SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
        if row is None:
            return RedirectResponse("/", status_code=303)
        listing = dict(row)
        listing["images"] = json.loads(listing["images"] or "[]")
        scores = [dict(r) for r in store.conn.execute(
            "SELECT * FROM scores WHERE listing_id=? ORDER BY id DESC", (listing_id,))]
        for s in scores:
            # The detail page used to decode only red_flags, so the deepest view
            # of a listing carried less of the model's output than the card did.
            for col in ("red_flags", "unknowns", "requirements"):
                s[col] = json.loads(s[col] or "[]")
        matches = [dict(r) for r in store.conn.execute(
            "SELECT * FROM hunt_matches WHERE listing_id=?", (listing_id,))]
        history = store.price_history(listing_id)
        return TEMPLATES.TemplateResponse(request, "listing.html", ctx(
            request, listing=listing, scores=scores, matches=matches,
            history=history, sparkline=_sparkline(history)))

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
    def toggle_hunts(enable: str = Form(...), kind: str = Form(""),
                     hunt_id: str = Form(""), back: str = Form("/")):
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
        return RedirectResponse(back, status_code=303)

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
        if status in ("saved", "dismissed", "contacted", "wanted", "free_find",
                      "scored"):
            store.set_status(hunt_id, listing_id, status, note or None)
        if request.headers.get("x-requested-with") == "fetch":
            return Response(status_code=204)
        return RedirectResponse(back, status_code=303)

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

    @app.get("/settings")
    def settings_view(request: Request, err: str = ""):
        cfg, sched = _live(), _schedule()
        off = store.disabled_hunts()
        rows = []
        for sw in store.wants(include_archived=True):
            hunt = next((h for h in cfg.hunts if h.id == sw.hunt_id), None)
            rows.append({
                "w": sw.want, "stored": sw, "hunt": hunt,
                "hunt_id": sw.hunt_id,
                "paused": sw.hunt_id in off,
                "matched": store.conn.execute(
                    "SELECT COUNT(*) c FROM hunt_matches WHERE hunt_id=?",
                    (sw.hunt_id,)).fetchone()["c"],
            })
        sweeps = []
        for h in (x for x in cfg.hunts if x.kind == "sweep"):
            counts = store.exclude_counts(h.id)
            mine = store.hunt_excludes().get(h.id, ())
            sweeps.append({
                "hunt": h, "paused": h.id in off,
                # The file's terms are shown but not removable here: they are
                # reviewed lines in a committed file, not a tap.
                "terms": [{"term": t, "n": counts.get(t, 0), "mine": t in mine}
                          for t in h.exclude],
            })
        return TEMPLATES.TemplateResponse(request, "settings.html", ctx(
            request, cfg=cfg, sched=sched, wants=rows, sweeps=sweeps,
            intervals=INTERVAL_CHOICES, ilabels=dict(INTERVAL_CHOICES),
            err=err,
            start=schedule_mod.fmt_hhmm(sched.start_minute),
            end=schedule_mod.fmt_hhmm(sched.end_minute)))

    @app.post("/settings/hours")
    def save_hours(enabled: str = Form("0"), start: str = Form(""),
                   end: str = Form(""), back: str = Form("/settings")):
        """The window the bot is awake. Stored in `settings`, so a hand edit of
        config.yaml and a tap on the phone are never fighting over one file."""
        current = _schedule()
        start_m = schedule_mod.parse_hhmm(start)
        end_m = schedule_mod.parse_hhmm(end)
        schedule_mod.save(
            store, enabled=enabled == "1",
            start_minute=current.start_minute if start_m is None else start_m,
            end_minute=current.end_minute if end_m is None else end_m)
        return RedirectResponse(back, status_code=303)

    @app.post("/settings/interval")
    def save_interval(hunt_id: str = Form(...), minutes: int = Form(...),
                      back: str = Form("/settings")):
        store.set_hunt_interval(hunt_id, _clean_interval(minutes))
        return RedirectResponse(back, status_code=303)

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
    def save_exclude(hunt_id: str = Form(...), term: str = Form(""),
                     remove: str = Form("0"), back: str = Form("/settings")):
        term = " ".join(term.lower().split())
        if remove == "1":
            store.remove_hunt_exclude(hunt_id, term)
            return RedirectResponse(back, status_code=303)

        cfg = _live()
        if len(term) < MIN_TERM:
            return RedirectResponse(f"{back}?err=short", status_code=303)
        if (clash := _would_block_a_want(cfg, term)):
            return RedirectResponse(f"{back}?err=wanted:{clash}", status_code=303)
        store.add_hunt_exclude(hunt_id, term)
        return RedirectResponse(back, status_code=303)

    @app.post("/settings/exclude.json")
    def save_exclude_async(hunt_id: str = Form(...), term: str = Form(""),
                           remove: str = Form("0")):
        """The same thing from a card, which cannot afford a page reload."""
        term = " ".join(term.lower().split())
        if remove == "1":
            store.remove_hunt_exclude(hunt_id, term)
            return {"ok": True, "term": term}
        if len(term) < MIN_TERM:
            return {"ok": False, "error": "Too short to block safely."}
        if (clash := _would_block_a_want(_live(), term)):
            return {"ok": False,
                    "error": f"You are hunting for {clash}. Not blocking that."}
        store.add_hunt_exclude(hunt_id, term)
        return {"ok": True, "term": term}

    # --- wants -------------------------------------------------------------

    def _want_form(request: Request, *, stored=None, values=None,
                   error: str | None = None, status: int = 200):
        """One template for new and edit. On a validation error it comes back
        with what was typed still in it -- a description is a paragraph of
        prose, and losing it to a bad price would be unforgivable on a phone."""
        cfg = _live()
        hunt_id = stored.hunt_id if stored else None
        hunt = next((h for h in cfg.hunts if h.id == hunt_id), None)
        return TEMPLATES.TemplateResponse(request, "want_form.html", ctx(
            request, cfg=cfg, stored=stored, error=error,
            intervals=INTERVAL_CHOICES,
            interval=hunt.interval_minutes if hunt else 60,
            v=values or {}), status_code=status)

    @app.get("/wants/new")
    def new_want(request: Request):
        return _want_form(request)

    @app.get("/wants/{name}")
    def edit_want(request: Request, name: str):
        stored = store.get_want(name)
        if stored is None:
            return RedirectResponse("/settings", status_code=303)
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
                  existing: str = Form(""), interval: int = Form(60)):
        """Create or edit one want.

        The name is derived once and then frozen. It is the hunt id, the URL of
        that hunt's view, and the key every score and every triage decision is
        filed under -- renaming would orphan the lot, silently.
        """
        values = {"name": name, "description": description,
                  "max_price": max_price, "queries": queries,
                  "requires": requires}
        stored = store.get_want(existing) if existing else None

        def fail(msg):
            return _want_form(request, stored=stored, values=values,
                              error=msg, status=400)

        slug = stored.want.name if stored else slugify_want(name)
        if not slug or not WANT_NAME_RE.match(slug):
            return fail("Give it a short name, letters and numbers.")
        if not description.strip():
            return fail("Say what you are looking for. This is what gets judged.")
        try:
            dollars = float((max_price or "").replace("$", "").replace(",", ""))
        except ValueError:
            return fail("Set a price cap, in dollars.")
        if dollars < 1:
            return fail("A price cap of zero would reject everything priced.")
        if stored is None and (clash := store.get_want(slug)) and not clash.archived:
            return fail(f"There is already a want called {slug}.")

        store.save_want(Want(
            name=slug, description=description.strip(),
            max_price_cents=int(round(dollars * 100)),
            queries=_lines(queries), requires=_lines(requires)))
        # Re-adding a name that was deleted brings it back rather than
        # colliding with it, and its old matches come back with it.
        store.restore_want(slug)
        store.set_hunt_interval(f"want:{slug}", _clean_interval(interval))
        return RedirectResponse("/settings", status_code=303)

    @app.post("/wants/archive")
    def archive_want(name: str = Form(...), restore: str = Form("0"),
                     back: str = Form("/settings")):
        """Deleting a want stops its hunt. It does NOT delete anything: every
        listing it matched, every score and every dismissal stays where it is,
        readable at /hunt/want:<name>, because nothing in this project is ever
        deleted."""
        if restore == "1":
            store.restore_want(name)
        else:
            store.archive_want(name)
        return RedirectResponse(back, status_code=303)

    @app.get("/runs")
    def runs(request: Request):
        rows = [dict(r) for r in store.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 200")]
        return TEMPLATES.TemplateResponse(request, "runs.html", ctx(request, runs=rows))

    return app
