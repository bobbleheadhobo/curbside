"""Local thumbnail cache.

Facebook's image URLs carry an expiry token and die after about four days --
225 of 619 listings with photos are Facebook. For a Discord alert you act on
within hours that hardly matters; for a dashboard you *browse*, a gallery where
a third of the images rot is a bad gallery, and the proportion grows with
history.

So a copy is kept for listings that reach a bin. Only those: judging a listing
does not mean you will ever look at it, and 500 listings a day of full-size
photos is gigabytes a year for nothing.

Downscaled to 512px, which is also what the vision pass uses -- colour, shape
and condition all survive it, and it keeps a thumbnail around 30-60KB.
"""
from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path

import requests

from .models import Listing

log = logging.getLogger("dealbot.thumbs")

MAX_EDGE = 512
MAX_BYTES = 8 * 1024 * 1024
TIMEOUT = 20


def _key(listing_id: str) -> str:
    return hashlib.sha256(listing_id.encode()).hexdigest()[:20]


class ThumbnailStore:
    def __init__(self, root: str | Path = "data/thumbs"):
        self.root = Path(root)

    def path_for(self, listing_id: str) -> Path:
        return self.root / f"{_key(listing_id)}.jpg"

    def has(self, listing_id: str) -> bool:
        return self.path_for(listing_id).exists()

    def store(self, listing: Listing) -> Path | None:
        """Fetch, downscale and save the first photo. Never raises: a listing
        with an unreachable image is still a perfectly good listing."""
        if not listing.images:
            return None
        target = self.path_for(listing.id)
        if target.exists():
            return target
        try:
            from PIL import Image
            with requests.get(listing.images[0], timeout=TIMEOUT, stream=True) as r:
                if r.status_code != 200:
                    return None
                if not r.headers.get("content-type", "").startswith("image/"):
                    return None
                blob = r.raw.read(MAX_BYTES + 1, decode_content=True)
            if len(blob) > MAX_BYTES:
                return None
            img = Image.open(io.BytesIO(blob))
            img.thumbnail((MAX_EDGE, MAX_EDGE))
            target.parent.mkdir(parents=True, exist_ok=True)
            img.convert("RGB").save(target, "JPEG", quality=82, optimize=True)
            return target
        except Exception as exc:                              # noqa: BLE001
            log.debug("thumbnail failed for %s: %s", listing.id, exc)
            return None

    def prune(self, keep_ids: set[str]) -> int:
        """Drop thumbnails for listings that no longer matter. `keep_ids` is
        whatever is still in a bin or triaged."""
        keep = {_key(i) for i in keep_ids}
        removed = 0
        if not self.root.exists():
            return 0
        for f in self.root.glob("*.jpg"):
            if f.stem not in keep:
                f.unlink(missing_ok=True)
                removed += 1
        return removed

    def disk_usage_mb(self) -> float:
        if not self.root.exists():
            return 0.0
        return sum(f.stat().st_size for f in self.root.glob("*.jpg")) / 1024 / 1024
