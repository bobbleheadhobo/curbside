"""Discord notifier. Nothing here touches the network -- `_post` is replaced."""
from datetime import datetime, timedelta, timezone

import pytest

from dealbot.db import Store
from dealbot.models import Hunt, Listing, Score
from dealbot.notify.discord import DiscordNotifier, build_embed

NOW = datetime.now(timezone.utc)


def make_listing(lid="fb:1", price=0, previous=None, days_old=1,
                 images=("p.jpg",), title="Free Organ with Bench"):
    return Listing(id=lid, source="facebook", source_id=lid.split(":")[-1],
                   title=title, description="d",
                   price_cents=price, previous_price_cents=previous,
                   currency="USD", url="https://example/x", distance_mi=9.0,
                   images=images, posted_at=NOW - timedelta(days=days_old))


def make_score(match="no", deal_score=7.0, **kw):
    base = dict(listing_id="fb:1", hunt_id="h", model="sonnet", scored_at=NOW,
                match=match, deal_score=deal_score, est_value_cents=50000,
                condition="good", matched_want=None, worth_grabbing=True,
                unknowns=(), requirements=(), red_flags=(), reasoning="because")
    base.update(kw)
    return Score(**base)


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / "t.db")
    hunt = Hunt(id="h", name="h", kind="sweep", queries=(), max_price_cents=0,
                exclude=(), wants=(), min_deal_score=7.0, free_find_min_score=5.0,
                interval_minutes=15, max_results=60)
    n = DiscordNotifier(store, wants_webhook="https://w", free_webhook="https://f",
                        dashboard_url="http://dash", mention_user_id="123",
                        pause_seconds=0)
    sent = []
    n._post = lambda hook, payload: (sent.append((hook, payload)), True)[1]
    return store, hunt, n, sent


def _register(store, hunt, listing):
    store.upsert_listing(listing)
    store.mark_matches(hunt.id, [listing])


# --- embed ------------------------------------------------------------------

def test_a_price_drop_renders_struck_through():
    e = build_embed(make_listing(price=0, previous=50000), make_score(), "http://d")
    price = next(f for f in e["fields"] if f["name"] == "Price")["value"]
    assert "~~$500~~" in price and "**FREE**" in price
    assert "-100%" in price


def test_the_photo_is_attached():
    assert build_embed(make_listing(), make_score(), "http://d")["image"]["url"] == "p.jpg"
    assert "image" not in build_embed(make_listing(images=()), make_score(), "http://d")


def test_an_unverified_match_says_what_to_check():
    e = build_embed(make_listing(),
                    make_score(match="unknown",
                               unknowns=("width not stated", "colour")),
                    "http://d")
    field = next(f for f in e["fields"] if f["name"] == "Worth checking")
    assert "width not stated" in field["value"]
    verdict = next(f for f in e["fields"] if f["name"] == "Verdict")
    assert "unverified" in verdict["value"]


def test_the_dashboard_link_is_included():
    e = build_embed(make_listing(), make_score(), "http://dash")
    assert "http://dash/listing/fb:1" in e["description"]


def test_a_motivated_seller_is_called_out():
    e = build_embed(make_listing(days_old=30, previous=50000), make_score(), "http://d")
    assert "motivated seller" in e["footer"]["text"]


# --- routing and restraint ---------------------------------------------------

def test_wants_and_free_go_to_different_channels(rig):
    store, hunt, n, sent = rig
    for lid, match in (("fb:1", "yes"), ("fb:2", "no")):
        l = make_listing(lid)
        _register(store, hunt, l)
        n.notify(hunt, [(l, make_score(match=match, listing_id=lid))])
    assert [hook for hook, _ in sent] == ["https://w", "https://f"]


def test_nothing_is_announced_twice(rig):
    store, hunt, n, sent = rig
    l = make_listing()
    _register(store, hunt, l)
    n.notify(hunt, [(l, make_score())])
    n.notify(hunt, [(l, make_score())])
    assert len(sent) == 1
    assert store.was_notified(hunt.id, l.id)


def test_the_backlog_is_caught_up_silently(rig):
    """Hundreds of listings are waiting. The first run after enabling must not
    announce them all -- most are long gone."""
    store, hunt, n, sent = rig
    old = make_listing("fb:old", days_old=30)
    _register(store, hunt, old)
    n.notify(hunt, [(old, make_score(listing_id="fb:old"))])
    assert sent == []                                   # not announced
    assert store.was_notified(hunt.id, old.id)          # but not re-checked either


def test_a_per_run_cap_applies(rig):
    store, hunt, n, sent = rig
    n.max_per_run = 2
    items = []
    for i in range(5):
        l = make_listing(f"fb:{i}")
        _register(store, hunt, l)
        items.append((l, make_score(listing_id=f"fb:{i}")))
    n.notify(hunt, items)
    assert len(sent) == 2


def test_the_muted_channel_only_pings_for_the_exceptional(rig):
    store, hunt, n, sent = rig
    for lid, score in (("fb:1", 6.0), ("fb:2", 9.0)):
        l = make_listing(lid)
        _register(store, hunt, l)
        n.notify(hunt, [(l, make_score(deal_score=score, listing_id=lid))])
    assert "content" not in sent[0][1]                  # routine: silent
    assert "<@123>" in sent[1][1]["content"]            # exceptional: pings


def test_a_missing_webhook_is_not_an_error(rig):
    store, hunt, n, sent = rig
    n.wants_webhook = None
    l = make_listing()
    _register(store, hunt, l)
    n.notify(hunt, [(l, make_score(match="yes"))])
    assert sent == []


def test_the_cap_defers_rather_than_drops(rig):
    """REGRESSION: the notifier only ever saw what the CURRENT run surfaced, so
    anything past the per-run cap was lost forever -- next run it is
    `unchanged` and never surfaces again. Two TV stands, the whole point of the
    thing, sat un-announced because of it."""
    store, hunt, n, sent = rig
    n.max_per_run = 2
    for i in range(5):
        l = make_listing(f"fb:{i}")
        _register(store, hunt, l)
        store.set_status(hunt.id, l.id, "free_find")
        store.save_score(make_score(listing_id=f"fb:{i}", deal_score=6.0 + i * 0.1),
                         priced_at_cents=0)

    # The counter is NOT reset by hand between these. It used to have to be:
    # `_sent_this_run` was set once in __init__, so the "per-run" cap was really
    # per process -- a `dealbot run` daemon builds one notifier and loops
    # forever, and went permanently silent after ten messages with nothing but
    # an INFO line to show for it.
    n.notify(hunt, [])                      # nothing surfaced THIS run
    assert len(sent) == 2                   # cap respected
    n.notify(hunt, [])
    assert len(sent) == 4                   # the rest are picked up next run
    n.notify(hunt, [])
    assert len(sent) == 5
    n.notify(hunt, [])
    assert len(sent) == 5                   # ...and never announced twice


def test_a_bin_entry_predating_notifications_still_gets_announced(rig):
    """The two TV stands entered the wants bin before Discord was switched on."""
    store, hunt, n, sent = rig
    l = make_listing("fb:old-find")
    _register(store, hunt, l)
    store.set_status(hunt.id, l.id, "wanted")
    store.save_score(make_score(listing_id="fb:old-find", match="yes"),
                     priced_at_cents=0)

    n.notify(hunt, [])                      # this run surfaced nothing new
    assert len(sent) == 1
    assert sent[0][0] == "https://w"        # and it went to the wants channel


def test_requirement_evidence_is_shown():
    """The most useful thing we know, and it was never in the alert: "at least
    70 inches wide -- 'six feet long' = 72 inches" makes it actionable without
    opening anything."""
    e = build_embed(make_listing(), make_score(match="yes", requirements=(
        {"req": "at least 70 inches wide", "met": "yes",
         "evidence": '"six feet long" = 72 inches'},
        {"req": "not a corner unit", "met": "unknown",
         "evidence": "shape not described"},
    )), "http://d")
    reqs = next(f for f in e["fields"] if f["name"] == "Requirements")["value"]
    assert "✅ at least 70 inches wide" in reqs
    assert "six feet long" in reqs
    assert "❓ not a corner unit" in reqs


def test_colour_and_badge_track_the_score():
    from dealbot.notify.discord import DIM, FAIR, GOOD, HOT
    assert build_embed(make_listing(), make_score(deal_score=9.5), "")["color"] == HOT
    assert build_embed(make_listing(), make_score(deal_score=7.5), "")["color"] == GOOD
    assert build_embed(make_listing(), make_score(deal_score=5.5), "")["color"] == FAIR
    assert build_embed(make_listing(), make_score(deal_score=2.0), "")["color"] == DIM
    assert build_embed(make_listing(), make_score(deal_score=9.5), "")["title"].startswith("🔥")
    assert build_embed(make_listing(), make_score(deal_score=8.0), "")["title"].startswith("⭐")
    assert not build_embed(make_listing(), make_score(deal_score=6.0), "")["title"].startswith(("🔥", "⭐"))


def test_a_malformed_requirement_does_not_break_the_embed():
    e = build_embed(make_listing(),
                    make_score(requirements=("junk", {"req": "ok", "met": "yes"})),
                    "http://d")
    assert any(f["name"] == "Requirements" for f in e["fields"])


def test_the_embed_stays_inside_discord_limits():
    long = make_score(reasoning="x" * 9000,
                      unknowns=tuple(f"u{i}" * 200 for i in range(20)),
                      red_flags=tuple(f"f{i}" * 200 for i in range(20)))
    e = build_embed(make_listing(title="t" * 400), long, "http://d")
    assert len(e["title"]) <= 256
    assert len(e["description"]) <= 4096
    assert all(len(f["value"]) <= 1024 for f in e["fields"])
    assert len(e["fields"]) <= 25


def _bin_with_score(store, lid, price, hunt="want:tv-stand", status="saved"):
    from datetime import datetime, timezone
    from dealbot.models import Score
    from conftest import make_listing
    l = make_listing(lid=lid, price_cents=price)
    store.upsert_listing(l)
    store.mark_matches(hunt, [l])
    store.save_score(Score(listing_id=l.id, hunt_id=hunt, model="m",
                           scored_at=datetime.now(timezone.utc), match="yes",
                           deal_score=8.0, est_value_cents=None, condition=None,
                           matched_want="tv-stand", worth_grabbing=False,
                           unknowns=(), requirements=(), red_flags=(),
                           reasoning="r"), priced_at_cents=price)
    store.set_status(hunt, l.id, status)
    return l


def test_a_saved_listing_getting_cheaper_is_announced_once(tmp_path):
    """What the append-only price history was for. Alerts only ever fired when
    a listing first reached a bin, so a saved $200 credenza falling to $120
    said nothing at all -- with every observation needed to spot it on disk."""
    from dealbot.db import Store
    from dealbot.pipeline import announce_price_drops
    from conftest import make_listing
    store = Store(tmp_path / "t.db")
    _bin_with_score(store, "x:1", 20000)

    sent = []
    class Fake:
        name = "fake"
        def notify_price_drop(self, hunt, listing, score, was):
            sent.append((listing.id, was, listing.price_cents))
            return True

    assert announce_price_drops(store, [Fake()]) == 0      # nothing moved yet

    store.upsert_listing(make_listing(lid="x:1", price_cents=12000))
    assert announce_price_drops(store, [Fake()]) == 1
    assert sent == [("x:1", 20000, 12000)]

    # ...and not again on the next run, for the same drop.
    assert announce_price_drops(store, [Fake()]) == 0

    # A further real drop re-arms it, measured from what you were last told.
    store.upsert_listing(make_listing(lid="x:1", price_cents=0))
    assert announce_price_drops(store, [Fake()]) == 1
    assert sent[-1] == ("x:1", 12000, 0)


def test_a_nudge_down_is_not_a_price_drop(tmp_path):
    """Sellers move prices constantly; 15% is the same bar the gate uses."""
    from dealbot.db import Store
    from dealbot.pipeline import announce_price_drops
    from conftest import make_listing
    store = Store(tmp_path / "t.db")
    _bin_with_score(store, "x:2", 10000)
    store.upsert_listing(make_listing(lid="x:2", price_cents=9500))
    class Fake:
        name = "fake"
        def notify_price_drop(self, *a): return True
    assert announce_price_drops(store, [Fake()]) == 0


def test_a_drop_is_stamped_even_when_no_webhook_takes_it(tmp_path):
    """Otherwise the same drop re-queues itself on every future run."""
    from dealbot.db import Store
    from dealbot.pipeline import announce_price_drops
    from conftest import make_listing
    store = Store(tmp_path / "t.db")
    _bin_with_score(store, "x:3", 20000)
    store.upsert_listing(make_listing(lid="x:3", price_cents=10000))
    class Silent:
        name = "silent"
        def notify_price_drop(self, *a): return False
    assert announce_price_drops(store, [Silent()]) == 0
    assert store.price_drops() == []          # stamped, not left queued


def test_something_confirmed_sold_is_not_announced(tmp_path):
    from dealbot.db import Store
    from dealbot.pipeline import announce_price_drops
    from conftest import make_listing
    store = Store(tmp_path / "t.db")
    _bin_with_score(store, "x:4", 20000)
    store.upsert_listing(make_listing(lid="x:4", price_cents=9000))
    store.mark_sold("x:4", "sold")
    class Fake:
        name = "fake"
        def notify_price_drop(self, *a): return True
    assert announce_price_drops(store, [Fake()]) == 0


def test_the_drop_message_leads_with_the_move(tmp_path):
    from dealbot.notify.discord import DiscordNotifier
    from dealbot.db import Store
    from conftest import make_listing
    from datetime import datetime, timezone
    from dealbot.models import Hunt, Score
    store = Store(tmp_path / "t.db")
    posted = {}
    n = DiscordNotifier(store, wants_webhook="https://example/hook",
                        dashboard_url="http://d")
    n._post = lambda hook, payload: posted.update(payload) or True
    listing = make_listing(lid="x:5", price_cents=12000)
    score = Score(listing_id="x:5", hunt_id="h", model="m",
                  scored_at=datetime.now(timezone.utc), match="yes",
                  deal_score=8.0, est_value_cents=None, condition=None,
                  matched_want="tv-stand", worth_grabbing=False, unknowns=(),
                  requirements=(), red_flags=(), reasoning="r")
    hunt = Hunt(id="h", name="h", kind="want", queries=(), max_price_cents=None,
                exclude=(), wants=(), min_deal_score=7.0, free_find_min_score=5.0,
                interval_minutes=60, max_results=5)
    assert n.notify_price_drop(hunt, listing, score, 20000) is True
    assert "$200" in posted["content"] and "$120" in posted["content"]
    assert "40%" in posted["embeds"][0]["title"]
