"""Thumbnail cache. No network: the fetch is replaced with a real PNG on disk."""
from pathlib import Path

import pytest

from dealbot.models import Listing
from dealbot.thumbs import ThumbnailStore

FIXTURE_PNG = Path(__file__).resolve().parents[1] / "fixtures/images/3101-0.png"


def listing(lid="fb:1", images=("https://example/a.jpg",)):
    return Listing(id=lid, source="facebook", source_id="1", title="t",
                   description=None, price_cents=0, currency="USD", url="u",
                   images=images)


@pytest.fixture
def fake_get(monkeypatch):
    blob = FIXTURE_PNG.read_bytes()

    class Resp:
        status_code = 200
        headers = {"content-type": "image/png"}
        raw = type("R", (), {"read": staticmethod(lambda *a, **k: blob)})()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("dealbot.thumbs.requests.get", lambda *a, **k: Resp())


def test_a_thumbnail_is_downscaled_and_stored(tmp_path, fake_get):
    t = ThumbnailStore(tmp_path)
    path = t.store(listing())
    assert path and path.exists() and path.suffix == ".jpg"
    assert path.stat().st_size < 200_000
    assert t.has("fb:1")


def test_storing_twice_does_not_refetch(tmp_path, fake_get):
    t = ThumbnailStore(tmp_path)
    first = t.store(listing())
    mtime = first.stat().st_mtime_ns
    assert t.store(listing()).stat().st_mtime_ns == mtime


def test_a_listing_with_no_photo_is_not_an_error(tmp_path, fake_get):
    assert ThumbnailStore(tmp_path).store(listing(images=())) is None


def test_an_unreachable_image_never_breaks_a_run(tmp_path, monkeypatch):
    """A listing whose photo 404s is still a perfectly good listing."""
    def boom(*a, **k):
        raise OSError("connection reset")
    monkeypatch.setattr("dealbot.thumbs.requests.get", boom)
    assert ThumbnailStore(tmp_path).store(listing()) is None


def test_ids_with_awkward_characters_are_safe_filenames(tmp_path, fake_get):
    t = ThumbnailStore(tmp_path)
    p = t.store(listing("craigslist:27yEWYhRsxaJjXB4DJiTP6"))
    assert p and ":" not in p.name and p.exists()


def test_prune_keeps_only_what_still_matters(tmp_path, fake_get):
    t = ThumbnailStore(tmp_path)
    for i in range(3):
        t.store(listing(f"fb:{i}"))
    assert t.prune({"fb:1"}) == 2
    assert t.has("fb:1") and not t.has("fb:0")
