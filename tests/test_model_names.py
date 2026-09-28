"""Always the newest model, and saying which one it was.

The scorer passes a FAMILY (`--model sonnet`) that Claude Code resolves to its
newest model, and learns the real version from the stream afterwards. These
pin the rules that make the label honest: which stream event to believe, and
which sightings must not move the label.
"""
import shutil

import pytest
from fastapi.testclient import TestClient

from curbside.config import load, with_store
from curbside.db import Store
from curbside.scoring.model_names import (MODEL_SETTING, learn_model,
                                           model_name, resolve_model,
                                           seen_models)
from curbside.scoring.stream import extract
from curbside.web.app import create_app


@pytest.mark.parametrize("raw,shown", [
    ("claude-sonnet-5", "Sonnet 5"),
    ("claude-sonnet-5-5", "Sonnet 5.5"),
    ("claude-haiku-4-5-20251001", "Haiku 4.5"),
    ("sonnet", "Sonnet"),
    ("claude-mystery-preview-x", "claude-mystery-preview-x"),   # shown raw
])
def test_model_names_read_as_a_person_would_say_them(raw, shown):
    assert model_name(raw) == shown


@pytest.mark.parametrize("raw,fam", [
    ("sonnet", "sonnet"), ("Opus", "opus"), ("claude-sonnet-5", "sonnet"),
    ("claude-haiku-4-5-20251001", "haiku"), ("gpt-4", None), ("", None),
    (None, None),
])
def test_a_full_id_resolves_to_its_family(raw, fam):
    assert resolve_model(raw) == fam


# --- the stream -------------------------------------------------------------

def _assistant(model, parent=None):
    return {"type": "assistant", "parent_tool_use_id": parent,
            "message": {"model": model}}


def test_the_last_main_loop_model_wins():
    facts = extract([_assistant("claude-sonnet-5"),
                     _assistant("claude-sonnet-5-5")])
    assert facts.model == "claude-sonnet-5-5"


def test_subagents_synthetic_errors_and_model_usage_are_ignored():
    """`modelUsage` lists Claude Code's own background Haiku calls, so reading
    it would label a Sonnet run as Haiku."""
    facts = extract([
        _assistant("claude-sonnet-5-5"),
        _assistant("claude-opus-5", parent="toolu_1"),
        _assistant("<synthetic>"),
        {"type": "result", "result": "ok", "total_cost_usd": 0.01,
         "modelUsage": {"claude-haiku-4-5-20251001": {}}},
    ])
    assert facts.model == "claude-sonnet-5-5"


def test_a_stream_with_no_assistant_event_knows_no_model():
    assert extract([{"type": "result", "result": "x"}]).model is None


# --- learning ---------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "db.sqlite")


def test_the_first_sighting_is_stored_without_a_notice(store):
    assert learn_model(store, "sonnet", "claude-sonnet-5") is None
    assert seen_models(store) == {"sonnet": "claude-sonnet-5"}


def test_an_upgrade_is_announced(store):
    learn_model(store, "sonnet", "claude-sonnet-5")
    notice = learn_model(store, "sonnet", "claude-sonnet-5-5")
    assert notice == "Sonnet runs are now on Sonnet 5.5 (was Sonnet 5)."
    assert seen_models(store)["sonnet"] == "claude-sonnet-5-5"


def test_a_run_that_straddled_the_update_does_not_move_it_back(store):
    learn_model(store, "sonnet", "claude-sonnet-5-5")
    assert learn_model(store, "sonnet", "claude-sonnet-5") is None
    assert seen_models(store)["sonnet"] == "claude-sonnet-5-5"


def test_a_fallback_to_another_family_is_not_learned(store):
    """Opus quietly falls back to Sonnet when its allowance runs out."""
    learn_model(store, "opus", "claude-opus-5")
    assert learn_model(store, "opus", "claude-sonnet-5-5") is None
    assert seen_models(store) == {"opus": "claude-opus-5"}


def test_a_dated_id_for_the_same_version_is_stored_quietly(store):
    learn_model(store, "haiku", "claude-haiku-4-5")
    assert learn_model(store, "haiku", "claude-haiku-4-5-20251001") is None
    assert seen_models(store)["haiku"] == "claude-haiku-4-5-20251001"


def test_what_was_learned_survives_a_restart(tmp_path):
    learn_model(Store(tmp_path / "db.sqlite"), "sonnet", "claude-sonnet-5-5")
    assert seen_models(Store(tmp_path / "db.sqlite")) == {
        "sonnet": "claude-sonnet-5-5"}


# --- config and the dashboard -----------------------------------------------

@pytest.fixture
def cfg_path(tmp_path):
    shutil.copy("config.yaml", tmp_path / "config.yaml")
    return tmp_path / "config.yaml"


def test_a_pinned_id_in_the_file_loads_as_its_family(cfg_path):
    """A pinned ID never moves when a new model ships."""
    text = cfg_path.read_text().replace("appraise_model: sonnet",
                                        "appraise_model: claude-opus-5")
    cfg_path.write_text(text)
    assert load(cfg_path).scorer.appraise_model == "opus"


def test_the_choice_on_settings_drives_every_call(cfg_path):
    """Drafting included: the harness cache is per model, so drafting on
    anything but what the poller keeps warm costs several times more."""
    cfg = load(cfg_path)
    store = Store(cfg.db_path)
    store.set_setting(MODEL_SETTING, "opus")
    sc = with_store(cfg, store).scorer
    assert (sc.triage_model, sc.appraise_model, sc.suggest_model) == (
        "opus", "opus", "opus")


def test_settings_offers_the_families_labelled_by_what_ran(cfg_path):
    cfg = load(cfg_path)
    store = Store(cfg.db_path)
    learn_model(store, "sonnet", "claude-sonnet-5-5")
    client = TestClient(create_app(cfg))

    page = client.get("/settings").text
    assert '<option value="sonnet" selected>Sonnet 5.5</option>' in page
    assert '<option value="opus" >Opus</option>' in page

    client.post("/settings/model", data={"model": "opus"},
                follow_redirects=False)
    assert with_store(cfg, store).scorer.appraise_model == "opus"


def test_an_unknown_model_is_refused_and_nothing_changes(cfg_path):
    cfg = load(cfg_path)
    client = TestClient(create_app(cfg))
    r = client.post("/settings/model", data={"model": "gpt-4"},
                    headers={"X-Requested-With": "fetch"})
    assert r.json()["ok"] is False
    assert Store(cfg.db_path).get_setting(MODEL_SETTING) is None
