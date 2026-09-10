# deal_bot — features and flow

Companion to DESIGN.md. This is *what it does* and *how control moves through it*.

## Part 1 — Features

### v1 (the thing is not useful without these)

| # | Feature | Notes |
|---|---|---|
| F1 | Hunts defined in YAML | saved search + plain-language criteria |
| F2 | Fetch per hunt from a source | adapter owns its own rate limiting |
| F3 | Persist everything, dedupe by listing id | including listings we filter out |
| F4 | Deterministic gates before any LLM call | price, radius, keywords, seen-before, blocked sellers |
| F5 | LLM scoring, structured output | only on gate survivors |
| F6 | Dashboard feed | scored listings by hunt, score desc |
| F7 | Triage: save / dismiss | per hunt, persists. `contacted` still exists as a status and still counts as triaged, but the UI stopped offering it |
| F8 | Run health | why was today quiet — broken or genuinely nothing |

### v1.5 (cheap to add once the above works, high value)

| # | Feature | Notes |
|---|---|---|
| F9 | **Price-drop detection** | append-only observations make this nearly free |
| F10 | **Relist detection** | same seller + fuzzy title = reposted item, new id |
| F11 | **Stale/motivated flags** | listed 21+ days, or two price drops = seller wants out |
| F12 | **Dismissal learning** | dismissed listings become negative examples in that hunt's prompt |
| F13 | Re-score on material change | price drop ≥15% re-opens a listing we already judged |

### v2

ntfy push · comparables drawn from our own price history · second source adapter ·
auto-populated seller blocklist from repeat dismissals · scheduled runs (systemd timer)

### Deliberately out of scope

Messaging sellers, auto-offers, posting, anything that writes to Facebook. Read-only.
That boundary is what keeps this a personal tool rather than something that gets an
account banned and deserves it.

### The three features that make this better than the GitHub alternatives

**F9/F11 — motivated sellers.** Every other project treats a listing as a static event:
saw it, scored it, done. Because we keep append-only price observations, we see the
*trajectory*. A $200 item that has been sitting 24 days and dropped twice is a better
deal than a $150 item posted an hour ago, and no keyword filter can express that.

**F12 — dismissal learning.** Every dismissal is a labeled negative example. Feed the
last N dismissed titles for a hunt back into that hunt's scoring prompt and the model
stops re-surfacing the same category of junk. No fine-tuning, no embeddings, ~20 lines.
The hunt gets sharper the more you use it.

**F10 — relist detection.** Marketplace is full of reposts. Without a fingerprint they
look like fresh listings forever and you keep re-scoring and re-surfacing the same sofa.
`fingerprint = hash(seller_id, normalized_title, price_bucket)` computed at parse time.

## Part 2 — Types

```python
@dataclass(frozen=True)
class RawListing:                  # what a source yields, untouched
    source: str
    source_id: str
    payload: dict
    fetched_at: datetime

@dataclass(frozen=True)
class Listing:                     # normalized, what the store holds
    id: str                        # f"{source}:{source_id}"
    source: str; source_id: str
    title: str
    description: str | None
    price_cents: int | None        # 0 = free; None = no price shown
    currency: str
    url: str
    city: str | None; lat: float | None; lng: float | None; distance_mi: float | None
    seller_id: str | None; seller_name: str | None
    images: tuple[str, ...]
    category: str | None
    posted_at: datetime | None
    fingerprint: str               # for relist detection
    raw: dict

@dataclass(frozen=True)
class UpsertResult:
    listing_id: str
    is_new: bool
    price_changed: bool
    previous_price_cents: int | None
    is_relist: bool

@dataclass(frozen=True)
class Candidate:                   # survived the gate, headed for the model
    listing: Listing
    reason: str                    # "new" | "price_drop" | "relist"
    previous_score: Score | None

@dataclass(frozen=True)
class GateResult:
    candidates: list[Candidate]
    rejected: list[tuple[str, str]]   # (listing_id, reason) — recorded, not discarded

@dataclass(frozen=True)
class Score:
    listing_id: str; hunt_id: str
    model: str; scored_at: datetime
    is_relevant: bool
    deal_score: float              # 0-10
    est_value_cents: int | None
    condition: str | None          # new|like_new|good|fair|parts
    red_flags: tuple[str, ...]
    reasoning: str
    input_tokens: int; output_tokens: int; cost_usd: float
```

## Part 3 — Interfaces

```python
class Source(Protocol):
    name: str
    def search(self, hunt: Hunt) -> Iterator[RawListing]: ...
    def parse(self, raw: RawListing) -> Listing | None: ...   # source-specific; None = unusable

class Scorer(Protocol):
    def score(self, hunt: Hunt, candidates: list[Candidate]) -> list[Score]: ...

class Notifier(Protocol):
    def notify(self, hunt: Hunt, surfaced: list[tuple[Listing, Score]]) -> None: ...
```

`parse` lives on the source because parsing is inherently source-specific, but it returns
the shared `Listing` type and `normalize.validate()` checks it before anything is written.
A broken adapter yields garbage that gets rejected and logged — it can't corrupt the store.

`DashboardNotifier` is a near-no-op that just flips status to `surfaced`; the dashboard
reads the DB directly. It exists so `NtfyNotifier` later is a drop-in, not a refactor.

## Part 4 — The main flow

`dealbot once --hunt power-tools`

```
cli.once()
 ├─ cfg   = config.load("config.yaml")
 ├─ store = db.connect(cfg.db_path)
 ├─ src   = sources.get(hunt.source)
 └─ pipeline.run_hunt(store, hunt, src, scorer, notifiers) -> RunResult
```

```
run_hunt(store, hunt, source, scorer, notifiers):

  1. run_id = store.start_run(hunt, source)

  2. FETCH        raw = list(source.search(hunt))
                  ── on exception: store.finish_run(run_id, error=...) and RETURN.
                     A dead scraper must fail loudly in the runs table, never silently
                     look like a quiet day.

  3. PARSE        listings = [l for r in raw if (l := source.parse(r)) and validate(l)]

  4. STORE        for each listing:
                      up = store.upsert_listing(listing)   -> UpsertResult
                      store.record_price(listing.id, listing.price_cents)   # always
                  store.mark_matches(hunt.id, listings)    # hunt_matches rows, status=new

  5. GATE         gr = filters.gate(hunt, listings, store) -> GateResult
                  store.record_rejections(hunt.id, gr.rejected)   # status=filtered + reason

  6. SCORE        scores = scorer.score(hunt, gr.candidates)
                  store.save_scores(scores)                       # status=scored

  7. SURFACE      surfaced = [(l, s) for (l, s) in scores
                              if s.is_relevant and s.deal_score >= hunt.min_deal_score]
                  for n in notifiers: n.notify(hunt, surfaced)    # status=surfaced

  8. store.finish_run(run_id, n_fetched, n_new, n_scored, n_surfaced, cost_usd)
```

Every step is idempotent. Re-running the same hunt five minutes later re-upserts the same
listings, appends identical price observations (cheap), gates out everything already scored
and unchanged, and makes zero API calls. That property is what makes a tight polling
interval safe.

### Step 5 in detail — the gate

This is the cost-control layer, so it's worth being explicit. A listing becomes a candidate
if **any** admit rule fires and **no** reject rule does.

```
REJECT (checked first, cheapest first):
  - status is dismissed/saved/contacted for this hunt   → "triaged"
  - seller_id in hunt.blocked_sellers                    → "blocked_seller"
  - price_cents > hunt.max_price                         → "over_price"
  - distance_mi > location.radius_miles                  → "too_far"
  - title/description matches hunt.exclude               → "excluded_kw"
  - already scored and nothing material changed          → "unchanged"

ADMIT:
  - never scored for this hunt                           → reason "new"
  - price dropped ≥ 15% since last score                 → reason "price_drop"
  - relist of a previously-gone listing                  → reason "relist"
```

`rejected` entries are **written to the DB**, not dropped. When a hunt returns nothing you
can see whether 200 listings were fetched and all rejected on `over_price` (your cap is too
low) or 0 were fetched (the scraper is broken). Debuggability of an empty result is the
difference between a tool you trust and one you stop opening.

### Step 6 in detail — scoring and the cache

Prompt assembled in strict stability order, because prefix caching is prefix-matched and
any byte change invalidates everything after it:

```
 system     rubric, scoring semantics, output schema         ← stable for weeks
 ─────────────────────────────────────────────  cache breakpoint
 block A    hunt criteria + exclusions                       ← stable until you edit the hunt
 ─────────────────────────────────────────────  cache breakpoint
 block B    up to N dismissed-title negative examples (F12)  ← refreshed ONCE DAILY, not per run
 ─────────────────────────────────────────────  cache breakpoint
 user       this listing: title, price, description, age, seller, price history
```

Block B is the subtle one. Dismissals arrive continuously, and rebuilding that block on
every run would invalidate the cache on every run and defeat the whole arrangement. So the
negative-example set is snapshotted daily and held fixed in between.

Candidates are scored concurrently under a semaphore (default 5). One failed scoring call
is logged against that listing and skipped — it must not abort the run and lose the other
49 results.

### Status state machine (per hunt, per listing)

```
                  ┌── gate reject ──→ filtered ──(price drop)──┐
                  │                                             │
   new ───────────┤                                             ↓
                  └── gate admit ──→ scored ──≥ threshold──→ surfaced
                                        │                       │
                                        └─ below threshold      ├─→ saved
                                           (visible in "all")   ├─→ dismissed ──→ feeds F12
                                                                └─→ contacted

   any state ──(absent from source N consecutive runs)──→ gone
```

`filtered` is not terminal — a price drop pulls a listing back into contention. `dismissed`
is terminal for surfacing but productive as training signal.

## Part 5 — Dashboard

Read-only over the same SQLite file; the only writes are triage actions.

Four bins, sorted by *why* a listing is there rather than by how sure we are:

- **Wants** (`/`) — matches for something on your list, `status = wanted`.
  Unverified matches sit here too, flagged amber, not in a bin of their own.
  Photo, title, price (old price struck through if dropped), distance, age,
  state chips, score. The model's reasoning, requirements and unknowns fold into
  a disclosure, because the photo and price are what you decide on first.
- **Free finds** (`/free`) — `status = free_find`. Worth grabbing regardless of
  the list.
- **Saved** (`/saved`) — `status IN (saved, contacted)`. What you decided to act on.
- **Skipped** (`/skipped`) — judged, then passed over. Renamed from `/near`,
  which read as "near me"; the old URL 308-redirects.
- **Hunt** (`/hunt/<id>`) — everything matched for one hunt, filtered by status,
  including rejected listings with their reason. This is how you tune a hunt.
- **Listing** (`/listing/<id>`) — images, full description, score history with
  requirements and unknowns, price sparkline, link out, triage.
- **Runs** (`/runs`) — per run: fetched / new / candidates / scored / wanted /
  free / images / cost / error. Also carries the two pause switches (sweeps, and
  everything) and the list of hunts.

Triage is **one** POST endpoint, `/triage`, writing `hunt_matches.status`
(+ optional dismiss note); `/hunts/toggle` is the only other write. The UI
offers Save and Dismiss only — `contacted` remains a valid status and still
counts as triaged, but nothing surfaces it any more.

## Part 6 — CLI

```
dealbot once   [--hunt NAME] [--dry-run] [--no-score]   one pass; --dry-run skips writes
dealbot run    [--interval 15m]                          loop over all enabled hunts
dealbot serve  [--port 8080]                             dashboard
dealbot hunts                                            list hunts + last run + counts
dealbot backfill --rescore [--hunt NAME]                 re-score against current criteria
dealbot fixtures capture --hunt NAME                     save live responses as test fixtures
```

`--no-score` is the everyday debugging flag: run the whole pipeline, spend nothing, see
what the gate would have admitted.

## Part 7 — Resolved

1. **Cadence.** Free sweep every **15 min**, want searches every **60 min**. Free
   items evaporate in minutes; priced ones sit for days. Splitting the cadence
   also keeps request volume down (96 + 144/day rather than 672).
2. **Threshold.** `min_deal_score` 7.0, overridable per hunt. The number only
   means something because the rubric anchors it: 9-10 drop everything, 7-8 worth
   a trip, 5-6 if convenient, 0-4 no.
3. **Free items still get scored**, but the sweep is free-only and the rubric
   treats them differently: price cannot be wrong, so the questions are
   usefulness and legitimacy rather than value-vs-ask.
4. **Wants require a `max_price`** — which forces each want to get its own
   targeted hunt, since the free sweep structurally cannot see a priced item.
5. **Scoring runs on `claude -p`** against the Pro subscription, not the API. See
   DESIGN.md section 6.
6. **Runs on koda.** Uptime is equivalent to otto's and the code lives here.

## Part 8 — Scoring output (P2)

```json
{"match": "yes|no|unknown",
 "matched_want": "<name>|null",
 "deal_score": 0-10,
 "requirements": [{"req": "...", "met": "yes|no|unknown", "evidence": "..."}],
 "unknowns": ["what a human should check"],
 "red_flags": [...], "est_value_usd": n, "condition": "...", "reasoning": "..."}
```

Routing into two bins, sorted by WHY a listing is there rather than by how sure
we are. Certainty is shown inside a card, not as a bin of its own — a 9.0
unconfirmed TV stand belongs next to a 9.0 confirmed one.

```
match in (yes, unknown)  and deal_score >= min_deal_score      -> wanted
match == no and worth_grabbing
                         and deal_score >= free_find_min_score -> free_find
otherwise                                                      -> scored (filed)
```

Two thresholds, because the bins answer different questions. "Is this the TV
stand I want" clears a high bar (7.0) because you will drive across town for it;
"is this free thing worth a look" is browsing (5.0). Holding both to 7.0 meant a
free working treadmill scored 5 and was never shown, which is the whole point of
the second bin.

`worth_grabbing` is an axis independent of `match`, and it is the half of the
sweep that finds things you never thought to search for. It first failed at
*triage*, not at routing: the instruction said keep anything "worth much more
than it costs", which is vacuously true of everything free, so the model
collapsed it to "does it match a want" and dropped a working treadmill and a
clean sectional before either was appraised.

### Image pass

After the text appraisal the model returns `needs_images` and an
`image_question`. Where it asks, we fetch the photos, downscale them, and run a
second appraisal with `Read` scoped to a directory holding only those files.

Letting the model opt in per listing is what makes it affordable, and it splits
the wants exactly as it should: the ottoman asks (colour and tier count are
visible) and the TV stand does not (a photo has no reference scale, so an
absolute width stays unknowable). Both scores are kept, so "the photos changed my
mind" is visible in the history rather than overwriting the text judgement.

`deal_score` is scored **as if the unknowns resolve favourably**, so uncertainty
is carried entirely by the match field. Without that rule a promising-but-
unverifiable listing scores mid-range and is buried, which is the common case
rather than an edge case.

Budget is deliberately absent from the model's prompt — the gate already rejects
over-price listings, and telling the model the budget only invites it to
re-litigate affordability and blend that back into the match decision.

## Part 9 — Still open

- Whether this Fedora container on koda is persistent enough to host the systemd
  timer, or whether that belongs on koda's host. Matters at P4, not before.
- `max_price` on both wants is a guess (tv-stand $250, ottoman $150).
- The stacked-ottoman description is an interpretation of "3 round stacks" and
  should be checked against a real example before it is trusted.
