"""Dashboard rendering of data we collect. All of this existed in the database
and none of it was visible."""
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
    for path in ("/", "/free", "/saved", "/near", "/runs"):
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

    near = client.get("/near").text
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


def test_the_free_page_can_pause_and_resume_sweeps(tmp_path):
    client, cfg = _client(tmp_path)
    from dealbot.db import Store
    s = Store(cfg.db_path)
    sweep = next(h for h in cfg.hunts if h.kind == "sweep")

    assert "pause free-stuff searches" in client.get("/free").text

    client.post("/hunts/toggle", data={"kind": "sweep", "enable": "0",
                                       "back": "/free"}, follow_redirects=False)
    assert sweep.id in s.disabled_hunts()
    assert not any(h.id in s.disabled_hunts() for h in cfg.hunts if h.kind == "want")

    page = client.get("/free").text
    assert "resume free-stuff searches" in page
    assert "Paused:" in page                       # banner
    assert "Paused:" in client.get("/").text       # ...on every page

    client.post("/hunts/toggle", data={"kind": "all", "enable": "1", "back": "/"},
                follow_redirects=False)
    assert s.disabled_hunts() == set()
