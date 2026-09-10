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

from .models import Hunt, Listing, Score, StoredWant, UpsertResult, Want

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
  -- 0 once the listing is known to be off the market. Until the re-check pass
  -- existed nothing ever set this to 0, so every row said 1 forever.
  is_active     INTEGER NOT NULL DEFAULT 1,
  -- When we CONFIRMED it was no longer available, and how we know. `sold` is
  -- the source saying so outright; `removed` is the item page no longer
  -- resolving, which is usually a sale but the source did not say. Both are
  -- different from `gone`, which only means it stopped appearing in results.
  sold_at       TEXT,
  sold_reason   TEXT,
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
  -- Paces the re-check pass: one detail fetch per listing per interval, so a
  -- bin of forty things cannot turn into forty requests every run.
  rechecked_at  TEXT,
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
  error         TEXT,
  -- A run that fetched, judged and surfaced, but lost something on the way --
  -- detail fetches cut short, appraisal interrupted by a rate limit. Kept
  -- apart from `error` because `last_success_at` reads that column: a degraded
  -- run recorded as failed makes every tick "due", so the cadence collapses to
  -- the timer period against a source that is already throttling us.
  warning       TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_hunt ON runs(hunt_id, started_at);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Wants lived only in config.yaml, which meant adding one needed an ssh session
-- and a service restart. They live here so the dashboard can edit them;
-- config.yaml seeds this table once and is never read for wants again. Same
-- reasoning as the pause switches: two writers on one committed file is how you
-- lose a comment, or a whole want.
CREATE TABLE IF NOT EXISTS wants (
  name            TEXT PRIMARY KEY,       -- also the hunt id: want:<name>
  description     TEXT NOT NULL,
  max_price_cents INTEGER NOT NULL DEFAULT 0,
  queries         TEXT NOT NULL DEFAULT '[]',
  requires        TEXT NOT NULL DEFAULT '[]',
  origin          TEXT NOT NULL DEFAULT 'web',   -- 'config' when seeded from the file
  -- Soft delete. Nothing is deleted here either: a removed want keeps its rows
  -- so /hunt/want:<name> still explains everything it ever matched, and the
  -- name stays taken so a seed cannot resurrect it.
  archived_at     TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
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
                          ("previous_price_cents", "INTEGER"),
                          ("sold_at", "TEXT"),
                          ("sold_reason", "TEXT"))),
            ("hunt_matches", (("miss_count", "INTEGER NOT NULL DEFAULT 0"),
                              ("status_before_gone", "TEXT"),
                              ("notified_at", "TEXT"),
                              ("rechecked_at", "TEXT"))),
            ("runs", (("warning", "TEXT"),
                      ("n_worth_a_look", "INTEGER NOT NULL DEFAULT 0"),
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
                 "error", "warning")]
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
                 -- Craigslist only reveals postedDate on the item page, so
                 -- leaving these out of the UPDATE meant every Craigslist
                 -- listing had no age: no "listed 12d ago", no motivated-seller
                 -- flag, and nothing for an age filter to work with.
                 posted_at    = COALESCE(?, posted_at),
                 category     = COALESCE(?, category),
                 seller_id    = COALESCE(?, seller_id),
                 seller_name  = COALESCE(?, seller_name),
                 images       = CASE WHEN json_array_length(?) >= json_array_length(images)
                                     THEN ? ELSE images END,
                 -- NOT a plain `is_active = 1`. Facebook keeps showing sold
                 -- items in search results, so seeing one again is not
                 -- evidence it is back on the market -- and a straight 1 here
                 -- would quietly resurrect everything the re-check retired.
                 last_seen    = ?,
                 is_active    = CASE WHEN sold_at IS NULL THEN 1 ELSE 0 END,
                 raw = ?
               WHERE id = ?""",
            (listing.title, listing.description, listing.price_cents,
             listing.previous_price_cents, listing.url,
             listing.city, listing.lat, listing.lng, listing.distance_mi,
             listing.dup_key,
             listing.posted_at.isoformat() if listing.posted_at else None,
             listing.category, listing.seller_id, listing.seller_name,
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

    def record_price(self, listing_id: str, price_cents: int | None,
                     observed_at: str | None = None) -> None:
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
        # `observed_at` is for backfills and for seed-demo, which needs a
        # history that spans weeks rather than one instant; the pipeline never
        # passes it.
        self.conn.execute(
            "INSERT INTO price_observations (listing_id, observed_at, price_cents) VALUES (?,?,?)",
            (listing_id, observed_at or _now(), price_cents),
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
                    status = CASE
                        WHEN status='gone' AND listing_id NOT IN (
                             SELECT id FROM listings WHERE sold_at IS NOT NULL)
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
        # COALESCE, because `note` defaults to None and the pipeline never
        # passes one: without it every status transition wiped the note that
        # explained the previous one. Dismissing with a reason and later saving
        # the same listing erased the reason. A note is only ever replaced by
        # another note.
        self.conn.execute(
            "UPDATE hunt_matches SET status=?, "
            "dismiss_note=COALESCE(?, dismiss_note), updated_at=? "
            "WHERE hunt_id=? AND listing_id=?",
            (status, note, _now(), hunt_id, listing_id),
        )

    def due_for_recheck(self, statuses: Sequence[str], older_than: str | None,
                        limit: int) -> list[tuple[str, str, Listing]]:
        """Listings in a bin whose availability has not been confirmed lately.

        Only things you might actually act on: re-checking the whole store
        would be hundreds of requests against sources that throttle, to learn
        something about listings nobody will look at. Never-checked first, then
        oldest, so a backlog drains in a stable order rather than starving one
        listing forever.
        """
        marks = ",".join("?" * len(statuses))
        # `older_than=None` means every one of them, however recently checked.
        # Timestamps are second-granular, so a cutoff of "now" would exclude a
        # listing stamped in this same second -- which is exactly what a manual
        # "check them all" run does.
        paced = "AND (m.rechecked_at IS NULL OR m.rechecked_at < ?)"
        rows = self.conn.execute(
            f"""SELECT m.hunt_id, m.status, l.* FROM hunt_matches m
                JOIN listings l ON l.id = m.listing_id
                WHERE m.status IN ({marks})
                  AND l.sold_at IS NULL
                  {paced if older_than is not None else ""}
                ORDER BY m.rechecked_at IS NOT NULL, m.rechecked_at
                LIMIT ?""",
            [*statuses, *( [older_than] if older_than is not None else [] ), limit]
        ).fetchall()
        return [(r["hunt_id"], r["status"], self._row_to_listing(r)) for r in rows]

    def retire_sold(self, listing_id: str) -> int:
        """Take a listing known to be off the market out of every bin.

        Every bin: one listing can match several hunts, and marking only the
        hunt that happened to re-check it would leave the same sold couch
        sitting in another tab. `saved` and `contacted` are deliberately
        untouched -- those are the user's own decisions, and something they
        saved should be marked sold, not quietly removed from their list.
        `new` and `scored` are included so nothing pays to judge it later.
        """
        cur = self.conn.execute(
            """UPDATE hunt_matches
               SET status_before_gone = COALESCE(status_before_gone, status),
                   status = 'gone', updated_at = ?
               WHERE listing_id = ?
                 AND status IN ('wanted', 'free_find', 'new', 'scored')""",
            (_now(), listing_id))
        return cur.rowcount

    def mark_rechecked(self, hunt_id: str, listing_id: str) -> None:
        self.conn.execute(
            "UPDATE hunt_matches SET rechecked_at=? WHERE hunt_id=? AND listing_id=?",
            (_now(), hunt_id, listing_id))

    def mark_sold(self, listing_id: str, reason: str) -> None:
        """Record that a listing is off the market. Stamped once: the first
        confirmation is the honest one, and a later re-check cannot move the
        date around."""
        self.conn.execute(
            """UPDATE listings SET sold_at = COALESCE(sold_at, ?),
                                   sold_reason = COALESCE(sold_reason, ?),
                                   is_active = 0
               WHERE id = ?""", (_now(), reason, listing_id))

    def pending_notifications(self, hunt_id: str, limit: int = 50
                              ) -> list[tuple[Listing, Score]]:
        """Everything sitting in a bin that has never been announced.

        The notifier used to see only what the CURRENT run surfaced, which meant
        a listing was announced or lost forever. Anything that entered a bin
        before notifications were switched on, or fell past the per-run cap, or
        hit a transient send failure, was never revisited -- on the next run it
        is `unchanged`, so it never surfaces again. Two TV stands, the whole
        point of the thing, sat un-announced because of it.

        With this, the cap DEFERS rather than drops.
        """
        rows = self.conn.execute(
            """SELECT l.*, s.* FROM hunt_matches m
               JOIN listings l ON l.id = m.listing_id
               JOIN scores  s ON s.id = (SELECT MAX(id) FROM scores
                    WHERE hunt_id = m.hunt_id AND listing_id = m.listing_id)
               WHERE m.hunt_id = ? AND m.notified_at IS NULL
                 AND m.status IN ('wanted', 'free_find')
               ORDER BY s.deal_score DESC LIMIT ?""", (hunt_id, limit)).fetchall()
        return [(self._row_to_listing(r), self._row_to_score(r)) for r in rows]

    @staticmethod
    def _row_to_listing(r: sqlite3.Row) -> Listing:
        return Listing(
            id=r["id"], source=r["source"], source_id=r["source_id"],
            title=r["title"], description=r["description"],
            price_cents=r["price_cents"],
            previous_price_cents=r["previous_price_cents"],
            currency=r["currency"], url=r["url"], city=r["city"],
            lat=r["lat"], lng=r["lng"], distance_mi=r["distance_mi"],
            seller_id=r["seller_id"], seller_name=r["seller_name"],
            images=tuple(json.loads(r["images"] or "[]")),
            category=r["category"],
            posted_at=(datetime.fromisoformat(r["posted_at"])
                       if r["posted_at"] else None))

    @staticmethod
    def _row_to_score(r: sqlite3.Row) -> Score:
        return Score(
            listing_id=r["listing_id"], hunt_id=r["hunt_id"], model=r["model"],
            scored_at=datetime.fromisoformat(r["scored_at"]),
            match=r["match"], deal_score=r["deal_score"],
            est_value_cents=r["est_value_cents"], condition=r["condition"],
            matched_want=r["matched_want"],
            worth_grabbing=bool(r["worth_grabbing"]),
            unknowns=tuple(json.loads(r["unknowns"] or "[]")),
            requirements=tuple(json.loads(r["requirements"] or "[]")),
            red_flags=tuple(json.loads(r["red_flags"] or "[]")),
            reasoning=r["reasoning"], images_checked=bool(r["images_checked"]))

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

    # --- wants --------------------------------------------------------------

    def seed_wants(self, wants: Iterable[Want]) -> int:
        """Copy config.yaml's wants into the table, ONCE ever.

        After this the table is the truth and the file is history. Seeding again
        on every start would undo every edit made from the phone, and seeding
        per-name would resurrect a want deleted from the web the moment the
        file still mentioned it. So it is a one-shot, remembered in `settings`.
        """
        if self.get_setting("wants.seeded") == "1":
            return 0
        n = sum(self.save_want(w, origin="config") for w in wants)
        self.set_setting("wants.seeded", "1")
        return n

    def save_want(self, want: Want, *, origin: str = "web") -> int:
        """Create or update one want. `created_at` and `origin` survive an edit."""
        now = _now()
        cur = self.conn.execute(
            """INSERT INTO wants (name, description, max_price_cents, queries,
                                  requires, origin, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 description=excluded.description,
                 max_price_cents=excluded.max_price_cents,
                 queries=excluded.queries,
                 requires=excluded.requires,
                 updated_at=excluded.updated_at""",
            (want.name, want.description, want.max_price_cents,
             json.dumps(list(want.queries)), json.dumps(list(want.requires)),
             origin, now, now))
        return cur.rowcount or 0

    def archive_want(self, name: str) -> None:
        """Retire a want. Its hunt stops running; everything it matched stays."""
        self.conn.execute(
            "UPDATE wants SET archived_at=?, updated_at=? WHERE name=?",
            (_now(), _now(), name))

    def restore_want(self, name: str) -> None:
        self.conn.execute(
            "UPDATE wants SET archived_at=NULL, updated_at=? WHERE name=?",
            (_now(), name))

    def wants(self, include_archived: bool = False) -> list[StoredWant]:
        sql = "SELECT * FROM wants"
        if not include_archived:
            sql += " WHERE archived_at IS NULL"
        sql += " ORDER BY name"
        return [self._row_to_stored_want(r) for r in self.conn.execute(sql)]

    def get_want(self, name: str) -> StoredWant | None:
        r = self.conn.execute("SELECT * FROM wants WHERE name=?", (name,)).fetchone()
        return self._row_to_stored_want(r) if r else None

    @staticmethod
    def _row_to_stored_want(r: sqlite3.Row) -> StoredWant:
        return StoredWant(
            want=Want(
                name=r["name"],
                description=r["description"],
                max_price_cents=r["max_price_cents"],
                queries=tuple(json.loads(r["queries"] or "[]")),
                requires=tuple(json.loads(r["requires"] or "[]")),
            ),
            origin=r["origin"],
            archived_at=r["archived_at"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    # --- settings -----------------------------------------------------------

    def hunt_intervals(self) -> dict[str, int]:
        """Per-hunt cadence set from the dashboard, overriding config.yaml.

        Same home and same reasoning as the pause switches. A value that will
        not parse is ignored rather than raised on: a bad settings row must not
        be able to stop the timer."""
        out: dict[str, int] = {}
        for r in self.conn.execute(
                "SELECT key, value FROM settings WHERE key LIKE 'hunt_interval:%'"):
            try:
                minutes = int(r["value"])
            except (TypeError, ValueError):
                continue
            if minutes > 0:
                out[r["key"].split(":", 1)[1]] = minutes
        return out

    def set_hunt_interval(self, hunt_id: str, minutes: int) -> None:
        self.set_setting(f"hunt_interval:{hunt_id}", str(int(minutes)))


    def hunt_enabled(self, hunt_id: str) -> bool:
        """A runtime override on top of config.yaml's `enabled`.

        It lives in the database rather than the config file so the dashboard
        can flip it: config.yaml is committed and hand-edited, and having two
        writers on it invites losing a comment or a whole want."""
        return self.get_setting(f"hunt_disabled:{hunt_id}") != "1"

    def set_hunt_enabled(self, hunt_id: str, enabled: bool) -> None:
        self.set_setting(f"hunt_disabled:{hunt_id}", "0" if enabled else "1")

    def disabled_hunts(self) -> set[str]:
        return {r["key"].split(":", 1)[1] for r in self.conn.execute(
            "SELECT key FROM settings WHERE key LIKE 'hunt_disabled:%' AND value='1'")}

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
