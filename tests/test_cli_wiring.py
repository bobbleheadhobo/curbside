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
