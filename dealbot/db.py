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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

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
  img_key       TEXT,
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
  -- When YOU went and got it, and what you actually handed over. The user's
  -- facts, not the source's: `upsert_listing` deliberately does not know these
  -- columns, so no amount of re-fetching can overwrite a purchase.
  --
  -- `paid_cents` is nullable and 0 is not the same answer. 0 means free, which
  -- is most of what this bot finds; NULL means you did not write it down. A
  -- forgotten figure recorded as free would be a calibration point that lies,
  -- and the whole reason these columns exist is that nothing else in the
  -- database can check what the model claims a thing is worth.
  grabbed_at    TEXT,
  paid_cents    INTEGER,
  -- The source's own version stamp, where it states one. Craigslist's item
  -- endpoint serves a cache that does not converge -- two fetches minutes
  -- apart returned a seller's pre-edit and post-edit copies of one posting --
  -- and `updatedDate` is what tells them apart, so an older payload can be
  -- refused instead of ping-ponging the price and buying an appraisal each
  -- time it swings down.
  source_updated_at TEXT,
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
  status        TEXT NOT NULL,          -- new|filtered|scored|wanted|free_find|saved|grabbed|dismissed|archived|gone
  filter_reason TEXT,
  dismiss_note  TEXT,
  miss_count    INTEGER NOT NULL DEFAULT 0,
  status_before_gone TEXT,
  notified_at   TEXT,
  -- Paces the re-check pass: one detail fetch per listing per interval, so a
  -- bin of forty things cannot turn into forty requests every run.
  rechecked_at  TEXT,
  -- The price we last told you about. A saved listing dropping from $200 to
  -- $120 is the best message this bot can send, and every observation needed
  -- to notice it was already on disk and unread.
  alerted_price_cents INTEGER,
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
  -- $0 in the price box, money asked for in the words. Structured rather than
  -- left inside `red_flags`, because the CARD has to stop saying FREE and
  -- string-matching the model's prose to decide that would be a coin toss.
  price_unclear      INTEGER NOT NULL DEFAULT 0,
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

-- Which search term found which listing, for each hunt. Every term costs one
-- request per source per run whether or not it finds anything, and without
-- this there was no way to tell a term that earns that from one whose every
-- find another term also made. One row per (hunt, term, listing), so it grows
-- with the listings rather than with the runs.
CREATE TABLE IF NOT EXISTS query_hits (
  hunt_id    TEXT NOT NULL,
  query      TEXT NOT NULL,
  listing_id TEXT NOT NULL,
  first_at   TEXT NOT NULL,
  last_at    TEXT NOT NULL,
  PRIMARY KEY (hunt_id, query, listing_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(when: datetime | None) -> str | None:
    return when.isoformat() if when else None


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
            ("scores", (("price_unclear", "INTEGER NOT NULL DEFAULT 0"),
                        ("match", "TEXT NOT NULL DEFAULT 'unknown'"),
                        ("unknowns", "TEXT NOT NULL DEFAULT '[]'"),
                        ("requirements", "TEXT NOT NULL DEFAULT '[]'"),
                        ("worth_grabbing", "INTEGER NOT NULL DEFAULT 0"),
                        ("needs_images", "INTEGER NOT NULL DEFAULT 0"),
                        ("image_question", "TEXT"),
                        ("images_checked", "INTEGER NOT NULL DEFAULT 0"))),
            ("listings", (("dup_key", "TEXT"),
                          ("img_key", "TEXT"),
                          ("previous_price_cents", "INTEGER"),
                          ("sold_at", "TEXT"),
                          ("sold_reason", "TEXT"),
                          ("grabbed_at", "TEXT"),
                          ("paid_cents", "INTEGER"),
                          ("source_updated_at", "TEXT"))),
            ("hunt_matches", (("miss_count", "INTEGER NOT NULL DEFAULT 0"),
                              ("status_before_gone", "TEXT"),
                              ("status_before_archive", "TEXT"),
                              ("notified_at", "TEXT"),
                              ("rechecked_at", "TEXT"),
                              ("alerted_price_cents", "INTEGER"))),
            ("runs", (("warning", "TEXT"),
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
        # `runs` is asked twice on every page render now -- the health pill
        # wants what was judged in the last hour -- and the only index it had
        # was (hunt_id, started_at), whose second column no clause here can
        # reach. A table nothing ever deletes from gets scanned forever.
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_runs_started "
                          "ON runs(started_at)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_listings_dup_key "
                          "ON listings(dup_key)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_listings_img_key "
                          "ON listings(img_key)")
        # The bin views filter on status ALONE, without a hunt_id, so
        # ix_matches_status(hunt_id, status) never applied to them -- every bin
        # page scanned hunt_matches. Worse, the one-row-per-listing subquery
        # matches on listing_id, the SECOND column of the primary key, so it
        # could only be served by a full index scan plus a sort, once per
        # candidate row. That is quadratic in a table nothing ever deletes from.
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_matches_listing "
                          "ON hunt_matches(listing_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_matches_bin "
                          "ON hunt_matches(status)")

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

    # Asked of the table rather than retyped: this list used to be a fourth
    # copy of the `runs` columns, after SCHEMA, _migrate and RunResult, and a
    # counter missing from it was written nowhere with nothing raised.
    _RUN_SET_NEVER = frozenset(("id", "hunt_id", "source",
                                "started_at", "finished_at"))

    def _run_cols(self) -> frozenset[str]:
        cached = getattr(self._local, "run_cols", None)
        if cached is None:
            cached = frozenset(
                r["name"] for r in self.conn.execute("PRAGMA table_info(runs)")
            ) - self._RUN_SET_NEVER
            self._local.run_cols = cached
        return cached

    def finish_run(self, run_id: int, **fields: Any) -> None:
        settable = self._run_cols()
        cols = [k for k in fields if k in settable]
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
            "SELECT price_cents, first_seen, source_updated_at "
            "FROM listings WHERE id=?", (listing.id,)).fetchone()

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
                     fingerprint, dup_key, img_key, first_seen, last_seen,
                     is_active, source_updated_at, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                (listing.id, listing.source, listing.source_id, listing.title,
                 listing.description, listing.price_cents,
                 listing.previous_price_cents, listing.currency,
                 listing.url, listing.city, listing.lat, listing.lng,
                 listing.distance_mi, listing.seller_id, listing.seller_name,
                 json.dumps(list(listing.images)), listing.category,
                 listing.posted_at.isoformat() if listing.posted_at else None,
                 listing.fingerprint, listing.dup_key, listing.image_key,
                 now, now,
                 _iso(listing.source_updated_at),
                 json.dumps(listing.raw)),
            )
            return UpsertResult(listing.id, True, False, None, is_relist,
                                first_seen=now)

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
                 -- COALESCE for the same reason as the rest: Facebook's search
                 -- feed carries one photo and no coordinates, so the key only
                 -- becomes computable at detail time.
                 img_key      = COALESCE(?, img_key),
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
                 -- The fourth edit, and COALESCE for the usual reason: only
                 -- the item page states this, so an index-only refresh must
                 -- not wipe it. Craigslist's own cache goes backwards, but
                 -- `detail` refuses an older payload before it ever reaches
                 -- here, so this only ever moves forward.
                 source_updated_at = COALESCE(?, source_updated_at),
                 raw = ?
               WHERE id = ?""",
            (listing.title, listing.description, listing.price_cents,
             listing.previous_price_cents, listing.url,
             listing.city, listing.lat, listing.lng, listing.distance_mi,
             listing.dup_key, listing.image_key,
             listing.posted_at.isoformat() if listing.posted_at else None,
             listing.category, listing.seller_id, listing.seller_name,
             json.dumps(list(listing.images)), json.dumps(list(listing.images)),
             now, _iso(listing.source_updated_at),
             json.dumps(listing.raw), listing.id),
        )
        # The stamp as it was BEFORE this write: that is what the enrichment
        # about to happen must compare a fresh payload against.
        return UpsertResult(listing.id, False, changed, previous, False,
                            first_seen=row["first_seen"],
                            source_updated_at=row["source_updated_at"])

    def record_query_hits(self, hunt_id: str,
                          hits: Mapping[str, Iterable[str]]) -> None:
        """Note which term found which listing this run. See `query_yield`."""
        now = _now()
        self.conn.executemany(
            """INSERT INTO query_hits (hunt_id, query, listing_id, first_at, last_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT (hunt_id, query, listing_id)
               DO UPDATE SET last_at = excluded.last_at""",
            [(hunt_id, q, lid, now, now)
             for q, ids in hits.items() for lid in ids])

    def query_yield(self, hunt_id: str, queries: Sequence[str],
                    bar: float) -> dict[str, dict]:
        """Per search term: how many listings it has found, how many of those
        no OTHER current term found, and how many of those cleared the bar.

        "Other" means the terms the want has now. A term you deleted found
        things too, but it is not searched any more, so its finds cannot make
        a remaining term look redundant.

        `since` is when the term's first find was recorded. A term added
        yesterday with nothing to its name has not had a fair chance yet, and
        the editor has to be able to say so.
        """
        if not queries:
            return {}
        marks = ",".join("?" * len(queries))
        rows = self.conn.execute(
            f"""WITH h AS (SELECT query, listing_id, first_at FROM query_hits
                           WHERE hunt_id = ? AND query IN ({marks})),
                    n AS (SELECT listing_id, COUNT(*) AS terms FROM h
                          GROUP BY listing_id)
               SELECT h.query, COUNT(*) AS found, MIN(h.first_at) AS since,
                      SUM(n.terms = 1) AS only,
                      SUM(n.terms = 1 AND s.match IN ('yes', 'unknown')
                          AND s.deal_score >= ?) AS good
               FROM h JOIN n USING (listing_id)
               LEFT JOIN scores s ON s.id = (
                   SELECT MAX(id) FROM scores
                   WHERE hunt_id = ? AND listing_id = h.listing_id)
               GROUP BY h.query""",
            (hunt_id, *queries, bar, hunt_id)).fetchall()
        return {r["query"]: {"found": r["found"], "only": r["only"] or 0,
                             "good": r["good"] or 0, "since": r["since"]}
                for r in rows}

    def listing(self, listing_id: str) -> Listing | None:
        """One listing as stored: whatever the search feed last said, plus
        everything enrichment has filled in. `raw` is not rebuilt, so this is
        for READING -- upserting it back would overwrite the stored payload."""
        row = self.conn.execute("SELECT * FROM listings WHERE id=?",
                                (listing_id,)).fetchone()
        return self.row_to_listing(row) if row else None

    def scored_duplicate(self, hunt_id: str, dup_key: str | None,
                         exclude_id: str,
                         img_key: str | None = None) -> str | None:
        """A different listing with the same physical identity that this hunt
        has already judged. Cross-posting to both marketplaces is common, and
        without this you pay to appraise the same item twice and see it twice.

        EITHER identity is enough. `dup_key` is title+price+place and catches
        the cross-post; `img_key` is photo+place and catches the repost, which
        `dup_key` misses whenever the seller changes anything it hashes -- one
        gas stove was posted twice three minutes apart and reached Discord
        twice because Craigslist called it $0 once and priceless the other.

        A None key matches nothing rather than everything: SQL equality against
        NULL is never true, which is the behaviour wanted here.
        """
        row = self.conn.execute(
            """SELECT l.id FROM listings l
               JOIN scores s ON s.listing_id = l.id AND s.hunt_id = ?
               WHERE (l.dup_key = ? OR l.img_key = ?) AND l.id <> ? LIMIT 1""",
            (hunt_id, dup_key, img_key, exclude_id)).fetchone()
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
        # `grabbed` joins `saved` here for the same reason: both are the user's
        # own decision about a listing, and a thing sitting in their garage
        # must not be retired because the seller took the post down.
        keep = "status NOT IN ('gone','saved','grabbed','archived')"
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
                    -- Cleared because it has just been used, or was stale.
                    -- NOT on a `grabbed` row: there it is what undo restores,
                    -- and Facebook goes on showing a listing after it sells.
                    status_before_gone = CASE WHEN status='grabbed'
                                              THEN status_before_gone END
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
        # scored, surfaced, saved, dismissed, grabbed -- is a real outcome that
        # outranks it. Without the guard, a surfaced listing that is unchanged on
        # the next run gets demoted to `filtered` and silently drops out of the
        # feed before it is ever triaged.
        self.conn.executemany(
            """UPDATE hunt_matches SET status='filtered', filter_reason=?, updated_at=?
               WHERE hunt_id=? AND listing_id=? AND status IN ('new','filtered')""",
            [(reason, now, hunt_id, lid) for lid, reason in rejected],
        )

    def set_status(self, hunt_id: str, listing_id: str, status: str,
                   note: str | None = None) -> int:
        """Returns the number of rows changed, which the dashboard checks.

        A triage that matches nothing used to be a silent no-op. That was
        survivable while every button reloaded the page -- you saw the listing
        still sitting there -- and became a lie the moment the card started
        folding away and a toast started saying "Dismissed"."""
        # COALESCE, because `note` defaults to None and the pipeline never
        # passes one: without it every status transition wiped the note that
        # explained the previous one. Dismissing with a reason and later saving
        # the same listing erased the reason. A note is only ever replaced by
        # another note.
        return self.conn.execute(
            "UPDATE hunt_matches SET status=?, "
            "dismiss_note=COALESCE(?, dismiss_note), updated_at=? "
            "WHERE hunt_id=? AND listing_id=?",
            (status, note, _now(), hunt_id, listing_id),
        ).rowcount

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
        return [(r["hunt_id"], r["status"], self.row_to_listing(r)) for r in rows]

    def retire_sold(self, listing_id: str) -> int:
        """Take a listing known to be off the market out of every bin.

        Every bin: one listing can match several hunts, and marking only the
        hunt that happened to re-check it would leave the same sold couch
        sitting in another tab. `saved` and `grabbed` are deliberately
        untouched -- those are the user's own decisions, and something they
        saved should be marked sold, not quietly removed from their list.
        `new` and `scored` are included so nothing pays to judge it later.

        `mark_grabbed` calls this too, which is what clears the same listing
        out of every OTHER hunt's bin when you go and collect it.
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

    def mark_grabbed(self, hunt_id: str, listing_id: str,
                     paid_cents: int | None) -> int:
        """Record that you went and got this thing.

        Assembled out of what already exists rather than new SQL, because every
        one of these steps is a rule written down somewhere else:

        1. the stamps. `grabbed_at` COALESCEs like `sold_at` -- the first time
           you said so is the honest date -- while `paid_cents` is a plain set,
           so correcting a figure lands.
        2. the status, on the row you acted on ONLY, remembering what it was.
           A listing matched by two hunts keeps the other hunt's decision
           intact, and undo therefore has one unambiguous thing to restore.
           The button started life on `/saved` alone, where "put it back"
           could only mean `saved`; it is on the listing page now, where the
           row you act on may be `wanted` or `free_find`, and undoing a mis-tap
           must not quietly move a free find onto your saved list.
        3. `mark_sold(..., 'grabbed')`. It is genuinely off the market, and this
           is the step that does most of the work: `due_for_recheck` and
           `price_drops` both already filter on `l.sold_at IS NULL`, so no
           request and no alert is ever spent on a thing in your garage. No new
           guard needed in either.
        4. `retire_sold`, which clears the same listing out of every OTHER
           hunt's bin while leaving `saved` and `grabbed` alone.

        Returns the rows the status change touched, which the dashboard checks
        the same way it checks `set_status`.
        """
        self.conn.execute(
            """UPDATE listings SET grabbed_at = COALESCE(grabbed_at, ?),
                                   paid_cents = ?
               WHERE id = ?""", (_now(), paid_cents, listing_id))
        # COALESCE so grabbing twice cannot overwrite the answer with
        # `grabbed`, which would make undo a no-op.
        self.conn.execute(
            """UPDATE hunt_matches
               SET status_before_gone = COALESCE(status_before_gone, status)
               WHERE hunt_id=? AND listing_id=? AND status <> 'grabbed'""",
            (hunt_id, listing_id))
        changed = self.set_status(hunt_id, listing_id, "grabbed")
        self.mark_sold(listing_id, "grabbed")
        self.retire_sold(listing_id)
        return changed

    def ungrab(self, hunt_id: str, listing_id: str) -> int:
        """Undo a grab, all the way down.

        A mis-tap must not leave a permanent purchase behind, so unlike
        `mark_sold` this really does clear the stamps. The `sold_reason`
        condition is the part that matters: a listing the re-check pass found
        genuinely sold, that you then grabbed and un-grabbed, must stay sold.
        Only a stamp THIS wrote is withdrawn.

        The other hunts' rows need no unwinding. `mark_gone` already restores a
        `gone` row from `status_before_gone` when the listing is seen again and
        `sold_at` is null, so clearing the stamp is what lets that happen.

        It goes back to whatever it WAS, which `mark_grabbed` wrote down. That
        used to be a flat `saved`, which was true while the button lived only
        on a saved card and stopped being true the moment it appeared on the
        listing page: undoing a mis-tap on a free find moved it to your saved
        list, silently, and nothing said so. `saved` survives as the fallback
        for rows grabbed before this was recorded.
        """
        self.conn.execute(
            """UPDATE listings
               SET grabbed_at = NULL, paid_cents = NULL,
                   sold_at    = CASE WHEN sold_reason='grabbed'
                                     THEN NULL ELSE sold_at END,
                   is_active  = CASE WHEN sold_reason='grabbed'
                                     THEN 1 ELSE is_active END,
                   sold_reason = CASE WHEN sold_reason='grabbed'
                                      THEN NULL ELSE sold_reason END
               WHERE id = ?""", (listing_id,))
        cur = self.conn.execute(
            """UPDATE hunt_matches
               SET status = COALESCE(status_before_gone, 'saved'),
                   status_before_gone = NULL, updated_at = ?
               WHERE hunt_id=? AND listing_id=? AND status='grabbed'""",
            (_now(), hunt_id, listing_id))
        return cur.rowcount

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
        return [(self.row_to_listing(r), self.row_to_score(r)) for r in rows]

    # Public, because the dashboard needs them too: the listing page re-decides
    # `pipeline.route` for one already-judged listing, and rebuilding a Score
    # by hand there would be a third place that knows the column list.
    @staticmethod
    def row_to_listing(r: sqlite3.Row) -> Listing:
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
                       if r["posted_at"] else None),
            source_updated_at=(datetime.fromisoformat(r["source_updated_at"])
                               if r["source_updated_at"] else None))

    @staticmethod
    def row_to_score(r: sqlite3.Row) -> Score:
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
            reasoning=r["reasoning"], images_checked=bool(r["images_checked"]),
            price_unclear=bool(r["price_unclear"]))

    def was_notified(self, hunt_id: str, listing_id: str) -> bool:
        row = self.conn.execute(
            "SELECT notified_at FROM hunt_matches WHERE hunt_id=? AND listing_id=?",
            (hunt_id, listing_id)).fetchone()
        return bool(row and row["notified_at"])

    def mark_notified(self, hunt_id: str, listing_id: str) -> None:
        self.conn.execute(
            "UPDATE hunt_matches SET notified_at=? WHERE hunt_id=? AND listing_id=?",
            (_now(), hunt_id, listing_id))

    # How many over-bar dismissals must pile up before the want is worth
    # rewriting. Counted PER WANT -- three spread across three wants is three
    # different disagreements and says nothing about any of them.
    #
    # Two. One is a fluke; two independent times the want said yes and you said
    # no is the smallest thing that is a pattern. Against the live counts of 9,
    # 2 and 1 that speaks up about `stacked-ottoman` and stays quiet about
    # `ceramic-plant-pots`, which is the right split: one dismissal is not yet
    # evidence that anything is missing.
    OVERRULED_THRESHOLD = 2

    def overruled(self, hunt_id: str, bar: float) -> int:
        """Dismissals of listings this hunt's own bar said were good enough.

        The disagreements, and nothing else. Dismissing something the model
        already scored poorly is you and it agreeing; a rate would not see the
        difference, because every hunt here sits at 97-100% dismissed -- that
        is simply how a bin gets emptied. `want:bookshelf` has 36 dismissals
        and NONE of them over the bar, and it is the want with nothing wrong.

        Each one that is over the bar is a rule the want does not state: the
        requirements said this qualifies and the user said it does not.
        """
        return self.conn.execute(
            """SELECT COUNT(*) n FROM hunt_matches m
               JOIN scores s ON s.id = (SELECT MAX(id) FROM scores
                                        WHERE hunt_id=m.hunt_id
                                          AND listing_id=m.listing_id)
               WHERE m.hunt_id=? AND m.status='dismissed' AND s.deal_score >= ?""",
            (hunt_id, bar)).fetchone()["n"]

    def overruled_baseline(self, hunt_id: str) -> int:
        """What `overruled` stood at when the want was last rewritten.

        The dismissals do not go away when you act on them, so without this the
        nudge would never stop. Saving the want IS the acknowledgement.
        """
        try:
            return int(self.get_setting(f"overruled:{hunt_id}") or 0)
        except (TypeError, ValueError):
            return 0

    def note_want_rewritten(self, hunt_id: str, bar: float) -> None:
        self.set_setting(f"overruled:{hunt_id}", str(self.overruled(hunt_id, bar)))

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

    def filter_reasons(self, hunt_id: str) -> dict[str, str]:
        """Why each listing this hunt filtered was filtered.

        The gate needs it to tell a listing it has never judged from one it
        already rejected for a reason that cannot change. A filtered listing
        has no score, so on the score alone the two look identical -- which is
        how a photoless post got re-fetched and re-dropped on every run for
        four days. See `filters.PERMANENT_REJECTIONS`.
        """
        return {r["listing_id"]: r["filter_reason"] for r in self.conn.execute(
            "SELECT listing_id, filter_reason FROM hunt_matches "
            "WHERE hunt_id=? AND status='filtered' AND filter_reason IS NOT NULL",
            (hunt_id,))}

    # --- scores -------------------------------------------------------------

    def save_score(self, score: Score, priced_at_cents: int | None) -> None:
        self.conn.execute(
            """INSERT INTO scores (listing_id, hunt_id, model, scored_at, match,
                 deal_score, unknowns, requirements, est_value_cents, condition,
                 matched_want, worth_grabbing, price_unclear, needs_images,
                 image_question, images_checked, red_flags, reasoning,
                 priced_at_cents,
                 input_tokens, output_tokens, cache_read_tokens, cost_usd)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (score.listing_id, score.hunt_id, score.model,
             score.scored_at.isoformat(timespec="seconds"), score.match,
             score.deal_score, json.dumps(list(score.unknowns)),
             json.dumps(list(score.requirements)), score.est_value_cents,
             score.condition, score.matched_want, int(score.worth_grabbing),
             int(score.price_unclear),
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

    def price_drops(self, threshold: float = 0.15,
                    statuses: Sequence[str] = ("saved", "wanted", "free_find"),
                    limit: int = 20) -> list[tuple[str, Listing, Score, int]]:
        """Things in a bin that are materially cheaper than when you last heard.

        The baseline is the price we last told you about, falling back to the
        price it carried when it was judged. So a listing that slides $200 ->
        $180 -> $160 announces itself once at $160, not twice, and a second
        alert needs another real drop from there.

        A drop to free is included, and is the headline case: `$500 -> FREE` is
        the strongest signal in the whole dataset.
        """
        rows = self.conn.execute(
            f"""SELECT m.hunt_id, m.listing_id, l.*, s.id AS score_id,
                       COALESCE(m.alerted_price_cents, s.priced_at_cents) AS was
                FROM hunt_matches m
                JOIN listings l ON l.id = m.listing_id
                JOIN scores s ON s.id = (SELECT MAX(id) FROM scores
                                         WHERE hunt_id=m.hunt_id
                                           AND listing_id=m.listing_id)
                WHERE m.status IN ({','.join('?' * len(statuses))})
                  AND l.sold_at IS NULL
                  AND l.price_cents IS NOT NULL
                  AND COALESCE(m.alerted_price_cents, s.priced_at_cents) > 0
                  AND l.price_cents <= ? * COALESCE(m.alerted_price_cents,
                                                    s.priced_at_cents)
                ORDER BY l.last_seen DESC LIMIT ?""",
            (*statuses, 1.0 - threshold, limit)).fetchall()
        if not rows:
            return []
        # The join above already picked the latest score per match, so its id
        # is in hand -- this used to re-query for it once PER ROW, on a path
        # the timer runs every 15 minutes.
        ids = [r["score_id"] for r in rows]
        scores = {sr["id"]: self.row_to_score(sr) for sr in self.conn.execute(
            f"SELECT * FROM scores WHERE id IN ({','.join('?' * len(ids))})",
            ids)}
        return [(r["hunt_id"], self.row_to_listing(r),
                 scores[r["score_id"]], r["was"]) for r in rows]

    def mark_price_alerted(self, hunt_id: str, listing_id: str,
                           price_cents: int) -> None:
        self.conn.execute(
            "UPDATE hunt_matches SET alerted_price_cents=?, updated_at=? "
            "WHERE hunt_id=? AND listing_id=?",
            (price_cents, _now(), hunt_id, listing_id))

    def unjudged(self, hunt_id: str, source: str,
                 limit: int) -> list[Listing]:
        """Listings this hunt collected and never judged, newest first.

        The cap in the pipeline leaves the overflow at `new` on the theory that
        it "drains over the next few runs". It does not: candidates only ever
        came from the CURRENT fetch, and by the next run those listings have
        fallen off page one and are never seen again. 130 of them were stranded
        that way, the oldest 47 hours old.

        Excludes anything confirmed off the market, so a backlog cannot resurrect
        something `recheck` already retired.
        """
        if limit <= 0:
            return []
        return [self.row_to_listing(r) for r in self.conn.execute(
            """SELECT l.* FROM hunt_matches m JOIN listings l ON l.id = m.listing_id
               WHERE m.hunt_id = ? AND m.status = 'new' AND l.source = ?
                 AND l.sold_at IS NULL
               ORDER BY COALESCE(l.posted_at, l.first_seen) DESC
               LIMIT ?""", (hunt_id, source, limit))]

    def unjudged_counts(self) -> dict[str, int]:
        """Per hunt, for the dashboard. Collected and never judged is the one
        failure the runs view could not show you: every individual run looks
        healthy while a third of what arrives is quietly discarded."""
        return {r["hunt_id"]: r["n"] for r in self.conn.execute(
            "SELECT hunt_id, COUNT(*) n FROM hunt_matches WHERE status='new' "
            "GROUP BY hunt_id")}

    # --- stats ----------------------------------------------------------------
    #
    # Every figure on /stats comes from `runs`, not from `scores`, and the
    # difference is not cosmetic: `runs.cost_usd` totals $23.75 over the first
    # five days where `scores.cost_usd` totals $16.58. The gap is the triage
    # pass, which is saved on its score row with `cost_usd = 0` and only ever
    # counted at the run level. `runs` also carries `hunt_id` and `source`, so
    # every split the page shows is both complete and attributable. Sum the
    # score rows instead and a third of the money disappears.

    def spend_since(self, since: str | None) -> dict[str, float]:
        """Total spend and run count since an ISO timestamp, or for all time.

        `since` is compared as a string against `started_at`, which is ISO-8601
        UTC throughout, so lexical order is chronological order.
        """
        where = "WHERE started_at >= ?" if since else ""
        args = (since,) if since else ()
        r = self.conn.execute(
            f"SELECT COALESCE(SUM(cost_usd), 0) usd, COUNT(*) runs, "
            f"COALESCE(SUM(n_scored), 0) scored FROM runs {where}", args
        ).fetchone()
        return {"usd": r["usd"], "runs": r["runs"], "scored": r["scored"]}

    def spend_by_day(self, days: int = 30,
                     offset_minutes: int = 0) -> list[dict[str, Any]]:
        """One row per day, newest last. Days with no run are absent rather
        than zero -- the caller fills the gaps, because a day the bot was off
        and a day it found nothing are different facts and only one of them is
        worth colouring.

        `offset_minutes` shifts each run into the USER's day before bucketing,
        so a bar means the day they lived through rather than the UTC one. It
        is the offset as it stands now, so in the week around a daylight-saving
        change an hour of runs can fall in the neighbouring bar. Both switches
        happen at 2am, so that hour is one the bot is generally asleep for.
        """
        shift = f"{int(offset_minutes)} minutes"
        return [dict(r) for r in self.conn.execute(
            """SELECT date(started_at, ?) day,
                      SUM(cost_usd) usd, COUNT(*) runs, SUM(n_scored) scored,
                      SUM(warning IS NOT NULL OR error IS NOT NULL) degraded
               FROM runs
               WHERE started_at >= date('now', ?)
               GROUP BY day ORDER BY day""",
            (shift, f"-{int(days) + 1} days"))]

    def run_activity(self, minutes: int = 60) -> dict[str, Any]:
        """What the bot has actually been DOING lately, for the health pill.

        `scored` is the figure that answers "is it looking at listings". A bot
        that fetches every 15 minutes and judges nothing is indistinguishable
        from a healthy one from the outside, and that is precisely the state
        both standdowns leave it in.

        `in_flight` is a run begun and not finished, bounded by the same
        window: a process killed mid-pass leaves `finished_at` NULL forever,
        so an unbounded check would say "Looking now" until someone noticed.
        """
        since = (datetime.now(timezone.utc)
                 - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        r = self.conn.execute(
            """SELECT COUNT(*) runs, COALESCE(SUM(n_scored), 0) scored,
                      COALESCE(SUM(finished_at IS NULL), 0) unfinished
               FROM runs WHERE started_at >= ?""", (since,)).fetchone()
        # WHICH pass is in flight, not just that one is: the pill can only fit
        # "Looking now", so the hunt and the source have to be reachable from
        # the page it links to.
        now = None
        if r["unfinished"]:
            row = self.conn.execute(
                """SELECT hunt_id, source, started_at FROM runs
                   WHERE finished_at IS NULL AND started_at >= ?
                   ORDER BY id DESC LIMIT 1""", (since,)).fetchone()
            now = dict(row) if row else None
        return {"runs": r["runs"], "scored": r["scored"],
                "in_flight": now is not None, "now": now}

    def judged_recently(self, limit: int = 12) -> list[dict[str, Any]]:
        """The last listings the model actually looked at, newest first.

        WITH the first-pass drops. They are written as scores with a `:triage`
        model and they count towards `n_scored`, so leaving them out would put
        this list at odds with the figure the health pill states -- and they
        are half of what "what is it judging" means: the cheap pass reads
        every one of them.
        """
        return [dict(r) for r in self.conn.execute(
            """SELECT s.scored_at, s.hunt_id, s.model, s.deal_score,
                      s.listing_id, s.reasoning, l.title, l.source
               FROM scores s JOIN listings l ON l.id = s.listing_id
               ORDER BY s.id DESC LIMIT ?""", (int(limit),))]

    def spend_by_hunt(self, since: str | None = None) -> list[dict[str, Any]]:
        """What each hunt cost and what it actually found.

        The outcome columns are the point. A want's cost means nothing on its
        own -- $3.65 is either cheap or pure waste depending on whether it
        found anything, and on the first five days of live data one want had
        spent that and saved nothing at all.

        The outcome columns are SUBQUERIES, not a join, and that is the whole
        trick. Joining `runs` to `hunt_matches` fans the run rows out once per
        match before `SUM` ever sees them, so every cost on the page would be
        multiplied by however many listings that hunt happened to match. A
        subquery also keeps a hunt with runs and no matches at zero rather than
        dropping it, and those are precisely the rows worth reading.
        """
        where = "WHERE r.started_at >= ?" if since else ""
        args = (since,) if since else ()
        return [dict(r) for r in self.conn.execute(
            f"""SELECT r.hunt_id,
                       SUM(r.cost_usd) usd, SUM(r.n_fetched) fetched,
                       SUM(r.n_scored) scored,
                       SUM(r.n_wanted + r.n_free_find) binned,
                       COUNT(*) runs,
                       (SELECT COUNT(*) FROM hunt_matches m
                         WHERE m.hunt_id = r.hunt_id AND m.status = 'saved') saved,
                       (SELECT COUNT(*) FROM hunt_matches m
                         WHERE m.hunt_id = r.hunt_id
                           AND m.status = 'dismissed') dismissed
                FROM runs r {where}
                GROUP BY r.hunt_id ORDER BY usd DESC""", args)]

    def spend_by_source(self, since: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE started_at >= ?" if since else ""
        args = (since,) if since else ()
        return [dict(r) for r in self.conn.execute(
            f"SELECT source, SUM(cost_usd) usd, SUM(n_fetched) fetched "
            f"FROM runs {where} GROUP BY source ORDER BY usd DESC", args)]

    def spend_by_stage(self, since: str | None = None) -> dict[str, float]:
        """Appraisal, the image pass, and everything else.

        The first two are attributed on the score row. The third is the
        remainder against `runs`, and is labelled as derived wherever it is
        shown, because it is "what the runs cost that no score claimed"
        rather than a measured triage figure. It is triage in practice.
        """
        # Two windows, two columns: a score is dated by `scored_at` and a run
        # by `started_at`. They agree because a score is written inside the run
        # that paid for it.
        rows = {r["model"]: r["usd"] for r in self.conn.execute(
            "SELECT model, COALESCE(SUM(cost_usd), 0) usd FROM scores "
            + ("WHERE scored_at >= ? " if since else "")
            + "GROUP BY model", (since,) if since else ())}
        # The three shapes a `scores.model` takes, and where each is written:
        # "<model>:triage" in pipeline._triage_scores, "<model>+images" in
        # ClaudeCodeScorer.resolve_with_images, and the bare model name for an
        # appraisal. Triage is matched EXPLICITLY rather than left to fall in
        # with appraisal: its rows cost 0 today, so lumping them in is
        # harmless right now and would silently double-count the day someone
        # bills them -- counted once under appraisal and once inside the
        # remainder below. tests/test_web.py pins the three spellings.
        images = sum(v for k, v in rows.items() if k.endswith("+images"))
        triage = sum(v for k, v in rows.items() if k.endswith(":triage"))
        appraisal = sum(v for k, v in rows.items()
                        if not k.endswith(("+images", ":triage")))
        total = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) t FROM runs"
            + (" WHERE started_at >= ?" if since else ""),
            (since,) if since else ()).fetchone()["t"]
        return {"appraisal": appraisal, "images": images,
                "other": max(0.0, total - appraisal - images - triage) + triage,
                "total": total}

    def token_totals(self) -> dict[str, int]:
        """Cache reads against fresh input.

        CLAUDE.md says prompt-prefix stability is money and that rebuilding
        the dismissal block per run would cost ~3x forever. That claim has
        never been checkable from the interface. If this ratio ever collapses,
        the bill triples and nothing else looks wrong.
        """
        r = self.conn.execute(
            "SELECT COALESCE(SUM(input_tokens), 0) input, "
            "COALESCE(SUM(cache_read_tokens), 0) cached, "
            "COALESCE(SUM(output_tokens), 0) output FROM scores").fetchone()
        return dict(r)

    def funnel(self) -> dict[str, int]:
        """Fetched to saved, in one row. The whole design in five numbers, and
        the fastest way to see a scraper that has stopped returning anything or
        a gate that has stopped holding."""
        r = self.conn.execute(
            "SELECT COALESCE(SUM(n_fetched), 0) fetched, "
            "COALESCE(SUM(n_candidates), 0) candidates, "
            "COALESCE(SUM(n_scored), 0) scored, "
            "COALESCE(SUM(n_wanted + n_free_find), 0) binned FROM runs"
        ).fetchone()
        saved = self.conn.execute(
            "SELECT COUNT(*) n FROM hunt_matches WHERE status='saved'"
        ).fetchone()["n"]
        # The only outcome number in the whole database. Everything above it
        # counts work done; this counts things that ended up in the user's
        # house, which is the only thing that makes the rest worth paying for.
        grabbed = self.conn.execute(
            "SELECT COUNT(*) n FROM hunt_matches WHERE status='grabbed'"
        ).fetchone()["n"]
        # `n_fetched` sums per-run counts, so every run re-reads the whole feed
        # and the same listing is counted again each time: 85,028 "fetched"
        # against 1,348 listings actually on file. The funnel is work done, not
        # things seen, and the page has to say so or the first number reads as
        # a claim about distinct listings.
        distinct = self.conn.execute(
            "SELECT COUNT(*) n FROM listings").fetchone()["n"]
        return {**dict(r), "saved": saved, "grabbed": grabbed,
                "distinct": distinct}

    def calibration(self) -> dict[str, Any]:
        """What the model said a grabbed thing was worth, against what you paid.

        The bot asserts a value on every listing it judges and nothing has ever
        checked one. This is the check, and it only becomes meaningful with a
        handful of rows -- which is why it is a line on /stats rather than a
        page: it is a fact about the bot, not a record of your shopping.

        Only listings with BOTH a figure paid and an estimate count, and the
        paid figure must be recorded rather than merely zero. A free thing with
        an estimate is the most common and most useful case here.
        """
        r = self.conn.execute(
            """SELECT COUNT(*) n,
                      COALESCE(SUM(s.est_value_cents), 0) est,
                      COALESCE(SUM(l.paid_cents), 0) paid
               FROM listings l
               JOIN scores s ON s.id = (SELECT MAX(id) FROM scores
                                        WHERE listing_id = l.id)
               WHERE l.grabbed_at IS NOT NULL
                 AND l.paid_cents IS NOT NULL
                 AND s.est_value_cents IS NOT NULL""").fetchone()
        return dict(r)

    def dearest_since(self, since: str, limit: int = 5) -> list[dict[str, Any]]:
        """The individual listings that cost the most, newest window first.

        The most literal answer to "what did I spend money on", and the one
        that catches a single odd listing eating an afternoon: an image pass
        runs about 15x a text appraisal, so one photo-checked junk post shows
        up here immediately.

        APPRAISAL COST ONLY. Triage is billed per batch and written to its
        score row as 0, so these figures are a floor rather than the whole of
        what a listing cost. The column says so.
        """
        return [dict(r) for r in self.conn.execute(
            """SELECT s.listing_id, s.hunt_id, s.cost_usd, s.deal_score,
                      s.images_checked, l.title, l.price_cents
               FROM scores s JOIN listings l ON l.id = s.listing_id
               WHERE s.scored_at >= ? AND s.cost_usd > 0
               ORDER BY s.cost_usd DESC LIMIT ?""", (since, int(limit)))]

    def first_run_at(self) -> str | None:
        """So the page can say how much history it is talking about instead of
        drawing an empty month and looking like a crash."""
        r = self.conn.execute("SELECT MIN(started_at) t FROM runs").fetchone()
        return r["t"] if r else None

    # --- wants --------------------------------------------------------------

    def seed_wants(self, wants: Iterable[Want]) -> int:
        """Copy config.yaml's wants into the table, ONCE ever.

        After this the table is the truth and the file is history. Seeding again
        on every start would undo every edit made from the phone, and seeding
        per-name would resurrect a want deleted from the web the moment the
        file still mentioned it. So it is a one-shot, remembered in `settings`.
        """
        # Per NAME, not once globally. A global flag meant a want added to
        # config.yaml after the first run never appeared and never said why.
        # Per-name is safe because deleting a want ARCHIVES it -- the row stays,
        # so the name stays taken and the file cannot resurrect it.
        #
        # There is no "seeded" marker row, and there must not be: the per-name
        # check below IS the one-shot. A marker was written here on every call,
        # and since `with_store` runs per web request, that put a WRITE on the
        # read path of every page -- taking a lock on a file the poller writes,
        # to store a constant nothing ever read.
        known = {r["name"] for r in self.conn.execute("SELECT name FROM wants")}
        return sum(self.save_want(w, origin="config")
                   for w in wants if w.name not in known)

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

    # What archiving a want may clear out of your bins, and what it may not.
    # `grabbed` is absent because that is a thing you own -- deleting the want
    # you found it through does not un-own it. `dismissed` is absent for the
    # opposite reason: it is already out of every bin AND it is a training
    # signal, and moving it would quietly stop teaching the hunt if the want
    # ever came back. `filtered` and `gone` are already out.
    ARCHIVABLE = ("wanted", "free_find", "saved", "scored", "new")

    def archive_matches(self, hunt_id: str) -> int:
        """Take a stopped hunt's listings out of every bin, keeping all of them.

        The want is gone, so its leftovers should stop following you around --
        two saved listings from a want deleted weeks ago were still sitting on
        /saved. But NOTHING here is deleted: the rows keep their scores, their
        reasons and their raw payloads, stay readable at /hunt/<id>, and
        remember what they were so restoring the want can put them back.

        A status of its own rather than `dismissed`, which would be the obvious
        reuse and is wrong twice: dismissed titles become negative examples in
        that hunt's next prompt, so this would teach the hunt to avoid exactly
        what you asked it to find, and it would say you rejected these when you
        did not.
        """
        marks = ",".join("?" * len(self.ARCHIVABLE))
        cur = self.conn.execute(
            f"""UPDATE hunt_matches
                SET status_before_archive = COALESCE(status_before_archive, status),
                    status = 'archived', updated_at = ?
                WHERE hunt_id = ? AND status IN ({marks})""",
            (_now(), hunt_id, *self.ARCHIVABLE))
        return cur.rowcount

    def unarchive_matches(self, hunt_id: str) -> int:
        """Put them back where they were. Restoring a want restores its list."""
        cur = self.conn.execute(
            """UPDATE hunt_matches
               SET status = COALESCE(status_before_archive, 'scored'),
                   status_before_archive = NULL, updated_at = ?
               WHERE hunt_id = ? AND status = 'archived'""", (_now(), hunt_id))
        return cur.rowcount

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

    # The numbers you would actually reach for, overriding config.yaml.
    # Deliberately NOT everything in that file: the source rate limits protect
    # you from being blocked by Facebook and live inside the adapter so a caller
    # cannot bypass them, which a tap on a phone would be.
    #
    # The fourth element is WHERE the number lands on `Config`. It is here
    # because `radius_miles` is the odd one out -- it sits on `location` while
    # the rest sit on `defaults` -- and without it `config.with_store` needed a
    # hand-written `replace` per destination, so a fifth number meant editing
    # the validation here AND the application there, in two files, with nothing
    # connecting them. `max_image_checks` was that fifth number, and landing it
    # on `scorer` cost exactly this one line.
    #
    # Zero is a legal image budget and means "never look at the photographs".
    # The others start at 1 because a hunt that judges nothing is a hunt that
    # has been turned off, and there is a pause switch for that; an image pass
    # is an extra on top of a judgement that happens either way.
    TUNING = {
        "min_deal_score":      (float, 0.0, 10.0, "defaults"),
        "free_find_min_score": (float, 0.0, 10.0, "defaults"),
        "max_results":         (int, 1, 50, "defaults"),
        "radius_miles":        (float, 1.0, 200.0, "location"),
        "max_image_checks":    (int, 0, 50, "scorer"),
    }

    def tuning(self) -> dict[str, float | int]:
        """Whatever has been set from the dashboard, parsed and clamped.

        A value that will not parse is ignored rather than raised on: these are
        settings rows, and a bad one must not be able to stop the timer."""
        out: dict[str, float | int] = {}
        for key, (cast, lo, hi, _) in self.TUNING.items():
            raw = self.get_setting(f"tune:{key}")
            if raw is None:
                continue
            try:
                out[key] = max(lo, min(cast(raw), hi))
            except (TypeError, ValueError):
                continue
        return out

    def set_tuning(self, key: str, value) -> None:
        cast, lo, hi, _ = self.TUNING[key]
        self.set_setting(f"tune:{key}", str(max(lo, min(cast(value), hi))))

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

    def hunt_excludes(self) -> dict[str, tuple[str, ...]]:
        """Words blocked from the dashboard, per hunt. These ADD to whatever
        `config.yaml` lists; the file's terms cannot be removed from the web,
        because they are reviewed lines in a committed file and this is a
        thumb on a phone."""
        out: dict[str, tuple[str, ...]] = {}
        for r in self.conn.execute(
                "SELECT key, value FROM settings WHERE key LIKE 'hunt_exclude:%'"):
            try:
                terms = json.loads(r["value"])
            except json.JSONDecodeError:
                continue
            if isinstance(terms, list):
                out[r["key"].split(":", 1)[1]] = tuple(
                    str(t) for t in terms if str(t).strip())
        return out

    def seed_excludes(self, from_file: Mapping[str, Sequence[str]]) -> int:
        """Copy config.yaml's exclude terms in, ONCE ever.

        Same one-shot as `seed_wants`, for the same reason and with the same
        consequence: after this the table is the truth and the file is history,
        so a term can be removed from the dashboard and stay removed. Merging
        the file in forever would mean four of the terms on that page could
        never be deleted, which is not a list you can edit."""
        # Per HUNT, not once globally: a sweep added to config.yaml later still
        # gets its terms. A hunt that already has a row is left alone, however
        # short that row is, so a term removed on the dashboard stays removed.
        #
        # The limit, and it is deliberate: a term APPENDED to a hunt that
        # already has a row does nothing. Applying it would mean re-adding
        # every term you had deleted, since the file cannot know which is which.
        #
        # Like `seed_wants`, the per-hunt check IS the one-shot -- no marker row
        # is written, so a GET that only reads stays a reader.
        n = 0
        existing = set(self.hunt_excludes())
        for hunt_id, terms in from_file.items():
            if terms and hunt_id not in existing:
                self.set_hunt_excludes(hunt_id, list(terms))
                n += len(terms)
        return n

    def set_hunt_excludes(self, hunt_id: str, terms: Sequence[str]) -> None:
        # Deduplicated, order kept: the list is read by a person on the
        # settings page, so it should look like what they added.
        clean = list(dict.fromkeys(t.strip().lower() for t in terms if t.strip()))
        self.set_setting(f"hunt_exclude:{hunt_id}", json.dumps(clean))

    def add_hunt_exclude(self, hunt_id: str, term: str) -> None:
        self.set_hunt_excludes(
            hunt_id, list(self.hunt_excludes().get(hunt_id, ())) + [term])

    def remove_hunt_exclude(self, hunt_id: str, term: str) -> None:
        self.set_hunt_excludes(hunt_id, [
            t for t in self.hunt_excludes().get(hunt_id, ())
            if t != term.strip().lower()])

    def exclude_counts(self, hunt_id: str) -> dict[str, int]:
        """How many listings each term has actually blocked.

        This is the whole point of putting the terms on a page. A blocked
        listing is gone without being read, so a term you cannot count is a
        term you cannot tell is too broad."""
        return {r["filter_reason"].split(":", 1)[1]: r["n"] for r in
                self.conn.execute(
                    "SELECT filter_reason, COUNT(*) n FROM hunt_matches "
                    "WHERE hunt_id=? AND filter_reason LIKE 'excluded_kw:%' "
                    "GROUP BY filter_reason", (hunt_id,))}


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
