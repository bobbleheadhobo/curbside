"""Dashboard rendering of data we collect. All of this existed in the database
and none of it was visible."""
import pytest
from dealbot.web.app import _sparkline


def test_sparkline_needs_two_real_observations():
    assert _sparkline([]) is None
    assert _sparkline([("t", 100)]) is None
    assert _sparkline([("t", None), ("t", None)]) is None


def test_sparkline_draws_and_colours_a_fall():
    svg = _sparkline([("t1", 20000), ("t2", 15000), ("t3", 5000)])
    assert svg.startswith("<svg") and "polyline" in svg
    assert "--good" in svg            # a falling price is the interesting one


def test_sparkline_of_a_firm_seller_is_not_highlighted():
    assert "--dim" in _sparkline([("t1", 5000), ("t2", 5000), ("t3", 5000)])


def test_rejection_reasons_are_countable(tmp_path):
    """"118 rejected on over_price" says the cap is wrong far faster than
    reading listings one at a time."""
    from dealbot.db import Store
    from dealbot.models import Listing
    from dealbot.web.app import REJECT_REASONS_SQL
    s = Store(tmp_path / "t.db")
    for i, reason in enumerate(["over_price", "over_price", "too_far"]):
        l = Listing(id=f"x:{i}", source="x", source_id=str(i), title="t",
                    description=None, price_cents=0, currency="USD", url="u")
        s.upsert_listing(l)
        s.mark_matches("h", [l])
        s.record_rejections("h", [(l.id, reason)])
    got = {r["filter_reason"]: r["n"] for r in
           s.conn.execute(REJECT_REASONS_SQL, ("h",))}
    assert got == {"over_price": 2, "too_far": 1}


def _client(tmp_path):
    """A dashboard wired to a throwaway database."""
    import shutil
    from fastapi.testclient import TestClient
    from dealbot.config import load
    from dealbot.web.app import create_app
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    shutil.copy(root / "config.yaml", tmp_path / "config.yaml")
    cfg = load(tmp_path / "config.yaml")
    return TestClient(create_app(cfg)), cfg


def test_every_view_renders_on_an_empty_database(tmp_path):
    client, _ = _client(tmp_path)
    for path in ("/", "/free", "/saved", "/skipped", "/runs"):
        assert client.get(path).status_code == 200, path


def test_saved_and_near_miss_views_show_the_right_rows(tmp_path):
    from datetime import datetime, timezone
    from dealbot.db import Store
    from dealbot.models import Listing, Score
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    hunt = cfg.hunts[0]
    for lid, status, score in (("x:1", "saved", 9.0), ("x:2", "scored", 6.0),
                               ("x:3", "scored", 1.0)):
        l = Listing(id=lid, source="x", source_id=lid[-1], title=f"item {lid}",
                    description=None, price_cents=0, currency="USD", url="u")
        s.upsert_listing(l); s.mark_matches(hunt.id, [l])
        s.set_status(hunt.id, lid, status)
        s.save_score(Score(listing_id=lid, hunt_id=hunt.id, model="m",
                           scored_at=datetime.now(timezone.utc), match="no",
                           deal_score=score, est_value_cents=None, condition=None,
                           matched_want=None, worth_grabbing=True, unknowns=(),
                           requirements=(), red_flags=(), reasoning="r"),
                     priced_at_cents=0)

    saved = client.get("/saved").text
    assert "item x:1" in saved and "item x:2" not in saved

    near = client.get("/skipped").text
    assert "item x:2" in near        # 6.0, under the bar
    assert "item x:3" not in near    # 1.0, junk, below the floor
    assert "item x:1" not in near    # saved, not a near miss


def test_thumb_falls_back_to_the_source_url(tmp_path):
    """Until a local copy exists, the original still works -- for about four
    days, in Facebook's case."""
    from dealbot.db import Store
    from dealbot.models import Listing
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    s.upsert_listing(Listing(id="x:9", source="x", source_id="9", title="t",
                             description=None, price_cents=0, currency="USD",
                             url="u", images=("https://img.example/a.jpg",)))
    r = client.get("/thumb/x:9", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "https://img.example/a.jpg"


def test_the_runs_page_can_pause_and_resume_sweeps(tmp_path):
    client, cfg = _client(tmp_path)
    from dealbot.db import Store
    s = Store(cfg.db_path)
    sweep = next(h for h in cfg.hunts if h.kind == "sweep")

    assert "pause free-stuff searches" in client.get("/runs").text.lower()

    client.post("/hunts/toggle", data={"kind": "sweep", "enable": "0",
                                       "back": "/free"}, follow_redirects=False)
    assert sweep.id in s.disabled_hunts()
    assert not any(h.id in s.disabled_hunts() for h in cfg.hunts if h.kind == "want")

    page = client.get("/runs").text
    assert "resume free-stuff searches" in page.lower()
    assert "1 hunt off" in page          # the pill, which replaced the banner
    assert "paused" in client.get("/").text.lower()       # ...on every page

    client.post("/hunts/toggle", data={"kind": "all", "enable": "1", "back": "/"},
                follow_redirects=False)
    assert s.disabled_hunts() == set()


def test_the_demo_database_populates_every_view(tmp_path):
    """A UI agent's first command. If any view is empty here they will design
    against nothing, or assume the dashboard is broken."""
    import shutil
    from fastapi.testclient import TestClient
    from pathlib import Path
    from dealbot.config import load
    from dealbot.demo import build
    from dealbot.web.app import create_app
    from dataclasses import replace

    root = Path(__file__).resolve().parents[1]
    shutil.copy(root / "config.yaml", tmp_path / "config.yaml")
    cfg = load(tmp_path / "config.yaml")
    build(cfg, cfg.db_path)
    client = TestClient(create_app(cfg))

    for path in ("/", "/free", "/saved", "/skipped", "/runs"):
        body = client.get(path).text
        assert '"count num">0<' not in body, path

    # ...and the awkward cases a designer needs on screen
    wants = client.get("/").text
    assert "78in wide" in wants                      # a price drop with evidence
    assert "no photo" in wants                       # the empty-image state
    assert "~~" in wants or "strike" in wants        # strikethrough treatment
    assert "Worth checking" in wants or "needs checking" in wants.lower()


def test_the_runs_page_can_pause_everything(tmp_path):
    """The switch that stops all collecting, wants included. It lives beside the
    sweep switch on /runs because that is the page you open to ask whether the
    bot is working."""
    client, cfg = _client(tmp_path)
    from dealbot.db import Store
    s = Store(cfg.db_path)

    assert "pause all searching" in client.get("/runs").text.lower()
    client.post("/hunts/toggle", data={"kind": "all", "enable": "0",
                                       "back": "/runs"}, follow_redirects=False)
    assert s.disabled_hunts() == {h.id for h in cfg.hunts}

    page = client.get("/runs").text
    assert "resume all searching" in page.lower()
    # The pill is the only announcement now, and it is on every page.
    for path in ("/", "/free", "/saved", "/runs"):
        body = client.get(path).text
        assert ">Paused<" in body, path
        assert 'href="/runs"' in body, path           # and it is the way back

    client.post("/hunts/toggle", data={"kind": "all", "enable": "1",
                                       "back": "/runs"}, follow_redirects=False)
    assert s.disabled_hunts() == set()


def test_a_wrong_address_gets_a_page_not_a_json_blob(tmp_path):
    """404 and a bad query string are states the dashboard can reach from a
    stale bookmark. Both used to answer with raw JSON."""
    client, _ = _client(tmp_path)

    r = client.get("/no-such-view")
    assert r.status_code == 404
    assert "Not found" in r.text and "Curbside" in r.text      # the real shell
    assert "<nav" in r.text                                    # nav still works

    r = client.get("/skipped?floor=banana")
    assert r.status_code == 400
    assert "Bad link" in r.text


def test_the_old_near_url_still_lands(tmp_path):
    """`/near` read as "near me", which it never meant. It is `/skipped` now,
    and the old URL redirects so a phone bookmark does not 404."""
    client, _ = _client(tmp_path)
    r = client.get("/near", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"].startswith("/skipped")
    assert client.get("/near").status_code == 200


def test_a_later_status_change_does_not_wipe_the_dismissal_note(tmp_path):
    """REGRESSION: `set_status` wrote `dismiss_note=?` unconditionally and the
    parameter defaults to None, so every transition erased the note explaining
    the last one. Reachable from the dashboard: dismiss with a reason, later
    save the same listing, and the reason is gone."""
    from dealbot.db import Store
    from dealbot.models import Listing
    s = Store(tmp_path / "t.db")
    l = Listing(id="x:1", source="x", source_id="1", title="t", description=None,
                price_cents=0, currency="USD", url="u")
    s.upsert_listing(l); s.mark_matches("h", [l])

    s.set_status("h", l.id, "dismissed", "too far to be worth the drive")
    s.set_status("h", l.id, "saved")
    row = s.conn.execute("SELECT status, dismiss_note FROM hunt_matches "
                         "WHERE hunt_id='h' AND listing_id='x:1'").fetchone()
    assert row["status"] == "saved"
    assert row["dismiss_note"] == "too far to be worth the drive"

    s.set_status("h", l.id, "dismissed", "gone")     # a note replaces a note
    assert s.conn.execute("SELECT dismiss_note FROM hunt_matches "
                          "WHERE listing_id='x:1'").fetchone()[0] == "gone"


# --- installable on a phone -------------------------------------------------

def test_the_install_assets_are_all_served(tmp_path):
    """Android offers to install given a manifest, the two icon sizes and a
    worker at the root. Missing any one of them and the offer never appears,
    with nothing in the interface to say why."""
    import json
    client, _ = _client(tmp_path)

    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    data = json.loads(manifest.text)
    assert data["display"] == "standalone" and data["start_url"] == "/"
    sizes = {i["sizes"] for i in data["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(i.get("purpose") == "maskable" for i in data["icons"])

    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]

    for path in ("/favicon.ico", "/static/icon.svg", "/static/apple-touch-icon.png",
                 "/static/offline.html"):
        assert client.get(path).status_code == 200, path


def test_the_worker_is_served_from_the_root_with_no_caching(tmp_path):
    """A worker's scope cannot rise above its own path, so at /static/sw.js it
    could only ever see /static. And a cached worker is a bug you cannot fix."""
    client, _ = _client(tmp_path)
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers.get("Service-Worker-Allowed") == "/"
    assert "no-cache" in r.headers.get("Cache-Control", "")


def test_the_worker_never_caches_a_page(tmp_path):
    """Half of what this finds is gone within the hour. A cached card that says
    a free sofa is still on the kerb sends someone across town for nothing."""
    from pathlib import Path
    sw = (Path(__file__).resolve().parents[1]
          / "dealbot/web/static/sw.js").read_text()
    assert "'/thumb/'" in sw and "'/static/'" in sw
    # Pages are fetched and only fall back to the offline notice.
    assert "fetch(request).catch" in sw
    assert "cache.put" not in sw.split("fetch(request).catch")[1]


def test_every_page_offers_the_manifest_and_the_settings_gear(tmp_path):
    client, _ = _client(tmp_path)
    for path in ("/", "/free", "/saved", "/skipped", "/runs", "/settings"):
        body = client.get(path).text
        assert 'rel="manifest"' in body, path
        assert 'href="/settings"' in body, path


def test_the_header_wears_the_same_mark_as_the_home_screen(tmp_path):
    """One file for the tab, the installed tile and the header, so the three
    cannot drift apart."""
    client, _ = _client(tmp_path)
    body = client.get("/").text
    assert 'class="mark" src="/static/icon.svg"' in body
    assert client.get("/static/icon.svg").status_code == 200


def test_the_wants_page_offers_a_way_to_add_one(tmp_path):
    """The page you are on when you think "I should look for one of those".
    Reaching it only through the settings gear reads as configuration."""
    client, _ = _client(tmp_path)
    body = client.get("/").text
    assert 'class="addbtn" href="/wants/new"' in body
    assert 'aria-label="Add a want"' in body        # it is an icon alone
    assert client.get("/wants/new").status_code == 200


def test_the_empty_wants_view_also_points_at_adding_one(tmp_path):
    """An empty list is exactly when you need the way out of it."""
    client, _ = _client(tmp_path)
    assert 'href="/wants/new">add another want' in client.get("/").text


# --- the health pill knows about bedtime ------------------------------------

def _one_run(cfg, minutes_ago, error=None):
    from datetime import datetime, timedelta, timezone
    from dealbot.db import Store
    store = Store(cfg.db_path)
    when = (datetime.now(timezone.utc)
            - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    store.conn.execute(
        "INSERT INTO runs (hunt_id, source, started_at, error) VALUES (?,?,?,?)",
        ("sweep:free-nearby", "fixture", when, error))
    store.close()


def test_asleep_is_not_reported_as_a_quiet_day_or_a_broken_scraper(tmp_path, monkeypatch):
    """Eight hours of deliberate silence and a scraper that died on Tuesday
    look identical unless the interface says which."""
    from datetime import datetime
    from dealbot.schedule import Schedule
    client, cfg = _client(tmp_path)
    _one_run(cfg, minutes_ago=9 * 60)
    monkeypatch.setattr(Schedule, "now",
                        lambda self: datetime(2026, 9, 10, 3, 0))
    body = client.get("/").text
    assert "Asleep till 12pm" in body
    assert "Quiet" not in body


def test_the_first_minutes_after_waking_are_not_a_warning(tmp_path, monkeypatch):
    """Waking at noon, the last run is legitimately as old as the night."""
    from datetime import datetime
    from dealbot.schedule import Schedule
    client, cfg = _client(tmp_path)
    _one_run(cfg, minutes_ago=9 * 60)
    monkeypatch.setattr(Schedule, "now",
                        lambda self: datetime(2026, 9, 10, 12, 5))
    body = client.get("/").text
    assert "Just woke" in body and "Quiet" not in body


def test_paused_outranks_asleep(tmp_path, monkeypatch):
    """Both are true at 3am with the hunts switched off, and the pill reported
    "Asleep till 12pm" over a bot that was simply off. Asleep resolves itself
    at noon; a pause does not resolve until you do something about it."""
    from datetime import datetime
    from dealbot.schedule import Schedule
    client, cfg = _client(tmp_path)
    _one_run(cfg, minutes_ago=30)
    monkeypatch.setattr(Schedule, "now",
                        lambda self: datetime(2026, 9, 10, 3, 0))
    client.post("/hunts/toggle", data={"kind": "all", "enable": "0"})
    body = client.get("/").text
    assert ">Paused<" in body
    assert "Asleep" not in body


def test_a_pause_is_announced_even_before_the_first_run(tmp_path):
    """A fresh install with everything off said "No runs yet", which is true
    and useless: being off is why there are none."""
    client, _ = _client(tmp_path)
    client.post("/hunts/toggle", data={"kind": "all", "enable": "0"})
    assert ">Paused<" in client.get("/").text


def test_the_card_actions_row_carries_two_labels_and_no_more(tmp_path):
    """Three labelled buttons across a phone broke "Dismiss" one letter per
    line, because the body sets overflow-wrap:anywhere for stranger-written
    titles. Save and Dismiss are labelled; blocking is an icon; the link out
    moved to the listing page, which is one tap away."""
    from pathlib import Path
    base = (Path(__file__).resolve().parents[1]
            / "dealbot/web/templates/_card.html").read_text()
    assert "btn open" not in base                    # the link-out is gone
    assert 'aria-label="Never show me things like this"' in base
    assert "white-space:nowrap" in (
        Path(__file__).resolve().parents[1]
        / "dealbot/web/static/app.css").read_text()

    client, _ = _client(tmp_path)
    # ...and the listing page still offers it.
    assert "Open on the marketplace" in (
        Path(__file__).resolve().parents[1]
        / "dealbot/web/templates/listing.html").read_text()


def test_a_failing_fetch_still_outranks_the_schedule(tmp_path, monkeypatch):
    """Asleep is not a reason to stop reporting that the last run died."""
    from datetime import datetime
    from dealbot.schedule import Schedule
    client, cfg = _client(tmp_path)
    _one_run(cfg, minutes_ago=30, error="HTTPError: 429")
    monkeypatch.setattr(Schedule, "now",
                        lambda self: datetime(2026, 9, 10, 3, 0))
    assert "Fetch failing" in client.get("/").text


def test_one_listing_shows_in_exactly_one_bin(tmp_path):
    """`hunt_matches` is per (hunt, listing) on purpose, so the same physical
    thing could be a card in Wants and a card in Free finds at once. That is
    defensible and still reads as a bug."""
    from datetime import datetime, timezone
    from dealbot.db import Store
    from dealbot.models import Listing, Score
    client, cfg = _client(tmp_path)
    store = Store(cfg.db_path)
    lst = Listing(id="x:1", source="x", source_id="1", title="A sectional",
                  description=None, price_cents=0, currency="USD", url="u")
    store.upsert_listing(lst)
    for hunt_id, status in (("want:tv-stand", "wanted"),
                            ("sweep:free-nearby", "free_find")):
        store.mark_matches(hunt_id, [lst])
        store.save_score(Score(listing_id=lst.id, hunt_id=hunt_id, model="m",
                               scored_at=datetime.now(timezone.utc),
                               match="unknown", deal_score=8.0,
                               est_value_cents=None, condition=None,
                               matched_want=None, worth_grabbing=True,
                               unknowns=(), requirements=(), red_flags=(),
                               reasoning="r"), priced_at_cents=0)
        store.set_status(hunt_id, lst.id, status)

    assert client.get("/").text.count('data-listing="x:1"') == 1
    assert client.get("/free").text.count('data-listing="x:1"') == 0

    # Saving it anywhere outranks both, and it leaves the other bins.
    store.set_status("sweep:free-nearby", lst.id, "saved")
    assert client.get("/saved").text.count('data-listing="x:1"') == 1
    assert client.get("/").text.count('data-listing="x:1"') == 0


def test_the_tab_counts_match_the_cards_on_the_page(tmp_path):
    """"12 waiting" over a list of ten reads as a bug, so the counts are
    deduplicated exactly like the bins are."""
    from datetime import datetime, timezone
    from dealbot.db import Store
    from dealbot.models import Listing, Score
    client, cfg = _client(tmp_path)
    store = Store(cfg.db_path)
    lst = Listing(id="x:2", source="x", source_id="2", title="A thing",
                  description=None, price_cents=0, currency="USD", url="u")
    store.upsert_listing(lst)
    for hunt_id in ("want:tv-stand", "want:stacked-ottoman"):
        store.mark_matches(hunt_id, [lst])
        store.save_score(Score(listing_id=lst.id, hunt_id=hunt_id, model="m",
                               scored_at=datetime.now(timezone.utc),
                               match="yes", deal_score=8.0, est_value_cents=None,
                               condition=None, matched_want=None,
                               worth_grabbing=False, unknowns=(), requirements=(),
                               red_flags=(), reasoning="r"), priced_at_cents=0)
        store.set_status(hunt_id, lst.id, "wanted")
    body = client.get("/").text
    assert body.count('data-listing="x:2"') == 1
    assert "<b data-bincount>1</b>" in body


def test_every_section_hue_is_declared_in_both_themes(tmp_path):
    """The dashboard is opened outdoors in daylight and in bed at night, so a
    colour defined in one theme and not the other is a half-built colour. The
    hues are also plain custom properties, which cascade by specificity -- easy
    to add a light one that silently outranks the dark one."""
    import re
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1]
           / "dealbot/web/static/app.css").read_text()
    sections = set(re.findall(r"body\[data-page=(\w+)\]\s*\{--tint", css))
    assert {"free", "saved", "skipped", "runs", "settings", "wants"} <= sections
    for page in sections:
        assert f":root:not([data-theme=light]) body[data-page={page}]" in css, page
        assert f":root[data-theme=dark] body[data-page={page}]" in css, page


def test_each_destination_declares_which_section_it_is(tmp_path):
    client, _ = _client(tmp_path)
    for path, page in (("/", "wants"), ("/free", "free"), ("/saved", "saved"),
                       ("/skipped", "skipped"), ("/runs", "runs"),
                       ("/settings", "settings"), ("/wants/new", "settings")):
        assert f'<body data-page="{page}"' in client.get(path).text, path


def test_the_browser_code_is_syntactically_valid(tmp_path):
    """It lived inline in a Jinja template for a day, where nothing could check
    it: two of its bugs this session were only found by reading. It is a real
    file now, so a parser can have an opinion."""
    import shutil
    import subprocess
    from pathlib import Path
    js = Path(__file__).resolve().parents[1] / "dealbot/web/static/app.js"
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node to parse with")
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_page_links_the_assets_with_a_cache_buster(tmp_path):
    """Served cache-first by the service worker, so an unversioned URL is an
    icon or a stylesheet that never updates on an installed phone."""
    import re
    client, _ = _client(tmp_path)
    body = client.get("/").text
    assert re.search(r'href="/static/app\.css\?v=\d+"', body)
    assert re.search(r'src="/static/app\.js\?v=\d+"', body)
    assert "<style>" not in body and "<script>" not in body
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


# --- the health ladder, without a web server in the way ----------------------

def _row(mins_ago=5, error=None):
    from datetime import datetime, timedelta, timezone
    when = datetime.now(timezone.utc) - timedelta(minutes=mins_ago)
    return {"started_at": when.isoformat(timespec="seconds"),
            "error": error, "mins": mins_ago}


def _hunts(n=3):
    return [type("H", (), {"id": f"h{i}", "name": f"h{i}"})() for i in range(n)]


def _sched(open_=True):
    from dealbot.schedule import Schedule
    from datetime import datetime
    at = datetime(2026, 9, 11, 13 if open_ else 3, 0)
    s = Schedule(enabled=True, start_minute=12 * 60, end_minute=20 * 60)
    return type("S", (), {
        "always_on": False, "window_label": s.window_label,
        "start_minute": s.start_minute,
        "is_open": lambda self: open_,
        "opened_at": lambda self: s.opened_at(at),
        "now": lambda self: at})()


def test_the_health_ladder_picks_the_most_actionable_true_fact():
    """Asleep, paused and quiet are usually all true at once, and picking the
    wrong one is how the pill starts lying -- which it has done twice."""
    from dealbot.web.app import health
    hunts = _hunts()

    # every hunt off beats everything, including a stale error and the clock
    assert health(_row(error="boom"), hunts, hunts, _sched(open_=False)
                  )["label"] == "Paused"
    # a failing fetch beats a partial pause
    assert health(_row(error="HTTPError"), hunts[:1], hunts, _sched()
                  )["label"] == "Fetch failing"
    # a quota pause is not a failing fetch: the fetch worked
    assert health(_row(error="scoring skipped: daily spend ceiling reached"),
                  [], hunts, _sched())["label"] == "Judging paused"
    # some hunts off beats the clock
    assert health(_row(), hunts[:2], hunts, _sched(open_=False)
                  )["label"] == "2 hunts off"
    assert health(_row(), hunts[:1], hunts, _sched())["label"] == "1 hunt off"
    # nothing else to say, so the clock
    assert health(_row(), [], hunts, _sched(open_=False)
                  )["label"] == "Asleep till 12pm"
    # a pause is announced before there has ever been a run
    assert health(None, hunts, hunts, _sched())["label"] == "Paused"
    assert health(None, [], hunts, _sched())["label"] == "No runs yet"
    # and the ordinary case
    assert health(_row(mins_ago=3), [], hunts, _sched())["label"] == "3m ago"
    assert health(_row(mins_ago=200), [], hunts, _sched())["label"].startswith("Quiet")


def test_a_listing_with_no_words_wears_an_amber_badge(tmp_path):
    """Not a fault, a caution: the seller wrote nothing, so every judgement on
    the card rests on the photographs."""
    from datetime import datetime, timezone
    from dealbot.db import Store
    from dealbot.models import Listing, Score
    client, cfg = _client(tmp_path)
    store = Store(cfg.db_path)
    for lid, desc in (("x:9", None), ("x:8", "a real description")):
        l = Listing(id=lid, source="x", source_id=lid[-1], title=f"Thing {lid}",
                    description=desc, price_cents=0, currency="USD", url="u",
                    images=("a.jpg",))
        store.upsert_listing(l)
        store.mark_matches("sweep:free-nearby", [l])
        store.save_score(Score(listing_id=lid, hunt_id="sweep:free-nearby",
                               model="m", scored_at=datetime.now(timezone.utc),
                               match="no", deal_score=6.0, est_value_cents=None,
                               condition=None, matched_want=None,
                               worth_grabbing=True, unknowns=(), requirements=(),
                               red_flags=(), reasoning="r"), priced_at_cents=0)
        store.set_status("sweep:free-nearby", lid, "free_find")

    body = client.get("/free").text
    assert body.count("no description") == 1        # only the wordless one
    card = body[body.index('data-listing="x:9"'):]
    assert 'class="chip unk"' in card[:card.index("</article>")]


def test_a_page_view_never_writes_to_the_database(tmp_path):
    """REGRESSION: every GET used to take a WRITE lock.

    `_live()` runs per request, and `seed_wants`/`seed_excludes` each ended
    with an unconditional `set_setting(...'seeded', '1')` -- a constant that
    nothing ever read. The poller writes the same file, so a read-only page
    could block on its lock, and did: a held write transaction turned GET / from
    0.2ms into a 1.5s wait, and an unreleased one into `database is locked` from
    the home page.

    The per-name and per-hunt checks are the real one-shot; no marker is needed.
    If a write reappears on a read path, this is the test that should stop it.
    """
    import sqlite3
    client, cfg = _client(tmp_path)

    # One request first. Against a BRAND-NEW database the first `_live()` does
    # legitimately write: it seeds config.yaml's wants and exclude terms into
    # their tables, once ever. That is the seeding, not the marker -- and it is
    # exactly what the per-name/per-hunt checks make idempotent.
    assert client.get("/").status_code == 200

    seen: list[str] = []
    real_connect = sqlite3.connect

    def watched(*a, **kw):
        conn = real_connect(*a, **kw)
        conn.set_trace_callback(
            lambda sql: seen.append(sql.strip().split()[0].upper()))
        return conn

    sqlite3.connect = watched
    try:
        for path in ("/", "/free", "/saved", "/skipped", "/settings", "/runs"):
            assert client.get(path).status_code == 200, path
    finally:
        sqlite3.connect = real_connect

    assert seen, "no statements traced -- the test is not watching anything"
    writes = [s for s in seen if s in ("INSERT", "UPDATE", "DELETE", "REPLACE")]
    assert not writes, (
        f"a GET wrote to an already-seeded database: {sorted(set(writes))}")


def test_the_bin_queries_are_built_from_a_clause_not_from_each_other():
    """REGRESSION: SAVED_SQL and NEAR_MISS_SQL were `str.replace` on QUEUE_SQL,
    which had itself been rebound to a longer string. A needle that stops
    matching is a SILENT no-op -- a valid query against the wrong rows.

    So: each bin query must actually carry its own WHERE, and only the two that
    are bins may carry the one-row-per-listing clause."""
    from dealbot.web.app import NEAR_MISS_SQL, ONE_BIN, QUEUE_SQL, SAVED_SQL

    assert "WHERE m.status = ?" in QUEUE_SQL
    assert "WHERE m.status IN ('saved', 'contacted')" in SAVED_SQL
    assert "WHERE m.status = 'scored'" in NEAR_MISS_SQL

    assert ONE_BIN in QUEUE_SQL and ONE_BIN in SAVED_SQL
    # /skipped is a band under the bar, not a bin: it keeps its own rows.
    assert ONE_BIN not in NEAR_MISS_SQL


def _run_js_harness(name):
    """Run one of the browser-code harnesses under node."""
    import shutil
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node to run the browser code with")
    r = subprocess.run(
        [node, str(root / "tests/js" / name),
         str(root / "dealbot/web/static/app.js")],
        capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL" not in r.stdout, r.stdout
    return r.stdout


def test_the_swipe_gestures_behave():
    """Swipe right to save, left to dismiss.

    The parts worth testing are the ones that go wrong quietly: it must not
    steal a vertical scroll (the list becomes unusable), must not fire on a
    short drag (you dismiss things by accident and only notice later), must
    not act in a direction whose button is absent -- `/saved` has no Save --
    and must swallow the click a touch ends in, or letting go over the title
    opens the marketplace instead."""
    assert _run_js_harness("swipe_harness.mjs").count("ok ") >= 14


def test_the_search_term_pills_behave():
    """The pill editor is the only real logic in app.js, and `node --check`
    only proves it parses.

    It rewrites a hidden <textarea> that is still the field that posts, so a
    bug here silently sends the wrong search terms -- or, worse, lets Enter
    submit a half-filled want. `tests/js/chips_harness.mjs` drives it against a
    minimal fake DOM and asserts each behaviour; this runs it."""
    assert _run_js_harness("chips_harness.mjs").count("ok ") >= 10


def test_a_card_offers_two_destinations_and_does_not_nest_links(tmp_path):
    """The card opens the DETAIL page; the title opens the marketplace.

    Two destinations on one card cannot be one wrapping anchor, because links
    do not nest -- a browser silently unnests them and you get neither target
    reliably. So the whole-card target is an overlay stretched under the card
    and the title sits above it on z-index. If that ever collapses back into a
    single <a class="card-main" href=...>, this fails."""
    import re
    from datetime import datetime, timezone
    from dealbot.db import Store
    from dealbot.models import Listing, Score
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    hunt = cfg.hunts[0]
    l = Listing(id="x:1", source="x", source_id="1", title="A Wide TV Stand",
                description=None, price_cents=0, currency="USD",
                url="https://example.com/item/1")
    s.upsert_listing(l); s.mark_matches(hunt.id, [l])
    s.set_status(hunt.id, "x:1", "wanted")
    s.save_score(Score(listing_id="x:1", hunt_id=hunt.id, model="m",
                       scored_at=datetime.now(timezone.utc), match="yes",
                       deal_score=9.0, est_value_cents=None, condition=None,
                       matched_want=None, worth_grabbing=True, unknowns=(),
                       requirements=(), red_flags=(), reasoning="r"),
                 priced_at_cents=0)

    card = re.search(r'<article class="card".*?</article>', client.get("/").text, re.S)
    assert card, "no card rendered"
    c = card.group(0)

    assert re.search(r'<a class="card-open" href="/listing/x:1"', c), \
        "the whole-card tap no longer opens the detail page"
    title = re.search(r'<h2><a class="out" href="([^"]+)" target="_blank"', c)
    assert title and title.group(1) == "https://example.com/item/1", \
        "the title no longer opens the marketplace"

    depth = 0
    for tag in re.findall(r"<a\b|</a>", c):
        depth += 1 if tag == "<a" else -1
        assert depth <= 1, "anchors are nested; a browser will unnest them"

    # Gestures are an ADDITION: both buttons must survive, because a swipe is
    # invisible, undiscoverable and unavailable without a touchscreen.
    assert re.findall(r'name="status" value="(\w+)"', c) == ["saved", "dismissed"]
