"""Dashboard rendering of data we collect. All of this existed in the database
and none of it was visible."""
import pytest
from curbside.web.app import _sparkline


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
    from curbside.db import Store
    from curbside.models import Listing
    from curbside.web.app import REJECT_REASONS_SQL
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
    from curbside.config import load
    from curbside.web.app import create_app
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    shutil.copy(root / "config.yaml", tmp_path / "config.yaml")
    cfg = load(tmp_path / "config.yaml")
    return TestClient(create_app(cfg)), cfg


def test_every_view_renders_on_an_empty_database(tmp_path):
    client, _ = _client(tmp_path)
    for path in ("/", "/free", "/saved", "/skipped", "/runs", "/stats"):
        assert client.get(path).status_code == 200, path


def test_saved_and_near_miss_views_show_the_right_rows(tmp_path):
    from datetime import datetime, timezone
    from curbside.db import Store
    from curbside.models import Listing, Score
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


def _saved_listing(cfg, lid="x:sold", price_cents=12000, **sold):
    """One saved listing, optionally marked off the market."""
    from datetime import datetime, timezone
    from curbside.db import Store
    from curbside.models import Listing, Score
    s = Store(cfg.db_path)
    hunt = cfg.hunts[0]
    l = Listing(id=lid, source="x", source_id=lid.split(":")[-1],
                title="Standing Desk", description="d",
                price_cents=price_cents, currency="USD", url="u",
                images=("https://img.example/a.jpg",))
    s.upsert_listing(l)
    s.mark_matches(hunt.id, [l])
    s.set_status(hunt.id, lid, "saved")
    # The bin views join scores, so an unjudged listing is in no bin at all.
    s.save_score(Score(listing_id=lid, hunt_id=hunt.id, model="m",
                       scored_at=datetime.now(timezone.utc), match="yes",
                       deal_score=8.0, est_value_cents=None, condition=None,
                       matched_want=None, worth_grabbing=True, unknowns=(),
                       requirements=(), red_flags=(), reasoning="r"),
                 priced_at_cents=price_cents)
    if sold:
        s.mark_sold(lid, sold["reason"])
    s.close()
    return lid


def test_something_that_sold_says_so_where_it_cannot_be_missed(tmp_path):
    """REPORTED: a saved desk had sold and the only sign was a grey chip the
    size of "photos checked", four chips along a row of attributes. It is the
    answer to the only question the card is being asked, so it goes across the
    photograph and the price it no longer has is struck out."""
    client, cfg = _client(tmp_path)
    _saved_listing(cfg, reason="sold")

    body = client.get("/saved").text
    assert '<article class="card gone"' in body
    assert '<span class="soldmark">sold</span>' in body
    # ... and the count above the list does not offer it as something to do.
    assert "<b>0</b> to act on" in body
    assert "<b>1</b> gone" in body


def test_a_listing_that_only_stopped_resolving_does_not_claim_it_sold(tmp_path):
    """`removed` is the weaker claim: the page went away, which is usually a
    sale and is sometimes a deletion. The badge must not overstate it."""
    client, cfg = _client(tmp_path)
    _saved_listing(cfg, reason="removed")

    body = client.get("/saved").text
    assert '<span class="soldmark">gone</span>' in body
    assert '<span class="soldmark">sold</span>' not in body


def test_the_detail_page_says_it_before_anything_you_could_act_on(tmp_path):
    """It said it nowhere at all: you could open a saved listing that had sold
    and read its price, its distance and its score without being told."""
    client, cfg = _client(tmp_path)
    lid = _saved_listing(cfg, reason="sold")

    body = client.get(f"/listing/{lid}").text
    assert "gonebanner" in body
    head, rest = body.split("gonebanner", 1)
    assert "<b>Sold.</b>" in rest
    assert '<p class="price gone"' in rest      # the banner comes first
    assert 'class="price"' not in head

    removed = _saved_listing(cfg, lid="x:vanished", reason="removed")
    assert "No longer listed." in client.get(f"/listing/{removed}").text


def test_thumb_falls_back_to_the_source_url(tmp_path):
    """Until a local copy exists, the original still works -- for about four
    days, in Facebook's case."""
    from curbside.db import Store
    from curbside.models import Listing
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    s.upsert_listing(Listing(id="x:9", source="x", source_id="9", title="t",
                             description=None, price_cents=0, currency="USD",
                             url="u", images=("https://img.example/a.jpg",)))
    r = client.get("/thumb/x:9", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "https://img.example/a.jpg"


def test_sweeps_pause_from_settings_and_resume_from_runs(tmp_path):
    """The switch is a decision made rarely, so it lives on /settings with the
    hours. It used to be the first thing on /runs, which is the page the pill
    opens to explain itself. /runs offers the way back when it matters."""
    client, cfg = _client(tmp_path)
    from curbside.db import Store
    s = Store(cfg.db_path)
    sweep = next(h for h in cfg.hunts if h.kind == "sweep")

    assert "pause free-stuff searches" in client.get("/settings").text.lower()
    assert "pause free-stuff searches" not in client.get("/runs").text.lower()

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
    from curbside.config import load
    from curbside.demo import build
    from curbside.web.app import create_app
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


def test_everything_pauses_from_settings_and_resumes_from_runs(tmp_path):
    """The switch that stops all collecting, wants included. It lives beside the
    sweep switch and the hours on /settings; /runs, where the pill sends you,
    says everything is paused and offers Resume."""
    client, cfg = _client(tmp_path)
    from curbside.db import Store
    s = Store(cfg.db_path)

    assert "pause all searching" in client.get("/settings").text.lower()
    client.post("/hunts/toggle", data={"kind": "all", "enable": "0",
                                       "back": "/runs"}, follow_redirects=False)
    assert s.disabled_hunts() == {h.id for h in cfg.hunts}

    page = client.get("/runs").text
    assert "resume all searching" in page.lower()
    assert "Everything is paused" in page
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
    from curbside.db import Store
    from curbside.models import Listing
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
          / "curbside/web/static/sw.js").read_text()
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
    from curbside.db import Store
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
    from curbside.schedule import Schedule
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
    from curbside.schedule import Schedule
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
    from curbside.schedule import Schedule
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
            / "curbside/web/templates/_card.html").read_text()
    assert "btn open" not in base                    # the link-out is gone
    assert 'aria-label="Never show me things like this"' in base
    assert "white-space:nowrap" in (
        Path(__file__).resolve().parents[1]
        / "curbside/web/static/app.css").read_text()

    client, _ = _client(tmp_path)
    # ...and the listing page still offers it.
    assert "Open on the marketplace" in (
        Path(__file__).resolve().parents[1]
        / "curbside/web/templates/listing.html").read_text()


def test_a_failing_fetch_still_outranks_the_schedule(tmp_path, monkeypatch):
    """Asleep is not a reason to stop reporting that the last run died."""
    from datetime import datetime
    from curbside.schedule import Schedule
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
    from curbside.db import Store
    from curbside.models import Listing, Score
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
    from curbside.db import Store
    from curbside.models import Listing, Score
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
           / "curbside/web/static/app.css").read_text()
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
    js = Path(__file__).resolve().parents[1] / "curbside/web/static/app.js"
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

def _row(mins_ago=5, error=None, warning=None):
    """Shaped like the real `runs` row, `warning` included.

    It was missing that key, which is the shape of fake this bug hid behind:
    a quota standdown belongs in `warning` and was being written to `error`."""
    from datetime import datetime, timedelta, timezone
    when = datetime.now(timezone.utc) - timedelta(minutes=mins_ago)
    return {"started_at": when.isoformat(timespec="seconds"),
            "error": error, "warning": warning, "mins": mins_ago}


def _act(scored=0, in_flight=False, runs=4):
    """What `Store.run_activity` hands the pill: whether a pass is in flight,
    and what the last hour judged."""
    return {"runs": runs, "scored": scored, "in_flight": in_flight,
            "now": None}


def _hunts(n=3):
    return [type("H", (), {"id": f"h{i}", "name": f"h{i}"})() for i in range(n)]


def _sched(open_=True):
    from curbside.schedule import Schedule
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
    from curbside.web.app import health
    hunts = _hunts()

    # every hunt off beats everything, including a stale error and the clock
    assert health(_row(error="boom"), hunts, hunts, _sched(open_=False), _act()
                  )["label"] == "Paused"
    # a failing fetch beats a partial pause
    assert health(_row(error="HTTPError"), hunts[:1], hunts, _sched(), _act()
                  )["label"] == "Fetch failing"
    # a quota pause is not a failing fetch: the fetch worked. The LABEL says
    # the state only. A reason cut off mid-word in a phone's top bar is worse
    # than no reason: it reads as broken rather than terse. The reason is in
    # `detail`, and stated in full on the page the pill links to.
    held = {"held": True, "override": False,
            "why": "Today's spend reached the $10.00 limit", "resumes": ""}
    pause = health(_row(), [], hunts, _sched(), _act(), held)
    assert pause["label"] == "Judging paused"
    assert "Today's spend reached the $10.00 limit" in pause["detail"]
    # some hunts off beats the clock
    assert health(_row(), hunts[:2], hunts, _sched(open_=False), _act()
                  )["label"] == "2 hunts off"
    assert health(_row(), hunts[:1], hunts, _sched(), _act()
                  )["label"] == "1 hunt off"
    # nothing else to say, so the clock
    assert health(_row(), [], hunts, _sched(open_=False), _act()
                  )["label"] == "Asleep till 12pm"
    # a pause is announced before there has ever been a run
    assert health(None, hunts, hunts, _sched(), _act())["label"] == "Paused"
    assert health(None, [], hunts, _sched(), _act())["label"] == "No runs yet"
    # and the ordinary case: the clock, and no claim about what it is doing
    assert health(_row(mins_ago=3), [], hunts, _sched(), _act(scored=6)
                  )["label"] == "3m ago"
    assert health(_row(mins_ago=200), [], hunts, _sched(), _act()
                  )["label"].startswith("Quiet")


def test_the_pill_only_claims_activity_while_a_pass_is_in_flight():
    """It says something is happening when something IS happening, and shows
    the clock the rest of the time.

    "Judged · 15m" and "Collecting · 15m" were both tried and both described a
    bot sitting still waiting for the timer: a state word beside a growing
    number reads as work in progress. What it has judged is a fact about the
    last hour rather than a state, so it belongs in the detail and on /runs.
    """
    from curbside.web.app import health
    hunts = _hunts()

    live = health(_row(mins_ago=0), [], hunts, _sched(),
                  _act(scored=3, in_flight=True))
    assert live["label"] == "Looking now"
    assert live["state"] == "ok"

    # Waiting for the next tick. Judging something twelve minutes ago is not
    # something it is doing now, so the label says only when it last ran.
    waiting = health(_row(mins_ago=12), [], hunts, _sched(), _act(scored=3))
    assert waiting["label"] == "12m ago"
    assert "3 listings judged in the last hour" in waiting["detail"]

    # Fetching fine, judging nothing. Still just the clock, and the detail
    # says so rather than the pill going amber over a quiet hour.
    idle = health(_row(mins_ago=2), [], hunts, _sched(), _act(scored=0))
    assert idle["label"] == "2m ago"
    assert idle["state"] == "ok"
    assert "Nothing new to judge" in idle["detail"]

    # One listing, not "1 listings".
    one = health(_row(mins_ago=5), [], hunts, _sched(), _act(scored=1))
    assert "1 listing judged" in one["detail"]

    # It never outranks a real problem: a fetch that died still wins.
    assert health(_row(error="boom"), [], hunts, _sched(),
                  _act(scored=9, in_flight=True))["label"] == "Fetch failing"


def test_a_listing_with_no_words_wears_an_amber_badge(tmp_path):
    """Not a fault, a caution: the seller wrote nothing, so every judgement on
    the card rests on the photographs."""
    from datetime import datetime, timezone
    from curbside.db import Store
    from curbside.models import Listing, Score
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
        for path in ("/", "/free", "/saved", "/skipped", "/settings", "/runs",
                     "/stats"):
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
    from curbside.web.app import NEAR_MISS_SQL, ONE_BIN, QUEUE_SQL, SAVED_SQL

    assert "WHERE m.status = ?" in QUEUE_SQL
    assert "WHERE m.status IN ('saved', 'grabbed')" in SAVED_SQL
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
         str(root / "curbside/web/static/app.js")],
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
    opens the marketplace instead.

    The same harness covers the toast, which can also be swiped away: it must
    dismiss WITHOUT undoing -- the save stays applied, exactly as when the
    timer runs out -- and the click a swipe ends in must not press Undo."""
    assert _run_js_harness("swipe_harness.mjs").count("ok ") >= 30


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
    from curbside.db import Store
    from curbside.models import Listing, Score
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

    # The `back` rides along, so Dismiss on the detail page can return to the
    # list the card was opened from instead of sitting there saying "dismissed".
    assert re.search(r'<a class="card-open" href="/listing/x:1\?back=', c), \
        "the whole-card tap no longer opens the detail page, or lost its back"
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


# --- saying why the judging stopped ---------------------------------------

def test_a_standdown_reason_is_found_even_beside_another_warning():
    """A run can lose a detail fetch AND stand aside, and `pipeline` joins the
    two with "; ". Matching only the start of the column meant that run
    reported neither pause."""
    from curbside.web.app import standdown_reason
    assert standdown_reason("scoring skipped: 5-hour plan window at 72% "
                            "(ceiling 70%) -- standing aside") \
        == "5-hour plan window at 72% (ceiling 70%)"
    assert standdown_reason("2 detail fetches failed; scoring skipped: "
                            "daily spend ceiling reached ($10.02 of $10.00)") \
        == "daily spend ceiling reached ($10.02 of $10.00)"
    # both standdowns count: interrupted stopped the judging just as skipped did
    assert standdown_reason("scoring interrupted: rate limit") == "rate limit"
    assert standdown_reason("2 detail fetches failed") is None
    assert standdown_reason(None) is None


def test_no_health_label_is_too_long_for_the_top_bar():
    """The pill shares a phone's top bar with the brand and the settings gear,
    so it has the least room in the interface. "Judging paused: plan 72%" was
    24 characters and was being cut off mid-word.

    19 is "Asleep till 12:30pm", the longest the ladder can legitimately
    produce. A new label over that budget fails here rather than on the phone.
    """
    from curbside.web.app import health
    hunts = _hunts()
    cases = [
        (_row(error="boom"), hunts, hunts, _sched(open_=False), _act()),
        (_row(error="HTTPError"), hunts[:1], hunts, _sched(), _act()),
        (_row(warning="scoring skipped: 5-hour plan window at 72% "
                      "(ceiling 70%) -- standing aside"), [], hunts, _sched(),
         _act()),
        (_row(), hunts[:2], hunts, _sched(open_=False), _act()),
        (_row(), hunts[:1], hunts, _sched(), _act()),
        (_row(), [], hunts, _sched(open_=False), _act()),
        (None, hunts, hunts, _sched(), _act()),
        (None, [], hunts, _sched(), _act()),
        (_row(mins_ago=3), [], hunts, _sched(), _act()),
        (_row(mins_ago=200), [], hunts, _sched(), _act()),
        (_row(mins_ago=4000), [], hunts, _sched(), _act()),
        (_row(mins_ago=3), [], hunts, _sched(), _act(scored=5)),
        (_row(mins_ago=0), [], hunts, _sched(), _act(in_flight=True)),
    ]
    for args in cases:
        label = health(*args)["label"]
        assert len(label) <= 19, f"{label!r} is {len(label)} characters"


def _judged(cfg, lid, title, hunt_id, source, score, model="sonnet"):
    from datetime import datetime, timezone
    from curbside.db import Store
    from curbside.models import Listing, Score
    s = Store(cfg.db_path)
    l = Listing(id=lid, source=source, source_id=lid.split(":")[-1], title=title,
                description="d", price_cents=1000, currency="USD", url="u")
    s.upsert_listing(l)
    s.mark_matches(hunt_id, [l])
    s.save_score(Score(listing_id=lid, hunt_id=hunt_id, model=model,
                       scored_at=datetime.now(timezone.utc), match="no",
                       deal_score=score, est_value_cents=None, condition=None,
                       matched_want=None, worth_grabbing=False, unknowns=(),
                       requirements=(), red_flags=(), reasoning="r"),
                 priced_at_cents=1000)
    s.close()


def _judging_panel(client):
    page = client.get("/runs").text
    return page[page.index('id="judging"'):page.index('id="passes"')]


def _priced_as_free(cfg, lid="facebook:9", status="scored", price_unclear=1,
                    hunt_id=None):
    """A listing marked $0 whose description asks for money.

    Scored as the rubric asks -- as if the thing really WERE free, because a
    listing scored down for being unclear falls under the /skipped floor too,
    and correct-and-invisible is not correct. The flag is what holds it back,
    not the number.
    """
    from datetime import datetime, timezone
    from curbside.db import Store
    from curbside.models import Listing, Score
    s = Store(cfg.db_path)
    hunt_id = hunt_id or next(h.id for h in cfg.hunts if h.kind == "sweep")
    l = Listing(id=lid, source="facebook", source_id=lid.split(":")[-1],
                title="Sockets and wrenches",
                description="Send me offers please over 50 wrenches",
                price_cents=0, currency="USD", url="u", city="Albuquerque",
                distance_mi=4.0, images=("https://img.example/a.jpg",))
    s.upsert_listing(l)
    s.mark_matches(hunt_id, [l])
    score = Score(listing_id=lid, hunt_id=hunt_id, model="sonnet",
                  scored_at=datetime.now(timezone.utc), match="no",
                  deal_score=7.0, est_value_cents=8000, condition=None,
                  matched_want=None, worth_grabbing=True, unknowns=(),
                  requirements=(),
                  red_flags=("Listed as free but the description asks for "
                             "offers",),
                  reasoning="an auction mislabelled as free",
                  price_unclear=bool(price_unclear))
    s.save_score(score, priced_at_cents=0)
    s.set_status(hunt_id, lid, status)
    s.close()
    return lid


def test_the_detail_page_says_which_marketplace_it_came_from(tmp_path):
    """It said "seller unknown" instead, on every listing ever collected --
    neither adapter parses a seller, and Facebook omits one from 93% of the
    payloads it sends us. The source is a fact we always have, and it is the
    one this page was missing: two listings from two sites read identically."""
    client, cfg = _client(tmp_path)
    lid = _priced_as_free(cfg)
    page = client.get(f"/listing/{lid}").text
    assert "Facebook" in page
    assert "seller unknown" not in page


def test_a_zero_price_the_seller_contradicted_survives_a_round_trip(tmp_path):
    """The fourth edit is the one that gets forgotten. `price_unclear` has to
    reach the database AND come back: the column was written and `row_to_score`
    did not read it, so everything outside the web SQL -- the Discord card most
    of all -- was handed a listing that looked ordinarily free."""
    from curbside.db import Store
    _, cfg = _client(tmp_path)
    hunt = next(h for h in cfg.hunts if h.kind == "sweep")
    lid = _priced_as_free(cfg, status="free_find", hunt_id=hunt.id)
    s = Store(cfg.db_path)
    row = s.conn.execute(
        "SELECT price_unclear FROM scores WHERE listing_id=?", (lid,)).fetchone()
    assert row["price_unclear"] == 1
    # ...and back out again, which is the half that was missing.
    _, score = s.pending_notifications(hunt.id)[0]
    assert score.price_unclear is True


def test_a_fake_free_listing_does_not_reach_the_page_about_free_things(tmp_path):
    """REPORTED: "Sockets and wrenches", $0, "Send me offers please over 50
    wrenches", sitting in the free bin as a free find.

    A price nobody knows is not a price, so there is nothing to weigh the trip
    against and this is not a free find. It leaves no trace on the page either,
    not even folded away: the group that used to carry them filtered no bin
    status, so a listing that reached the bin anyway rendered TWICE."""
    client, cfg = _client(tmp_path)
    _priced_as_free(cfg)
    page = client.get("/free").text
    assert "Sockets and wrenches" not in page
    assert 'id="notfree"' not in page


def test_a_fake_free_listing_lands_on_skipped_saying_the_price_is_not_settled(
        tmp_path):
    """Correct and invisible is not correct. It is scored as if it really were
    free, which keeps it above the floor here, and the card says plainly that
    the price is not settled -- FREE in green over it is the one thing that
    must not print."""
    client, cfg = _client(tmp_path)
    _priced_as_free(cfg)
    page = client.get("/skipped").text
    assert "Sockets and wrenches" in page
    assert "price unclear" in page
    assert "seller wants offers" in page                # the chip
    assert '<span class="now free">free</span>' not in page


def test_dismissing_a_fake_free_listing_takes_it_off_that_list_too(tmp_path):
    """It is a listing like any other: dismissed means gone, everywhere."""
    client, cfg = _client(tmp_path)
    _priced_as_free(cfg, status="dismissed")
    assert "Sockets and wrenches" not in client.get("/skipped").text


def test_an_ordinary_free_find_still_reaches_the_free_bin(tmp_path):
    """The guard. Only the contradicted $0 is held back; a listing that really
    is free is a free find like any other."""
    client, cfg = _client(tmp_path)
    _priced_as_free(cfg, lid="facebook:10", status="free_find", price_unclear=0)
    page = client.get("/free").text
    assert "Sockets and wrenches" in page
    assert "price unclear" not in page


def test_the_page_the_pill_links_to_says_what_it_is_judging(tmp_path):
    """The pill has room for "Judging" and a clock. Tapping it should finish
    the sentence: which listings, from which hunt, at which site."""
    client, cfg = _client(tmp_path)
    _judged(cfg, "facebook:1", "Wooden bookshelf", "want:bookshelf",
            "facebook", 8.0)

    panel = _judging_panel(client)
    assert "Wooden bookshelf" in panel
    assert "bookshelf" in panel and "facebook" in panel      # what and where
    assert "8.0" in panel
    assert '/listing/facebook:1?back=/runs' in panel         # and openable


def test_a_first_pass_drop_is_not_shown_as_a_verdict_of_zero(tmp_path):
    """The cheap batched pass reads every listing and writes what it let go as
    a score of 0.0 with a `:triage` model. Those rows count towards the number
    the pill states, so they belong on this list -- but 0.0 in a score badge
    reads as "judged, and worthless", which is not what happened."""
    client, cfg = _client(tmp_path)
    _judged(cfg, "craigslist:2", "Vintage footstool", "want:stacked-ottoman",
            "craigslist", 0.0, model="claude_code:triage")

    panel = _judging_panel(client)
    assert "Vintage footstool" in panel
    assert "first pass" in panel
    assert "0.0" not in panel


def test_a_pass_in_flight_says_which_hunt_and_which_site(tmp_path):
    """"Looking now" in the pill, and the rest of it here. The row is bounded
    by the same hour the pill uses, so a process killed mid-pass stops being
    reported as live rather than saying it forever."""
    from curbside.db import Store
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    s.start_run(type("H", (), {"id": "sweep:free-nearby"})(), "facebook")
    s.close()

    panel = _judging_panel(client)
    assert "Running now." in panel
    assert "free-nearby" in panel and "facebook" in panel


def test_how_long_a_run_took_is_derived_from_the_stamps_it_already_keeps():
    """`started_at` and `finished_at` were both recorded from the first day
    and neither was ever shown. A `duration` column would be a third copy of
    a fact those two state between them, free to disagree with them."""
    from curbside.web.app import took
    assert took("2026-09-16T10:00:00+00:00", "2026-09-16T10:00:42+00:00") == "42s"
    assert took("2026-09-16T10:00:00+00:00", "2026-09-16T10:01:05+00:00") == "1m 05s"
    assert took("2026-09-16T10:00:00+00:00", "2026-09-16T12:03:05+00:00") == "2h 03m"
    # Nothing honest to say: still running, killed mid-pass, or unparseable.
    assert took("2026-09-16T10:00:00+00:00", None) is None
    assert took(None, None) is None
    assert took("whenever", "2026-09-16T10:00:00+00:00") is None
    assert took("2026-09-16T10:00:42+00:00", "2026-09-16T10:00:00+00:00") is None


def test_the_runs_page_shows_how_long_each_run_took(tmp_path):
    from curbside.db import Store
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    hunt = cfg.hunts[0]
    done = s.start_run(hunt, "x")
    s.finish_run(done)
    s.conn.execute("UPDATE runs SET started_at=?, finished_at=? WHERE id=?",
                   ("2026-09-16T10:00:00+00:00",
                    "2026-09-16T10:01:05+00:00", done))
    s.start_run(hunt, "y")                    # begun, never finished
    s.close()

    # The per-run log moved to /runs/all; /runs shows passes.
    page = client.get("/runs/all").text
    assert ">Took<" in page
    assert "1m 05s" in page
    # A run with no finish stamp has no duration. It says so rather than
    # printing a zero, which would read as an instant pass.
    assert "unfinished" in page


def test_the_pill_and_runs_ask_the_gate_not_the_last_run(tmp_path):
    """Both used to read the latest run's warning. A run with nothing to judge
    records no standdown, so the pill read a green "2m ago" while the plan was
    at 79% and nothing could be judged, and /runs said "Running" in one panel
    and "judging stands aside" in the next. They ask `judging_state` now, which
    is what the scorer itself obeys."""
    from curbside.db import Store
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    s.finish_run(s.start_run(cfg.hunts[0], "x"))       # clean, nothing to judge
    _record_reading(cfg, five="0.79")                  # but the plan is over

    page = client.get("/runs").text
    assert ">Judging paused<" in page                  # the pill
    assert "Judging is paused" in page                 # the card, agreeing
    assert "past the 70% mark" in page

    # And the other way round: a stale standdown on the last run, with the
    # window since rolled, is not a pause.
    s.finish_run(s.start_run(cfg.hunts[0], "x"),
                 warning="scoring skipped: 5-hour plan window at 72% "
                         "(ceiling 70%) -- standing aside")
    _record_reading(cfg, five="0.30")
    page = client.get("/runs").text
    assert ">Judging paused<" not in page
    assert "Judging is paused" not in page


# --- deciding on the detail page closes it --------------------------------

def _detail(tmp_path, status="wanted"):
    from datetime import datetime, timezone
    from curbside.db import Store
    from curbside.models import Listing, Score
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    hunt = cfg.hunts[0]
    l = Listing(id="x:1", source="x", source_id="1", title="A Wide TV Stand",
                description=None, price_cents=0, currency="USD", url="u")
    s.upsert_listing(l); s.mark_matches(hunt.id, [l])
    s.set_status(hunt.id, "x:1", status)
    s.save_score(Score(listing_id="x:1", hunt_id=hunt.id, model="m",
                       scored_at=datetime.now(timezone.utc), match="yes",
                       deal_score=9.0, est_value_cents=None, condition=None,
                       matched_want=None, worth_grabbing=True, unknowns=(),
                       requirements=(), red_flags=(), reasoning="r"),
                 priced_at_cents=0)
    return client, cfg, hunt


def test_deciding_on_the_detail_page_returns_to_the_list(tmp_path):
    """It used to save in place, so the listing you had just dismissed stayed
    on screen with the word "dismissed" in it -- the one view where acting on
    a listing left it sitting in front of you."""
    client, _, hunt = _detail(tmp_path)
    page = client.get("/listing/x:1?back=/free").text
    assert 'value="/free"' in page, "the detail page lost where it came from"
    assert "data-inplace" not in page, \
        "a decision here must leave the page, not re-render it"

    r = client.post("/triage", data={"hunt_id": hunt.id, "listing_id": "x:1",
                                     "status": "dismissed", "back": "/free"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/free"


def test_the_detail_page_defaults_to_somewhere_real(tmp_path):
    """Opened from a bookmark there is no list to go back to."""
    client, _, _ = _detail(tmp_path)
    assert 'value="/"' in client.get("/listing/x:1").text


def test_a_back_off_this_dashboard_is_refused(tmp_path):
    """`back` is now a query parameter as well as a form field, so it is
    reader-supplied and ends up in a Location header."""
    from curbside.web.app import _safe_back
    for hostile in ("https://evil.test/x", "//evil.test/x", "javascript:x"):
        assert _safe_back(hostile) == "/"
    assert _safe_back("/free") == "/free"

    client, _, hunt = _detail(tmp_path)
    r = client.post("/triage",
                    data={"hunt_id": hunt.id, "listing_id": "x:1",
                          "status": "saved", "back": "https://evil.test/x"},
                    follow_redirects=False)
    assert r.headers["location"] == "/"


# --- /stats ----------------------------------------------------------------

def _spent(store, hunt_id, source, usd, **counts):
    """One finished run, for the money it cost."""
    store.finish_run(store.start_run(
        type("H", (), {"id": hunt_id})(), source), cost_usd=usd, **counts)


def test_stats_costs_come_from_runs_not_scores(tmp_path):
    """`runs.cost_usd` totalled $23.75 on the live box where `scores.cost_usd`
    totalled $16.58: triage is saved on its score row with a zero cost and is
    only ever counted at the run level. Sum the score rows and a third of the
    money is gone."""
    from curbside.db import Store
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    _spent(s, "want:tv-stand", "facebook", 2.00, n_scored=10)
    _spent(s, "want:tv-stand", "craigslist", 1.00, n_scored=5)

    assert s.spend_since(None)["usd"] == pytest.approx(3.00)
    page = client.get("/stats").text
    assert "$3.00" in page


def test_stats_shows_what_a_hunt_cost_against_what_it_found(tmp_path):
    """A cost on its own says nothing. The same $4 is cheap or pure waste
    depending on whether anything came back, and on the first five days of
    live data one want had spent that and saved nothing at all."""
    from curbside.db import Store
    from curbside.models import Listing
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    hunt = cfg.hunts[0]
    _spent(s, hunt.id, "facebook", 4.00, n_scored=100, n_fetched=900)
    l = Listing(id="x:1", source="x", source_id="1", title="t", description=None,
                price_cents=0, currency="USD", url="u")
    s.upsert_listing(l); s.mark_matches(hunt.id, [l])
    s.set_status(hunt.id, "x:1", "saved")

    row = next(r for r in s.spend_by_hunt() if r["hunt_id"] == hunt.id)
    assert row["usd"] == pytest.approx(4.00)
    assert row["scored"] == 100 and row["saved"] == 1

    page = client.get("/stats").text
    assert "$4.00" in page and "$0.040" in page      # spent, and per judged


def test_a_hunt_that_found_nothing_still_appears(tmp_path):
    """An inner join would hide exactly the rows worth reading: the hunts that
    cost money and matched nothing."""
    from curbside.db import Store
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    _spent(s, "want:ghost", "facebook", 2.50, n_scored=40)

    rows = s.spend_by_hunt()
    assert [r["hunt_id"] for r in rows] == ["want:ghost"]
    assert rows[0]["saved"] == 0
    page = client.get("/stats").text
    assert "ghost" in page
    # Judged on PICKED. The note that judged on saved named every hunt on the
    # page, since nothing is ever marked saved, and a warning that always
    # fires is furniture.
    assert "nothing picked" in page, "a barren hunt has to say so"


def test_a_deleted_want_keeps_its_spend_on_the_page(tmp_path):
    """Deleting a want archives it. What it spent is still your money."""
    from curbside.db import Store
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    _spent(s, "want:long-gone", "facebook", 1.25, n_scored=9)
    page = client.get("/stats").text
    # "removed", the word the want editor uses for the same act.
    assert "long-gone" in page and "removed" in page


def test_a_young_database_does_not_repeat_the_same_figure_three_times(tmp_path):
    """Five days of history makes "last 7 days", "last 30 days" and "all time"
    the same number, and three identical figures read as a bug."""
    from curbside.db import Store
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    _spent(s, "want:tv-stand", "facebook", 6.00, n_scored=20)
    page = client.get("/stats").text
    assert page.count("all of it so far") == 2       # the week and the month


def test_the_funnel_does_not_claim_repeats_are_distinct_listings(tmp_path):
    """`n_fetched` sums per-run counts, so every run re-reads the whole feed:
    85,028 "fetched" against 1,348 listings on file. Calling that "listings
    seen" is a claim the number does not support."""
    from curbside.db import Store
    from curbside.models import Listing
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    _spent(s, cfg.hunts[0].id, "facebook", 1.0, n_fetched=900, n_scored=3)
    l = Listing(id="x:1", source="x", source_id="1", title="t", description=None,
                price_cents=0, currency="USD", url="u")
    s.upsert_listing(l)

    assert s.funnel()["fetched"] == 900
    assert s.funnel()["distinct"] == 1
    page = client.get("/stats").text
    assert "index rows read" in page
    assert "listings seen" not in page
    assert "counts repeats" in page


def test_stats_renders_on_an_empty_database(tmp_path):
    """Before the first run there is no spend, no hunt row and no chart, and
    dividing by any of them would 500 the page."""
    client, _ = _client(tmp_path)
    r = client.get("/stats")
    assert r.status_code == 200
    assert "Nothing has run yet" in r.text


def test_the_stats_page_is_reachable_without_a_sixth_tab(tmp_path):
    """Five tabs is what fits across a phone. This is read monthly, so it is
    signposted from the two pages you would go looking on."""
    import re
    client, _ = _client(tmp_path)
    for path in ("/runs", "/settings"):
        assert 'href="/stats"' in client.get(path).text, path

    tabbar = re.search(r'<nav class="tabbar".*?</nav>', client.get("/").text,
                       re.S)
    assert tabbar, "no tab bar rendered"
    assert "/stats" not in tabbar.group(0), "stats must not be a sixth tab"


def test_the_way_into_stats_is_a_control_not_a_footnote(tmp_path):
    """Reported: the link was not apparent enough. It is a button now.

    NOT `.primary` though: filled accent means "the action to take now"
    everywhere else here, and a link to another page is not that."""
    import re
    client, _ = _client(tmp_path)
    for path in ("/runs", "/settings"):
        link = re.search(r'<a class="([^"]*)" href="/stats"',
                         client.get(path).text)
        assert link, f"{path} lost its way into /stats"
        classes = link.group(1).split()
        assert "btn" in classes, f"{path} link is not a control"
        assert "primary" not in classes, \
            f"{path} link claims to be the page's main action"


def test_the_spend_chart_flags_a_day_lost_to_quota_not_every_day():
    """Colouring any standdown painted all five live days amber, which is a
    legend that describes every bar and so says nothing. Half the day's runs
    is where the bill stops meaning what the bot would normally spend."""
    from curbside.web.app import _daybars
    mostly = _daybars([{"day": "2026-09-11", "usd": 2.76, "runs": 346,
                        "degraded": 295}], ceiling=10.0)
    assert "var(--warn)" in mostly

    a_few = _daybars([{"day": "2026-09-12", "usd": 6.90, "runs": 182,
                       "degraded": 36}], ceiling=10.0)
    assert "var(--accent)" in a_few and "var(--warn)" not in a_few


def test_a_day_the_bot_did_not_run_is_a_gap_not_a_zero():
    """A silent day and a day that found nothing are different facts, and a
    zero-height bar claims the second."""
    from curbside.web.app import _daybars, _fill_days
    filled = _fill_days([], 5)
    assert len(filled) == 5 and all(d["usd"] is None for d in filled)
    assert "<rect" not in _daybars(filled, ceiling=10.0)


def _statline(html_text):
    import re
    from html import unescape
    m = re.search(r'<p class="stats">.*?</p>', html_text, re.S)
    return re.sub(r"\s+", " ",
                  unescape(re.sub(r"<[^>]+>", " ", m.group(0)))).strip() if m else ""


def test_no_page_head_stat_line_is_too_long_for_a_phone(tmp_path):
    """Reported from the phone: the line wraps. /runs read "200 fetch attempts
    36 failed $9.45 spent 77 waiting to be judged" -- 64 characters, which
    pushes the page head onto three lines before a single listing is visible.

    40 is what fits one line on a 360px phone. 48 was measured by eye and was
    still wrapping in the screenshot that came back. The count is of the
    rendered words, so shortening a label fixes it and adding a fifth figure
    fails here rather than on the phone."""
    from datetime import datetime, timezone
    from curbside.db import Store
    from curbside.models import Listing, Score
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    hunt = cfg.hunts[0]

    # Enough of everything that every conditional figure is showing.
    for i in range(3):
        lid = f"x:{i}"
        l = Listing(id=lid, source="x", source_id=str(i), title=f"item {i}",
                    description=None, price_cents=0, currency="USD", url="u")
        s.upsert_listing(l); s.mark_matches(hunt.id, [l])
        s.set_status(hunt.id, lid, "scored")
        s.save_score(Score(listing_id=lid, hunt_id=hunt.id, model="m",
                           scored_at=datetime.now(timezone.utc),
                           match="unknown", deal_score=7.0, est_value_cents=None,
                           condition=None, matched_want=None, worth_grabbing=True,
                           unknowns=("a",), requirements=(), red_flags=(),
                           reasoning="r"), priced_at_cents=0)
    s.finish_run(s.start_run(hunt, "facebook"), cost_usd=9.45,
                 error="boom", n_scored=1)

    for path in ("/", "/free", "/saved", "/skipped", "/runs", "/stats",
                 "/settings"):
        line = _statline(client.get(path).text)
        assert len(line) <= 40, f"{path} stat line is {len(line)}: {line!r}"


def test_the_stage_split_matches_the_names_the_scorer_actually_writes():
    """`spend_by_stage` classifies on the shape of `scores.model`, and those
    strings are built in two other files. If either renames its suffix the
    split goes quietly wrong -- triage would be counted as appraisal AND left
    in the remainder, so the page would add up to more than was spent."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert ':triage"' in (root / "curbside/pipeline.py").read_text()
    assert '+images"' in (root / "curbside/scoring/claude_code.py").read_text()


def test_the_stage_split_adds_up_to_what_was_actually_spent(tmp_path):
    """Every dollar in `runs` lands in exactly one of the three figures.

    Triage rows cost 0 today, so classifying them with appraisal looks
    harmless -- and would double-count the day they cost anything, once under
    appraisal and once inside the remainder."""
    from datetime import datetime, timezone
    from curbside.db import Store
    from curbside.models import Listing, Score
    _client(tmp_path)
    s = Store(tmp_path / "t2.db")
    hunt = type("H", (), {"id": "want:x"})()
    s.finish_run(s.start_run(hunt, "facebook"), cost_usd=10.0)

    l = Listing(id="x:1", source="x", source_id="1", title="t", description=None,
                price_cents=0, currency="USD", url="u")
    s.upsert_listing(l)
    for model, usd in (("sonnet", 4.0), ("sonnet+images", 1.5),
                       ("claude_code:triage", 2.5)):
        s.save_score(Score(listing_id="x:1", hunt_id="want:x", model=model,
                           scored_at=datetime.now(timezone.utc), match="no",
                           deal_score=1.0, est_value_cents=None, condition=None,
                           matched_want=None, worth_grabbing=False, unknowns=(),
                           requirements=(), red_flags=(), reasoning="r",
                           cost_usd=usd), priced_at_cents=0)

    st = s.spend_by_stage()
    assert st["appraisal"] == pytest.approx(4.0)
    assert st["images"] == pytest.approx(1.5)
    # 2.5 billed triage, plus the 2.0 of the run that no score row claimed.
    assert st["other"] == pytest.approx(4.5)
    assert (st["appraisal"] + st["images"] + st["other"]
            == pytest.approx(st["total"]))
    s.close()


def test_a_back_that_only_looks_local_is_refused():
    """Both holes were the same mistake: judging the string handed over rather
    than the URL a browser resolves. Tab, newline and carriage return are
    stripped BEFORE parsing, and a backslash is normalised to a slash, so
    "/<tab>/evil.test" and "/\\evil.test" are both "//evil.test" by the time
    they reach the network."""
    from curbside.web.app import _safe_back
    for hostile in ("//evil.test/x", "https://evil.test", "javascript:x",
                    "/\\evil.test/x", "/\t/evil.test", "/\r\n/evil.test", ""):
        assert _safe_back(hostile) == "/", hostile
    for ours in ("/", "/free", "/free?a=1", "/hunt/want:tv-stand",
                 "/listing/facebook:123"):
        assert _safe_back(ours) == ours, ours


def test_a_standdown_with_no_reason_does_not_report_its_own_prefix():
    """Splitting on ": " returns the whole clause when there is no separator,
    so a bare "scoring skipped" came back as its own reason and the pill read
    "Judging paused: scoring skipped"."""
    from curbside.web.app import standdown_reason
    for bare in ("scoring skipped", "scoring skipped:", "scoring skipped: "):
        assert standdown_reason(bare) == "reason not recorded", bare
    # and it still has to report the pause, not fall through and hide it
    assert standdown_reason("2 detail fetches failed") is None


# --- what today's money went on -------------------------------------------

def _today_iso(cfg=None):
    """The user's midnight, as the page and the spend ceiling both compute it."""
    from zoneinfo import ZoneInfo
    from curbside.schedule import local_day_start
    name = cfg.schedule.tz_name if cfg else None
    return local_day_start(ZoneInfo(name) if name else None)


def _spend_today(store, cfg, hunt_id, source, usd, **counts):
    """A run an hour into the user's day, so it lands in today whatever the
    clock says when the suite runs."""
    from datetime import timedelta
    run_id = store.start_run(type("H", (), {"id": hunt_id})(), source)
    store.conn.execute("UPDATE runs SET started_at=? WHERE id=?",
                       ((_today_iso(cfg) + timedelta(hours=1)).isoformat(), run_id))
    store.finish_run(run_id, cost_usd=usd, **counts)
    return run_id


def test_today_breaks_down_where_the_money_went(tmp_path):
    """The Today figure says how much. This says what on."""
    client, cfg = _client(tmp_path)
    from curbside.db import Store
    s = Store(cfg.db_path)
    _spend_today(s, cfg, "want:tv-stand", "facebook", 2.00,
                 n_scored=20, n_wanted=2)
    _spend_today(s, cfg, "sweep:free-nearby", "craigslist", 0.50, n_scored=6)

    page = client.get("/stats?period=today").text
    body = page[page.index("<h2>Each hunt"):page.index("<h2>Fetched to grabbed")]
    assert "tv-stand" in body and "free-nearby" in body
    assert "$2.00" in body and "$0.50" in body
    assert "facebook" in body and "craigslist" in body


def test_what_a_day_cost_is_one_tap_on_its_bar(tmp_path):
    """The bars answer "is today normal". They could not answer "what did
    Tuesday cost", which is the question you have the moment one looks tall,
    so the week used to be listed again underneath as seven cards. Each bar
    links to its own day instead, and that day gets the whole page."""
    import re
    from datetime import timedelta
    from curbside.db import Store
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    _spend_today(s, cfg, "want:tv-stand", "facebook", 1.23, n_scored=9)

    page = client.get("/stats").text
    days = re.findall(r'<a href="\?day=(\d{4}-\d{2}-\d{2})"', page)
    assert len(days) == 1, "one bar, for the one day that ran"
    one = client.get(f"/stats?day={days[0]}").text
    body = one[one.index("<h2>Each hunt"):one.index("<h2>Fetched to grabbed")]
    assert "tv-stand" in body and "$1.23" in body

    # A day with no run says so rather than printing zeros.
    before = (_today_iso(cfg) - timedelta(days=3)).date().isoformat()
    quiet = client.get(f"/stats?day={before}").text
    assert "Nothing ran on" in quiet


def test_a_week_is_not_listed_twice(tmp_path):
    """Every breakdown on the page follows one period switch. It used to split
    spend by stage and by source for today, and again for all time."""
    from curbside.db import Store
    client, cfg = _client(tmp_path)
    _spend_today(Store(cfg.db_path), cfg, "want:tv-stand", "facebook", 1.0,
                 n_scored=3)
    page = client.get("/stats").text
    assert page.count("<h3>By stage</h3>") == 1
    assert page.count("<h3>By source</h3>") == 1
    assert "Last 7 days</h2>" not in page


def test_today_ignores_yesterday_evening(tmp_path):
    """The boundary this page and the spend ceiling share is the USER's
    midnight. UTC's is 6pm in Albuquerque, so an evening's spend used to show
    up as the next morning's."""
    from datetime import timedelta
    from curbside.db import Store
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)

    run_id = s.start_run(type("H", (), {"id": "want:tv-stand"})(), "facebook")
    s.conn.execute("UPDATE runs SET started_at=? WHERE id=?",
                   ((_today_iso(cfg) - timedelta(hours=2)).isoformat(), run_id))
    s.finish_run(run_id, cost_usd=7.77, n_scored=30)

    page = client.get("/stats?period=today").text
    body = page[page.index("<h2>Each hunt"):page.index("<h2>Fetched to grabbed")]
    assert "Nothing ran today" in body
    assert "7.77" not in body
    assert "$7.77" in page, "and it still counts towards all time"


def test_the_stage_split_says_first_pass_not_triage(tmp_path):
    """"Triage" is what the code calls it. It is not a word the interface gets
    to use: it appeared once as the listing page's heading, was not recognised,
    and came back here as a stage label. `/triage` and `scores.model` keep it;
    the page says what happens instead.

    The three are listed in the order the money is spent, which is what "by
    stage" claims -- "First pass" sitting last read as a contradiction."""
    from curbside.db import Store
    client, cfg = _client(tmp_path)
    _spend_today(Store(cfg.db_path), cfg, "sweep:free-nearby", "craigslist",
                 0.75, n_scored=4)

    page = client.get("/stats").text
    assert "First pass" in page
    assert "Triage" not in page and "triage" not in page
    assert page.index("First pass") < page.index("Appraisal") < \
        page.index("Photo checks")


def test_the_listing_that_cost_the_most_today_is_findable(tmp_path):
    """An image pass runs about 15x a text appraisal, so one photo-checked
    junk post can eat an afternoon. It should be one glance away.

    The heading says "Cost the most". It said "Dearest judged", which is
    British for the same thing on a page of dollar figures, where it reads as
    a price floor rather than a cost ranking."""
    from datetime import timedelta
    from curbside.db import Store
    from curbside.models import Listing, Score
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    _spend_today(s, cfg, "sweep:free-nearby", "craigslist", 1.0, n_scored=2)

    when = _today_iso(cfg) + timedelta(hours=1)
    for lid, title, model, usd in (
            ("x:1", "A cheap chair", "sonnet", 0.003),
            ("x:2", "Free junk metal removal", "sonnet+images", 0.052)):
        l = Listing(id=lid, source="x", source_id=lid[-1], title=title,
                    description=None, price_cents=0, currency="USD", url="u")
        s.upsert_listing(l)
        s.save_score(Score(listing_id=lid, hunt_id="sweep:free-nearby",
                           model=model, scored_at=when, match="no",
                           deal_score=1.0, est_value_cents=None, condition=None,
                           matched_want=None, worth_grabbing=False, unknowns=(),
                           requirements=(), red_flags=(), reasoning="r",
                           images_checked=1 if "image" in model else 0,
                           cost_usd=usd), priced_at_cents=0)

    rows = s.dearest_since(_today_iso(cfg).isoformat())
    assert [r["listing_id"] for r in rows] == ["x:2", "x:1"], "dearest first"

    page = client.get("/stats").text
    assert "<h3>Cost the most</h3>" in page
    assert "Free junk metal removal" in page
    assert "$0.052" in page
    assert "Appraisal only" in page, "the figure is a floor and must say so"


def test_a_free_judgement_is_not_listed_as_something_we_paid_for(tmp_path):
    """Triage rows carry a zero cost. A list of what money went on must not be
    padded with things that cost nothing."""
    from datetime import timedelta
    from curbside.db import Store
    from curbside.models import Listing, Score
    _client(tmp_path)
    s = Store(tmp_path / "dearest.db")
    l = Listing(id="x:1", source="x", source_id="1", title="t", description=None,
                price_cents=0, currency="USD", url="u")
    s.upsert_listing(l)
    when = _today_iso()
    s.save_score(Score(listing_id="x:1", hunt_id="h", model="claude_code:triage",
                       scored_at=when + timedelta(hours=1), match="no",
                       deal_score=0.0, est_value_cents=None, condition=None,
                       matched_want=None, worth_grabbing=False, unknowns=(),
                       requirements=(), red_flags=(), reasoning="",
                       cost_usd=0.0), priced_at_cents=0)
    assert s.dearest_since(when.isoformat()) == []
    s.close()


# --- whether the model looked at the photographs --------------------------

def _score_row(**kw):
    base = dict(hunt_id="want:tv-stand", deal_score=5.0, images_checked=0,
                needs_images=0, image_question=None)
    return {**base, **kw}


def test_the_photo_verdict_has_three_states_not_two():
    """Reported: Discord says whether the photos were checked and the site
    did not appear to. It did, as a chip that showed up when they HAD been --
    so "judged on the text alone" and "asked for a look and never got one"
    were the same blank space. 207 collected scores are the second."""
    from curbside.web.app import photo_verdict

    checked = photo_verdict([_score_row(images_checked=1, deal_score=6.0)])
    assert checked["checked"] and not checked["asked"]

    asked = photo_verdict([_score_row(needs_images=1)])
    assert asked["asked"] and not asked["checked"]

    text_only = photo_verdict([_score_row()])
    assert not text_only["checked"] and not text_only["asked"]

    assert photo_verdict([]) is None


def test_the_score_before_the_photos_is_kept_and_shown():
    """Both rows are kept for exactly this reason: the text judgement stays
    next to the one that looked. Every sampled pair moved, and one went DOWN
    from 7.0 to 6.0 when the photos showed a corner unit."""
    from curbside.web.app import photo_verdict
    v = photo_verdict([_score_row(images_checked=1, deal_score=6.0),
                       _score_row(deal_score=7.0)])
    assert v["before"] == 7.0 and v["after"] == 6.0


def test_a_before_score_from_another_hunt_is_not_borrowed():
    """One listing can be judged by several hunts, and their scores answer
    different questions. A 9.0 from the free sweep is not the 'before' of a
    tv-stand appraisal."""
    from curbside.web.app import photo_verdict
    v = photo_verdict([_score_row(images_checked=1, deal_score=6.0),
                       _score_row(hunt_id="sweep:free-nearby", deal_score=9.0)])
    assert v["before"] is None


def _asked_and_not_looked(tmp_path, deal_score: float):
    """A listing the model said it could not settle without seeing the photos,
    and never did. `deal_score` decides which side of the hunt's bar it lands
    on, which is what decides WHY the photos were never bought."""
    from datetime import datetime, timezone
    from curbside.db import Store
    from curbside.models import Listing, Score
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    hunt = cfg.hunts[0]
    l = Listing(id="x:1", source="x", source_id="1", title="A media console",
                description=None, price_cents=0, currency="USD", url="u")
    s.upsert_listing(l); s.mark_matches(hunt.id, [l])
    s.save_score(Score(listing_id="x:1", hunt_id=hunt.id, model="m",
                       scored_at=datetime.now(timezone.utc), match="unknown",
                       deal_score=deal_score, est_value_cents=None,
                       condition=None, matched_want=None, worth_grabbing=True,
                       unknowns=(), requirements=(), red_flags=(), reasoning="r",
                       needs_images=True,
                       image_question="Is it at least 70 inches wide?"),
                 priced_at_cents=0)
    return client.get("/listing/x:1").text, hunt


def test_an_unlooked_at_listing_says_so_on_its_page(tmp_path):
    """The state that used to be silent: the model could not settle it without
    seeing the photos, and never got to."""
    page, _ = _asked_and_not_looked(tmp_path, 9.0)
    assert "Photos not checked" in page
    assert "Is it at least 70 inches wide?" in page, \
        "the question the photos were meant to answer is worth reading"


def test_the_budget_is_only_blamed_when_the_budget_was_the_reason(tmp_path):
    """The page blamed `max_image_checks` for every unbought image pass. It is
    the reason for very few of them: photographs are only bought for a listing
    that would already reach a bin on its text score, so one scoring under the
    bar is turned down by `route` long before the budget is consulted -- 162 of
    169 unmet requests in the live data, i.e. the sentence was wrong on 96% of
    the listings carrying it, while naming the one number a reader might go and
    raise in response.

    The two cases differ ONLY in the score, which is the point: nothing about
    the listing or the request changed."""
    over, hunt = _asked_and_not_looked(tmp_path, hunt_bar := 9.0)
    assert hunt.min_deal_score <= hunt_bar
    assert "image budget ran out" in over

    second = tmp_path / "b"; second.mkdir()
    under, _ = _asked_and_not_looked(second, 1.0)
    assert "not going to be picked on its text score" in under, (
        "and NOT 'it scored too low' -- a want hunt declines a non-match "
        "whatever it scored, so the sentence has to name the test, not one "
        "of the several things that can fail it")
    assert "image budget" not in under


# --- timestamps, in the reader's clock ------------------------------------

def test_a_stamp_is_shown_in_the_readers_hours_not_utc():
    """`raw[:16].replace("T", " ")` was wrong twice. It rendered 24-hour time,
    which reads like a log line, and it rendered UTC with nothing saying so --
    a run at "19:50" in the table happened at 1:50pm where the user is, so
    every timestamp on the site was six hours out."""
    from zoneinfo import ZoneInfo
    from curbside.web.app import local_stamp
    abq = ZoneInfo("America/Denver")

    assert local_stamp("2026-09-14T19:50:14+00:00", abq) == "14 Sep, 1:50pm"
    # the two the clock arithmetic gets wrong: there is no 0am and no 0pm
    assert local_stamp("2026-09-14T06:05:00+00:00", abq) == "14 Sep, 12:05am"
    assert local_stamp("2026-09-14T18:00:00+00:00", abq) == "14 Sep, 12:00pm"
    # and a conversion that moves the DATE as well as the time
    assert local_stamp("2026-01-02T00:30:00+00:00", abq) == "1 Jan, 5:30pm"


def test_a_stamp_with_no_zone_is_read_as_utc():
    """Everything this project stores is UTC. A naive one is not local time."""
    from zoneinfo import ZoneInfo
    from curbside.web.app import local_stamp
    abq = ZoneInfo("America/Denver")
    assert local_stamp("2026-09-14T19:50:14", abq) == \
        local_stamp("2026-09-14T19:50:14+00:00", abq)


def test_an_unreadable_stamp_is_shown_rather_than_swallowed():
    """Failing open: a value that will not parse is still a value, and an
    empty cell would hide that something is wrong with it."""
    from curbside.web.app import local_stamp
    assert local_stamp("not a date") == "not a date"
    assert local_stamp("") == "" and local_stamp(None) == ""


def test_no_template_prints_a_raw_utc_timestamp():
    """The old slice-and-replace is easy to reach for and wrong every time.
    If a new table needs a date, it goes through `when()`."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for tpl in (root / "curbside/web/templates").glob("*.html"):
        text = tpl.read_text()
        assert "replace('T',' ')" not in text, tpl.name
        assert 'replace("T"," ")' not in text, tpl.name


def test_the_listing_page_says_when_the_seller_posted_it(tmp_path):
    """Asked for directly. It was captured all along -- Facebook states it on
    every listing -- and the card carried only a relative age, so the exact
    date existed nowhere but the database."""
    from datetime import datetime, timezone
    from curbside.db import Store
    from curbside.models import Listing
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    l = Listing(id="x:1", source="x", source_id="1", title="A media console",
                description=None, price_cents=0, currency="USD", url="u",
                posted_at=datetime(2026, 9, 6, 19, 24, tzinfo=timezone.utc))
    s.upsert_listing(l); s.mark_matches(cfg.hunts[0].id, [l])
    assert "listed 6 Sep, 1:24pm" in client.get("/listing/x:1").text


def test_a_listing_with_no_posted_date_says_so(tmp_path):
    """Craigslist only reveals it on the item page, so one that was never
    enriched genuinely has none. Silence would read as "posted today"."""
    from curbside.db import Store
    from curbside.models import Listing
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    l = Listing(id="x:2", source="x", source_id="2", title="A media console",
                description=None, price_cents=0, currency="USD", url="u")
    s.upsert_listing(l); s.mark_matches(cfg.hunts[0].id, [l])
    assert "listing date unknown" in client.get("/listing/x:2").text


# --- plan usage on /runs ---------------------------------------------------
# The pill says "Judging paused" and links here, so this is where the plan's
# own numbers have to be. They arrive only in Claude Code's event stream, and
# only when something is judged, so every one of these is a memory with an
# expiry date -- which is the thing these tests are about.

def _window(used, ceiling=0.70, resets_in=3600, expired=False, stale=False,
            now=None):
    import time
    from curbside.scoring.claude_code import PlanUsage, PlanWindow
    now = time.time() if now is None else now
    return PlanUsage((PlanWindow(label="5-hour", used=used, ceiling=ceiling,
                                 resets_at=now + resets_in, expired=expired,
                                 stale=stale),), None, stale)


def test_a_spent_window_cannot_overflow_its_own_track():
    """The wire really does report 1.01 once a window is spent, and the percent
    is written straight into a CSS width."""
    import time
    from curbside.web.app import plan_usage_view
    now = time.time()
    w = plan_usage_view(_window(1.01, now=now), now)["windows"][0]
    assert w["pct"] == 100 and w["over"] and w["ceiling_pct"] == 70
    assert w["resets_in"] == "in 1h 0m"


def test_an_expired_reading_draws_no_bar():
    """The window it measured has rolled, so a bar would assert a fact about a
    window that no longer exists."""
    import time
    from curbside.web.app import plan_usage_view
    now = time.time()
    w = plan_usage_view(_window(0.99, resets_in=-60, expired=True, now=now),
                        now)["windows"][0]
    assert w["pct"] is None and w["over"] is False and w["resets_in"] == ""


def test_a_countdown_longer_than_a_day_is_said_in_days():
    """The seven-day window is routinely more than a day out, and "in 114h 9m"
    is correct and unreadable."""
    from curbside.web.app import ago_words, until_words
    assert until_words(4 * 86400 + 18 * 3600) == "in 4d 18h"
    assert until_words(-5) == "" and until_words(None) == ""
    assert ago_words(0) == "just now" and ago_words(90) == "1h ago"


def _record_reading(cfg, five="0.42", seven=None, minutes_ago=0, ahead=3600,
                    dated=True):
    import time
    from datetime import datetime, timedelta, timezone
    from curbside.db import Store
    from curbside.scoring.claude_code import (RESET_5H, RESET_7D, UTIL_5H,
                                             UTIL_7D, UTIL_AT)
    s = Store(cfg.db_path)
    s.set_setting(UTIL_5H, five)
    if dated:
        s.set_setting(RESET_5H, str(time.time() + ahead))
    if seven is not None:
        s.set_setting(UTIL_7D, seven)
        s.set_setting(RESET_7D, str(time.time() + 4 * 86400))
    s.set_setting(UTIL_AT, (datetime.now(timezone.utc)
                            - timedelta(minutes=minutes_ago)
                            ).isoformat(timespec="seconds"))
    return s


def test_runs_shows_how_much_of_the_plan_is_gone(tmp_path):
    """Asked for directly: when judging is paused, how much has been used."""
    client, cfg = _client(tmp_path)
    _record_reading(cfg, five="0.42", seven="0.86")
    page = client.get("/runs").text
    assert "5-hour window" in page and "42%" in page
    assert "7-day window" in page and "86%" in page
    assert "Stands aside at 70%" in page       # the mark, next to the number


def test_a_window_over_the_mark_says_that_is_why_judging_stopped(tmp_path):
    client, cfg = _client(tmp_path)
    _record_reading(cfg, five="0.82")
    assert "so judging stands aside" in client.get("/runs").text


def test_an_undated_reading_too_old_to_enforce_says_so(tmp_path):
    """A stale number that named no reset gates nothing -- enforcing one is
    self-sealing. A page showing 82% over a bot that is judging away would read
    as broken."""
    client, cfg = _client(tmp_path)
    _record_reading(cfg, five="0.82", minutes_ago=180, dated=False)
    page = client.get("/runs").text
    assert "Too old to enforce" in page
    assert "so judging stands aside" not in page


def test_an_old_reading_that_named_its_reset_is_still_the_reason(tmp_path):
    """The lie in the other direction. This one IS enforcing -- the window it
    named has not rolled yet -- so the page must not offer it as history."""
    client, cfg = _client(tmp_path)
    _record_reading(cfg, five="0.82", minutes_ago=180)
    page = client.get("/runs").text
    assert "so judging stands aside" in page
    assert "Held until the window resets" in page
    assert "Too old to enforce" not in page


def test_with_nothing_read_yet_the_panel_teaches_instead_of_lying(tmp_path):
    """Claude Code reports the windows only while judging, so a database that
    has never judged has no reading -- which is not zero."""
    client, _ = _client(tmp_path)
    page = client.get("/runs").text
    assert "No reading yet" in page and "0%" not in page


# --- recording what you grabbed --------------------------------------------


def _saved_card(tmp_path, price_cents=25000):
    """A saved listing with a score, ready to be grabbed off /saved."""
    from datetime import datetime, timezone
    from curbside.db import Store
    from curbside.models import Listing, Score
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    l = Listing(id="fb:7", source="facebook", source_id="7",
                title="Mid century walnut bookshelf", description="solid",
                price_cents=price_cents, currency="USD", url="u")
    s.upsert_listing(l)
    s.mark_matches("h", [l])
    s.set_status("h", l.id, "saved")
    s.save_score(Score(listing_id=l.id, hunt_id="h", model="m",
                       scored_at=datetime.now(timezone.utc), match="yes",
                       deal_score=8.0, est_value_cents=40000, condition=None,
                       matched_want="bookshelf", worth_grabbing=True,
                       unknowns=(), requirements=(), red_flags=(),
                       reasoning="r"), priced_at_cents=price_cents)
    return client, s, l


def test_the_saved_page_offers_the_price_before_recording_it(tmp_path):
    """A tap that filed the asking price would record a haggled $180 as $250,
    and a calibration point that lies is worse than none. So the button reveals
    a field pre-filled with the asking price rather than acting on it."""
    client, _, _ = _saved_card(tmp_path)
    body = client.get("/saved").text
    assert "Grabbed it" in body
    assert 'class="grabtoggle"' in body
    assert 'action="/grabbed"' in body
    assert 'value="250"' in body, "the asking price is the sensible default"


def test_grabbing_it_records_what_you_paid_and_leaves_the_other_bins(tmp_path):
    client, store, l = _saved_card(tmp_path)
    store.mark_matches("sweep", [l])
    store.set_status("sweep", l.id, "free_find")

    r = client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                      "paid": "180"}, follow_redirects=False)
    assert r.status_code == 303

    row = store.conn.execute(
        "SELECT grabbed_at, paid_cents FROM listings WHERE id=?",
        (l.id,)).fetchone()
    assert row["paid_cents"] == 18000 and row["grabbed_at"] is not None
    assert store.statuses("h")[l.id] == "grabbed"
    assert store.statuses("sweep")[l.id] == "gone"

    body = client.get("/saved?show=grabbed").text
    assert "Paid $180" in body
    assert "1</b> grabbed" in body, "counted apart from things still to act on"
    assert "Grabbed it" not in body, "no second offer on a thing you already own"


def test_a_dollar_sign_and_a_comma_are_not_a_typo(tmp_path):
    """It is typed on a phone, and "$1,350" is how a person writes money."""
    client, store, l = _saved_card(tmp_path)
    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "paid": "$1,350"})
    assert store.conn.execute(
        "SELECT paid_cents FROM listings WHERE id=?", (l.id,)
    ).fetchone()["paid_cents"] == 135000


def test_an_unrecorded_price_is_stored_as_nothing_not_as_free(tmp_path):
    """Blank and 0 are different answers, and the page must not render one as
    the other: free is most of what this bot finds, and "I did not note it" is
    not a data point at all. An unparseable figure is treated as blank rather
    than raised on -- a typo must not cost the record of the purchase."""
    client, store, l = _saved_card(tmp_path)
    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "paid": "banana"})
    assert store.conn.execute(
        "SELECT paid_cents, grabbed_at FROM listings WHERE id=?", (l.id,)
    ).fetchone()["paid_cents"] is None

    body = client.get("/saved?show=grabbed").text
    # The card's own figure line, not the word anywhere on the page -- the site
    # description says "Free and underpriced things".
    assert 'class="grabbed"' not in body
    assert "1</b> grabbed" in body, "still recorded, just without a figure"


def test_free_is_recorded_as_free(tmp_path):
    client, store, l = _saved_card(tmp_path, price_cents=0)
    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "paid": "0"})
    assert store.conn.execute(
        "SELECT paid_cents FROM listings WHERE id=?", (l.id,)
    ).fetchone()["paid_cents"] == 0
    assert "Free." in client.get("/saved?show=grabbed").text


def test_undo_withdraws_the_purchase(tmp_path):
    client, store, l = _saved_card(tmp_path)
    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "paid": "180"})
    r = client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                      "undo": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert store.statuses("h")[l.id] == "saved"
    assert store.conn.execute(
        "SELECT paid_cents FROM listings WHERE id=?", (l.id,)
    ).fetchone()["paid_cents"] is None
    assert "Grabbed it" in client.get("/saved").text


def test_undo_puts_a_free_find_back_where_it_was(tmp_path):
    """REGRESSION. `ungrab` restored a flat `saved`, which was true only while
    the button lived on a saved card. It is on the listing page now, where the
    row you act on may be `free_find` or `wanted` -- so undoing a mis-tap
    silently moved a free find onto your saved list."""
    client, store, l = _saved_card(tmp_path)
    store.set_status("h", l.id, "free_find")

    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "paid": "0"})
    assert store.statuses("h")[l.id] == "grabbed"

    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "undo": "1"})
    assert store.statuses("h")[l.id] == "free_find"


def test_seeing_a_grabbed_listing_again_does_not_lose_the_undo(tmp_path):
    """`mark_gone` clears `status_before_gone` for everything it sees again,
    and Facebook goes on showing a listing after it sells. A grabbed row keeps
    its hint, or undo quietly reverts to the old flat `saved`."""
    client, store, l = _saved_card(tmp_path)
    store.set_status("h", l.id, "wanted")
    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id})

    store.mark_gone("h", "facebook", [l.id])          # still in the feed
    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "undo": "1"})
    assert store.statuses("h")[l.id] == "wanted"


def test_a_grab_recorded_before_the_hint_existed_still_undoes(tmp_path):
    """Rows grabbed by the earlier version carry no `status_before_gone`, so
    the old behaviour has to survive as the fallback."""
    client, store, l = _saved_card(tmp_path)
    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id})
    store.conn.execute(
        "UPDATE hunt_matches SET status_before_gone=NULL WHERE listing_id=?",
        (l.id,))
    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "undo": "1"})
    assert store.statuses("h")[l.id] == "saved"


def test_the_listing_page_does_not_call_your_own_bookshelf_unlisted(tmp_path):
    """`mark_grabbed` stamps `sold_at` too, so the sold banner would claim a
    thing in your house is no longer available. The grabbed state is checked
    first, and this is the one page with room for the date."""
    client, store, l = _saved_card(tmp_path)
    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "paid": "180"})
    body = client.get(f"/listing/{l.id}").text
    assert "Yours." in body and "Paid $180" in body
    assert "No longer listed" not in body and "Sold." not in body


def test_grabbing_something_that_is_not_there_says_so(tmp_path):
    """Same reason `/triage` checks: the card is already folding away and the
    toast is about to claim it worked."""
    client, _, _ = _saved_card(tmp_path)
    assert client.post("/grabbed", data={"hunt_id": "h", "listing_id": "nope",
                                         "paid": "1"}).status_code == 404


def test_stats_checks_the_model_against_what_you_paid(tmp_path):
    """The only falsification in the database. Every judged listing carries an
    estimated value and nothing else can ever check one."""
    client, store, l = _saved_card(tmp_path)
    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "paid": "180"})
    body = client.get("/stats").text
    assert "Grabbed" in body
    assert "$400" in body and "$180" in body


def test_the_bot_never_messaged_a_seller_so_nothing_says_contacted():
    """`contacted` meant "I messaged the seller". It was never offered in the
    interface, was used zero times in the life of the bot, and sat in a dozen
    status lists that all had to keep agreeing about it -- while contradicting
    the read-only boundary, since this tool does not message anyone. Removed
    alongside `grabbed` so the number of statuses stayed where it was.

    In the spirit of `tests/test_docs.py`: the cheapest way to stop a retired
    concept creeping back is to fail if its name reappears in the CODE. The docs
    still name it, deliberately -- this project records what it rejected and why,
    and "a status a dozen places maintained for months while holding zero rows"
    is worth keeping written down. Only `curbside/` must be clean."""
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    out = subprocess.run(["grep", "-rn", "contacted", "curbside"],
                         cwd=root, capture_output=True, text=True).stdout
    assert not out.strip(), f"`contacted` is back in the code:\n{out}"


def test_a_labelled_action_that_is_not_a_form_still_shares_the_row():
    """REPORTED: the Grabbed it button overlapped Dismiss on /saved.

    `.actions` is a flex row. `.actions button` sets `width:100%`, which the
    triage buttons survive because their wrapper carries `flex:1 1 0` -- the
    basis is the form's, not the button's. `.grabtoggle` is a bare button with
    no wrapper, so that `width:100%` became its OWN flex basis: it claimed the
    whole row, the Dismiss form shrank to zero, and a `white-space:nowrap`
    label spilled out of a zero-width box on top of it.

    `.blocktoggle` never hit this because it is a fixed-width icon square with
    `flex:none`. Any future labelled control in this row needs the rule too.
    """
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1]
           / "curbside/web/static/app.css").read_text()
    assert ".actions .grabtoggle{flex:1 1 0;min-width:0}" in css, (
        "a bare button in .actions must declare its own flex basis, or it "
        "inherits width:100% as one and starves everything beside it")


def test_saved_opens_on_the_things_still_to_decide(tmp_path):
    """Things you own accumulate; things to act on do not. Within a few months
    an undivided page would be mostly archive, and a thing already in your
    hallway is not a thing to decide about -- so the default list is the one
    with work in it, and the other is one tap away."""
    client, store, l = _saved_card(tmp_path)
    assert "grabbed" not in client.get("/saved").text.split("<h1>")[1][:400]

    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "paid": "180"})

    default = client.get("/saved").text
    assert "Mid century walnut bookshelf" not in default
    assert "Nothing saved yet" in default, "the work list is empty now"
    assert 'href="/saved?show=grabbed"' in default, "and the other tab is offered"

    other = client.get("/saved?show=grabbed").text
    assert "Mid century walnut bookshelf" in other


def test_the_tabs_only_appear_once_there_is_a_second_list(tmp_path):
    """A lone tab on a page that is usually three cards long is furniture."""
    client, _, _ = _saved_card(tmp_path)
    assert "filterchips" not in client.get("/saved").text


def test_something_you_own_does_not_look_like_something_you_lost(tmp_path):
    """REPORTED: a grabbed card was greyed out and its price struck through,
    which is the styling for a listing somebody else bought.

    `mark_grabbed` stamps `sold_at`, so the card's `gone` flag picked it up.
    Same fact, opposite news: the market did not take this one away from you.
    """
    client, store, l = _saved_card(tmp_path)
    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "paid": "180"})
    body = client.get("/saved?show=grabbed").text

    assert 'class="card got"' in body
    assert "card gone" not in body, "greyed out and struck through"
    assert 'class="soldmark got">yours' in body


def test_a_card_you_own_offers_nothing(tmp_path):
    """Two reasons, and they point the same way.

    Dismissed titles become negative examples in that hunt's next prompt, so
    offering Dismiss on something you liked enough to drive out and buy would
    teach the hunt the exact opposite of what happened.

    And "Not mine" was here, in the slot Dismiss occupies on every other card:
    REPORTED pressed by accident. One unconfirmed tap on a scrolling list threw
    away a date and a figure nothing else remembers -- the stamp had to be
    restored by hand, to the minute, from what a screenshot happened to show.
    It lives on the listing page behind a confirmation now."""
    client, store, l = _saved_card(tmp_path)
    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "paid": "180"})
    body = client.get("/saved?show=grabbed").text

    assert "Dismiss" not in body
    assert "Not mine" not in body
    assert 'name="undo"' not in body, "nothing destructive one tap from a list"


def test_not_mine_puts_it_back_a_week_later(tmp_path):
    """Undo in the toast covers the mistake you notice at once. This covers the
    one you notice after the page has been reloaded."""
    client, store, l = _saved_card(tmp_path)
    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "paid": "180"})
    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "undo": "1", "back": "/saved?show=grabbed"})

    assert store.statuses("h")[l.id] == "saved"
    assert "Mid century walnut bookshelf" in client.get("/saved").text
    assert "filterchips" not in client.get("/saved").text, "no empty second tab"


def test_the_motivated_chip_does_not_repeat_the_price_line(tmp_path):
    """REPORTED from a saved card: a green "motivated seller" chip carrying a
    down arrow, directly under a price line that already showed the drop and by
    how much. The arrow restated it a few millimetres away and without the
    figure, which is the same redundancy the "N price drops" chip is already
    guarded against. The word is the chip."""
    from pathlib import Path
    card = (Path(__file__).resolve().parents[1]
            / "curbside/web/templates/_card.html").read_text()
    motivated = card.split("{% if motivated %}")[1].split("{% elif")[0]
    assert "motivated seller" in motivated
    assert "i-down" not in motivated, "the price line above already says this"


def test_a_flag_does_not_wear_the_colour_of_uncertainty():
    """Amber means "we do not know" here -- `unknowns`, an unverified match, an
    unanswered photo request. A flag is not missing information: it is
    something the model noticed AGAINST the listing. They were sharing a
    colour, so two different statements read as one.

    Flags are red again, but the fill is what had made red shout. A soft ground
    on the chip and a bare rule in the list say "counts against it" without
    claiming danger, which is right for a set whose commonest member is "Only
    one photo"."""
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1]
           / "curbside/web/static/app.css").read_text()
    chip = css.split(".chip.flag{")[1].split("}")[0]
    assert "var(--bad-soft)" in chip and "var(--bad)" in chip

    item = css.split(".flags li{")[1].split("}")[0]
    assert "var(--bad)" in item and "border-left" in item
    assert "background" not in item, "a note inside a panel is not a banner"

    # And uncertainty keeps amber, so the two stay distinguishable.
    unknowns = css.split(".unknowns{")[1].split("}")[0]
    assert "var(--warn)" in unknowns and "--bad" not in unknowns


def test_the_listing_page_colours_its_flags_too(tmp_path):
    """REPORTED, with a screenshot: flags were amber chips on the card and
    plain grey text in the score table you land on after tapping it. That table
    cell was the only model output on the page with no treatment at all, sitting
    directly above `unknowns` on its amber panel.

    Also fixes a second thing the comma join hid: several flags became one
    run-on sentence, and a flag can itself contain a comma -- "Description
    claims 'Ethan Allen Country French Bergere' but then says the set is 'from
    JC Penny's', contradictory" is one flag that reads as two."""
    from datetime import datetime, timezone
    from curbside.db import Store
    from curbside.models import Listing, Score
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    l = Listing(id="x:5", source="x", source_id="5", title="A tv stand",
                description=None, price_cents=15000, currency="USD", url="u")
    s.upsert_listing(l); s.mark_matches("h", [l])
    s.save_score(Score(listing_id="x:5", hunt_id="h", model="m",
                       scored_at=datetime.now(timezone.utc), match="unknown",
                       deal_score=5.0, est_value_cents=None, condition=None,
                       matched_want="tv-stand", worth_grabbing=True,
                       unknowns=("Width of the stand",), requirements=(),
                       red_flags=("Generic one-line description",
                                  "Only one photo"),
                       reasoning="r"), priced_at_cents=15000)

    body = client.get("/listing/x:5").text
    block = body.split('class="judgement-why"')[1].split("</ul>")[0]
    assert '<ul class="flags">' in block, "styled like the card, not plain text"
    assert block.count("<li>") == 2, "one item per flag, not a comma-joined run"
    assert "i-alert" in block

    # And the same on an earlier pass, which keeps its own flags.
    s.save_score(Score(listing_id="x:5", hunt_id="h", model="sonnet+images",
                       scored_at=datetime.now(timezone.utc), match="yes",
                       deal_score=6.0, est_value_cents=None, condition=None,
                       matched_want="tv-stand", worth_grabbing=True,
                       unknowns=(), requirements=(),
                       red_flags=("Only one photo",), reasoning="r2",
                       images_checked=True), priced_at_cents=15000)
    earlier = client.get("/listing/x:5").text.split('class="pass"')[1]
    assert '<ul class="flags">' in earlier


def _scored_listing(tmp_path, *scores):
    """A listing on the detail page carrying the scores given."""
    from datetime import datetime, timezone, timedelta
    from curbside.db import Store
    from curbside.models import Listing, Score
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    l = Listing(id="x:6", source="x", source_id="6", title="A tv stand",
                description=None, price_cents=15000, currency="USD", url="u")
    s.upsert_listing(l); s.mark_matches("want:tv-stand", [l])
    now = datetime.now(timezone.utc)
    for n, (model, score, checked) in enumerate(scores):
        s.save_score(Score(listing_id=l.id, hunt_id="want:tv-stand",
                           model=model, scored_at=now + timedelta(minutes=n),
                           match="unknown", deal_score=score,
                           est_value_cents=None, condition=None,
                           matched_want="tv-stand", worth_grabbing=True,
                           unknowns=("Width of the stand",), requirements=(),
                           red_flags=(), reasoning=f"because {model}",
                           images_checked=checked), priced_at_cents=15000)
    return client.get("/listing/x:6").text


def test_the_judgement_comes_before_the_paperwork(tmp_path):
    """The Scores panel was a `table.responsive`, which on a phone stacks into
    one labelled row per column: When, Hunt, Model, Score, Want, Flags,
    Reasoning. That put a timestamp as the largest text in the panel and the
    judgement last, behind four fields of metadata -- two of which said the
    same thing, since a want hunt's id IS `want:` plus its name.

    So the score and the sentence lead, and the rest is one quiet line."""
    body = _scored_listing(tmp_path, ("sonnet", 5.0, False))
    panel = body.split("<h2>Scores</h2>")[1]

    assert 'class="judgement-top"' in panel
    assert panel.index("judgement-facts") < panel.index("because sonnet"), \
        "the facts are a header above the sentence, not a footnote below it"
    assert panel.index("5.0") < panel.index("because sonnet"), \
        "and the score leads"
    assert 's-mid' in panel, "the score keeps the colour it has on the card"


def test_history_appears_only_when_there_is_some(tmp_path):
    """87% of listings carry exactly one score, and there is no history to show
    for those. The 13% with a second pass are the case worth drawing: what the
    photographs changed."""
    one = _scored_listing(tmp_path, ("sonnet", 5.0, False))
    panel = one.split("<h2>Scores</h2>")[1].split("<h2>")[0]
    assert 'class="pass"' not in panel
    assert "Earlier passes" not in panel

    second = tmp_path / "b"; second.mkdir()
    two = _scored_listing(second, ("sonnet", 5.0, False),
                          ("sonnet+images", 7.0, True))
    panel = two.split("<h2>Scores</h2>")[1].split("<h2>")[0]
    assert "Earlier passes" in panel
    assert panel.index("judgement-top") < panel.index('class="pass"')


def test_the_history_never_repeats_the_judgement(tmp_path):
    """REPORTED with a screenshot: the table's first row WAS the verdict above
    it -- same score, hunt, model, timestamp and sentence -- so the older row
    beneath it read as the page contradicting itself rather than as history.

    Only `scores[1:]` is drawn, so what is shown is what came before."""
    two = _scored_listing(tmp_path, ("sonnet", 5.0, False),
                          ("sonnet+images", 7.0, True))
    panel = two.split("<h2>Scores</h2>")[1].split("<h2>")[0]

    assert panel.count("because sonnet+images") == 1, "the latest, stated once"
    history = panel.split("Earlier passes")[1]
    assert "because sonnet+images" not in history
    assert "because sonnet<" in history or "because sonnet " in history \
        or "because sonnet</p>" in history

    assert "<table" not in panel, (
        "two rows sharing three of four columns is not a table, and it stacked "
        "into HUNT / MODEL / SCORE / FLAGS labels on a phone")


def test_one_filled_block_per_panel(tmp_path):
    """REPORTED with a screenshot: six amber things in the Scores panel, three
    of them filled boxes stacked in a row -- the photo banner, the flags and
    the unknowns. When everything is emphasised nothing is.

    The banner keeps its fill because it is a state of the whole panel. The two
    inside it became left rules: same colour coding, no shouting."""
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1]
           / "curbside/web/static/app.css").read_text()
    for sel in (".flags li{", ".unknowns{"):
        block = css.split(sel)[1].split("}")[0]
        assert "border-left" in block, sel
        assert "background" not in block, f"{sel} is filled again"
    banner = css.split(".state-line.photostate.asked{")[1].split("}")[0]
    assert "background" in banner, "the panel's own state still reads as one"


def test_the_listing_page_does_not_borrow_a_class_that_is_already_positioned():
    """REPORTED with a screenshot: the whole Scores panel rendered on top of the
    listing's photographs.

    `app.js` stamps `<div class="verdict">` into a card as it folds away, and
    that class is `position:absolute; inset:0; z-index:2`. Its rule sits later
    in app.css than the panel's, so it won, and a block of prose became an
    overlay. One stylesheet is one namespace across every page, and a name that
    reads as generic ("verdict", "panel", "row") is the kind most likely to be
    taken already."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    page = (root / "curbside/web/templates/listing.html").read_text()
    assert 'class="verdict"' not in page

    css = (root / "curbside/web/static/app.css").read_text()
    badge = css.split(".verdict{")[1].split("}")[0]
    assert "position:absolute" in badge, (
        "if the badge is no longer absolute this test has lost its point, "
        "but the name is still shared: pick a different one anyway")


def test_a_deleted_want_can_still_explain_itself(tmp_path):
    """A want is archived rather than dropped and everything it matched stays
    readable -- but its hunt leaves `cfg.hunts`, so `route` had nothing to
    re-decide against and the photo line fell back to "did not get one", the
    least useful of its three answers. That hit 254 of 1,378 listings here,
    every one judged by `want:tv-stand` before it was deleted."""
    from datetime import datetime, timezone
    from curbside.db import Store
    from curbside.models import Listing, Score, Want
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    s.seed_wants((Want(name="bookcase", description="a bookcase",
                       max_price_cents=10000, queries=("bookcase",)),))
    l = Listing(id="x:9", source="x", source_id="9", title="A bookcase",
                description=None, price_cents=0, currency="USD", url="u")
    s.upsert_listing(l); s.mark_matches("want:bookcase", [l])
    s.save_score(Score(listing_id=l.id, hunt_id="want:bookcase", model="m",
                       scored_at=datetime.now(timezone.utc), match="unknown",
                       deal_score=1.0, est_value_cents=None, condition=None,
                       matched_want="bookcase", worth_grabbing=True,
                       unknowns=(), requirements=(), red_flags=(),
                       reasoning="r", needs_images=True,
                       image_question="how wide?"), priced_at_cents=0)
    s.archive_want("bookcase")

    body = client.get("/listing/x:9").text
    assert "not going to be picked on its text score" in body
    assert "did not get one" not in body, (
        "the hunt is archived, not gone: it can still be asked")


def test_you_can_record_a_purchase_from_the_listing_page(tmp_path):
    """REPORTED: Grabbed it was on the card only, so opening the listing to
    look at it properly took the control away. Exactly the mistake blocking a
    word made, noted in the comment directly below this control."""
    client, store, l = _saved_card(tmp_path)
    body = client.get(f"/listing/{l.id}").text

    assert 'class="grabctx"' in body
    assert "Grabbed it" in body
    assert 'name="paid"' in body
    assert 'value="250"' in body, "pre-filled from the asking price, as on a card"

    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "paid": "180", "back": f"/listing/{l.id}"})
    after = client.get(f"/listing/{l.id}").text
    assert "Yours." in after and "Paid $180" in after
    assert "Not mine" in after, "and the way back out"
    assert "What did you pay?" not in after, "no second offer on a thing you own"


def test_the_listing_page_records_against_the_bin_you_would_act_from(tmp_path):
    """Any match would do -- `mark_grabbed` clears the listing out of the
    others regardless -- but the row acted on is the one undo restores, so it
    ranks the way the bin views do: a decision already made outranks a
    candidacy."""
    client, store, l = _saved_card(tmp_path)
    store.mark_matches("sweep:free", [l])
    store.set_status("sweep:free", l.id, "free_find")

    body = client.get(f"/listing/{l.id}").text
    ctx = body.split('class="grabctx"')[1].split(">")[0] + \
        body.split('class="grabctx"')[1].split("</div>")[0]
    assert 'data-hunt="h"' in ctx, "the saved row, not the free_find one"


def test_a_listing_in_no_bin_is_not_offered_the_button(tmp_path):
    """You have not decided anything about it yet, so there is nothing to
    record having collected."""
    client, cfg = _client(tmp_path)
    from curbside.db import Store
    from curbside.models import Listing
    s = Store(cfg.db_path)
    l = Listing(id="x:20", source="x", source_id="20", title="A thing",
                description=None, price_cents=0, currency="USD", url="u")
    s.upsert_listing(l); s.mark_matches("h", [l])          # status `new`

    assert 'class="grabctx"' not in client.get("/listing/x:20").text


def test_withdrawing_a_purchase_asks_first(tmp_path):
    """It throws away a date and a figure nothing else in the database
    remembers, so it is not a bare button anywhere."""
    client, store, l = _saved_card(tmp_path)
    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "paid": "180"})
    body = client.get(f"/listing/{l.id}").text

    assert 'class="ungrabtoggle"' in body
    assert "Forget that you got this?" in body
    assert "$180" in body.split("ungrabrow")[1][:400], \
        "the confirmation names what is about to be lost"
    # The row is revealed, not linked: the post itself still needs a press.
    assert '<div class="ungrabrow" hidden>' in body

    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "undo": "1"})
    assert store.statuses("h")[l.id] == "saved"


def test_a_listing_you_own_stops_offering_to_open_the_marketplace(tmp_path):
    """The seller takes the post down after a sale, so the link opens "this
    content isn't available" -- an invitation that cannot be accepted, and on
    the card it sat in the most tappable spot there is.

    The title becomes plain text rather than a dead link, which falls through
    to the card's own overlay: tapping it still opens the listing page, where
    what you paid and when you got it now live."""
    client, store, l = _saved_card(tmp_path)
    assert 'class="out"' in client.get("/saved").text
    assert "Open on the marketplace" in client.get(f"/listing/{l.id}").text

    client.post("/grabbed", data={"hunt_id": "h", "listing_id": l.id,
                                  "paid": "180"})

    card = client.get("/saved?show=grabbed").text
    assert 'class="out"' not in card
    assert l.title in card, "the title is still there, just not a link"
    assert 'href="u"' not in card

    page = client.get(f"/listing/{l.id}").text
    assert "Open on the marketplace" not in page


# --- /runs says what is happening, in words ---------------------------------

def test_a_run_warning_is_said_the_way_a_person_would_say_it():
    """The journal's words are for grepping. This one, from the live box, was
    printed on /runs verbatim."""
    from curbside.web.app import plain_error, plain_warning
    said = plain_warning(
        "detail fetch stopped: BudgetExhausted: request budget exhausted "
        "(25 this run); scoring skipped: 5-hour plan window at 79% "
        "(ceiling 70%) -- standing aside")
    assert said == [
        "Ran out of requests before opening every listing.",
        "Not judged: the 5-hour plan window is at 79%, past the 70% mark."]
    assert plain_warning("fetch skipped: request budget exhausted (4 "
                         "searches, 0 of 25 left this run)") == [
        "Skipped. The pass had used up its requests."]
    assert plain_warning("1 detail fetches failed") == [
        "1 listing could not be opened."]
    assert plain_error("SourceBlocked: no feed data in 566641 bytes -- "
                       "almost certainly throttled").startswith(
        "The site sent a page with no listings")
    # Something it does not recognise is shown as it is, not swallowed.
    assert plain_warning("something new") == ["something new"]


def _run(id, start, end, hunt="want:a", source="facebook", **kw):
    row = {"id": id, "hunt_id": hunt, "source": source, "started_at": start,
           "finished_at": end, "error": None, "warning": None, "n_fetched": 10,
           "n_new": 1, "n_scored": 1, "n_wanted": 0, "n_free_find": 0,
           "cost_usd": 0.01}
    row.update(kw)
    return row


def test_runs_are_grouped_into_the_passes_that_made_them():
    """One pass is ten runs, each starting as the last finished; passes are
    minutes apart. No pass column is needed to tell them apart."""
    from curbside.web.app import group_passes
    rows = [
        _run(1, "2026-09-24T17:00:00+00:00", "2026-09-24T17:01:00+00:00"),
        _run(2, "2026-09-24T17:01:00+00:00", "2026-09-24T17:02:30+00:00",
             source="craigslist", warning="fetch skipped: request budget "
                                          "exhausted (25 this run)"),
        _run(3, "2026-09-24T17:15:00+00:00", "2026-09-24T17:16:00+00:00",
             error="HTTP 403 for https://x"),
    ]
    passes = group_passes(list(reversed(rows)))     # as the page reads them
    assert [len(p["runs"]) for p in passes] == [1, 2], "newest pass first"
    newest, older = passes
    assert newest["failed"] == 1
    assert newest["notes"][0]["text"] == "The site answered 403."
    assert older["fetched"] == 20 and older["took"] == "2m 30s"
    assert older["notes"][0]["text"] == "Skipped. The pass had used up its requests."


def test_the_status_card_leads_with_what_the_pill_says(tmp_path):
    """The pill opens /runs, which used to open on three switches. The top of
    it is now the rest of the pill's sentence, each fact with the one action
    it calls for."""
    from curbside.web.app import now_lines
    hunts = _hunts()
    for h in hunts:
        h.kind = "want"
    held = {"held": True, "override": False, "raw": "r",
            "why": "The 5-hour plan window is at 79%, past the 70% mark",
            "resumes": "Resumes in 12m."}
    running = {"held": False, "override": False, "why": "", "resumes": "",
               "raw": ""}

    lines = now_lines(judging=held, paused=[], hunts=hunts, sched=_sched(),
                      last=None)
    assert lines[0]["title"] == "Judging is paused"
    assert lines[0]["action"] == {"override": 120,
                                  "label": "Judge anyway for 2 hours"}
    assert "Resumes in 12m. Still collecting." in lines[0]["text"]

    off = now_lines(judging=running, paused=hunts, hunts=hunts, sched=_sched(),
                    last=None)
    assert off[0]["title"] == "Everything is paused"
    assert off[0]["action"]["label"] == "Resume all searching"

    fine = now_lines(judging=running, paused=[], hunts=hunts, sched=_sched(),
                     last=None)
    assert [l["title"] for l in fine] == ["Working"]


def test_runs_puts_the_hunts_above_the_log(tmp_path):
    """The hunt list, the only per-hunt view of the backlog, was 46,000px down
    the page on a phone, under 200 run cards. The log is ten passes now, with
    the rest one tap away."""
    from curbside.db import Store
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    for _ in range(3):
        s.finish_run(s.start_run(cfg.hunts[0], "facebook"))
    page = client.get("/runs").text
    assert page.index('id="now"') < page.index('id="hunts"') \
        < page.index('id="passes"')
    assert 'href="/runs/all"' in page
    assert client.get("/runs/all").status_code == 200
    only = client.get(f"/runs/all?hunt={cfg.hunts[0].id}").text
    assert "Every run of" in only


def test_every_cadence_is_set_in_one_place(tmp_path):
    """The sweep's cadence was on /settings and each want's on its own editor.
    One form now sets them all; a field for a hunt that does not exist is
    ignored rather than written."""
    from curbside.db import Store
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    form = {f"iv:{h.id}": "120" for h in cfg.hunts}
    form["iv:want:nobody"] = "15"
    r = client.post("/settings/intervals", data=form, follow_redirects=False)
    assert r.status_code == 303
    from curbside.config import with_store
    assert {h.interval_minutes for h in with_store(cfg, s).hunts} == {120}
    assert s.get_setting("hunt_interval:want:nobody") is None
    page = client.get("/settings").text
    assert page.count('name="iv:') == len(cfg.hunts)


# --- each limit saves on its own, and the small print is readable ---------

def test_each_limit_saves_without_touching_the_others(tmp_path):
    """One Save under four numbers re-sent all four, so changing the radius
    also resubmitted three you had not touched. Each has its own form now,
    and the endpoint leaves alone any field it is not sent."""
    import re
    from curbside.config import with_store
    from curbside.db import Store
    client, cfg = _client(tmp_path)
    page = client.get("/settings").text
    assert len(re.findall(r'<form[^>]*action="/settings/tuning"', page)) == 4
    before = with_store(cfg, Store(cfg.db_path))

    client.post("/settings/tuning", data={"radius_miles": "12"},
                follow_redirects=False)
    after = with_store(cfg, Store(cfg.db_path))
    assert after.location.radius_miles == 12
    assert after.defaults.min_deal_score == before.defaults.min_deal_score
    assert after.defaults.max_results == before.defaults.max_results
    assert after.scorer.max_image_checks == before.scorer.max_image_checks


def _tokens(css: str, block_start: str) -> dict:
    import re
    block = css[css.index(block_start):]
    block = block[:block.index("}")]
    out = {}
    for name, hexa in re.findall(r"--([\w-]+):\s*#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b",
                                 block):
        out[name] = "#" + (hexa if len(hexa) == 6 else "".join(c * 2 for c in hexa))
    return out


def _contrast(a: str, b: str) -> float:
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
             for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_the_grey_text_is_readable_on_every_surface_in_both_themes():
    """`--faint` was 4.42:1 on `--line-soft` and 4.47:1 on `--bad-soft`, just
    under 4.5: the zeros in a failed run and the score on a low card. Measured
    from the stylesheet, so a later token edit cannot slip back under."""
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1]
           / "curbside/web/static/app.css").read_text()
    for start in (":root{", ":root[data-theme=dark]{"):
        t = _tokens(css, start)
        for fg in ("dim", "faint"):
            for bg in ("bg", "card", "line-soft", "good-soft", "warn-soft",
                       "bad-soft", "accent-soft"):
                ratio = _contrast(t[fg], t[bg])
                assert ratio >= 4.5, f"{start} --{fg} on --{bg}: {ratio:.2f}"


def test_runs_all_pages_by_fifty_and_filters_on_the_page(tmp_path):
    """200 runs as stacked cards were 42,719px on a phone. Fifty a page, a
    compact line each on a phone, and the hunt filter on the page itself."""
    from curbside.db import Store
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    hunt = cfg.hunts[0]
    for _ in range(60):
        s.finish_run(s.start_run(hunt, "facebook"), n_fetched=3)
    page = client.get("/runs/all").text
    assert page.count('<li class="">') == 50
    assert "Older runs" in page and "before=" in page
    older = client.get("/runs/all?before=11").text
    assert older.count('<li class="">') == 10 and "Older runs" not in older
    for h in cfg.hunts:
        assert f'href="/runs/all?hunt={h.id}"' in page


def test_runs_shows_five_judged_and_folds_the_rest(tmp_path):
    """Twelve rows were the largest block on /runs, bigger than the passes."""
    client, cfg = _client(tmp_path)
    for n in range(8):
        _judged(cfg, f"facebook:{n}", f"Thing {n}", "want:bookshelf",
                "facebook", 5.0)
    panel = _judging_panel(client)
    top, _, folded = panel.partition("<details")
    assert top.count('class="judgedrow"') == 5
    assert "Show all 8" in folded and folded.count('class="judgedrow"') == 3


def test_the_card_says_the_last_pass_as_an_age_like_the_pill(tmp_path):
    from curbside.db import Store
    client, cfg = _client(tmp_path)
    s = Store(cfg.db_path)
    s.finish_run(s.start_run(cfg.hunts[0], "facebook"))
    page = client.get("/runs").text
    assert "Last pass just now" in page


# --- a hunt's page ------------------------------------------------------------

def _hunt_fixture(cfg):
    """One want hunt holding a listing in each state worth telling apart."""
    from datetime import datetime, timezone
    from curbside.db import Store
    from curbside.models import Listing, Score
    s = Store(cfg.db_path)
    hunt = next(h for h in cfg.hunts if h.kind == "want")
    def put(lid, title, status, score=None, reason=None):
        l = Listing(id=lid, source="facebook", source_id=lid.split(":")[1],
                    title=title, description="d", price_cents=5000,
                    currency="USD", url="u", images=("a.jpg",))
        s.upsert_listing(l)
        s.mark_matches(hunt.id, [l])
        if score is not None:
            s.save_score(Score(listing_id=lid, hunt_id=hunt.id, model="m",
                               scored_at=datetime.now(timezone.utc),
                               match="yes", deal_score=score,
                               est_value_cents=None, condition=None,
                               matched_want=None, worth_grabbing=False,
                               unknowns=(), requirements=(), red_flags=(),
                               reasoning="r"), priced_at_cents=5000)
        if reason:
            s.record_rejections(hunt.id, [(lid, reason)])
        elif status != "new":
            s.set_status(hunt.id, lid, status)
    put("facebook:1", "A picked one", "wanted", 8.0)
    put("facebook:2", "A judged one", "scored", 4.0)
    put("facebook:3", "A good one you dismissed", "dismissed", 9.0)
    put("facebook:4", "A poor one you dismissed", "dismissed", 2.0)
    put("facebook:5", "Long gone", "gone", 6.0)
    put("facebook:6", "Too dear", "filtered", reason="over_price")
    put("facebook:7", "Seen twice", "filtered", reason="duplicate_of:craigslist:aaa")
    put("facebook:8", "Seen twice again", "filtered", reason="duplicate_of:craigslist:bbb")
    put("facebook:9", "Far away", "filtered", reason="too_far_by_city")
    s.conn.commit()
    return hunt


def _cards(page):
    import re
    return re.findall(r'data-title="([^"]+)"', page)


def test_a_hunt_opens_on_what_it_judged_not_on_everything(tmp_path):
    """It opened on every listing the hunt ever matched -- mostly gone or
    already dismissed -- under the database's words for each state."""
    client, cfg = _client(tmp_path)
    hunt = _hunt_fixture(cfg)
    page = client.get(f"/hunt/{hunt.id}").text
    assert set(_cards(page)) == {"A picked one", "A judged one"}
    for word in ("Judged", "Picked", "Dismissed", "Rejected", "Gone"):
        assert f">{word}\n" in page or f">{word} " in page, word
    for raw in ("free_find<", ">filtered<", ">scored<"):
        assert raw not in page
    assert "Waiting" not in page, "an empty view has no chip"
    assert "4 turned away before judging" in page
    # ...and the figure is the Rejected view's own count, not every row that
    # ever carried a reason.
    assert "Rejected\n    <span class=\"num\">4</span>" in page


def test_each_card_offers_only_what_its_state_allows(tmp_path):
    """Every card offered Save and Dismiss, so a listing you had dismissed
    offered Dismiss again. Undecided gets both; dismissed gets Put back, to
    wherever its score earns; gone gets nothing."""
    import re
    client, cfg = _client(tmp_path)
    hunt = _hunt_fixture(cfg)

    def buttons(view, title):
        page = client.get(f"/hunt/{hunt.id}?view={view}").text
        card = page[page.index(f'data-title="{title}"'):]
        card = card[:card.index('<article') if '<article' in card else None]
        card = card[:card.index('</article>')]
        return (re.findall(r'name="status" value="(\w+)"', card),
                "Put back" in card)

    assert buttons("judged", "A picked one") == (["saved", "dismissed"], False)
    # 9.0 on a want clears the bar, so it goes back to the wants list...
    assert buttons("dismissed", "A good one you dismissed") == (["wanted"], True)
    # ...and 2.0 goes back to Skipped, which is where it came from.
    assert buttons("dismissed", "A poor one you dismissed") == (["scored"], True)
    assert buttons("gone", "Long gone") == ([], False)


def test_rejections_are_grouped_named_and_each_shows_its_own(tmp_path):
    """Every duplicate was its own `duplicate_of:<id>` tag, and every tag
    linked to all the rejections rather than its own."""
    client, cfg = _client(tmp_path)
    hunt = _hunt_fixture(cfg)
    page = client.get(f"/hunt/{hunt.id}?view=rejected").text
    chips = page.split("Why they were rejected")[1].split("</nav>")[0]
    assert chips.count("Duplicate") == 1, "one chip for both duplicates"
    assert "Over your price" in chips and "Too far" in chips
    assert "duplicate_of:" not in chips and "too_far_by_city" not in chips
    dup = client.get(f"/hunt/{hunt.id}?reason=duplicate").text
    assert set(_cards(dup)) == {"Seen twice", "Seen twice again"}
    far = client.get(f"/hunt/{hunt.id}?reason=too_far").text
    assert set(_cards(far)) == {"Far away"}
    # The old `?status=` address is retired. It answers with the default view
    # rather than an error, and never with the database's word for a state.
    old = client.get(f"/hunt/{hunt.id}?status=gone")
    assert old.status_code == 200
    assert set(_cards(old.text)) == {"A picked one", "A judged one"}


def test_every_page_colour_is_a_colour_not_a_grey():
    """The active tab takes its page's `--tint`, and the inactive tabs are
    grey. Skipped's tint was slate (#4a5568, saturation 0.17), so on /skipped
    its tab read as one more inactive icon. A tint has to be a hue, and it has
    to be readable as the active tab's label on the tab bar."""
    import colorsys
    import re
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1]
           / "curbside/web/static/app.css").read_text()
    tints = re.findall(r"body\[data-page=(\w+)\]\s*\{--tint:(#[0-9a-fA-F]{6})", css)
    assert {p for p, _ in tints} >= {"free", "saved", "skipped", "runs",
                                     "settings", "wants"}
    for page, colour in tints:
        r, g, b = (int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
        _, _, sat = colorsys.rgb_to_hls(r, g, b)
        assert sat >= 0.35, f"{page} tint {colour} is a grey (s={sat:.2f})"
    light = _tokens(css, ":root{")
    for page, colour in tints[:7]:
        assert _contrast(colour, light["card"]) >= 4.5 or \
            _contrast(colour, "#17181b") >= 4.5, page


def test_no_page_colour_borrows_a_state_colour():
    """Settings' tint was #8a5300, which is --warn, so the page's own figures
    ("3 wants", "Noon-8pm awake") read as cautions. Amber means we do not
    know and red counts against it; a page hue says only where you are, so it
    may be neither, in either theme. Green is left out on purpose: Free is
    green because free is itself the green state, and its dark tint is --good."""
    import re
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1]
           / "curbside/web/static/app.css").read_text()
    state = {c.lower() for c in re.findall(
        r"--(?:warn|bad)(?:-soft)?:\s*(#[0-9a-fA-F]{6})", css)}
    tints = re.findall(
        r"body\[data-page=(\w+)\]\s*\{--tint:(#[0-9a-fA-F]{6});--tint-soft:(#[0-9a-fA-F]{6})",
        css)
    assert tints
    for page, tint, soft in tints:
        assert tint.lower() not in state, f"{page} tint {tint} is a state colour"
        assert soft.lower() not in state, f"{page} wash {soft} is a state colour"


def test_settings_figures_match_the_pages_they_point_at():
    """Settings' head figures are coloured by where each thing lives: the want
    count in Wants' hue, the hours in Runs'. CSS cannot read another page's
    tint, so they are copies, and this keeps each copy equal to its page in
    every theme block."""
    import re
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1]
           / "curbside/web/static/app.css").read_text()
    blocks = re.findall(r"^(.*?)body\[data-page=(\w+)\]\s*\{([^}]*)\}", css, re.M)
    by_theme = {}
    for prefix, page, body in blocks:
        props = dict(re.findall(r"--([\w-]+):(#[0-9a-fA-F]{6})", body))
        by_theme.setdefault(prefix.strip(), {})[page] = props
    assert len(by_theme) == 3
    for theme, pages in by_theme.items():
        s = pages["settings"]
        assert s["fig-wants"] == pages["wants"]["tint"], theme
        assert s["fig-hours"] == pages["runs"]["tint"], theme
