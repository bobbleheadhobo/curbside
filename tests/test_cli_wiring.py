"""The composition root.

Every other test builds sources, scorers and notifiers by hand, so nothing
exercised the functions that assemble them from config. A bad edit turned
`_build_notifiers` into a call to itself; 140 tests passed and the very first
scheduled run died with RecursionError.
"""
import pytest

from dealbot.cli import _build_notifiers, _build_one_source, _build_scorer, _build_sources
from dealbot.config import load
from dealbot.db import Store
from dealbot.notify.dashboard import DashboardNotifier
from dealbot.notify.discord import DiscordNotifier

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
    from dealbot.cli import cmd_notify
    from dealbot.models import Listing, Score
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
    from dealbot.cli import _hunts
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
    from dealbot.cli import _hunts
    shutil.copy(CONFIG, tmp_path / "config.yaml")
    cfg = load(tmp_path / "config.yaml")
    st = Store(cfg.db_path)
    from dealbot.config import WantHuntSpec
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
    from dealbot.scoring.claude_code import ClaudeCodeScorer

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
    from dealbot import cli
    from dealbot.config import load as load_cfg

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

    from dealbot.sources.fixture import FixtureSource
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(cli, "_build_sources", lambda c: [
        ("fixture", Counting(FixtureSource(c.location, root / "fixtures/listings")))])

    args = type("A", (), {"config": str(tmp_path / "config.yaml"), "hunt": None,
                          "dry_run": False, "no_score": True, "no_images": True,
                          "due": False})()
    assert cli.cmd_once(args) == 0
    assert resets["n"] == 1
