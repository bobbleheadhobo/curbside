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
    assert r.status_code == 200                  # followed to the wants list

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
    ({"name": "lamp", "description": "d", "max_price": "-5"}, "negative"),
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


def test_the_wants_page_survives_a_want_the_file_never_knew(app):
    client, _, _ = app
    client.post("/wants/save", data={"name": "lamp", "description": "d",
                                     "max_price": "20", "queries": "lamp"})
    body = client.get("/").text
    assert "lamp" in body


def test_saving_a_want_lands_on_the_list_it_changed(app):
    """It used to land at the top of /settings, with the row you had just
    edited somewhere below the fold, past the hours and the limits."""
    client, _, _ = app
    r = client.post("/wants/save", follow_redirects=False,
                    data={"name": "lamp", "description": "d",
                          "max_price": "20", "queries": "lamp"})
    assert r.status_code == 303
    assert r.headers["location"] == "/?manage=1#manage"
    # ... and asking for it that way renders the panel already open.
    assert '<details class="panel manage" id="manage" open>' in client.get(
        "/?manage=1").text
    assert ' open>' not in client.get("/").text        # closed on a plain load


def test_the_wants_are_managed_on_the_wants_page_not_in_settings(app):
    """One list, one place to edit it. Settings keeps a signpost, because that
    is where it used to be."""
    client, _, _ = app
    client.post("/wants/save", data={"name": "lamp", "description": "d",
                                     "max_price": "20", "queries": "lamp"})
    front = client.get("/").text
    assert 'id="manage"' in front
    assert '/wants/lamp' in front and 'href="/wants/new"' in front

    settings = client.get("/settings").text
    assert 'href="/wants/lamp"' not in settings
    assert 'href="/?manage=1#manage"' in settings


def test_a_want_with_no_terms_says_so_rather_than_looking_normal(app):
    """The form will not make one any more, but config.yaml can still seed one
    and older wants predate the rule. It has no hunt of its own, so only the
    free sweep sees it -- and the sweep searches "free", not the thing you
    asked for. That is much less than it looks like, so the row says it."""
    from dealbot.models import Want
    client, _, store = app
    store.save_want(Want("lamp", "d", 2000, queries=()))
    assert "no search terms" in client.get("/").text


def test_a_free_only_want_does_not_say_up_to_0(app):
    from dealbot.models import Want
    client, _, store = app
    store.save_want(Want("kayak", "d", 0, queries=("kayak",)))
    body = client.get("/").text
    assert "Free ones only" in body
    assert "Up to $0" not in body


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


def test_every_tuning_lever_reaches_the_hunts(app):
    """The wants bar especially: /skipped exists so the threshold can be judged
    rather than guessed at, and until now there was no way to act on what you
    learned there without an ssh session."""
    client, cfg, store = app
    client.post("/settings/tuning", data={
        "min_deal_score": "6.5", "free_find_min_score": "4",
        "max_results": "12", "radius_miles": "45", "max_image_checks": "3"})
    live = with_store(cfg, store)
    assert live.defaults.min_deal_score == 6.5
    assert live.location.radius_miles == 45
    assert live.scorer.max_image_checks == 3
    for hunt in live.hunts:
        assert hunt.min_deal_score == 6.5
        assert hunt.free_find_min_score == 4
        assert hunt.max_results == 12


def test_an_image_budget_of_zero_means_never_look(app):
    """Zero is a legal answer here, unlike the batch cap, which clamps to 1: a
    hunt that judges nothing has been turned off and there is a pause switch
    for that, while an image pass is an extra on top of a judgement that
    happens either way. Someone who never wants to spend on photographs must be
    able to say so from the phone rather than by editing config.yaml."""
    client, cfg, store = app
    client.post("/settings/tuning", data={"max_image_checks": "0"})
    assert with_store(cfg, store).scorer.max_image_checks == 0


def test_a_lever_cannot_be_set_somewhere_silly(app):
    """These are settings rows typed on a phone. A score of 99 or a radius of
    a thousand miles should be clamped, not obeyed, and a value that will not
    parse must not be able to stop the timer."""
    client, cfg, store = app
    fetch = {"X-Requested-With": "fetch"}
    r = client.post("/settings/tuning", headers=fetch, data={
        "min_deal_score": "99", "free_find_min_score": "-4",
        "max_results": "9999"})
    live = with_store(cfg, store)
    assert live.defaults.min_deal_score == 10.0
    assert live.defaults.free_find_min_score == 0.0
    assert live.defaults.max_results == 50
    # Clamped, and SAID: it used to toast "Saved." over a number it changed.
    note = r.json()["note"]
    assert "The wants score saved as 10, the most it allows." in note
    assert "Listings per run saved as 50, the most it allows." in note

    # Unparseable is refused and named, never toasted as saved -- and nothing
    # else in the same post goes in half-way.
    r = client.post("/settings/tuning", headers=fetch, data={
        "radius_miles": "banana", "max_image_checks": "3"})
    assert r.json() == {"ok": False, "error": "The radius has to be a number."}
    live = with_store(cfg, store)
    assert live.location.radius_miles == cfg.location.radius_miles   # unchanged
    assert live.scorer.max_image_checks == cfg.scorer.max_image_checks


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
                  "Photos to look at each run",
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


def test_a_price_cap_of_zero_means_free_things_only(app):
    """It used to be refused as a mistake -- "a cap of zero would reject
    everything priced" -- which is exactly what someone asking for it wants.

    `over_price` drops anything dearer than the cap, so a cap of 0 keeps free
    listings and nothing else. It is how you say "I want one of these, but only
    if someone is giving it away", and it leaves the search terms free to mean
    what they say rather than doubling as a free-only switch."""
    from dealbot.filters import gate
    from dealbot.models import Listing, Location

    client, cfg, store = app
    r = client.post("/wants/save", follow_redirects=False,
                    data={"name": "kayak", "description": "a kayak",
                          "max_price": "0", "queries": "kayak"})
    assert r.status_code == 303, error_in(r.text)
    want = store.get_want("kayak").want
    assert want.max_price_cents == 0
    # ... and it still gets its own hunt, which the sweep is no substitute for:
    # the sweep searches "free", not "kayak".
    hunt = next(h for h in with_store(cfg, store).hunts if h.id == "want:kayak")
    assert hunt.queries == ("kayak",)

    def listing(lid, price):
        return Listing(id=lid, source="x", source_id=lid, title="kayak",
                       description=None, price_cents=price, currency="USD", url="u")

    gr = gate(hunt, [listing("x:1", 0), listing("x:2", 9900)],
              Location(lat=35.0844, lng=-106.6504, radius_miles=50.0), {}, {}, {})
    assert [c.listing.id for c in gr.candidates] == ["x:1"]
    assert gr.rejected == [("x:2", "over_price")]


class _FakeScorer:
    """Stands in for the real one. Records what it was asked."""
    def __init__(self, result=("tv stand", "media console"), boom=None):
        self.result, self.boom, self.calls = result, boom, []

    def suggest_queries(self, name, description, requires=()):
        self.calls.append((name, description, tuple(requires)))
        if self.boom:
            raise self.boom
        return self.result


def _app_with(tmp_path, scorer):
    cfg_path = tmp_path / "config.yaml"
    shutil.copy(CONFIG, cfg_path)
    cfg = load(cfg_path)
    return TestClient(create_app(cfg, scorer=scorer)), cfg, Store(cfg.db_path)


def test_the_suggest_button_drafts_terms_into_the_form(tmp_path):
    """Describing what you want and naming it the way a SELLER would are two
    different skills. "tv stand" and "media console" are the same object and
    share no word, so the terms are the part worth handing over.

    It fills the FIELD and hands the form back unsaved: these become two
    searches per tick for as long as the want exists, so they are read and
    edited by the person who will live with them before being committed to."""
    scorer = _FakeScorer()
    client, _, store = _app_with(tmp_path, scorer)

    r = client.post("/wants/save",
                    data={"name": "bookcase", "description": "a wide bookcase",
                          "max_price": "", "queries": "",
                          "requires": "at least 70 inches wide",
                          "action": "suggest"})
    assert r.status_code == 200
    assert "tv stand\nmedia console" in r.text        # in the textarea
    assert store.get_want("bookcase") is None          # and NOT saved
    # The requirements go too: they often name the object more precisely than
    # the prose does.
    assert scorer.calls == [("bookcase", "a wide bookcase",
                             ("at least 70 inches wide",))]


def test_suggesting_adds_to_typed_terms_rather_than_replacing_them(tmp_path):
    """A term typed into the box and not yet committed to a pill would have
    been thrown away. This form's rule is that a round trip loses nothing."""
    client, _, _ = _app_with(tmp_path, _FakeScorer(("media console", "tv stand")))
    r = client.post("/wants/save",
                    data={"name": "bookcase", "description": "a wide bookcase",
                          "max_price": "250", "queries": "tv stand\ncredenza",
                          "action": "suggest"})
    import re
    box = re.search(r'<textarea id="f-queries"[^>]*>(.*?)</textarea>', r.text, re.S)
    # what was there, kept and first; what was drafted, appended; no duplicate
    assert box.group(1).split("\n") == ["tv stand", "credenza", "media console"]


def test_the_suggest_button_answers_a_fetch_without_navigating(tmp_path):
    """Pressing it used to re-render the whole form and drop you at the top of
    it, having scrolled past the description you had just written. Same post,
    answered as JSON, and only the pills change.

    The merge stays on the server so both answers produce the same list in the
    same order."""
    client, _, store = _app_with(tmp_path, _FakeScorer(("media console",
                                                        "tv stand")))
    r = client.post("/wants/save", headers={"X-Requested-With": "fetch"},
                    data={"name": "bookcase", "description": "a wide bookcase",
                          "max_price": "250", "queries": "tv stand\ncredenza",
                          "action": "suggest"})
    assert r.status_code == 200
    assert r.json() == {"ok": True,
                        "queries": ["tv stand", "credenza", "media console"]}
    assert store.get_want("bookcase") is None          # still unsaved


@pytest.mark.parametrize("data,fragment", [
    ({"name": "bookcase", "description": " "}, "looking for"),
    ({"name": "", "description": "a wide bookcase"}, "short name"),
])
def test_a_fetch_that_cannot_draft_gets_a_sentence_not_a_page(tmp_path, data,
                                                              fragment):
    """Every way this can answer has a JSON twin, or the script downloads a
    400-page worth of HTML, fails to parse it and says nothing useful."""
    client, _, _ = _app_with(tmp_path, _FakeScorer())
    r = client.post("/wants/save", headers={"X-Requested-With": "fetch"},
                    data={**data, "max_price": "250", "queries": "",
                          "action": "suggest"})
    assert r.status_code == 200
    assert r.json()["ok"] is False and fragment in r.json()["error"]


def test_a_failed_draft_over_fetch_says_so_too(tmp_path):
    client, _, _ = _app_with(tmp_path, _FakeScorer(boom=RuntimeError("busy")))
    r = client.post("/wants/save", headers={"X-Requested-With": "fetch"},
                    data={"name": "kayak", "description": "a kayak",
                          "max_price": "300", "queries": "",
                          "action": "suggest"})
    assert r.json()["ok"] is False and "Could not draft" in r.json()["error"]


def test_suggesting_keeps_the_cadence_you_just_chose(tmp_path):
    """REGRESSION: the dropdown was re-read from the stored hunt, so pressing
    Suggest silently reset a cadence chosen seconds earlier."""
    import re
    client, _, _ = _app_with(tmp_path, _FakeScorer())
    r = client.post("/wants/save",
                    data={"name": "bookcase", "description": "a wide bookcase",
                          "max_price": "250", "queries": "", "interval": "15",
                          "action": "suggest"})
    picked = re.search(r'<option value="(\d+)" selected', r.text)
    assert picked and picked.group(1) == "15"


def test_suggesting_keeps_everything_else_that_was_typed(tmp_path):
    """It is a round trip through the form, so anything already filled in has
    to survive it -- losing a paragraph of prose to a button would be worse
    than not having the button."""
    client, _, _ = _app_with(tmp_path, _FakeScorer())
    r = client.post("/wants/save",
                    data={"name": "bookcase", "description": "a wide bookcase",
                          "max_price": "250", "queries": "",
                          "requires": "at least 70 inches wide",
                          "action": "suggest"})
    assert "a wide bookcase" in r.text
    assert "250" in r.text
    assert "at least 70 inches wide" in r.text


def test_saving_never_spends_on_terms_by_itself(tmp_path):
    """Only the button drafts. A plain save with the field empty is REFUSED
    rather than quietly drafting -- nothing is spent that was not asked for,
    and no half-useful want is created behind your back."""
    scorer = _FakeScorer()
    client, _, store = _app_with(tmp_path, scorer)
    r = client.post("/wants/save",
                    data={"name": "kayak", "description": "a kayak",
                          "max_price": "300", "queries": ""})
    assert r.status_code == 400
    assert "at least one search term" in error_in(r.text)
    assert store.get_want("kayak") is None
    assert scorer.calls == []


def test_terms_that_were_typed_are_never_second_guessed(tmp_path):
    scorer = _FakeScorer()
    client, _, store = _app_with(tmp_path, scorer)
    client.post("/wants/save",
                data={"name": "kayak", "description": "a kayak",
                      "max_price": "300", "queries": "kayak\nsit-on-top"})
    assert store.get_want("kayak").want.queries == ("kayak", "sit-on-top")
    assert scorer.calls == []


@pytest.mark.parametrize("boom", [
    RuntimeError("quota paused"),
    OSError("claude not found"),
])
def test_a_failed_draft_says_so_and_keeps_the_form(tmp_path, boom):
    """FAILS OPEN. The model being busy must not cost someone the paragraph of
    prose they just typed, so the form comes back intact with a note."""
    client, _, store = _app_with(tmp_path, _FakeScorer(boom=boom))
    r = client.post("/wants/save",
                    data={"name": "kayak", "description": "a kayak",
                          "max_price": "300", "queries": "",
                          "action": "suggest"})
    assert r.status_code == 200
    assert "Could not draft" in error_in(r.text)
    assert "a kayak" in r.text
    assert store.get_want("kayak") is None


def test_with_no_scorer_wired_the_button_is_harmless(tmp_path):
    """`create_app` takes the scorer optionally, so a dashboard built without
    one simply cannot draft -- it must not 500."""
    client, _, store = _app_with(tmp_path, None)
    r = client.post("/wants/save",
                    data={"name": "kayak", "description": "a kayak",
                          "max_price": "300", "queries": "",
                          "action": "suggest"})
    assert r.status_code == 200
    assert "Could not draft" in error_in(r.text)


def test_drafting_uses_the_model_the_config_names(tmp_path):
    """REGRESSION: `suggest_model` was added to the config, documented with a
    cost measurement, and then never read -- `suggest_queries` went on calling
    `triage_model`. The knob looked real and did nothing, and the default
    happened to match, so every check passed.

    Also asserts the spend is counted ONCE. `_invoke` already adds to
    `_spent_this_process`; adding it again made every draft count double
    against the daily ceiling, which is the overlapping-counters bug
    `begin_run` exists to prevent."""
    from dataclasses import replace as dc_replace
    from dealbot.config import load
    from dealbot.db import Store
    from dealbot.scoring.claude_code import ClaudeCodeScorer

    cfg = load(CONFIG)
    store = Store(tmp_path / "t.db")
    scorer = ClaudeCodeScorer(
        dc_replace(cfg.scorer, suggest_model="a-distinct-model",
                   triage_model="not-this-one", daily_cost_limit_usd=10.0),
        store)

    seen = {}

    class Facts:
        cost_usd = 0.25
        text = '{"queries": ["tv stand", "media console"]}'

    def fake_invoke(system, user, model, read_dir=None, timeout=None):
        seen["model"] = model
        seen["timeout"] = timeout
        # what the real one does, and the reason the caller must not repeat it
        scorer._spent_this_process += Facts.cost_usd
        return Facts()

    scorer._invoke = fake_invoke
    got = scorer.suggest_queries("tv-stand", "a wide tv stand")

    assert seen["model"] == "a-distinct-model", "suggest_model is not wired up"
    assert seen["timeout"] == scorer.SUGGEST_TIMEOUT_SECONDS, (
        "a person is waiting on a form; it must not use the 180s scoring timeout")
    assert got == ("tv stand", "media console")
    assert scorer._spent_this_process == 0.25, "the draft was counted twice"
    assert scorer.drain_unbilled() == 0.0, (
        "nothing drains unbilled spend in the web process; it would pile up unread")


# --- a want you are finished with should stop following you around ----------


def _want_with_listings(client, cfg, store, name="lamp"):
    """A want holding one of each status archiving has an opinion about."""
    from datetime import datetime, timezone
    from dealbot.models import Listing, Score, Want
    store.seed_wants((Want(name=name, description="a lamp",
                           max_price_cents=10000, queries=("lamp",)),))
    hid = f"want:{name}"
    made = {}
    for n, status in enumerate(("saved", "wanted", "scored", "dismissed",
                                "grabbed")):
        l = Listing(id=f"x:{n}", source="x", source_id=str(n),
                    title=f"A {status} lamp", description=None, price_cents=0,
                    currency="USD", url="u")
        store.upsert_listing(l)
        store.mark_matches(hid, [l])
        store.save_score(Score(listing_id=l.id, hunt_id=hid, model="m",
                               scored_at=datetime.now(timezone.utc),
                               match="yes", deal_score=8.0,
                               est_value_cents=None, condition=None,
                               matched_want=name, worth_grabbing=True,
                               unknowns=(), requirements=(), red_flags=(),
                               reasoning="r"), priced_at_cents=0)
        if status == "grabbed":
            store.mark_grabbed(hid, l.id, 0)
        else:
            store.set_status(hid, l.id, status)
        made[status] = l.id
    return hid, made


def test_removing_a_want_can_clear_what_it_found(app):
    """Two saved listings from a want deleted weeks ago were still sitting on
    /saved, and nothing but dismissing them one at a time would move them."""
    client, cfg, store = app
    hid, made = _want_with_listings(client, cfg, store)

    client.post("/wants/archive", data={"name": "lamp", "clear": "1"})
    after = store.statuses(hid)

    assert after[made["saved"]] == "archived"
    assert after[made["wanted"]] == "archived"
    assert after[made["scored"]] == "archived"
    assert "Nothing saved yet" in client.get("/saved").text


def test_clearing_is_not_deleting_and_not_dismissing(app):
    """`dismissed` is the obvious reuse and is wrong twice: those titles become
    negative examples in that hunt's next prompt, so it would teach the hunt to
    avoid exactly what you asked it to find -- and it would record that you
    rejected these when you did not.

    Nothing is deleted either. The rows keep their scores and stay readable."""
    client, cfg, store = app
    hid, made = _want_with_listings(client, cfg, store)
    client.post("/wants/archive", data={"name": "lamp", "clear": "1"})

    assert store.dismissed_titles(hid) == ["A dismissed lamp"], \
        "archiving taught the hunt nothing it was not already taught"
    n = store.conn.execute(
        "SELECT COUNT(*) n FROM hunt_matches WHERE hunt_id=?", (hid,)
    ).fetchone()["n"]
    assert n == 5, "every row is still there"
    kept = store.conn.execute(
        "SELECT COUNT(*) n FROM scores WHERE hunt_id=?", (hid,)).fetchone()["n"]
    assert kept == 5, "every score is still there"


def test_a_thing_you_own_is_not_cleared_with_the_want(app):
    """Deleting the want you found it through does not un-own it."""
    client, cfg, store = app
    hid, made = _want_with_listings(client, cfg, store)
    client.post("/wants/archive", data={"name": "lamp", "clear": "1"})

    assert store.statuses(hid)[made["grabbed"]] == "grabbed"
    assert "A grabbed lamp" in client.get("/saved?show=grabbed").text


def test_removing_a_want_without_the_tick_leaves_its_bins_alone(app):
    """Unchecking is right while you are still driving out to something it
    found."""
    client, cfg, store = app
    hid, made = _want_with_listings(client, cfg, store)
    client.post("/wants/archive", data={"name": "lamp"})

    assert store.statuses(hid)[made["saved"]] == "saved"
    assert "A saved lamp" in client.get("/saved").text


def test_restoring_a_want_brings_its_list_back(app):
    """They remember what they were, so this is not a one-way door."""
    client, cfg, store = app
    hid, made = _want_with_listings(client, cfg, store)
    client.post("/wants/archive", data={"name": "lamp", "clear": "1"})
    client.post("/wants/archive", data={"name": "lamp", "restore": "1"})

    after = store.statuses(hid)
    assert after[made["saved"]] == "saved"
    assert after[made["wanted"]] == "wanted"
    assert after[made["scored"]] == "scored"


def test_grabbing_can_end_the_search_it_came_from(app):
    """Finding the thing is the reason the want existed, and this is the one
    moment the user knows the search is over."""
    client, cfg, store = app
    hid, made = _want_with_listings(client, cfg, store)

    client.post("/grabbed", data={"hunt_id": hid, "listing_id": made["saved"],
                                  "paid": "40", "done": "1"})

    assert "lamp" not in [w.want.name for w in store.wants()], "the search stopped"
    after = store.statuses(hid)
    assert after[made["saved"]] == "grabbed", "the thing you just bought"
    assert after[made["wanted"]] == "archived", "the rest of the search"


def test_grabbing_without_the_tick_keeps_looking(app):
    """You might be buying one of several."""
    client, cfg, store = app
    hid, made = _want_with_listings(client, cfg, store)
    client.post("/grabbed", data={"hunt_id": hid, "listing_id": made["saved"],
                                  "paid": "40"})
    assert "lamp" in [w.want.name for w in store.wants()]


def test_the_free_sweep_is_never_finished(app):
    """The tick is offered on a want hunt only: there is no "done" for the
    trawl that finds things you never thought to search for."""
    client, cfg, store = app
    _want_with_listings(client, cfg, store)
    assert "Done looking for" in client.get("/saved").text
    assert "sweep:" not in client.get("/saved").text.split("donerow")[0][-400:]


def test_a_want_removed_before_the_tick_existed_can_still_be_cleared(app):
    """The tick only helps wants removed from now on, and the case that
    prompted this was a want deleted weeks ago with two saved listings still
    on /saved. Offered on the Removed list, and only when there is something
    to clear -- a button that would do nothing is worse than no button."""
    client, cfg, store = app
    hid, made = _want_with_listings(client, cfg, store)
    client.post("/wants/archive", data={"name": "lamp"})      # no tick

    panel = client.get("/?manage=1").text
    assert "Clear 3" in panel, "saved + wanted + scored, not the grabbed one"

    client.post("/wants/archive", data={"name": "lamp", "clear": "1"})
    assert "Clear 3" not in client.get("/?manage=1").text
    assert store.statuses(hid)[made["saved"]] == "archived"


def test_nothing_to_clear_offers_no_button(app):
    client, cfg, store = app
    hid, made = _want_with_listings(client, cfg, store)
    client.post("/wants/archive", data={"name": "lamp", "clear": "1"})
    assert "Clear " not in client.get("/?manage=1").text


# --- when a want is asking the wrong question -------------------------------


def _dismiss_at(store, hunt_id, n, score, start=100):
    """`n` dismissals on `hunt_id`, each scored `score`."""
    from datetime import datetime, timezone
    from dealbot.models import Listing, Score
    for i in range(n):
        lid = f"d:{hunt_id}:{start + i}"
        l = Listing(id=lid, source="x", source_id=str(start + i),
                    title=f"A bookshelf {i}", description=None, price_cents=0,
                    currency="USD", url="u")
        store.upsert_listing(l)
        store.mark_matches(hunt_id, [l])
        store.save_score(Score(listing_id=lid, hunt_id=hunt_id, model="m",
                               scored_at=datetime.now(timezone.utc),
                               match="yes", deal_score=score,
                               est_value_cents=None, condition=None,
                               matched_want="lamp", worth_grabbing=True,
                               unknowns=(), requirements=(), red_flags=(),
                               reasoning="r"), priced_at_cents=0)
        store.set_status(hunt_id, lid, "dismissed")


def test_only_dismissals_that_overrule_the_model_count(app):
    """Every hunt here sits at 97-100% dismissed, because that is how a bin
    gets emptied -- a rate cannot tell a want that is asking the wrong question
    from one that is working. What can: dismissing something the hunt's own bar
    called good enough. `want:bookshelf` has 36 dismissals and none over the
    bar, and it is the want with nothing wrong with it."""
    client, cfg, store = app
    _dismiss_at(store, "want:agree", 8, score=3.0)
    _dismiss_at(store, "want:disagree", 4, score=9.0, start=200)

    assert store.overruled("want:agree", 7.0) == 0
    assert store.overruled("want:disagree", 7.0) == 4


def test_the_nudge_appears_only_once_there_is_a_pattern(app):
    client, cfg, store = app
    store.seed_wants((Want(name="lamp", description="a lamp",
                           max_price_cents=10000, queries=("lamp",)),))

    _dismiss_at(store, "want:lamp", 1, score=9.0)
    assert "Tighten what it asks for" not in client.get("/?manage=1").text, \
        "one is a fluke, not a pattern"

    _dismiss_at(store, "want:lamp", 1, score=9.0, start=300)
    body = client.get("/?manage=1").text
    assert "Tighten what it asks for" in body
    assert "<b>2</b> that scored well enough" in body


def test_rewriting_the_want_is_the_acknowledgement(app):
    """The dismissals do not disappear when you act on them, so without a
    baseline the nudge would never stop."""
    client, cfg, store = app
    store.seed_wants((Want(name="lamp", description="a lamp",
                           max_price_cents=10000, queries=("lamp",)),))
    _dismiss_at(store, "want:lamp", 4, score=9.0)
    assert "Tighten what it asks for" in client.get("/?manage=1").text

    client.post("/wants/save", data={
        "existing": "lamp", "name": "lamp", "description": "a lamp, big",
        "max_price": "100", "queries": "lamp", "requires": "at least 20 inches",
        "interval": 60})
    assert "Tighten what it asks for" not in client.get("/?manage=1").text

    # And it comes back only if the new rule does not catch them either.
    _dismiss_at(store, "want:lamp", 3, score=9.0, start=400)
    assert "Tighten what it asks for" in client.get("/?manage=1").text


def test_a_stopped_want_is_not_nagged(app):
    """It cannot act on the advice, and the row's job is Restore or Clear."""
    client, cfg, store = app
    store.seed_wants((Want(name="lamp", description="a lamp",
                           max_price_cents=10000, queries=("lamp",)),))
    _dismiss_at(store, "want:lamp", 5, score=9.0)
    client.post("/wants/archive", data={"name": "lamp"})
    assert "Tighten what it asks for" not in client.get("/?manage=1").text


def test_the_nudge_is_about_one_want_not_about_you(app):
    """Three over-bar dismissals spread across three wants are three separate
    disagreements and say nothing about any of them. The count is per hunt, so
    none of those three wants is nudged."""
    client, cfg, store = app
    from dealbot.models import Want
    for n in ("one", "two", "three"):
        store.seed_wants((Want(name=n, description=n, max_price_cents=10000,
                               queries=(n,)),))
        _dismiss_at(store, f"want:{n}", 1, score=9.0, start=500 + 50 * "one two three".split().index(n))

    body = client.get("/?manage=1").text
    assert "Tighten what it asks for" not in body
    for n in ("one", "two", "three"):
        assert store.overruled(f"want:{n}", 7.0) == 1


# --- a search term is a standing charge ---------------------------------------
#
# Each term is one request per source on every run of the want, paid whether or
# not it finds anything, out of a Facebook budget of 25 a pass shared by every
# hunt. So the list is capped, its cost is on the form, and each term's record
# is on the editor, because trimming by guesswork is how a want ends up with
# four brand searches that may find nothing the plain one does not.

def test_a_term_past_the_cap_is_refused_and_nothing_typed_is_lost(app):
    from dealbot.models import MAX_QUERIES
    client, _, store = app
    terms = "\n".join(f"term {i}" for i in range(MAX_QUERIES + 1))
    r = client.post("/wants/save", data={
        "name": "receiver", "description": "a receiver with wifi",
        "max_price": "150", "queries": terms, "requires": "wifi"})
    assert r.status_code == 400
    assert f"At most {MAX_QUERIES}" in error_in(r.text)
    assert store.get_want("receiver") is None
    assert "a receiver with wifi" in r.text and f"term {MAX_QUERIES}" in r.text

    ok = "\n".join(f"term {i}" for i in range(MAX_QUERIES))
    r = client.post("/wants/save", data={
        "name": "receiver", "description": "a receiver with wifi",
        "max_price": "150", "queries": ok})
    assert store.get_want("receiver") is not None


def test_a_full_list_does_not_pay_to_draft_more(tmp_path):
    """Anything drafted would be thrown away, so nothing is spent drafting."""
    from dealbot.models import MAX_QUERIES
    scorer = _FakeScorer()
    client, _, _ = _app_with(tmp_path, scorer)
    r = client.post("/wants/save", headers={"X-Requested-With": "fetch"}, data={
        "name": "bookcase", "description": "a wide bookcase",
        "queries": "\n".join(f"t{i}" for i in range(MAX_QUERIES)),
        "action": "suggest"})
    assert r.json()["ok"] is False and "Remove one" in r.json()["error"]
    assert scorer.calls == []


def test_drafting_stops_at_the_cap(tmp_path):
    """Typed terms first, drafted ones after, and never more than a save would
    accept -- otherwise the button hands back a form that cannot be saved."""
    from dealbot.models import MAX_QUERIES
    typed = [f"t{i}" for i in range(MAX_QUERIES - 1)]
    client, _, _ = _app_with(tmp_path, _FakeScorer(("a", "b", "c")))
    r = client.post("/wants/save", headers={"X-Requested-With": "fetch"}, data={
        "name": "bookcase", "description": "a wide bookcase",
        "queries": "\n".join(typed), "action": "suggest"})
    assert r.json()["queries"] == typed + ["a"]


def test_the_form_says_what_the_terms_cost(app):
    """A want saved before the cap keeps working, and its editor says it is
    over rather than waiting for the save to refuse it."""
    from dealbot.models import MAX_QUERIES
    client, _, store = app
    store.save_want(Want("receiver", "d", 15000,
                         tuple(f"t{i}" for i in range(MAX_QUERIES + 1))))
    page = client.get("/wants/receiver").text
    assert re.search(rf'termcost overcap"[^>]*>\s*<b>{MAX_QUERIES + 1}</b> of '
                     rf'{MAX_QUERIES} terms', page)

    store.save_want(Want("lamp", "d", 5000, ("lamp",)))
    page = client.get("/wants/lamp").text
    assert "overcap" not in page and f"<b>1</b> of {MAX_QUERIES} terms" in page


def test_the_editor_says_what_each_term_has_found(app):
    client, cfg, store = app
    store.save_want(Want("receiver", "d", 15000,
                         ("AV receiver", "Denon receiver", "Onkyo receiver")))
    store.record_query_hits("want:receiver", {
        "AV receiver": {"x:1", "x:2", "x:3"},
        "Denon receiver": {"x:1", "x:4"},        # x:4 is a find of its own
        "Onkyo receiver": {"x:2"},               # nothing the others missed
    })
    store.conn.commit()
    page = client.get("/wants/receiver").text
    assert "What each term finds" in page
    rows = dict(re.findall(r'class="who">([^<]+?)\s*<small>(.*?)</small>',
                           page, re.S))
    assert "3 found &middot; 1 only by this term" in rows["AV receiver"]
    assert "2 found &middot; 1 only by this term" in rows["Denon receiver"]
    assert "1 found &middot; 0 only by this term" in rows["Onkyo receiver"]
    assert "Counted since" in page


def test_a_term_you_removed_cannot_make_another_look_redundant(tmp_path):
    """"Only by this term" is measured against the terms the want has NOW.
    A deleted term is not searched any more, so what it once found is not
    being found by anything else."""
    store = Store(tmp_path / "t.db")
    store.record_query_hits("want:w", {"kept": {"x:1"}, "deleted": {"x:1"}})
    y = store.query_yield("want:w", ["kept"], bar=7.0)
    assert y["kept"]["only"] == 1


def test_made_the_bar_is_the_hunts_own_bar(tmp_path):
    """Found only by one term AND judged good enough to show you, by the
    hunt's latest score. That is the number that says a term is worth its
    request, rather than merely busy."""
    from datetime import datetime, timezone
    from conftest import make_listing
    from dealbot.models import Score
    store = Store(tmp_path / "t.db")
    for n in (1, 2, 3):
        store.upsert_listing(make_listing(lid=f"x:{n}", title=f"receiver {n}"))
    store.record_query_hits("want:w", {"a": {"x:1", "x:2", "x:3"}})

    def score(lid, s, match="yes"):
        store.save_score(Score(
            listing_id=lid, hunt_id="want:w", model="m",
            scored_at=datetime.now(timezone.utc), match=match, deal_score=s,
            est_value_cents=None, condition=None, matched_want=None,
            worth_grabbing=False, unknowns=(), requirements=(), red_flags=(),
            reasoning=""), priced_at_cents=None)

    score("x:1", 8.0)
    score("x:2", 9.0, match="no")      # a good price on the wrong thing
    score("x:3", 9.0)
    score("x:3", 4.0)                  # judged again, lower: the latest counts
    y = store.query_yield("want:w", ["a"], bar=7.0)["a"]
    assert (y["found"], y["only"], y["good"]) == (3, 3, 1)
