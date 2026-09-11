"""Never show me this again.

A blocked word is the counterpart to a dismissal: deterministic, listed on a
page, and countable as `excluded_kw:<term>` on the hunt view. It is also the
only rule in the gate that fails CLOSED -- a blocked listing is dropped without
ever being read -- so the matching is narrow and there is a guard against
blocking the thing you are hunting for.
"""
import shutil

import pytest
from fastapi.testclient import TestClient

from dealbot.config import load, with_store
from dealbot.db import Store
from dealbot.filters import matches_any
from dealbot.web.app import create_app

from conftest import make_listing

CONFIG = "config.yaml"
SWEEP = "sweep:free-nearby"


@pytest.fixture
def app(tmp_path):
    shutil.copy(CONFIG, tmp_path / "config.yaml")
    cfg = load(tmp_path / "config.yaml")
    return TestClient(create_app(cfg)), cfg, Store(cfg.db_path)


# --- matching ---------------------------------------------------------------

@pytest.mark.parametrize("term,title,hit", [
    ("bed", "Free bed frame", True),
    ("bed", "Two beds", True),
    ("bed", "Free bedroom set", False),        # the trap substrings walked into
    ("mattress", "King mattress", True),
    ("mattress", "Free mattresses", True),     # plurals are the common case
    ("couch", "Couches for free", True),
    ("rug", "Drugstore fixtures", False),
    ("free estimate", "Free estimate today", True),
    ("tv", "Old TVs", True),
])
def test_a_blocked_word_matches_whole_words_and_plurals(term, title, hit):
    got = matches_any(make_listing(title=title, description=None), [term])
    assert (got == term) is hit


def test_a_term_that_is_only_in_the_description_still_fires():
    """The gate sees no description, so this only bites after enrichment --
    which is exactly why the pipeline runs the check a second time."""
    lst = make_listing(title="Free stuff", description="includes a mattress")
    assert matches_any(lst, ["mattress"]) == "mattress"


# --- storage and the gate ---------------------------------------------------

def test_a_blocked_word_reaches_the_hunt_and_drops_listings(app):
    from dealbot.filters import gate
    client, cfg, store = app
    client.post("/settings/exclude", data={"hunt_id": SWEEP, "term": "Mattress"})

    sweep = next(h for h in with_store(cfg, store).hunts if h.id == SWEEP)
    assert "mattress" in sweep.exclude
    assert "free estimate" in sweep.exclude          # the file's terms survive

    # price 0: the free sweep caps at free, so anything priced is rejected
    # for that reason instead and proves nothing about the blocked word.
    listings = [make_listing(lid="fixture:1", title="Free king mattress",
                             price_cents=0),
                make_listing(lid="fixture:2", title="Free tv stand",
                             price_cents=0)]
    result = gate(sweep, listings, cfg.location, statuses={}, last_scores={},
                  upserts={})
    assert [c.listing.id for c in result.candidates] == ["fixture:2"]
    assert result.rejected == [("fixture:1", "excluded_kw:mattress")]


def test_blocking_a_word_you_are_hunting_for_is_refused(app):
    """Blocking "console" on the sweep would drop the free media console the
    tv-stand hunt exists to find, and nothing would ever say so."""
    client, cfg, store = app
    r = client.post("/settings/exclude", data={"hunt_id": SWEEP, "term": "console"},
                    follow_redirects=False)
    assert r.headers["location"] == "/settings?err=wanted:tv-stand"
    assert "console" not in store.hunt_excludes().get(SWEEP, ())

    j = client.post("/settings/exclude", headers={"X-Requested-With": "fetch"},
                    data={"hunt_id": SWEEP, "term": "ottoman"}).json()
    assert j["ok"] is False and "stacked-ottoman" in j["error"]


def test_a_word_too_short_to_be_safe_is_refused(app):
    client, _, store = app
    assert client.post("/settings/exclude", data={"hunt_id": SWEEP, "term": "ab"},
                       follow_redirects=False
                       ).headers["location"] == "/settings?err=short"
    assert client.post("/settings/exclude", headers={"X-Requested-With": "fetch"},
                       data={"hunt_id": SWEEP, "term": "ab"}
                       ).json()["ok"] is False
    assert "ab" not in store.hunt_excludes().get(SWEEP, ())


def test_terms_round_trip_and_can_be_removed(app):
    client, _, store = app
    client.post("/settings/exclude", data={"hunt_id": SWEEP, "term": "  Firewood "})
    assert "firewood" in store.hunt_excludes()[SWEEP]        # trimmed, lowered
    client.post("/settings/exclude", data={"hunt_id": SWEEP, "term": "firewood",
                                           "remove": "1"})
    assert "firewood" not in store.hunt_excludes().get(SWEEP, ())


def test_what_a_term_has_cost_is_countable(app):
    """The number beside a term is the point of it being on a page. A term you
    cannot count is a term you cannot tell is too broad."""
    client, _, store = app
    lst = make_listing(lid="fixture:9", title="Free mattress")
    store.upsert_listing(lst)
    store.mark_matches(SWEEP, [lst])
    store.record_rejections(SWEEP, [(lst.id, "excluded_kw:mattress")])
    assert store.exclude_counts(SWEEP) == {"mattress": 1}

    client.post("/settings/exclude", data={"hunt_id": SWEEP, "term": "mattress"})
    body = client.get("/settings").text
    assert "mattress" in body and "Never show me" in body


def test_the_file_seeds_the_list_once_and_then_it_is_yours(app):
    """Same one-shot as wants, and for the same reason: a list where four of
    the entries can never be deleted is not a list you can edit."""
    client, cfg, store = app
    body = client.get("/settings").text
    assert "free estimate" in body and "Never show me" in body
    assert 'class="term locked"' not in body            # nothing is locked now

    client.post("/settings/exclude", data={"hunt_id": SWEEP,
                                           "term": "free quote", "remove": "1"})
    assert "free quote" not in with_store(cfg, store).hunts[0].exclude
    # and it stays gone -- the file does not put it back on the next load
    client.get("/settings")
    assert "free quote" not in with_store(cfg, store).hunts[0].exclude


def test_the_list_has_its_own_section_and_the_free_page_points_at_it(app):
    """It was a field at the bottom of the sweep panel and nobody found it."""
    client, _, _ = app
    assert 'id="blocked"' in client.get("/settings").text
    assert '/settings#blocked' in client.get("/free").text


# --- the daily loop ---------------------------------------------------------

def test_triage_answers_a_fetch_without_a_redirect(app):
    """204 to a script, 303 to a form, so one endpoint serves both and the
    buttons keep working with JavaScript off."""
    client, _, store = app
    lst = make_listing(lid="fixture:probe", title="A thing", price_cents=0)
    store.upsert_listing(lst)
    store.mark_matches(SWEEP, [lst])
    assert client.post("/triage", headers={"X-Requested-With": "fetch"},
                       data={"hunt_id": SWEEP, "listing_id": lst.id,
                             "status": "saved"}).status_code == 204
    assert client.post("/triage", follow_redirects=False,
                       data={"hunt_id": SWEEP, "listing_id": lst.id,
                             "status": "saved", "back": "/free"}
                       ).status_code == 303


def test_a_free_card_carries_what_the_script_needs(tmp_path):
    """Undo has to put the card back in the state it left, so the card states
    what that was. Without data-status an undo would guess."""
    from datetime import datetime, timezone
    from dealbot.models import Score
    shutil.copy(CONFIG, tmp_path / "config.yaml")
    cfg = load(tmp_path / "config.yaml")
    store = Store(cfg.db_path)
    lst = make_listing(lid="fixture:5", title="Free king mattress", price_cents=0)
    store.upsert_listing(lst)
    store.mark_matches(SWEEP, [lst])
    store.save_score(Score(listing_id=lst.id, hunt_id=SWEEP, model="m",
                           scored_at=datetime.now(timezone.utc), match="no",
                           deal_score=6.0, est_value_cents=None, condition=None,
                           matched_want=None, worth_grabbing=True, unknowns=(),
                           requirements=(), red_flags=(), reasoning="r"),
                     priced_at_cents=0)
    store.set_status(SWEEP, lst.id, "free_find")

    body = TestClient(create_app(cfg)).get("/free").text
    assert 'data-status="free_find"' in body
    assert f'data-listing="{lst.id}"' in body
    assert 'class="blocktoggle"' in body            # only the free sweep gets it
    assert 'action="/triage"' in body               # still a real form


def test_the_wants_page_has_no_block_button(tmp_path):
    """Blocking a word on a want hunt would block the thing you are hunting.
    The affordance only exists where it is safe."""
    shutil.copy(CONFIG, tmp_path / "config.yaml")
    client = TestClient(create_app(load(tmp_path / "config.yaml")))
    # The script naming the class ships on every page; the button does not.
    assert 'class="blocktoggle"' not in client.get("/").text


def test_the_script_and_the_markup_still_agree(tmp_path):
    """The interaction layer finds things by selector, so a rename in a
    template is a silent breakage: the buttons keep working, they just go back
    to reloading the page and dumping you at the top, which is the thing this
    was built to stop. Assert the contract in both directions.
    """
    from pathlib import Path
    from datetime import datetime, timezone
    from dealbot.models import Score
    shutil.copy(CONFIG, tmp_path / "config.yaml")
    cfg = load(tmp_path / "config.yaml")
    store = Store(cfg.db_path)
    lst = make_listing(lid="fixture:7", title="Free recliner", price_cents=0)
    store.upsert_listing(lst)
    store.mark_matches(SWEEP, [lst])
    store.save_score(Score(listing_id=lst.id, hunt_id=SWEEP, model="m",
                           scored_at=datetime.now(timezone.utc), match="no",
                           deal_score=6.0, est_value_cents=None, condition=None,
                           matched_want=None, worth_grabbing=True, unknowns=(),
                           requirements=(), red_flags=(), reasoning="r"),
                     priced_at_cents=0)
    store.set_status(SWEEP, lst.id, "free_find")
    body = TestClient(create_app(cfg)).get("/free").text

    for needed in ('<article class="card"', 'name="status"', 'class="info"',
                   'class="blockrow"', 'class="chips"', 'class="addterm"',
                   'data-bincount', 'data-hunt=', 'data-listing=',
                   'data-status=', 'action="/triage"',
                   'id="i-saved"', 'id="i-x"', 'id="i-gone"', 'id="i-check"'):
        assert needed in body, needed

    script = (Path(__file__).resolve().parents[1]
              / "dealbot/web/templates/base.html").read_text()
    # It must stay a progressive enhancement: real forms, intercepted.
    assert 'preventDefault' in script and 'X-Requested-With' in script
    assert '"/triage"' in script and '"/settings/exclude"' in script
    assert 'data-inplace' in script


def test_undo_can_put_a_skipped_card_back(app):
    """A card on /skipped is `scored`, so undo has to be able to write that
    status back. Rejecting it would leave the list showing a card the database
    thinks was dismissed."""
    client, _, store = app
    lst = make_listing(lid="fixture:8", title="A thing", price_cents=0)
    store.upsert_listing(lst)
    store.mark_matches(SWEEP, [lst])
    store.set_status(SWEEP, lst.id, "dismissed")
    client.post("/triage", headers={"X-Requested-With": "fetch"},
                data={"hunt_id": SWEEP, "listing_id": lst.id, "status": "scored"})
    assert store.statuses(SWEEP)[lst.id] == "scored"


# --- nothing that saves should throw you back to the top --------------------

INPLACE = [
    ("/hunts/toggle", {"kind": "sweep", "enable": "0"}),
    ("/settings/hours", {"enabled": "1", "start": "12:00", "end": "20:00"}),
    ("/settings/interval", {"hunt_id": SWEEP, "minutes": "60"}),
    ("/settings/exclude", {"hunt_id": SWEEP, "term": "firewood"}),
    ("/wants/archive", {"name": "tv-stand", "restore": "1"}),
    ("/triage", {"hunt_id": SWEEP, "listing_id": "fixture:probe",
                 "status": "saved"}),
]


@pytest.fixture(autouse=True)
def _a_listing_to_triage(app):
    """`/triage` 404s on a pair it cannot find, so the probe needs a real row."""
    _, _, store = app
    lst = make_listing(lid="fixture:probe", title="A thing", price_cents=0)
    store.upsert_listing(lst)
    store.mark_matches(SWEEP, [lst])


@pytest.mark.parametrize("path,data", INPLACE)
def test_every_saving_endpoint_answers_a_fetch_without_navigating(app, path, data):
    """A 303 sends the browser somewhere, which on a settings screen means
    reloading and hunting for where you were. These answer 204 or JSON to a
    fetch, and keep the redirect for when the script is absent."""
    client, _, _ = app
    r = client.post(path, data=data, headers={"X-Requested-With": "fetch"},
                    follow_redirects=False)
    assert r.status_code in (200, 204), path
    if r.status_code == 200:
        assert r.json().get("ok") is True, path

    plain = client.post(path, data={**data, "back": "/settings"},
                        follow_redirects=False)
    assert plain.status_code == 303, path          # still works with no script


def test_every_in_place_form_posts_to_one_of_those(tmp_path):
    """The attribute and the endpoint have to agree. A form marked
    data-inplace whose endpoint still redirects would fetch the redirect,
    download a page, and silently do nothing visible."""
    import re
    from pathlib import Path
    answers = {path for path, _ in INPLACE}
    root = Path(__file__).resolve().parents[1] / "dealbot/web/templates"
    found = 0
    for tpl in root.glob("*.html"):
        for m in re.finditer(r'<form[^>]*\bdata-inplace\b[^>]*>', tpl.read_text()):
            action = re.search(r'action="([^"]+)"', m.group(0))
            assert action, f"{tpl.name}: data-inplace form with no action"
            assert action.group(1) in answers, f"{tpl.name}: {action.group(1)}"
            found += 1
    assert found >= 7, f"expected the settings and runs forms, found {found}"


@pytest.mark.parametrize("template,route", [
    ("settings.html", "/settings"),
    ("runs.html", "/runs"),
    ("listing.html", None),
    ("want_form.html", "/wants/tv-stand"),
])
def test_every_in_place_target_is_something_the_server_renders(app, template, route):
    """The handler swaps the target element for the same element in a freshly
    fetched copy of the page, so the selector must resolve in BOTH. It used to
    invent an id on the live element when a form named no target, then ask the
    fresh html for that id and find nothing: the save went through, the toast
    fired, and the panel kept showing stale state."""
    import re
    from pathlib import Path
    client, _, _ = app
    tpl = (Path(__file__).resolve().parents[1] / "dealbot/web/templates"
           / template).read_text()
    targets = [t for t in re.findall(r'data-inplace="([^"]*)"', tpl) if t]
    assert len(targets) == len(re.findall(r'\bdata-inplace\b', tpl)), (
        f"{template}: a data-inplace with no target relies on the default")
    if route is None:
        return
    body = client.get(route).text
    for t in targets:
        if t == "main":
            assert "<main" in body
        else:
            assert f'id="{t[1:]}"' in body, f"{template}: {t} is not rendered"


def test_a_triage_that_matches_nothing_says_so(app):
    """It was a silent no-op. Survivable while every button reloaded the page
    and you saw the listing still sitting there; a lie once the card folds away
    and a toast says "Dismissed"."""
    client, _, _ = app
    r = client.post("/triage", headers={"X-Requested-With": "fetch"},
                    data={"hunt_id": SWEEP, "listing_id": "fixture:ghost",
                          "status": "dismissed"})
    assert r.status_code == 404

    bad = client.post("/triage", headers={"X-Requested-With": "fetch"},
                      data={"hunt_id": SWEEP, "listing_id": "fixture:probe",
                            "status": "nonsense"})
    assert bad.status_code == 400


def test_a_new_hunt_in_the_file_gets_its_terms(app):
    """Per hunt, not one global flag: a sweep added to config.yaml later still
    gets seeded. A hunt that already has a list is left alone, however short,
    so a term removed on the dashboard stays removed."""
    _, _, store = app
    store.set_hunt_excludes(SWEEP, ["mattress"])
    store.seed_excludes({SWEEP: ["free estimate"],
                         "sweep:curb-alerts": ["mattress", "firewood"]})
    assert store.hunt_excludes()[SWEEP] == ("mattress",)          # untouched
    assert store.hunt_excludes()["sweep:curb-alerts"] == ("mattress", "firewood")
