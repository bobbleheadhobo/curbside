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
