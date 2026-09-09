"""SQLite store.

Two rules the rest of the code depends on:

  * Nothing is ever deleted. Listings we reject are kept with the reason, and
    price observations are append-only. Today's junk is next month's answer to
    "what does this actually go for around here", and that history cannot be
    backfilled after the fact.
  * Every fetch attempt gets a `runs` row, success or failure. An empty result
    has to be distinguishable from a broken scraper, or a silently dead bot
    looks exactly like a quiet week.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import Hunt, Listing, Score, UpsertResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  id            TEXT PRIMARY KEY,
  source        TEXT NOT NULL,
  source_id     TEXT NOT NULL,
  title         TEXT NOT NULL,
  description   TEXT,
  price_cents   INTEGER,
  previous_price_cents INTEGER,
  currency      TEXT NOT NULL DEFAULT 'USD',
  url           TEXT NOT NULL,
  city          TEXT,
  lat           REAL,
  lng           REAL,
  distance_mi   REAL,
  seller_id     TEXT,
  seller_name   TEXT,
  images        TEXT NOT NULL DEFAULT '[]',
  category      TEXT,
  posted_at     TEXT,
  fingerprint   TEXT NOT NULL,
  dup_key       TEXT,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  is_active     INTEGER NOT NULL DEFAULT 1,
  raw           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_listings_fingerprint ON listings(fingerprint);
CREATE INDEX IF NOT EXISTS ix_listings_last_seen   ON listings(last_seen);

-- Append-only. A price trajectory is the difference between "cheap" and
-- "seller wants this gone", and it is the one signal no competitor has.
CREATE TABLE IF NOT EXISTS price_observations (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_id   TEXT NOT NULL REFERENCES listings(id),
  observed_at  TEXT NOT NULL,
  price_cents  INTEGER
);
CREATE INDEX IF NOT EXISTS ix_price_obs_listing ON price_observations(listing_id);

-- "We saw it" and "it matched hunt X" are different facts: one listing can match
-- several hunts with independent triage state.
CREATE TABLE IF NOT EXISTS hunt_matches (
  hunt_id       TEXT NOT NULL,
  listing_id    TEXT NOT NULL REFERENCES listings(id),
  matched_at    TEXT NOT NULL,
  status        TEXT NOT NULL,          -- new|filtered|scored|wanted|free_find|saved|dismissed|contacted|gone
  filter_reason TEXT,
  dismiss_note  TEXT,
  miss_count    INTEGER NOT NULL DEFAULT 0,
  status_before_gone TEXT,
  notified_at   TEXT,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (hunt_id, listing_id)
);
CREATE INDEX IF NOT EXISTS ix_matches_status ON hunt_matches(hunt_id, status);

CREATE TABLE IF NOT EXISTS scores (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_id         TEXT NOT NULL REFERENCES listings(id),
  hunt_id            TEXT NOT NULL,
  model              TEXT NOT NULL,
  scored_at          TEXT NOT NULL,
  match              TEXT NOT NULL DEFAULT 'unknown',   -- yes|no|unknown
  deal_score         REAL NOT NULL,
  unknowns           TEXT NOT NULL DEFAULT '[]',
  requirements       TEXT NOT NULL DEFAULT '[]',
  est_value_cents    INTEGER,
  condition          TEXT,
  matched_want       TEXT,
  worth_grabbing     INTEGER NOT NULL DEFAULT 0,
  needs_images       INTEGER NOT NULL DEFAULT 0,
  image_question     TEXT,
  images_checked     INTEGER NOT NULL DEFAULT 0,
  red_flags          TEXT NOT NULL DEFAULT '[]',
  reasoning          TEXT NOT NULL DEFAULT '',
  priced_at_cents    INTEGER,           -- price when scored, so a later drop is detectable
  input_tokens       INTEGER NOT NULL DEFAULT 0,
  output_tokens      INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  cost_usd           REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_scores_lookup ON scores(hunt_id, listing_id, scored_at);

CREATE TABLE IF NOT EXISTS runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  hunt_id       TEXT NOT NULL,
  source        TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  n_fetched     INTEGER NOT NULL DEFAULT 0,
  n_new         INTEGER NOT NULL DEFAULT 0,
  n_candidates  INTEGER NOT NULL DEFAULT 0,
  n_scored      INTEGER NOT NULL DEFAULT 0,
  n_surfaced    INTEGER NOT NULL DEFAULT 0,
  n_worth_a_look INTEGER NOT NULL DEFAULT 0,
  n_wanted      INTEGER NOT NULL DEFAULT 0,
  n_free_find   INTEGER NOT NULL DEFAULT 0,
  n_image_checks INTEGER NOT NULL DEFAULT 0,
  n_deferred    INTEGER NOT NULL DEFAULT 0,
  full_pass     INTEGER NOT NULL DEFAULT 1,
  cost_usd      REAL NOT NULL DEFAULT 0,
  error         TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_hunt ON runs(hunt_id, started_at);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

TERMINAL_TRIAGE = ("saved", "dismissed", "contacted")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """One connection per thread.

    The dashboard runs sync handlers in Starlette's threadpool, and a sqlite3
    connection may only be used from the thread that made it. WAL mode makes
    several connections to one file cheap and safe, so each thread simply opens
    its own rather than sharing one behind a lock.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns that arrived after a database was first created. Nothing
        is ever dropped or rewritten -- old rows keep their defaults."""
        for table, cols in (
            ("scores", (("match", "TEXT NOT NULL DEFAULT 'unknown'"),
                        ("unknowns", "TEXT NOT NULL DEFAULT '[]'"),
                        ("requirements", "TEXT NOT NULL DEFAULT '[]'"),
                        ("worth_grabbing", "INTEGER NOT NULL DEFAULT 0"),
                        ("needs_images", "INTEGER NOT NULL DEFAULT 0"),
                        ("image_question", "TEXT"),
                        ("images_checked", "INTEGER NOT NULL DEFAULT 0"))),
            ("listings", (("dup_key", "TEXT"),
                          ("previous_price_cents", "INTEGER"))),
            ("hunt_matches", (("miss_count", "INTEGER NOT NULL DEFAULT 0"),
                              ("status_before_gone", "TEXT"),
                              ("notified_at", "TEXT"))),
            ("runs", (("n_worth_a_look", "INTEGER NOT NULL DEFAULT 0"),
                      ("n_wanted", "INTEGER NOT NULL DEFAULT 0"),
                      ("n_free_find", "INTEGER NOT NULL DEFAULT 0"),
                      ("n_image_checks", "INTEGER NOT NULL DEFAULT 0"),
                      ("n_deferred", "INTEGER NOT NULL DEFAULT 0"),
                      ("full_pass", "INTEGER NOT NULL DEFAULT 1"))),
        ):
            have = {r["name"] for r in
                    self.conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols:
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

        # Indexes on migrated columns must be created HERE, not in SCHEMA:
        # executescript runs before the ALTER TABLE above, so an index naming a
        # new column fails on every pre-existing database.
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_listings_dup_key "
                          "ON listings(dup_key)")

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            # The poller and the dashboard are separate processes writing the
            # same file by design. Without this, a triage click landing while a
            # run is mid-write raises "database is locked" and 500s the request.
            # WAL lets readers through regardless; this covers writer overlap.
            conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # --- runs ---------------------------------------------------------------

    def start_run(self, hunt: Hunt, source: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (hunt_id, source, started_at) VALUES (?,?,?)",
            (hunt.id, source, _now()),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, **fields: Any) -> None:
        cols = [k for k in fields if k in
                ("n_fetched", "n_new", "n_candidates", "n_scored", "n_surfaced",
                 "n_worth_a_look", "n_wanted", "n_free_find",
                 "n_image_checks", "n_deferred", "full_pass", "cost_usd",
                 "error")]
        sets = ", ".join(f"{c}=?" for c in cols)
        args = [fields[c] for c in cols]
        self.conn.execute(
            f"UPDATE runs SET finished_at=?{',' + sets if sets else ''} WHERE id=?",
            [_now(), *args, run_id],
        )

    def last_success_at(self, hunt_id: str, source: str) -> str | None:
        """When this hunt last completed for this source WITHOUT error.

        A failed run does not count, so a transient outage is retried on the
        next tick rather than waiting out a full interval. Sources carry their
        own rate limits, so retrying cannot become hammering.

        Neither does a `--no-score` pass: it fetched but never judged, and
        letting it satisfy the cadence pushes the real pass out by a full
        interval."""
        row = self.conn.execute(
            "SELECT started_at FROM runs WHERE hunt_id=? AND source=? "
            "AND finished_at IS NOT NULL AND error IS NULL AND full_pass=1 "
            "ORDER BY id DESC LIMIT 1", (hunt_id, source)).fetchone()
        return row["started_at"] if row else None

    def cost_since(self, iso_start: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) c FROM runs WHERE started_at >= ?",
            (iso_start,)).fetchone()
        return float(row["c"])

    # --- listings -----------------------------------------------------------

    def upsert_listing(self, listing: Listing) -> UpsertResult:
        now = _now()
        row = self.conn.execute(
            "SELECT price_cents FROM listings WHERE id=?", (listing.id,)
        ).fetchone()

        if row is None:
            # A relist is a *new* id whose fingerprint we have seen before. That
            # is the only way to catch a repost, since the id always changes.
            is_relist = self.conn.execute(
                "SELECT 1 FROM listings WHERE fingerprint=? AND id<>? LIMIT 1",
                (listing.fingerprint, listing.id),
            ).fetchone() is not None
            self.conn.execute(
                """INSERT INTO listings (id, source, source_id, title, description,
                     price_cents, previous_price_cents, currency, url, city, lat, lng, distance_mi,
                     seller_id, seller_name, images, category, posted_at,
                     fingerprint, dup_key, first_seen, last_seen, is_active, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (listing.id, listing.source, listing.source_id, listing.title,
                 listing.description, listing.price_cents,
                 listing.previous_price_cents, listing.currency,
                 listing.url, listing.city, listing.lat, listing.lng,
                 listing.distance_mi, listing.seller_id, listing.seller_name,
                 json.dumps(list(listing.images)), listing.category,
                 listing.posted_at.isoformat() if listing.posted_at else None,
                 listing.fingerprint, listing.dup_key, now, now,
                 json.dumps(listing.raw)),
            )
            return UpsertResult(listing.id, True, False, None, is_relist)

        previous = row["price_cents"]
        changed = previous != listing.price_cents
        # COALESCE on every enrichable column. The search feed carries no
        # description and no coordinates, so a later index-only refresh must not
        # wipe what the detail fetch filled in -- and the detail fetch must be
        # able to fill them in at all. Leaving lat/lng/city out of this UPDATE
        # was silently discarding every coordinate we fetched, while descriptions
        # and photos landed fine and made enrichment look like it worked.
        self.conn.execute(
            """UPDATE listings SET
                 title = ?,
                 description  = COALESCE(?, description),
                 price_cents  = ?,
                 previous_price_cents = COALESCE(?, previous_price_cents),
                 url          = ?,
                 city         = COALESCE(?, city),
                 lat          = COALESCE(?, lat),
                 lng          = COALESCE(?, lng),
                 distance_mi  = COALESCE(?, distance_mi),
                 dup_key      = COALESCE(?, dup_key),
                 seller_id    = COALESCE(?, seller_id),
                 seller_name  = COALESCE(?, seller_name),
                 images       = CASE WHEN json_array_length(?) >= json_array_length(images)
                                     THEN ? ELSE images END,
                 last_seen    = ?, is_active = 1, raw = ?
               WHERE id = ?""",
            (listing.title, listing.description, listing.price_cents,
             listing.previous_price_cents, listing.url,
             listing.city, listing.lat, listing.lng, listing.distance_mi,
             listing.dup_key, listing.seller_id, listing.seller_name,
             json.dumps(list(listing.images)), json.dumps(list(listing.images)),
             now, json.dumps(listing.raw), listing.id),
        )
        return UpsertResult(listing.id, False, changed, previous, False)

    def scored_duplicate(self, hunt_id: str, dup_key: str,
                         exclude_id: str) -> str | None:
        """A different listing with the same physical identity that this hunt has
        already judged. Cross-posting to both marketplaces is common, and without
        this you pay to appraise the same item twice and see it twice."""
        row = self.conn.execute(
            """SELECT l.id FROM listings l
               JOIN scores s ON s.listing_id = l.id AND s.hunt_id = ?
               WHERE l.dup_key = ? AND l.id <> ? LIMIT 1""",
            (hunt_id, dup_key, exclude_id)).fetchone()
        return row["id"] if row else None

    def record_price(self, listing_id: str, price_cents: int | None) -> None:
        """Append-only, but only when the price actually MOVED.

        Writing an unchanged price every run meant ~200 identical rows per hunt
        per 15 minutes -- roughly 19k rows a day, millions a year, all noise,
        and every one of them slowing the history query the price-drop signal
        depends on. The first observation is always recorded, so a listing's
        opening price is never lost."""
        last = self.conn.execute(
            "SELECT price_cents FROM price_observations WHERE listing_id=? "
            "ORDER BY id DESC LIMIT 1", (listing_id,)).fetchone()
        if last is not None and last["price_cents"] == price_cents:
            return
        self.conn.execute(
            "INSERT INTO price_observations (listing_id, observed_at, price_cents) VALUES (?,?,?)",
            (listing_id, _now(), price_cents),
        )

    def price_history(self, listing_id: str) -> list[tuple[str, int | None]]:
        return [(r["observed_at"], r["price_cents"]) for r in self.conn.execute(
            "SELECT observed_at, price_cents FROM price_observations "
            "WHERE listing_id=? ORDER BY observed_at", (listing_id,))]

    def mark_gone(self, hunt_id: str, source: str, seen_ids: Sequence[str],
                  threshold: int = 3) -> int:
        """Retire listings this source has stopped showing.

        Two things this must NOT do, both of which it used to:

        * **Cross sources.** Several sources run the same hunt, so scoping only
          by hunt_id meant whichever ran last marked every other source's
          listings `gone` -- including ones scored moments earlier. Whoever went
          last was the only source whose results survived.
        * **Trust a single miss.** We see one page of results. A listing that
          scrolls off page one has not been sold, and Facebook shuffles
          constantly. So misses are COUNTED, and only a listing absent from
          `threshold` consecutive runs of its own source is retired.
        """
        if not seen_ids:
            return 0
        marks = ",".join("?" * len(seen_ids))
        keep = "status NOT IN ('gone','saved','contacted')"
        of_source = "listing_id IN (SELECT id FROM listings WHERE source=?)"

        # Seen again: reset the miss counter, and un-retire it. A listing that
        # dropped off page one for a few runs was not sold, and Facebook
        # reshuffles constantly -- without this, something that had reached your
        # wants bin was hidden permanently the moment it briefly vanished.
        self.conn.execute(
            f"""UPDATE hunt_matches
                SET miss_count = 0,
                    status = CASE WHEN status='gone'
                                  THEN COALESCE(status_before_gone, 'new')
                                  ELSE status END,
                    status_before_gone = NULL
                WHERE hunt_id=? AND listing_id IN ({marks})""",
            [hunt_id, *seen_ids])
        self.conn.execute(
            f"""UPDATE hunt_matches SET miss_count = miss_count + 1
                WHERE hunt_id=? AND {keep} AND {of_source}
                  AND listing_id NOT IN ({marks})""",
            [hunt_id, source, *seen_ids])
        cur = self.conn.execute(
            f"""UPDATE hunt_matches
                SET status_before_gone = status, status='gone', updated_at=?
                WHERE hunt_id=? AND {keep} AND {of_source}
                  AND miss_count >= ?""",
            [_now(), hunt_id, source, threshold])
        return cur.rowcount

    # --- hunt matches -------------------------------------------------------

    def mark_matches(self, hunt_id: str, listings: Iterable[Listing]) -> None:
        now = _now()
        self.conn.executemany(
            """INSERT INTO hunt_matches (hunt_id, listing_id, matched_at, status, updated_at)
               VALUES (?,?,?,'new',?)
               ON CONFLICT(hunt_id, listing_id) DO NOTHING""",
            [(hunt_id, l.id, now, now) for l in listings],
        )

    def record_rejections(self, hunt_id: str, rejected: Sequence[tuple[str, str]]) -> None:
        now = _now()
        # A filter outcome may only replace "not yet judged". Everything else --
        # scored, surfaced, saved, dismissed, contacted -- is a real outcome that
        # outranks it. Without the guard, a surfaced listing that is unchanged on
        # the next run gets demoted to `filtered` and silently drops out of the
        # feed before it is ever triaged.
        self.conn.executemany(
            """UPDATE hunt_matches SET status='filtered', filter_reason=?, updated_at=?
               WHERE hunt_id=? AND listing_id=? AND status IN ('new','filtered')""",
            [(reason, now, hunt_id, lid) for lid, reason in rejected],
        )

    def set_status(self, hunt_id: str, listing_id: str, status: str,
                   note: str | None = None) -> None:
        self.conn.execute(
            "UPDATE hunt_matches SET status=?, dismiss_note=?, updated_at=? "
            "WHERE hunt_id=? AND listing_id=?",
            (status, note, _now(), hunt_id, listing_id),
        )

    def was_notified(self, hunt_id: str, listing_id: str) -> bool:
        row = self.conn.execute(
            "SELECT notified_at FROM hunt_matches WHERE hunt_id=? AND listing_id=?",
            (hunt_id, listing_id)).fetchone()
        return bool(row and row["notified_at"])

    def mark_notified(self, hunt_id: str, listing_id: str) -> None:
        self.conn.execute(
            "UPDATE hunt_matches SET notified_at=? WHERE hunt_id=? AND listing_id=?",
            (_now(), hunt_id, listing_id))

    def dismissed_titles(self, hunt_id: str, limit: int = 20) -> list[str]:
        """What you have rejected for this hunt, newest first.

        These become negative examples in the scoring prompt, which is the only
        thing that makes the dismiss button worth pressing: without it you are
        shown the same category of junk tomorrow."""
        return [r["title"] for r in self.conn.execute(
            """SELECT l.title FROM hunt_matches m JOIN listings l ON l.id = m.listing_id
               WHERE m.hunt_id = ? AND m.status = 'dismissed'
               ORDER BY m.updated_at DESC LIMIT ?""", (hunt_id, limit))]

    def statuses(self, hunt_id: str) -> dict[str, str]:
        return {r["listing_id"]: r["status"] for r in self.conn.execute(
            "SELECT listing_id, status FROM hunt_matches WHERE hunt_id=?", (hunt_id,))}

    # --- scores -------------------------------------------------------------

    def save_score(self, score: Score, priced_at_cents: int | None) -> None:
        self.conn.execute(
            """INSERT INTO scores (listing_id, hunt_id, model, scored_at, match,
                 deal_score, unknowns, requirements, est_value_cents, condition,
                 matched_want, worth_grabbing, needs_images, image_question,
                 images_checked, red_flags, reasoning, priced_at_cents,
                 input_tokens, output_tokens, cache_read_tokens, cost_usd)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (score.listing_id, score.hunt_id, score.model,
             score.scored_at.isoformat(timespec="seconds"), score.match,
             score.deal_score, json.dumps(list(score.unknowns)),
             json.dumps(list(score.requirements)), score.est_value_cents,
             score.condition, score.matched_want, int(score.worth_grabbing),
             int(score.needs_images), score.image_question,
             int(score.images_checked),
             json.dumps(list(score.red_flags)), score.reasoning,
             priced_at_cents, score.input_tokens, score.output_tokens,
             score.cache_read_tokens, score.cost_usd),
        )

    def last_scores(self, hunt_id: str) -> dict[str, sqlite3.Row]:
        """Most recent score per listing for this hunt."""
        return {r["listing_id"]: r for r in self.conn.execute(
            """SELECT s.* FROM scores s
               JOIN (SELECT listing_id, MAX(id) AS mx FROM scores
                     WHERE hunt_id=? GROUP BY listing_id) m
                 ON s.id = m.mx""", (hunt_id,))}

    # --- settings -----------------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
