"""Wants, editable from the phone.

They lived in config.yaml, which meant adding one needed an ssh session and a
restart. The file is a SEED now and the table is the truth, so the two things
that have to hold are: seeding never clobbers an edit, and the hunt list follows
the table without anything being restarted.
"""
import re
import shutil

import pytest
from fastapi.testclient import TestClient

from dealbot.config import load, with_store
from dealbot.db import Store
from dealbot.models import Want
from dealbot.web.app import create_app

CONFIG = "config.yaml"


@pytest.fixture
def app(tmp_path):
    shutil.copy(CONFIG, tmp_path / "config.yaml")
    cfg = load(tmp_path / "config.yaml")
    return TestClient(create_app(cfg)), cfg, Store(cfg.db_path)


def error_in(html):
    m = re.search(r'role="alert">.*?</svg>(.*?)</p>', html, re.S)
    return m.group(1).strip() if m else None


def test_the_file_seeds_the_table_once(tmp_path):
    store = Store(tmp_path / "t.db")
    w = Want(name="tv-stand", description="from the file", max_price_cents=25000,
             queries=("tv stand",))
    assert store.seed_wants([w]) == 1
    assert store.seed_wants([w]) == 0                  # never a second time


def test_seeding_again_cannot_undo_an_edit(tmp_path):
    """The whole point of the one-shot. Re-seeding per start would quietly
    revert every change made on the phone at the next timer tick."""
    store = Store(tmp_path / "t.db")
    store.seed_wants([Want("tv-stand", "from the file", 25000, ("tv stand",))])
    store.save_want(Want("tv-stand", "edited on the phone", 9900, ("console",)))
    store.seed_wants([Want("tv-stand", "from the file", 25000, ("tv stand",))])
    assert store.get_want("tv-stand").want.description == "edited on the phone"


def test_a_deleted_want_is_not_resurrected_by_the_file(tmp_path):
    store = Store(tmp_path / "t.db")
    store.seed_wants([Want("tv-stand", "d", 25000, ("tv stand",))])
    store.archive_want("tv-stand")
    store.seed_wants([Want("tv-stand", "d", 25000, ("tv stand",))])
    assert store.wants() == []
    assert len(store.wants(include_archived=True)) == 1   # nothing is deleted


def test_a_want_added_on_the_web_becomes_a_hunt(app):
    client, cfg, store = app
    before = {h.id for h in with_store(cfg, store).hunts}
    r = client.post("/wants/save", data={
        "name": "Patio Umbrella", "description": "a big one",
        "max_price": "80", "queries": "patio umbrella\nmarket umbrella",
        "requires": "at least 9 feet", "interval": "120"})
    assert r.status_code == 200                        # followed to /settings

    live = with_store(cfg, store)
    hunt = next(h for h in live.hunts if h.id == "want:patio-umbrella")
    assert hunt.id not in before
    assert hunt.max_price_cents == 8000
    assert hunt.queries == ("patio umbrella", "market umbrella")
    assert hunt.interval_minutes == 120
    # The sweep carries every want, so free stuff is judged against it too.
    sweep = next(h for h in live.hunts if h.kind == "sweep")
    assert "patio-umbrella" in [w.name for w in sweep.wants]


def test_removing_a_want_stops_its_hunt_and_keeps_its_history(app):
    from dealbot.models import Listing
    client, cfg, store = app
    client.post("/wants/save", data={"name": "lamp", "description": "d",
                                     "max_price": "20", "queries": "lamp"})
    lst = Listing(id="x:1", source="x", source_id="1", title="lamp",
                  description=None, price_cents=500, currency="USD", url="u")
    store.upsert_listing(lst)
    store.mark_matches("want:lamp", [lst])

    client.post("/wants/archive", data={"name": "lamp"})
    assert "want:lamp" not in [h.id for h in with_store(cfg, store).hunts]
    assert store.conn.execute(
        "SELECT COUNT(*) c FROM hunt_matches WHERE hunt_id='want:lamp'"
    ).fetchone()["c"] == 1
    assert client.get("/hunt/want:lamp").status_code == 200


def test_re_adding_a_removed_name_brings_it_back(app):
    client, cfg, store = app
    client.post("/wants/save", data={"name": "lamp", "description": "first",
                                     "max_price": "20", "queries": "lamp"})
    client.post("/wants/archive", data={"name": "lamp"})
    r = client.post("/wants/save", data={"name": "lamp", "description": "second",
                                         "max_price": "30", "queries": "lamp"})
    assert error_in(r.text) is None
    assert store.get_want("lamp").want.description == "second"
    assert not store.get_want("lamp").archived


def test_the_name_is_frozen_once_it_exists(app):
    """It is the hunt id, the URL, and the key every score is filed under.
    Renaming would orphan the lot, silently."""
    client, cfg, store = app
    client.post("/wants/save", data={"name": "lamp", "description": "d",
                                     "max_price": "20", "queries": "lamp"})
    client.post("/wants/save", data={"existing": "lamp", "name": "something-else",
                                     "description": "d2", "max_price": "25",
                                     "queries": "lamp"})
    assert store.get_want("something-else") is None
    assert store.get_want("lamp").want.description == "d2"


@pytest.mark.parametrize("data,fragment", [
    ({"name": "", "description": "d", "max_price": "5"}, "short name"),
    ({"name": "lamp", "description": " ", "max_price": "5"}, "looking for"),
    ({"name": "lamp", "description": "d", "max_price": "nope"}, "price cap"),
    ({"name": "lamp", "description": "d", "max_price": "0"}, "reject everything"),
])
def test_a_bad_form_says_why_and_keeps_what_was_typed(app, data, fragment):
    """A description is a paragraph of prose. Losing it to a mistyped price
    would be unforgivable on a phone."""
    client, _, _ = app
    r = client.post("/wants/save", data={**data, "queries": "lamp\nlight"})
    assert r.status_code == 400
    assert fragment in error_in(r.text)
    assert "lamp\nlight" in r.text                   # the textarea is repopulated


def test_a_duplicate_name_is_refused(app):
    client, _, _ = app
    client.post("/wants/save", data={"name": "lamp", "description": "d",
                                     "max_price": "20", "queries": "lamp"})
    r = client.post("/wants/save", data={"name": "Lamp", "description": "d",
                                         "max_price": "20", "queries": "lamp"})
    assert r.status_code == 400 and "already a want" in error_in(r.text)


def test_the_cadence_is_editable_and_cannot_go_below_the_timer(app):
    """The timer fires every 15 minutes, so anything under that is a number
    with no effect against two sources that throttle silently."""
    client, cfg, store = app
    client.post("/settings/interval", data={"hunt_id": "sweep:free-nearby",
                                            "minutes": "1"})
    assert store.hunt_intervals()["sweep:free-nearby"] == 15

    client.post("/settings/interval", data={"hunt_id": "sweep:free-nearby",
                                            "minutes": "240"})
    sweep = next(h for h in with_store(cfg, store).hunts if h.kind == "sweep")
    assert sweep.interval_minutes == 240


def test_one_want_can_be_paused_without_touching_the_others(app):
    client, cfg, store = app
    client.post("/hunts/toggle", data={"hunt_id": "want:tv-stand", "enable": "0"})
    assert store.disabled_hunts() == {"want:tv-stand"}


def test_the_settings_page_survives_a_want_the_file_never_knew(app):
    client, _, _ = app
    client.post("/wants/save", data={"name": "lamp", "description": "d",
                                     "max_price": "20", "queries": ""})
    body = client.get("/settings").text
    assert "lamp" in body
    assert "free stuff only" in body       # no queries means sweep-only, said so


def test_a_want_added_to_the_file_later_still_arrives(tmp_path):
    """The seed used to be one global flag, so anything added to config.yaml
    after a database's first open never appeared and never said why."""
    from dealbot.models import Want
    store = Store(tmp_path / "t.db")
    store.seed_wants([Want("tv-stand", "d", 25000, ("tv stand",))])
    assert [w.want.name for w in store.wants()] == ["tv-stand"]

    store.seed_wants([Want("tv-stand", "d", 25000, ("tv stand",)),
                      Want("lamp", "a lamp", 2000, ("lamp",))])
    assert sorted(w.want.name for w in store.wants()) == ["lamp", "tv-stand"]


def test_but_a_deleted_want_is_still_not_resurrected(tmp_path):
    """Safe only because deleting ARCHIVES: the row stays, so the name stays
    taken and per-name seeding cannot bring it back."""
    from dealbot.models import Want
    store = Store(tmp_path / "t.db")
    store.seed_wants([Want("lamp", "a lamp", 2000, ("lamp",))])
    store.archive_want("lamp")
    store.seed_wants([Want("lamp", "a lamp", 2000, ("lamp",))])
    assert store.wants() == []
    assert store.get_want("lamp").archived


def test_seeding_never_overwrites_an_edited_want(tmp_path):
    from dealbot.models import Want
    store = Store(tmp_path / "t.db")
    store.seed_wants([Want("lamp", "from the file", 2000, ("lamp",))])
    store.save_want(Want("lamp", "edited on the phone", 9900, ("lamp",)))
    store.seed_wants([Want("lamp", "from the file", 2000, ("lamp",))])
    assert store.get_want("lamp").want.description == "edited on the phone"


def test_the_four_tuning_levers_reach_the_hunts(app):
    """The wants bar especially: /skipped exists so the threshold can be judged
    rather than guessed at, and until now there was no way to act on what you
    learned there without an ssh session."""
    client, cfg, store = app
    client.post("/settings/tuning", data={
        "min_deal_score": "6.5", "free_find_min_score": "4",
        "max_results": "12", "radius_miles": "45"})
    live = with_store(cfg, store)
    assert live.defaults.min_deal_score == 6.5
    assert live.location.radius_miles == 45
    for hunt in live.hunts:
        assert hunt.min_deal_score == 6.5
        assert hunt.free_find_min_score == 4
        assert hunt.max_results == 12


def test_a_lever_cannot_be_set_somewhere_silly(app):
    """These are settings rows typed on a phone. A score of 99 or a radius of
    a thousand miles should be clamped, not obeyed, and a value that will not
    parse must not be able to stop the timer."""
    client, cfg, store = app
    client.post("/settings/tuning", data={
        "min_deal_score": "99", "free_find_min_score": "-4",
        "max_results": "9999", "radius_miles": "banana"})
    live = with_store(cfg, store)
    assert live.defaults.min_deal_score == 10.0
    assert live.defaults.free_find_min_score == 0.0
    assert live.defaults.max_results == 50
    assert live.location.radius_miles == cfg.location.radius_miles   # unchanged


def test_each_limit_says_what_it_does_and_what_it_costs(app):
    """A threshold you cannot see the effect of is one you guess at -- and a
    label that names an internal concept ("Wants bar", "Judge per run") is one
    only the person who built it can read."""
    client, _, _ = app
    body = client.get("/settings").text
    assert 'id="tuning"' in body
    for label in ("Show a want scoring at least",
                  "Show a free find scoring at least",
                  "Listings to send to the model each run",
                  "How far you will drive"):
        assert label in body, label
    assert "config.yaml" in body          # and says what is deliberately not here


def test_the_batch_cap_explains_itself_from_live_numbers(app):
    """The cap is per hunt per source, so what it means depends on how many of
    each there are. A typed number goes stale the moment a want is added."""
    client, cfg, store = app
    client.post("/settings/tuning", data={"max_results": "4"})
    live = with_store(cfg, store)
    combos = len(live.hunts) * len(live.sources)
    assert f"means up to {4 * combos} judged" in client.get("/settings").text
