"""End-to-end over fixtures. No network, no quota.

Two of these are regressions for bugs found the first time this ran, both of the
kind that look like normal behaviour until you check.
"""
import tempfile
from pathlib import Path

import pytest
from conftest import ABQ

from dealbot.config import load
from dealbot.db import Store
from dealbot.notify.dashboard import DashboardNotifier
from dealbot.pipeline import run_hunt
from dealbot.scoring.stub import StubScorer
from dealbot.sources.fixture import FixtureSource

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def rig():
    """Loads the real config, but pins the batch cap high.

    Production tuning must not change what these tests mean: setting
    `max_results_per_run: 5` for the live bot silently made "run twice, spend
    nothing the second time" fail, because run one deferred half the fixtures.
    Tests that care about the cap set it themselves."""
    from dataclasses import replace as _replace
    with tempfile.TemporaryDirectory() as tmp:
        cfg = load(ROOT / "config.yaml")
        cfg = _replace(
            cfg,
            defaults=_replace(cfg.defaults, max_results=1000),
            sweeps=tuple(_replace(s, max_results=None) for s in cfg.sweeps),
            want_hunts={n: _replace(w, max_results=None)
                        for n, w in cfg.want_hunts.items()})
        store = Store(Path(tmp) / "t.db")
        source = FixtureSource(cfg.location, ROOT / "fixtures/listings")
        yield cfg, store, source, StubScorer(), [DashboardNotifier(store)]
        store.close()


def _run(rig, hunt_name, **kw):
    cfg, store, source, scorer, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == hunt_name)
    return hunt, run_hunt(store, hunt, source, scorer, notifiers, cfg.location, **kw)


def test_first_run_fetches_scores_and_routes(rig):
    _, r = _run(rig, "free-nearby")
    assert r.error is None
    assert r.n_fetched == 10 and r.n_new == 10
    assert r.n_scored > 0
    # The stub cannot judge a hard requirement, so it reports `unknown`, and
    # `unknown` is routed by score exactly like `yes`: over the bar it reaches a
    # bin, under it stays `scored` and shows up in /skipped. That is the honest
    # answer, and the routing must respect it.
    assert r.n_wanted + r.n_free_find > 0


def test_second_run_spends_nothing(rig):
    """The property that makes a 15-minute poll interval safe."""
    _run(rig, "free-nearby")
    _, r2 = _run(rig, "free-nearby")
    assert r2.n_fetched == 10          # still sees everything
    assert r2.n_new == 0
    assert r2.n_candidates == 0        # but judges nothing
    assert r2.n_scored == 0


def test_triage_drops_are_remembered(rig):
    """REGRESSION: a triage drop used to leave no score row, so the gate had no
    memory of it and re-admitted it as `new` on every single run, forever."""
    _run(rig, "tv-stand")
    _, r2 = _run(rig, "tv-stand")
    assert r2.n_candidates == 0


def test_queued_items_survive_later_runs(rig):
    """REGRESSION: `record_rejections` used to overwrite a queue status with
    `filtered` when a listing came back unchanged, so anything awaiting triage
    silently vanished after one cycle. Applies to both queues."""
    cfg, store, *_ = rig
    hunt, r1 = _run(rig, "free-nearby")
    queued = r1.n_wanted + r1.n_free_find
    assert queued > 0
    for _ in range(3):
        _run(rig, "free-nearby")
    still = [s for s in store.statuses(hunt.id).values()
             if s in ("wanted", "free_find")]
    assert len(still) == queued


def test_every_run_is_recorded_even_when_it_finds_nothing(rig):
    """A quiet feed has to be explainable."""
    cfg, store, *_ = rig
    _run(rig, "free-nearby")
    _run(rig, "free-nearby")
    rows = list(store.conn.execute("SELECT n_fetched, error FROM runs"))
    assert len(rows) == 2 and all(r["error"] is None for r in rows)


def test_a_broken_source_fails_loudly(rig):
    cfg, store, _, scorer, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")

    class Broken:
        name = "broken"
        def search(self, hunt): raise RuntimeError("connection reset")
        def parse(self, raw): return None

    r = run_hunt(store, hunt, Broken(), scorer, notifiers, cfg.location)
    assert r.error and "connection reset" in r.error
    row = store.conn.execute("SELECT error FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["error"] == r.error      # visible on the dashboard, not just in a log


def test_price_history_records_moves_not_heartbeats(rig):
    """Append-only, but only when the price actually changed. Recording an
    unchanged price every run was ~19k rows a day of pure noise, slowing the
    very query the price-drop signal depends on."""
    from dataclasses import replace
    cfg, store, *_ = rig
    _run(rig, "free-nearby")
    _run(rig, "free-nearby")

    def n():
        return store.conn.execute(
            "SELECT COUNT(*) c FROM price_observations WHERE listing_id=?",
            ("fixture:1001",)).fetchone()["c"]

    assert n() == 1                       # two runs, price never moved

    row = store.conn.execute(
        "SELECT * FROM listings WHERE id='fixture:1001'").fetchone()
    from dealbot.models import Listing
    moved = Listing(id=row["id"], source=row["source"], source_id=row["source_id"],
                    title=row["title"], description=row["description"],
                    price_cents=4200, currency="USD", url=row["url"])
    store.upsert_listing(moved)
    store.record_price(moved.id, moved.price_cents)
    assert n() == 2                       # a real move is recorded
    store.record_price(moved.id, moved.price_cents)
    assert n() == 2                       # ... and not re-recorded


def test_rejected_listings_are_kept_with_their_reason(rig):
    cfg, store, *_ = rig
    hunt, _ = _run(rig, "free-nearby")
    reasons = {r["filter_reason"] for r in store.conn.execute(
        "SELECT filter_reason FROM hunt_matches WHERE hunt_id=? AND status='filtered'",
        (hunt.id,))}
    assert any(r and r.startswith("excluded_kw") for r in reasons)
    assert "too_far" in reasons


def test_a_scoring_outage_costs_judgement_not_data(rig):
    """Fetching costs no quota, so it continues. Listings must land in the store
    and stay `new`, ready to be judged when the window reopens."""
    from dealbot.scoring.claude_code import ScoringUnavailable

    cfg, store, source, _, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")

    class Paused:
        name = "paused"
        def triage(self, hunt, candidates):
            raise ScoringUnavailable("rate limited")
        def appraise(self, hunt, candidates):
            raise AssertionError("must not be reached")

    from dealbot.pipeline import run_hunt
    r = run_hunt(store, hunt, source, Paused(), notifiers, cfg.location)

    assert "scoring skipped" in r.error
    assert r.n_fetched == 10 and r.n_new == 10          # data landed
    assert store.conn.execute(
        "SELECT COUNT(*) c FROM listings").fetchone()["c"] == 10
    assert store.conn.execute(
        "SELECT COUNT(*) c FROM price_observations").fetchone()["c"] == 10
    statuses = set(store.statuses(hunt.id).values())
    assert statuses <= {"new", "filtered"}              # nothing judged


def test_free_finds_are_a_separate_axis_from_wants(rig):
    """REGRESSION: `worth_grabbing` used to have nowhere to go. Triage was told
    to keep anything "worth much more than it costs" -- vacuously true of
    everything free -- so it collapsed to "does it match a want" and dropped a
    working treadmill before it was ever appraised."""
    from dealbot.models import Score
    from datetime import datetime, timezone

    cfg, store, source, _, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")
    now = datetime.now(timezone.utc)

    class Fixed:
        """Matches nothing, but says everything is worth collecting."""
        name = "fixed"
        def triage(self, hunt, candidates):
            from dealbot.scoring.base import TriageResult
            return TriageResult(list(candidates), {})
        def appraise(self, hunt, candidates):
            return [Score(listing_id=c.listing.id, hunt_id=hunt.id, model="fixed",
                          scored_at=now, match="no", deal_score=6.0,
                          est_value_cents=None, condition=None, matched_want=None,
                          worth_grabbing=True, unknowns=(), requirements=(),
                          red_flags=(), reasoning="")
                    for c in candidates]

    from dealbot.pipeline import run_hunt
    r = run_hunt(store, hunt, source, Fixed(), notifiers, cfg.location)
    # 6.0 clears the free-finds bar (5.0) but not the wants bar (7.0).
    assert r.n_wanted == 0
    assert r.n_free_find > 0
    assert "free_find" in set(store.statuses(hunt.id).values())


def test_the_notifier_does_not_clobber_the_bin(rig):
    """REGRESSION: DashboardNotifier wrote `surfaced` over the `wanted` /
    `free_find` status the pipeline had just assigned, collapsing both bins."""
    cfg, store, *_ = rig
    hunt, r = _run(rig, "free-nearby")
    statuses = set(store.statuses(hunt.id).values())
    assert "surfaced" not in statuses
    assert statuses & {"wanted", "free_find", "scored"}


def test_a_detail_outage_leaves_the_rest_for_next_run(rig):
    """Enrichment stopping partway must not kill the run or burn the listings it
    never reached: judging a title like "Free" with no description would waste
    the one chance to score it."""
    from dealbot.sources.facebook import SourceBlocked

    cfg, store, source, scorer, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")

    class Flaky:
        name = "flaky"
        def __init__(self, inner): self.inner, self.n = inner, 0
        def search(self, hunt): return self.inner.search(hunt)
        def parse(self, raw): return self.inner.parse(raw)
        def detail(self, listing):
            self.n += 1
            if self.n > 2:
                raise SourceBlocked("request budget exhausted")
            return listing

    from dealbot.pipeline import run_hunt
    r = run_hunt(store, hunt, Flaky(source), scorer, notifiers, cfg.location)
    assert "detail fetch stopped" in r.warning
    assert r.error is None                      # degraded, not failed
    assert r.n_fetched == 10                    # everything still stored
    assert r.n_candidates == 2                  # only the enriched ones judged
    left = [s for s in store.statuses(hunt.id).values() if s == "new"]
    assert left                                  # the rest wait for next run


def test_enrichment_survives_a_later_index_only_refresh(rig):
    """REGRESSION: upsert's UPDATE omitted the coordinate columns, so every
    lat/lng fetched by the detail pass was silently discarded -- while
    descriptions and photos landed fine, making enrichment look like it worked.
    The mirror risk is a thin index refresh wiping what detail filled in."""
    from dataclasses import replace
    cfg, store, source, *_ = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")
    thin = next(source.parse(r) for r in source.search(hunt))
    thin = replace(thin, description=None, lat=None, lng=None, distance_mi=None,
                   images=())
    store.upsert_listing(thin)

    rich = replace(thin, description="a real description", lat=35.1, lng=-106.6,
                   distance_mi=2.5, images=("a.png", "b.png"))
    store.upsert_listing(rich)
    row = store.conn.execute("SELECT * FROM listings WHERE id=?", (thin.id,)).fetchone()
    assert row["lat"] == 35.1 and row["distance_mi"] == 2.5
    assert row["description"] == "a real description"

    store.upsert_listing(thin)          # index-only refresh must not undo it
    row = store.conn.execute("SELECT * FROM listings WHERE id=?", (thin.id,)).fetchone()
    assert row["lat"] == 35.1 and row["description"] == "a real description"
    assert row["distance_mi"] == 2.5


def test_a_degraded_run_says_so_in_the_runs_table(rig):
    """A partial enrichment must be visible on the dashboard, not only in a log
    line nobody reads.

    In `warning`, not `error`. `last_success_at` reads `error IS NULL`, so a
    degraded run recorded as a failure makes `--due` true on every tick -- and
    the hourly want hunts would start fetching every 15 minutes against a
    source that is already gating us, which is the opposite of backing off."""
    from dealbot.sources.facebook import SourceBlocked
    cfg, store, source, scorer, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")

    class Stops:
        name = "stops"
        def search(self, hunt): return source.search(hunt)
        def parse(self, raw): return source.parse(raw)
        def detail(self, listing): raise SourceBlocked("request budget exhausted")

    from dealbot.pipeline import run_hunt
    run_hunt(store, hunt, Stops(), scorer, notifiers, cfg.location, no_score=True)
    row = store.conn.execute(
        "SELECT error, warning FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["warning"] and "budget" in row["warning"]
    assert row["error"] is None
    assert store.last_success_at(hunt.id, "stops") is None   # --no-score pass


def test_a_large_first_run_is_capped_and_the_rest_deferred(rig):
    """Cold start against a big category would otherwise mean hundreds of detail
    fetches and appraisals at once. The overflow must stay `new`, not be marked
    filtered, so it drains over later runs."""
    from dataclasses import replace
    cfg, store, source, scorer, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")
    hunt = replace(hunt, max_results=3)

    from dealbot.pipeline import run_hunt
    r = run_hunt(store, hunt, source, scorer, notifiers, cfg.location)
    assert r.n_candidates == 3
    assert r.n_fetched == 10                       # everything still stored
    left = [s for s in store.statuses(hunt.id).values() if s == "new"]
    assert len(left) >= 4                          # backlog waits, not discarded


def test_one_source_does_not_retire_anothers_listings(rig):
    """REGRESSION (severe): mark_gone was scoped only by hunt, but every source
    runs the same hunt. The source that ran last marked all the others' listings
    `gone` -- including ones scored moments earlier -- so only the final source's
    results ever survived a run."""
    cfg, store, source, scorer, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")
    from dealbot.pipeline import run_hunt

    run_hunt(store, hunt, source, scorer, notifiers, cfg.location)
    before = {k: v for k, v in store.statuses(hunt.id).items()}
    assert before and "gone" not in set(before.values())

    class OtherSource:
        """A different source for the same hunt that returns nothing."""
        name = "other"
        def search(self, hunt): return iter(())
        def parse(self, raw): return None

    run_hunt(store, hunt, OtherSource(), scorer, notifiers, cfg.location)
    after = store.statuses(hunt.id)
    assert after == before          # the fixture source's listings are untouched


def test_a_listing_must_be_missed_repeatedly_before_being_retired(rig):
    """We only ever see one page. Something that scrolls off page one has not
    been sold, and Facebook reshuffles constantly."""
    cfg, store, source, scorer, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")
    from dealbot.pipeline import run_hunt
    run_hunt(store, hunt, source, scorer, notifiers, cfg.location)

    class Empty:
        name = "fixture"            # SAME source, now returning nothing
        def search(self, hunt): return iter(())
        def parse(self, raw): return None

    for expected_gone in (False, False, True):
        store.mark_gone(hunt.id, "fixture", ["sentinel"])
        gone = "gone" in set(store.statuses(hunt.id).values())
        assert gone == expected_gone


def test_concurrent_writers_do_not_get_a_locked_database(rig):
    """The poller and the dashboard are separate processes writing the same file
    by design; a triage click during a run used to 500 with 'database is
    locked'."""
    import sqlite3
    cfg, store, *_ = rig
    other = sqlite3.connect(store.path, isolation_level=None)
    other.execute("PRAGMA busy_timeout=10000")
    try:
        assert store.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        store.set_setting("k", "v")
        other.execute("INSERT OR REPLACE INTO settings VALUES ('k2','v2')")
        assert store.get_setting("k") == "v"
    finally:
        other.close()


def test_a_returning_listing_is_un_retired(rig):
    """REGRESSION: a listing that reached the wants bin, dropped off page one
    for a few runs, then came back stayed hidden forever. Facebook reshuffles
    constantly, so this silently loses exactly what the bot exists to find."""
    from dealbot.models import Listing
    cfg, store, *_ = rig
    l = Listing(id="fb:1", source="facebook", source_id="1", title="Oak console",
                description=None, price_cents=0, currency="USD", url="u")
    store.upsert_listing(l)
    store.mark_matches("h", [l])
    store.set_status("h", "fb:1", "wanted")

    for _ in range(3):
        store.mark_gone("h", "facebook", ["sentinel"])
    assert store.statuses("h")["fb:1"] == "gone"

    store.mark_gone("h", "facebook", ["fb:1"])          # it is back
    assert store.statuses("h")["fb:1"] == "wanted"      # ...and so is its bin


def test_a_listing_with_no_detail_is_deferred_not_judged_thin(rig):
    """Scoring the index-level record means judging a title like "Free" with no
    description, and it burns the one chance to score that listing."""
    cfg, store, source, scorer, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")

    class NoDetail:
        name = "fixture"
        def search(self, hunt): return source.search(hunt)
        def parse(self, raw): return source.parse(raw)
        def detail(self, listing): return None      # always unavailable

    from dealbot.pipeline import run_hunt
    r = run_hunt(store, hunt, NoDetail(), scorer, notifiers, cfg.location)
    assert r.n_fetched == 10          # still stored
    assert r.n_candidates == 0        # but nothing judged on thin data
    assert "new" in set(store.statuses(hunt.id).values())


def test_the_deferred_backlog_is_recorded_on_the_run(rig):
    """A backlog that keeps growing means arrivals are outrunning
    max_results_per_run and old listings will never be reached. That has to be
    visible, not just logged."""
    from dataclasses import replace
    cfg, store, source, scorer, notifiers = rig
    hunt = replace(next(h for h in cfg.hunts if h.name == "free-nearby"),
                   max_results=3)
    from dealbot.pipeline import run_hunt
    r = run_hunt(store, hunt, source, scorer, notifiers, cfg.location)
    assert r.n_deferred >= 4
    row = store.conn.execute(
        "SELECT n_deferred FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["n_deferred"] == r.n_deferred


def test_a_triage_drop_records_the_models_reason(rig):
    """"dropped in triage" told you nothing. When triage rejects something that
    looks like a genuine candidate you need to know whether it spotted a stated
    width you missed or simply got it wrong."""
    cfg, store, *_ = rig
    hunt, _ = _run(rig, "tv-stand")
    rows = [r["reasoning"] for r in store.conn.execute(
        "SELECT reasoning FROM scores WHERE hunt_id=? AND model LIKE '%triage%'",
        (hunt.id,))]
    assert rows
    assert all(r.startswith("dropped in triage: ") for r in rows)
    assert any(len(r) > len("dropped in triage: ") for r in rows)


def test_only_due_hunts_run_on_a_timer_tick(rig):
    """The timer fires on a fixed period; each hunt decides for itself whether
    enough time has passed. Without this, a 15-minute timer would run the hourly
    want searches four times an hour."""
    from datetime import datetime, timedelta, timezone
    from dealbot.cli import _is_due
    cfg, store, *_ = rig
    sweep = next(h for h in cfg.hunts if h.interval_minutes == 15)
    want = next(h for h in cfg.hunts if h.interval_minutes == 60)

    assert _is_due(store, sweep, "fixture")        # never run
    assert _is_due(store, want, "fixture")

    def record(hunt, minutes_ago, error=None):
        when = (datetime.now(timezone.utc)
                - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        store.conn.execute(
            "INSERT INTO runs (hunt_id, source, started_at, finished_at, error) "
            "VALUES (?,?,?,?,?)", (hunt.id, "fixture", when, when, error))

    record(sweep, 5); record(want, 5)
    assert not _is_due(store, sweep, "fixture")    # 5 min < 15
    assert not _is_due(store, want, "fixture")     # 5 min < 60

    record(sweep, 20)
    assert _is_due(store, sweep, "fixture")        # 20 min >= 15
    assert not _is_due(store, want, "fixture")


def test_a_failed_run_does_not_satisfy_the_cadence(rig):
    """A transient outage should be retried on the next tick, not wait out a
    full interval."""
    from datetime import datetime, timezone
    from dealbot.cli import _is_due
    cfg, store, *_ = rig
    want = next(h for h in cfg.hunts if h.interval_minutes == 60)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.conn.execute(
        "INSERT INTO runs (hunt_id, source, started_at, finished_at, error) "
        "VALUES (?,?,?,?,?)", (want.id, "fixture", now, now, "boom"))
    assert _is_due(store, want, "fixture")


def test_the_daily_ceiling_stops_scoring_but_not_collecting(rig):
    """An unattended bot draining a cold-start backlog shares quota with otter.
    Fetching costs no quota, so it must continue."""
    from datetime import datetime, timezone
    from dealbot.config import ScorerConfig
    from dealbot.scoring.claude_code import ClaudeCodeScorer, ScoringUnavailable
    import pytest as _pytest

    cfg, store, source, _, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.conn.execute(
        "INSERT INTO runs (hunt_id, source, started_at, finished_at, cost_usd) "
        "VALUES (?,?,?,?,?)", (hunt.id, "fixture", now, now, 5.0))

    sc = ClaudeCodeScorer(ScorerConfig(daily_cost_limit_usd=3.0), store)
    with _pytest.raises(ScoringUnavailable, match="daily spend ceiling"):
        sc.check_available()

    from dealbot.pipeline import run_hunt
    r = run_hunt(store, hunt, source, sc, notifiers, cfg.location)
    assert "spend ceiling" in r.error
    assert r.n_fetched == 10                      # collecting continues
    assert store.conn.execute(
        "SELECT COUNT(*) c FROM listings").fetchone()["c"] == 10


def test_a_no_score_pass_does_not_satisfy_the_cadence(rig):
    """It fetched but never judged. Letting it count pushes the real pass out by
    a full interval -- which is exactly what happened the first time the timer
    fired after a --no-score dry run."""
    from dealbot.cli import _is_due
    cfg, store, source, scorer, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")
    from dealbot.pipeline import run_hunt

    run_hunt(store, hunt, source, scorer, notifiers, cfg.location, no_score=True)
    assert _is_due(store, hunt, "fixture")        # still due

    run_hunt(store, hunt, source, scorer, notifiers, cfg.location)
    assert not _is_due(store, hunt, "fixture")    # a full pass satisfies it


def test_triage_spend_is_counted_toward_the_run_and_the_ceiling(rig):
    """A run that triage-dropped everything reported $0.00 while having made a
    real batched model call -- so the daily ceiling never saw that spend."""
    from dealbot.scoring.base import TriageResult
    cfg, store, source, _, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")

    class DropsEverything:
        name = "t"
        def triage(self, hunt, candidates):
            return TriageResult([], {c.listing.id: "nope" for c in candidates},
                                cost_usd=0.042)
        def appraise(self, hunt, candidates): return []

    from dealbot.pipeline import run_hunt
    r = run_hunt(store, hunt, source, DropsEverything(), notifiers, cfg.location)
    assert r.n_scored > 0            # everything was judged (and dropped)
    assert r.cost_usd == pytest.approx(0.042)
    row = store.conn.execute(
        "SELECT cost_usd FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["cost_usd"] == pytest.approx(0.042)


def test_a_cross_source_duplicate_is_not_appraised_twice(rig):
    """9 titles already appeared on both marketplaces in live data."""
    from dataclasses import replace
    cfg, store, source, scorer, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")
    from dealbot.pipeline import run_hunt

    run_hunt(store, hunt, source, scorer, notifiers, cfg.location)
    # Must be one we already PAID to judge -- the check deliberately only skips
    # duplicates of listings that already cost us an appraisal.
    original = store.conn.execute(
        """SELECT l.* FROM listings l JOIN scores s ON s.listing_id = l.id
           WHERE l.dup_key IS NOT NULL LIMIT 1""").fetchone()
    assert original, "fixtures should produce at least one scored dedupable listing"

    class Twin:
        """The same item, posted to the other marketplace."""
        name = "other"
        def search(self, hunt):
            from dealbot.models import RawListing
            from datetime import datetime, timezone
            return iter([RawListing("other", "999", {}, datetime.now(timezone.utc))])
        def parse(self, raw):
            from dealbot.models import Listing
            return Listing(id="other:999", source="other", source_id="999",
                           title=original["title"], description="d",
                           price_cents=original["price_cents"], currency="USD",
                           url="u", lat=original["lat"], lng=original["lng"],
                           distance_mi=1.0)

    r = run_hunt(store, hunt, Twin(), scorer, notifiers, cfg.location)
    assert r.n_candidates == 0
    reason = store.conn.execute(
        "SELECT filter_reason FROM hunt_matches WHERE listing_id='other:999'"
    ).fetchone()["filter_reason"]
    assert reason.startswith("duplicate_of:")


def test_db_path_is_relative_to_the_config_not_the_cwd(tmp_path, monkeypatch):
    """A relative db_path interpreted per working directory is how the data
    ended up split across three databases, one of which held the best find."""
    import shutil
    from dealbot.config import load
    cfg_dir = tmp_path / "proj"
    cfg_dir.mkdir()
    shutil.copy(ROOT / "config.yaml", cfg_dir / "config.yaml")

    monkeypatch.chdir(tmp_path)          # run from somewhere else entirely
    cfg = load(cfg_dir / "config.yaml")
    assert cfg.db_path == cfg_dir / "data/dealbot.db"
    assert cfg.db_path.is_absolute()


def test_home_coordinates_can_be_overridden_from_the_environment(tmp_path, monkeypatch):
    """Your real address belongs in .env, not in a committed config file."""
    import shutil
    from dealbot.config import load
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")

    plain = load(tmp_path / "config.yaml")
    monkeypatch.setenv("HOME_LAT", "35.1450")
    monkeypatch.setenv("HOME_LNG", "-106.5900")
    monkeypatch.setenv("RADIUS_MILES", "12")
    override = load(tmp_path / "config.yaml")

    assert (override.location.lat, override.location.lng) == (35.145, -106.59)
    assert override.location.radius_miles == 12
    assert override.location.lat != plain.location.lat


def test_enrichment_dates_are_not_discarded(rig):
    """REGRESSION: posted_at was in the INSERT but missing from the UPDATE, so
    every Craigslist listing ended up with no age at all -- no "listed 12d ago",
    no motivated-seller flag, and nothing for an age filter to work with. Same
    class of bug as the coordinates one."""
    from dataclasses import replace
    from datetime import datetime, timezone
    cfg, store, source, *_ = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")
    thin = next(source.parse(r) for r in source.search(hunt))
    thin = replace(thin, posted_at=None, category=None)
    store.upsert_listing(thin)

    when = datetime(2026, 8, 1, tzinfo=timezone.utc)
    store.upsert_listing(replace(thin, posted_at=when, category="fuo"))
    row = store.conn.execute("SELECT * FROM listings WHERE id=?", (thin.id,)).fetchone()
    assert row["posted_at"].startswith("2026-08-01")
    assert row["category"] == "fuo"

    store.upsert_listing(thin)          # a later index-only pass must not wipe it
    row = store.conn.execute("SELECT * FROM listings WHERE id=?", (thin.id,)).fetchone()
    assert row["posted_at"].startswith("2026-08-01")


def test_stale_free_listings_are_not_paid_for(rig):
    """A free couch posted a fortnight ago is gone. Judging it costs real money
    and can only ever produce a wasted trip."""
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone
    cfg, store, source, scorer, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")
    assert hunt.max_age_days == 7

    class Aged:
        """Every fixture listing, backdated a month."""
        name = "fixture"
        def search(self, hunt): return source.search(hunt)
        def parse(self, raw):
            l = source.parse(raw)
            return replace(l, posted_at=datetime.now(timezone.utc) - timedelta(days=30))

    from dealbot.pipeline import run_hunt
    r = run_hunt(store, hunt, Aged(), scorer, notifiers, cfg.location)
    assert r.n_fetched == 10          # still collected -- history is free
    assert r.n_candidates == 0        # but nothing judged
    assert r.cost_usd == 0.0
    reasons = {x["filter_reason"] for x in store.conn.execute(
        "SELECT filter_reason FROM hunt_matches WHERE hunt_id=?", (hunt.id,))}
    assert "too_old" in reasons


def test_an_undated_listing_is_never_dropped_for_age(rig):
    """Craigslist's search feed omits the date. Failing closed would silently
    discard most of what it returns."""
    from dataclasses import replace
    cfg, store, source, scorer, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")

    class Undated:
        name = "fixture"
        def search(self, hunt): return source.search(hunt)
        def parse(self, raw): return replace(source.parse(raw), posted_at=None)

    from dealbot.pipeline import run_hunt
    r = run_hunt(store, hunt, Undated(), scorer, notifiers, cfg.location)
    assert r.n_candidates > 0


def test_want_hunts_have_no_age_limit_by_default(rig):
    """Priced things sit, and age there is a BUY signal -- the motivated-seller
    flag depends on exactly the listings an age filter would throw away."""
    cfg, *_ = rig
    for h in cfg.hunts:
        if h.kind == "want":
            assert h.max_age_days == 0


def test_photos_are_only_fetched_for_listings_that_already_matter(rig):
    """An image pass costs about twice a text appraisal, and two thirds were
    being spent confirming that things scoring 3/10 are indeed poor."""
    from dealbot.pipeline import _would_bin
    from dealbot.models import Score
    from datetime import datetime, timezone
    cfg, *_ = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")

    from conftest import make_listing
    free = make_listing(price_cents=0)

    def sc(match, score, grab=True):
        return Score(listing_id="x", hunt_id=hunt.id, model="m",
                     scored_at=datetime.now(timezone.utc), match=match,
                     deal_score=score, est_value_cents=None, condition=None,
                     matched_want=None, worth_grabbing=grab, unknowns=(),
                     requirements=(), red_flags=(), reasoning="")

    # the case worth paying for: a high text score photos might demolish
    assert _would_bin(sc("unknown", 9.0), hunt, free)
    assert _would_bin(sc("yes", 7.0), hunt, free)
    # a free find clearing its own, lower bar
    assert _would_bin(sc("no", 5.0), hunt, free)
    # and the two thirds that were pure waste
    assert not _would_bin(sc("unknown", 3.0), hunt, free)
    assert not _would_bin(sc("no", 4.0), hunt, free)
    assert not _would_bin(sc("no", 9.0, grab=False), hunt, free)


# --- what the model was already paid for must not be lost -------------------

def test_an_interrupted_appraisal_still_routes_what_it_bought(rig):
    """REGRESSION: a pause partway through appraisal saved the finished scores
    with status `scored` and then returned BEFORE routing. On the next run the
    gate sees a score, no price drop, and rejects the listing as `unchanged` --
    forever. It never reaches a bin, so `pending_notifications` never sees it
    either. That is the silent loss that left two TV stands un-announced, and
    the appraisal was paid for."""
    from dataclasses import replace
    from dealbot.scoring.claude_code import ScoringUnavailable

    cfg, store, source, scorer, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")

    class Interrupted:
        """Appraises all but the last, then the plan window closes."""
        name = "stub"
        def triage(self, hunt, cands):
            return scorer.triage(hunt, cands)
        def appraise(self, hunt, cands):
            raise ScoringUnavailable("rate limited",
                                     partial=scorer.appraise(hunt, cands[:-1]))

    r = run_hunt(store, hunt, source, Interrupted(), notifiers, cfg.location)
    assert "interrupted" in r.error
    assert r.n_wanted + r.n_free_find > 0
    binned = [s for s in store.statuses(hunt.id).values()
              if s in ("wanted", "free_find")]
    assert len(binned) == r.n_wanted + r.n_free_find
    assert store.pending_notifications(hunt.id)      # and it can still be announced


def test_a_triage_outage_records_the_chunks_it_paid_for(rig):
    """The cost has to reach `runs.cost_usd` -- the only thing the daily ceiling
    reads -- and a verdict already bought must not be bought again."""
    from dealbot.scoring.base import TriageResult
    from dealbot.scoring.claude_code import ScoringUnavailable

    cfg, store, source, scorer, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")
    dropped = {}

    class Stops:
        name = "stub"
        def triage(self, hunt, cands):
            dropped[cands[0].listing.id] = "a service ad"
            raise ScoringUnavailable(
                "rate limited",
                partial=TriageResult([], dict(dropped), cost_usd=0.05))
        def appraise(self, hunt, cands):
            raise AssertionError("must not appraise during an outage")

    r = run_hunt(store, hunt, source, Stops(), notifiers, cfg.location)
    assert r.cost_usd == 0.05
    row = store.conn.execute(
        "SELECT cost_usd FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["cost_usd"] == 0.05
    lid = next(iter(dropped))
    assert lid in store.last_scores(hunt.id)         # not re-triaged next run


# --- one bad payload is not an outage ---------------------------------------

def test_one_unparsable_detail_does_not_strand_every_listing_behind_it(rig):
    """REGRESSION: the enrichment loop caught bare Exception and BROKE, which is
    right for "the site is gating us" and wrong for "this one payload would not
    parse". An image entry that is not a string used to leave every candidate
    behind it unenriched and unjudged."""
    cfg, store, source, scorer, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")

    class OneBadPayload:
        name = "fixture"
        def search(self, hunt): return source.search(hunt)
        def parse(self, raw): return source.parse(raw)
        def detail(self, listing):
            if listing.id.endswith("1001"):
                raise AttributeError("'int' object has no attribute 'split'")
            return listing

    r = run_hunt(store, hunt, OneBadPayload(), scorer, notifiers, cfg.location)
    assert r.n_fetched == 10
    assert r.n_candidates == 7              # a clean run gates 8 through
    assert r.n_scored == 7                  # the eight behind it still judged
    assert store.statuses(hunt.id)["fixture:1001"] == "new"   # deferred, not lost
    assert r.error is None                  # degraded, not failed
    assert "1 detail fetches failed" in r.warning


# --- exclude terms only become visible after enrichment ---------------------

def test_an_exclude_term_in_the_description_is_caught_before_it_is_paid_for(rig):
    """The gate only ever sees the search feed, where Facebook supplies no
    description and Craigslist hardcodes None -- so an exclude term that appears
    only in the body could never fire there, and the listing was paid for at
    triage AND at appraisal."""
    from dataclasses import replace
    cfg, store, source, scorer, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "free-nearby")
    hunt = replace(hunt, exclude=("free estimate",))

    class BodySaysService:
        name = "fixture"
        def search(self, hunt): return source.search(hunt)
        def parse(self, raw): return source.parse(raw)
        def detail(self, listing):
            return replace(listing, description="Free estimate, licensed crew")

    r = run_hunt(store, hunt, BodySaysService(), scorer, notifiers, cfg.location)
    assert r.n_candidates == 0
    reasons = {r["filter_reason"] for r in store.conn.execute(
        "SELECT filter_reason FROM hunt_matches WHERE hunt_id=?", (hunt.id,))}
    assert "excluded_kw:free estimate" in reasons


# --- the free bin asks more of a listing that costs money --------------------

def test_a_fairly_priced_listing_is_not_a_free_find(rig):
    """`worth_grabbing` asks "would a sensible person collect this at this
    price?", which for a fairly priced thing is a low bar. A $140 meditation
    chair the model valued at $160 cleared it and was announced -- "fair value,
    nothing special", in the model's own words, in a tab called free finds.
    Free costs a drive; a price costs the price, so the bin asks for a margin.
    """
    from datetime import datetime, timezone
    from dealbot.pipeline import _is_a_bargain
    from dealbot.models import Score
    from conftest import make_listing

    def sc(est):
        return Score(listing_id="x", hunt_id="h", model="m",
                     scored_at=datetime.now(timezone.utc), match="no",
                     deal_score=6.0, est_value_cents=est, condition=None,
                     matched_want=None, worth_grabbing=True, unknowns=(),
                     requirements=(), red_flags=(), reasoning="")

    pipersong = make_listing(price_cents=14000)
    assert not _is_a_bargain(sc(16000), pipersong)       # 1.14x -- a purchase
    assert _is_a_bargain(sc(28000), pipersong)           # 2.0x  -- a find

    # Free is unaffected: free costs a drive, not money.
    assert _is_a_bargain(sc(3000), make_listing(price_cents=0))
    # And it fails open on both kinds of missing information.
    assert _is_a_bargain(sc(None), pipersong)
    assert _is_a_bargain(sc(None), make_listing(price_cents=None))


def test_a_priced_bargain_still_reaches_the_free_bin(rig):
    """The rule must not cost you the $60 credenza worth $300."""
    from dataclasses import replace
    from datetime import datetime, timezone

    from dealbot.models import Score
    from dealbot.scoring.base import TriageResult

    cfg, store, source, _, notifiers = rig
    hunt = next(h for h in cfg.hunts if h.name == "stacked-ottoman")

    class Valuer:
        """Everything is a non-match worth grabbing; only the value moves."""
        name = "valuer"
        def __init__(self, multiple): self.multiple = multiple
        def triage(self, hunt, cands):
            return TriageResult(list(cands), {})
        def appraise(self, hunt, cands):
            now = datetime.now(timezone.utc)
            return [Score(listing_id=c.listing.id, hunt_id=hunt.id, model="m",
                          scored_at=now, match="no", deal_score=6.0,
                          est_value_cents=int((c.listing.price_cents or 0)
                                              * self.multiple),
                          condition=None, matched_want=None, worth_grabbing=True,
                          unknowns=(), requirements=(), red_flags=(),
                          reasoning="") for c in cands]

    fair = run_hunt(store, hunt, source, Valuer(1.15), notifiers, cfg.location)
    assert fair.n_candidates > 0 and fair.n_free_find == 0

    # Same listings, same scores, a real margin: the bin takes them.
    store2 = Store(Path(store.path).parent / "again.db")
    bargain = run_hunt(store2, hunt, source, Valuer(3.0), notifiers, cfg.location)
    assert bargain.n_free_find == bargain.n_candidates
    store2.close()
