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
