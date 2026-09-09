"""Fingerprint behaviour -- relist detection depends entirely on it."""
from conftest import make_listing


def test_same_item_same_fingerprint_despite_new_id():
    a = make_listing("fixture:1", title="Walnut Media Console 75 inch")
    b = make_listing("fixture:99", title="walnut media console 75 inch!!")
    assert a.fingerprint == b.fingerprint


def test_different_seller_is_a_different_item():
    a = make_listing("fixture:1", seller_id="s1")
    b = make_listing("fixture:2", seller_id="s2")
    assert a.fingerprint != b.fingerprint


def test_price_bucket_is_coarse_so_a_repost_discount_still_matches():
    """Sellers routinely shave the price when reposting; an exact-price key
    would miss exactly the relists worth catching."""
    a = make_listing("fixture:1", price_cents=10000)
    b = make_listing("fixture:2", price_cents=10500)
    assert a.fingerprint == b.fingerprint


def test_a_real_price_move_is_a_different_bucket():
    a = make_listing("fixture:1", price_cents=10000)
    b = make_listing("fixture:2", price_cents=40000)
    assert a.fingerprint != b.fingerprint


def test_free_is_not_the_same_as_no_price():
    assert (make_listing(price_cents=0).fingerprint
            != make_listing(price_cents=None).fingerprint)


def test_without_a_seller_relist_detection_is_off_not_wrong():
    """REGRESSION: neither Facebook nor Craigslist exposes a seller in search
    results, so the key collapsed to title+price. Four distinct "Curb alert"
    posts hashed identically and each would have been reported as a relist of
    the others. Inert beats confidently wrong."""
    a = make_listing("fb:1", title="Curb alert", price_cents=0, seller_id=None)
    b = make_listing("fb:2", title="Curb alert", price_cents=0, seller_id=None)
    assert a.fingerprint != b.fingerprint


def test_a_known_seller_still_gets_real_relist_detection():
    a = make_listing("fb:1", title="Oak console", price_cents=10000, seller_id="s9")
    b = make_listing("fb:2", title="OAK CONSOLE!", price_cents=10500, seller_id="s9")
    assert a.fingerprint == b.fingerprint


def test_the_same_title_on_different_sources_is_not_one_item():
    a = make_listing("fb:1", source="facebook", seller_id="s1")
    b = make_listing("cl:1", source="craigslist", seller_id="s1")
    assert a.fingerprint != b.fingerprint


def test_the_same_item_on_two_sources_shares_a_dup_key():
    """People cross-post to Facebook and Craigslist. Without this you pay to
    appraise the same item twice and it appears twice in the bin."""
    a = make_listing("facebook:1", source="facebook",
                     title="Mid Century Walnut Credenza", price_cents=20000,
                     lat=35.08, lng=-106.65)
    b = make_listing("craigslist:9", source="craigslist",
                     title="Mid-Century Walnut Credenza!", price_cents=20000,
                     lat=35.081, lng=-106.652)
    assert a.dup_key is not None and a.dup_key == b.dup_key


def test_dup_key_refuses_to_guess():
    """Merging two different couches loses a listing silently, which is worse
    than showing one duplicate card."""
    base = dict(title="Mid Century Walnut Credenza", price_cents=20000,
                lat=35.08, lng=-106.65)
    a = make_listing("a:1", **base)
    assert a.dup_key != make_listing("a:2", **{**base, "price_cents": 9900}).dup_key
    assert a.dup_key != make_listing("a:3", **{**base, "lat": 35.4}).dup_key
    assert make_listing("a:4", **{**base, "lat": None}).dup_key is None
    assert make_listing("a:5", **{**base, "title": "Gone"}).dup_key is None
