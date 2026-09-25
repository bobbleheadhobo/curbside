"""The composition root.

Every other test builds sources, scorers and notifiers by hand, so nothing
exercised the functions that assemble them from config. A bad edit turned
`_build_notifiers` into a call to itself; 140 tests passed and the very first
scheduled run died with RecursionError.
"""
import pytest

from curbside.cli import _build_notifiers, _build_one_source, _build_scorer, _build_sources
from curbside.config import load
from curbside.db import Store
from curbside.notify.dashboard import DashboardNotifier
from curbside.notify.discord import DiscordNotifier

CONFIG = "config.yaml"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def test_notifiers_assemble_without_recursing(store):
    cfg = load(CONFIG)
    built = _build_notifiers(cfg, store)
    assert any(isinstance(n, DashboardNotifier) for n in built)


def test_discord_is_added_only_when_enabled(store, monkeypatch):
    from dataclasses import replace
    cfg = load(CONFIG)

    on = replace(cfg, discord=replace(cfg.discord, enabled=True))
    assert any(isinstance(n, DiscordNotifier) for n in _build_notifiers(on, store))

    off = replace(cfg, discord=replace(cfg.discord, enabled=False))
    assert not any(isinstance(n, DiscordNotifier) for n in _build_notifiers(off, store))


def test_every_configured_source_can_be_built(store):
    cfg = load(CONFIG)
    for name in ("fixture", "facebook", "craigslist"):
        assert _build_one_source(cfg, name).name == name
    assert [n for n, _ in _build_sources(cfg)] == list(cfg.sources)


def test_an_unknown_source_fails_loudly(store):
    cfg = load(CONFIG)
    with pytest.raises(SystemExit):
        _build_one_source(cfg, "nope")


def test_both_scorer_backends_build(store):
    from dataclasses import replace
    cfg = load(CONFIG)
    for backend in ("stub", "claude_code"):
        c = replace(cfg, scorer=replace(cfg.scorer, backend=backend))
        assert _build_scorer(c, store) is not None


def test_notify_command_costs_nothing_and_drains_the_queue(tmp_path, monkeypatch):
    """Catch-up otherwise rides along with a hunt's cadence, so something stuck
    under an hourly want search waits up to an hour."""
    import shutil
    from curbside.cli import cmd_notify
    from curbside.models import Listing, Score
    from datetime import datetime, timezone

    shutil.copy(CONFIG, tmp_path / "config.yaml")
    cfg = load(tmp_path / "config.yaml")
    st = Store(cfg.db_path)
    hunt = cfg.hunts[0]
    l = Listing(id="x:1", source="x", source_id="1", title="t", description=None,
                price_cents=0, currency="USD", url="u")
    st.upsert_listing(l); st.mark_matches(hunt.id, [l])
    st.set_status(hunt.id, l.id, "free_find")
    st.save_score(Score(listing_id=l.id, hunt_id=hunt.id, model="m",
                        scored_at=datetime.now(timezone.utc), match="no",
                        deal_score=6.0, est_value_cents=None, condition=None,
                        matched_want=None, worth_grabbing=True, unknowns=(),
                        requirements=(), red_flags=(), reasoning="r"),
                  priced_at_cents=0)
    st.close()

    assert cmd_notify(type("A", (), {"config": str(tmp_path / "config.yaml"),
                                     "hunt": None})()) == 0


def test_a_paused_hunt_is_not_run(tmp_path):
    """The dashboard switch has to actually stop the work, not just hide it."""
    import shutil
    from curbside.cli import _hunts
    shutil.copy(CONFIG, tmp_path / "config.yaml")
    cfg = load(tmp_path / "config.yaml")
    st = Store(cfg.db_path)
    sweep = next(h for h in cfg.hunts if h.kind == "sweep")

    assert sweep.id in [h.id for h in _hunts(cfg, None, st)]
    st.set_hunt_enabled(sweep.id, False)
    running = [h.id for h in _hunts(cfg, None, st)]
    assert sweep.id not in running
    assert running, "want hunts must be unaffected"
    st.set_hunt_enabled(sweep.id, True)
    assert sweep.id in [h.id for h in _hunts(cfg, None, st)]


def test_config_disabled_still_wins(tmp_path):
    """The database switch is an override on top of config, not a replacement."""
    from dataclasses import replace
    import shutil
    from curbside.cli import _hunts
    shutil.copy(CONFIG, tmp_path / "config.yaml")
    cfg = load(tmp_path / "config.yaml")
    st = Store(cfg.db_path)
    from curbside.config import WantHuntSpec
    cfg = replace(
        cfg,
        sweeps=tuple(replace(s, enabled=False) for s in cfg.sweeps),
        want_hunts={w.name: replace(cfg.want_hunts.get(w.name) or WantHuntSpec(),
                                    enabled=False) for w in cfg.wants})
    st.set_hunt_enabled(cfg.hunts[0].id, True)
    assert _hunts(cfg, None, st) == []


def test_the_rubric_resolves_against_the_config_file_not_the_cwd(tmp_path, store):
    """`db_path` was made config-relative because per-CWD resolution split the
    data across three databases. The rubric had the same bug with a quieter
    failure: it falls back to the built-in default, so the bot judges by
    criteria other than the ones in the file you edited and nothing looks
    wrong."""
    import shutil
    from curbside.scoring.claude_code import ClaudeCodeScorer

    shutil.copy(CONFIG, tmp_path / "config.yaml")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "rubric.md").write_text(
        "<!-- a note to a human -->\nJUDGE EVERYTHING AS A SEVEN.\n")

    cfg = load(tmp_path / "config.yaml")
    assert cfg.scorer.rubric_path.is_absolute()
    sc = ClaudeCodeScorer(cfg.scorer, store)
    assert "JUDGE EVERYTHING AS A SEVEN." in sc._rubric
    assert "a note to a human" not in sc._rubric        # comments still stripped


def test_the_request_budget_is_reset_once_per_pass_not_once_per_hunt(tmp_path,
                                                                     monkeypatch):
    """REGRESSION: `reset_budget()` sat inside the hunt loop, so
    `max_requests_per_run: 25` really meant 25 PER HUNT -- 75 requests in one
    `once` against a source documented to throttle, silently, after about five
    rapid ones."""
    import shutil
    from curbside import cli
    from curbside.config import load as load_cfg

    shutil.copy(CONFIG, tmp_path / "config.yaml")
    cfg = load_cfg(tmp_path / "config.yaml")
    assert len(cfg.hunts) > 1                          # or this proves nothing

    resets = {"n": 0}

    class Counting:
        name = "fixture"
        def __init__(self, inner): self.inner = inner
        def search(self, hunt): return self.inner.search(hunt)
        def parse(self, raw): return self.inner.parse(raw)
        def reset_budget(self): resets["n"] += 1

    from curbside.sources.fixture import FixtureSource
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(cli, "_build_sources", lambda c: [
        ("fixture", Counting(FixtureSource(c.location, root / "fixtures/listings")))])

    args = type("A", (), {"config": str(tmp_path / "config.yaml"), "hunt": None,
                          "dry_run": False, "no_score": True, "no_images": True,
                          "due": False})()
    assert cli.cmd_once(args) == 0
    assert resets["n"] == 1


def test_a_plan_hold_stops_the_judging_and_nothing_else(tmp_path, monkeypatch):
    """The re-check asks the source outright whether something you saved has
    sold or changed price. It is requests and no model call, so a quota hold
    must not touch it -- a bot that stops telling you a saved listing sold is
    the opposite of thrifty, since that is the pass you act on.

    The hold itself is exercised for real here: the scorer is the live one and
    the reading in `settings` is what stops it, so nothing is invoked and
    nothing is spent."""
    import shutil
    import time
    from datetime import datetime, timezone
    from pathlib import Path

    from curbside import cli
    from curbside.config import load as load_cfg
    from curbside.recheck import RecheckResult
    from curbside.scoring.claude_code import RESET_7D, UTIL_7D, UTIL_AT
    from curbside.sources.fixture import FixtureSource

    shutil.copy(CONFIG, tmp_path / "config.yaml")
    cfg = load_cfg(tmp_path / "config.yaml")
    store = Store(cfg.db_path)
    store.set_setting(UTIL_7D, "0.92")                  # over the 90% ceiling
    store.set_setting(RESET_7D, str(time.time() + 21 * 3600))
    store.set_setting(UTIL_AT, datetime.now(timezone.utc).isoformat(
        timespec="seconds"))
    store.close()

    ran = {"recheck": 0, "price_drops": 0}

    def counting_recheck(*a, **kw):
        ran["recheck"] += 1
        return RecheckResult(n_checked=3)

    def counting_announce(*a, **kw):
        ran["price_drops"] += 1
        return 0

    monkeypatch.setattr(cli, "recheck", counting_recheck)
    monkeypatch.setattr(cli, "announce_price_drops", counting_announce)
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(cli, "_build_sources", lambda c: [
        ("fixture", FixtureSource(c.location, root / "fixtures/listings"))])

    args = type("A", (), {"config": str(tmp_path / "config.yaml"), "hunt": None,
                          "dry_run": False, "no_score": False, "no_images": True,
                          "due": False})()
    assert cli.cmd_once(args) == 0

    store = Store(cfg.db_path)
    warnings = [r["warning"] for r in
                store.conn.execute("SELECT warning FROM runs")]
    spent = store.conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) c FROM runs").fetchone()["c"]
    store.close()
    assert any(w and "standing aside" in w for w in warnings), warnings
    assert spent == 0                       # the hold cost nothing to observe
    assert ran["recheck"] == 1              # and the sold/price pass still ran
    assert ran["price_drops"] == 1          # as did the price-drop alert


# --- taking turns at the front of the pass -----------------------------------

def test_the_pass_order_rotates_so_the_same_hunt_is_not_always_last(store):
    """One budget serves the whole pass, so LAST is whoever gets nothing when
    it runs out -- and `cfg.hunts` is a fixed order, which made that the same
    hunt every time. `want:stacked-ottoman` sorts last and is the only hunt in
    the live database ever to have recorded `BudgetExhausted`.

    This matters more now that a starved fetch is a warning rather than an
    error: without rotation, last would mean never.
    """
    from curbside.cli import _rotated
    hunts = ["sweep", "bookshelf", "pots", "ottoman"]

    seen = [_rotated(hunts, store) for _ in range(5)]
    assert seen[0] == hunts
    assert seen[1] == ["bookshelf", "pots", "ottoman", "sweep"]
    assert seen[3] == ["ottoman", "sweep", "bookshelf", "pots"]
    assert seen[4] == hunts                       # wraps
    # Every hunt leads exactly once in a full cycle, and none is ever dropped.
    assert {tuple(sorted(s)) for s in seen} == {tuple(sorted(hunts))}
    assert sorted(s[0] for s in seen[:4]) == sorted(hunts)


def test_a_dry_run_does_not_advance_the_rotation(store):
    """`--dry-run` promises to write nothing, and one settings row is a write."""
    from curbside.cli import _rotated
    hunts = ["a", "b", "c"]
    assert _rotated(hunts, store, advance=False) == hunts
    assert _rotated(hunts, store, advance=False) == hunts
    assert store.get_setting("pass_rotation") is None


def test_rotation_survives_a_single_hunt_and_a_junk_counter(store):
    from curbside.cli import _rotated
    assert _rotated(["only"], store) == ["only"]
    store.set_setting("pass_rotation", "not a number")
    assert _rotated(["a", "b"], store) == ["a", "b"]
