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
    cfg = replace(cfg, hunts=tuple(replace(h, enabled=False) for h in cfg.hunts))
    st.set_hunt_enabled(cfg.hunts[0].id, True)
    assert _hunts(cfg, None, st) == []
