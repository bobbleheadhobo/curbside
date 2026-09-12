"""Core data types.

Frozen dataclasses throughout, so every pipeline stage is a pure function of its
inputs and can be tested with no database and no network. Prices are integer
cents everywhere; `None` means the source showed no price at all, which is
different from 0 (free).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def normalize_title(title: str) -> str:
    """Lowercase, punctuation to SPACE, whitespace collapsed.

    Deleting punctuation instead of replacing it made "Mid-Century" normalise to
    "midcentury" while "Mid Century" stayed "mid century" -- so the identical
    item cross-posted with a hyphen looked like a different thing."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", title.lower())).strip()


# --- configuration-shaped types ---------------------------------------------

@dataclass(frozen=True)
class Want:
    """Something Joey actually wants. `queries` cast the net for a targeted
    search; `description` is what the model judges a listing against. Both are
    needed: keywords can't know "media console" means "tv stand", and a
    description can't be typed into a search box."""
    name: str
    description: str
    max_price_cents: int
    queries: tuple[str, ...] = ()
    # Hard pass/fail conditions, kept OUT of the prose. The model reports on each
    # one individually with its evidence, so a near-miss is visible rather than
    # blended into a single number -- and an unverifiable one becomes an explicit
    # `unknown` instead of quietly scoring 4.5 and never being seen.
    requires: tuple[str, ...] = ()


WANT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def slugify_want(text: str) -> str:
    """"TV Stand" -> "tv-stand". The name is structural, not decoration: it
    becomes the hunt id (`want:tv-stand`), the URL of that hunt's view, and the
    key everything already scored is filed under. So it is derived once, on
    creation, and never edited afterwards -- renaming would orphan every match
    and score the old name owns."""
    out = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return out[:40].strip("-")


@dataclass(frozen=True)
class StoredWant:
    """A want as the database holds it: the want itself plus its provenance.

    `archived_at` is a soft delete -- the hunt stops running, everything it ever
    matched stays readable, and the name stays taken so re-seeding cannot bring
    a deleted want back."""
    want: Want
    origin: str = "web"              # "config" when seeded from config.yaml
    archived_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def archived(self) -> bool:
        return self.archived_at is not None

    @property
    def hunt_id(self) -> str:
        return f"want:{self.want.name}"


@dataclass(frozen=True)
class Hunt:
    """One scheduled search. A sweep carries *every* want, so a free listing can
    match any of them; a want-hunt carries only its own."""
    id: str
    name: str
    kind: str                        # "sweep" | "want"
    queries: tuple[str, ...]
    max_price_cents: int | None
    exclude: tuple[str, ...]
    wants: tuple[Want, ...]
    min_deal_score: float
    # A separate, lower bar for the free-finds bin. The two bins answer different
    # questions: "is this the TV stand I want" has to clear a high bar because
    # you will drive across town for it, whereas "is this free thing worth a
    # look" is browsing. Holding both to 7.0 meant a free working treadmill
    # scored 5 and was never shown, which is the entire point of the bin.
    free_find_min_score: float
    interval_minutes: int
    max_results: int
    # Judge nothing older than this. Free things evaporate -- a couch posted a
    # fortnight ago is gone, and paying to appraise it is pure waste. Priced
    # things sit, and age there is a BUY signal (see the motivated-seller flag),
    # so want hunts default to no limit at all. 0 disables.
    max_age_days: int = 0
    enabled: bool = True


@dataclass(frozen=True)
class Location:
    lat: float
    lng: float
    radius_miles: float


# --- pipeline types ----------------------------------------------------------

@dataclass(frozen=True)
class RawListing:
    """Exactly what a source yielded, untouched. Kept so a parser fix can be
    replayed against old data instead of needing a re-fetch."""
    source: str
    source_id: str
    payload: dict[str, Any]
    fetched_at: datetime


@dataclass(frozen=True)
class Listing:
    id: str                          # f"{source}:{source_id}"
    source: str
    source_id: str
    title: str
    description: str | None
    price_cents: int | None          # 0 = free; None = no price shown
    currency: str
    url: str
    # What the seller was asking BEFORE. Facebook hands this over as
    # `strikethrough_price` on 40 of 144 listings and we were discarding it --
    # including "$500 -> free" and "$100 -> free", which is the strongest buy
    # signal in the whole dataset.
    previous_price_cents: int | None = None
    city: str | None = None
    lat: float | None = None
    lng: float | None = None
    distance_mi: float | None = None
    seller_id: str | None = None
    seller_name: str | None = None
    images: tuple[str, ...] = ()
    category: str | None = None
    posted_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def dup_key(self) -> str | None:
        """Identifies the same physical item CROSS-SOURCE, for people who post
        the same thing to Facebook and Craigslist.

        Normalised title + exact price + coordinates rounded to ~1km. Returns
        None when a match could not be trusted -- no coordinates, or a title too
        short and generic to discriminate ("Free", "Gone", "credenza"). Failing
        to dedupe costs one duplicate card and one wasted appraisal; merging two
        different couches loses a listing silently, which is worse.
        """
        if self.lat is None or self.lng is None:
            return None
        norm = normalize_title(self.title)
        if len(norm) < 8:
            return None
        key = f"{norm}|{self.price_cents}|{round(self.lat, 2)}|{round(self.lng, 2)}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    @property
    def fingerprint(self) -> str:
        """Identifies the same *item* across relists, which get fresh ids.

        Seller + normalized title + a coarse price bucket. The bucket is coarse
        on purpose: sellers routinely shave the price when they repost, and an
        exact-price key would miss exactly the relists worth catching.

        **Without a seller there is no fingerprint.** Neither Facebook's nor
        Craigslist's search feed exposes one, and title+price alone collapses
        genuinely different listings together -- four separate "Curb alert"
        posts hashed identically and would each have been reported as a relist
        of the others. So when the seller is unknown the key falls back to this
        listing's own id, which makes relist detection inert for that source
        rather than confidently wrong. Sources that do supply a seller get the
        real behaviour."""
        if not self.seller_id:
            return hashlib.sha256(
                f"{self.source}|{self.source_id}".encode()).hexdigest()[:16]
        norm = normalize_title(self.title)
        bucket = "na" if self.price_cents is None else str(self.price_cents // 2500)
        key = f"{self.source}|{self.seller_id}|{norm}|{bucket}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class UpsertResult:
    listing_id: str
    is_new: bool
    price_changed: bool
    previous_price_cents: int | None
    is_relist: bool
    # When we FIRST saw it, ISO-8601. The store knows this and the parsed
    # `Listing` cannot: it comes off a search feed, not out of the database.
    # The batch cap needs it -- Craigslist's feed carries no posting date, so
    # this is the only date it has to order by.
    first_seen: str | None = None


@dataclass(frozen=True)
class Score:
    listing_id: str
    hunt_id: str
    model: str
    scored_at: datetime
    # Three-way, not a boolean. "unknown" means the listing plausibly matches but
    # something could not be verified from the text -- the single most common
    # real case, since sellers rarely state dimensions. Collapsing it into a
    # mid-range score is how genuine candidates silently disappear.
    match: str                       # "yes" | "no" | "unknown"
    deal_score: float                # 0-10, scored as if the unknowns resolve well
    est_value_cents: int | None
    condition: str | None            # new|like_new|good|fair|parts
    matched_want: str | None
    # Independent of `match`: a working treadmill given away free is worth
    # collecting even though it matches nothing on the wants list. Without this
    # axis the sweep can only ever find things you already thought to ask for.
    worth_grabbing: bool
    unknowns: tuple[str, ...]        # what a human needs to check
    requirements: tuple[dict, ...]   # [{req, met, evidence}] -- auditable
    red_flags: tuple[str, ...]
    reasoning: str
    # Set by the model when looking at the photos would actually settle an
    # unknown. Deliberately its own decision: colour and shape are visible,
    # absolute width is not, so the model opts in only where it would help.
    needs_images: bool = False
    image_question: str | None = None
    images_checked: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class Candidate:
    """Survived the gate and is headed for the model."""
    listing: Listing
    reason: str                      # "new" | "price_drop" | "relist"


@dataclass(frozen=True)
class GateResult:
    candidates: list[Candidate]
    rejected: list[tuple[str, str]]  # (listing_id, reason) — recorded, not dropped


@dataclass
class RunResult:
    run_id: int
    hunt_id: str
    n_fetched: int = 0
    n_new: int = 0
    n_candidates: int = 0
    n_scored: int = 0
    n_surfaced: int = 0
    n_wanted: int = 0
    n_free_find: int = 0
    n_image_checks: int = 0
    n_deferred: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    # Degraded but not failed: see the `warning` column on `runs`.
    warning: str | None = None
