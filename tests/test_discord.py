"""Discord notifier. Nothing here touches the network -- `_post` is replaced."""
from datetime import datetime, timedelta, timezone

import pytest

from dealbot.db import Store
from dealbot.models import Hunt, Listing, Score
from dealbot.notify.discord import DiscordNotifier, build_embed

NOW = datetime.now(timezone.utc)


def make_listing(lid="fb:1", price=0, previous=None, days_old=1, images=("p.jpg",)):
    return Listing(id=lid, source="facebook", source_id=lid.split(":")[-1],
                   title="Free Organ with Bench", description="d",
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
    assert price == "~~$500~~ → **FREE**"


def test_the_photo_is_attached():
    assert build_embed(make_listing(), make_score(), "http://d")["image"]["url"] == "p.jpg"
    assert "image" not in build_embed(make_listing(images=()), make_score(), "http://d")


def test_an_unverified_match_says_what_to_check():
    e = build_embed(make_listing(),
                    make_score(match="unknown",
                               unknowns=("width not stated", "colour")),
                    "http://d")
    assert "Needs checking" in e["description"]
    assert "width not stated" in e["description"]


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

    n.notify(hunt, [])                      # nothing surfaced THIS run
    assert len(sent) == 2                   # cap respected
    n._sent_this_run = 0
    n.notify(hunt, [])
    assert len(sent) == 4                   # the rest are picked up next run
    n._sent_this_run = 0
    n.notify(hunt, [])
    assert len(sent) == 5
    n._sent_this_run = 0
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
