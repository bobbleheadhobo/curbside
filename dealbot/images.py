"""Getting listing photos in front of the model, safely.

**We fetch, the model never does.** Images are downloaded (or, for fixtures,
copied) by our code into a scratch directory, and the model is given read access
to that directory and nothing else. No attacker-controlled URL ever becomes a
model capability.

Downscaling is the cost lever: a 1024x768 image is ~1,200 tokens (~$0.006),
a 512px one ~260 (~$0.0013). Colour, shape and visible damage all survive 512px
comfortably, and those are the only questions a photo can settle anyway.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol

import requests

from .models import Listing

MAX_IMAGES_PER_LISTING = 3

# Shared by the vision pass and the dashboard's thumbnail cache. Both download
# a stranger's URL and downscale it to 512px, and both used to carry their own
# copy of the caps below plus their own copy of the fetch. The hardening is the
# security-relevant part, so it gets one implementation rather than two kept in
# step by hand.
MAX_BYTES = 8 * 1024 * 1024
MAX_EDGE = 512
TIMEOUT = 20


def fetch_downscaled(url: str, *, max_edge: int = MAX_EDGE,
                     timeout: float = TIMEOUT, max_bytes: int = MAX_BYTES):
    """Download one image and return it downscaled, or None if unusable.

    Defensive throughout, because the URL came from a listing written by a
    stranger: a timeout, a content-type check, a byte cap enforced while
    reading rather than after, and re-encoding through Pillow so whatever a
    caller writes to disk is an image WE produced rather than bytes we were
    handed.

    Returns None for the ordinary "nothing usable here" cases -- non-200, wrong
    content-type, over the cap. Anything genuinely exceptional propagates, so
    each caller keeps its own policy: the vision pass skips the photo, the
    thumbnail cache logs and moves on.
    """
    import io

    from PIL import Image

    with requests.get(url, timeout=timeout, stream=True) as r:
        if r.status_code != 200:
            return None
        if not r.headers.get("content-type", "").startswith("image/"):
            return None
        blob = r.raw.read(max_bytes + 1, decode_content=True)
    if len(blob) > max_bytes:
        return None
    img = Image.open(io.BytesIO(blob))
    img.thumbnail((max_edge, max_edge))
    return img


class ImageProvider(Protocol):
    name: str

    def fetch(self, listing: Listing, dest: Path,
              limit: int = MAX_IMAGES_PER_LISTING) -> list[Path]: ...


class FixtureImageProvider:
    """Serves images from disk, keyed by the listing's source id, so the whole
    vision path is exercisable with no network and known ground truth."""
    name = "fixture"

    def __init__(self, root: str | Path = "fixtures/images"):
        self.root = Path(root)

    def fetch(self, listing: Listing, dest: Path,
              limit: int = MAX_IMAGES_PER_LISTING) -> list[Path]:
        dest.mkdir(parents=True, exist_ok=True)
        out: list[Path] = []
        for i in range(limit):
            src = self.root / f"{listing.source_id}-{i}.png"
            if not src.exists():
                break
            target = dest / src.name
            shutil.copyfile(src, target)
            out.append(target)
        return out


class HttpImageProvider:
    """Downloads listing photos, downscales them, writes them to a scratch dir.

    Downscaling is the whole cost lever: a 1024x768 image is ~1,200 tokens
    (~$0.006), a 512px one ~260 (~$0.0013). Colour, shape, tier count and visible
    damage all survive 512px, and those are the only questions a photograph can
    settle anyway.

    The fetching and the hardening are `fetch_downscaled`'s; this adds only
    where the result is written.
    """
    name = "http"

    def __init__(self, max_edge: int = MAX_EDGE):
        self.max_edge = max_edge

    def fetch(self, listing: Listing, dest: Path,
              limit: int = MAX_IMAGES_PER_LISTING) -> list[Path]:
        dest.mkdir(parents=True, exist_ok=True)
        out: list[Path] = []
        for i, url in enumerate(listing.images[:limit]):
            try:
                img = fetch_downscaled(url, max_edge=self.max_edge)
                if img is None:
                    continue
                target = dest / f"photo{i}.png"
                img.convert("RGB").save(target, "PNG")
                out.append(target)
            except Exception:                             # noqa: BLE001
                continue          # a bad photo must never fail the run
        return out
