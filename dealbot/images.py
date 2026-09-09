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

from .models import Listing

MAX_IMAGES_PER_LISTING = 3


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

    Everything here is defensive because the URLs come from listings written by
    strangers: a size cap, a content-type check, a timeout, and re-encoding
    through Pillow so whatever lands on disk is an image we produced rather than
    bytes we were handed.
    """
    name = "http"

    MAX_BYTES = 8 * 1024 * 1024
    MAX_EDGE = 512
    TIMEOUT = 20

    def __init__(self, max_edge: int = MAX_EDGE):
        self.max_edge = max_edge

    def fetch(self, listing: Listing, dest: Path,
              limit: int = MAX_IMAGES_PER_LISTING) -> list[Path]:
        import io

        import requests
        from PIL import Image

        dest.mkdir(parents=True, exist_ok=True)
        out: list[Path] = []
        for i, url in enumerate(listing.images[:limit]):
            try:
                with requests.get(url, timeout=self.TIMEOUT, stream=True) as r:
                    if r.status_code != 200:
                        continue
                    if not r.headers.get("content-type", "").startswith("image/"):
                        continue
                    blob = r.raw.read(self.MAX_BYTES + 1, decode_content=True)
                if len(blob) > self.MAX_BYTES:
                    continue
                img = Image.open(io.BytesIO(blob))
                img.thumbnail((self.max_edge, self.max_edge))
                target = dest / f"photo{i}.png"
                img.convert("RGB").save(target, "PNG")
                out.append(target)
            except Exception:                             # noqa: BLE001
                continue          # a bad photo must never fail the run
        return out
