"""Dashboard. Read-mostly over the same SQLite file the pipeline writes; the only
writes are triage actions.

The Runs view is not decoration. It is what distinguishes "nothing good posted
today" from "the scraper broke last Tuesday", and an empty result you cannot
explain is a tool you stop opening.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import Config
from ..db import Store

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

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
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'role="img" aria-label="price history">'
            f'<polyline fill="none" stroke="{colour}" stroke-width="1.5" '
            f'points="{coords}"/></svg>')


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="deal_bot")
    store = Store(cfg.db_path)
    hunts = {h.id: h for h in cfg.hunts}

    def ctx(request: Request, **kw):
        return {"request": request, "hunts": cfg.hunts, **kw}

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

    @app.get("/free")
    def free_finds(request: Request):
        """Bin two: worth grabbing regardless of the wants list. This is the
        half of the sweep that finds things you never thought to search for."""
        return _bin(request, "free.html", "free_find")

    @app.get("/hunt/{hunt_id:path}")
    def hunt_view(request: Request, hunt_id: str, status: str | None = None):
        st = status or ""
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
            s["red_flags"] = json.loads(s["red_flags"] or "[]")
        matches = [dict(r) for r in store.conn.execute(
            "SELECT * FROM hunt_matches WHERE listing_id=?", (listing_id,))]
        history = store.price_history(listing_id)
        return TEMPLATES.TemplateResponse(request, "listing.html", ctx(
            request, listing=listing, scores=scores, matches=matches,
            history=history, sparkline=_sparkline(history)))

    @app.post("/triage")
    def triage(hunt_id: str = Form(...), listing_id: str = Form(...),
               status: str = Form(...), note: str = Form(""),
               back: str = Form("/")):
        if status in ("saved", "dismissed", "contacted", "wanted", "free_find"):
            store.set_status(hunt_id, listing_id, status, note or None)
        return RedirectResponse(back, status_code=303)

    @app.get("/runs")
    def runs(request: Request):
        rows = [dict(r) for r in store.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 200")]
        return TEMPLATES.TemplateResponse(request, "runs.html", ctx(request, runs=rows))

    return app
