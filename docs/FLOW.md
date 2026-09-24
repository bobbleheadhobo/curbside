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
| F7 | Triage: save / dismiss / grabbed | per hunt, persists. `grabbed` records what you actually paid, which is the only ground truth the database has |
| F8 | Run health | why was today quiet — broken or genuinely nothing |

### v1.5 (cheap to add once the above works, high value)

| # | Feature | Notes |
|---|---|---|
| F9 | **Price-drop detection** | append-only observations make this nearly free |
| F10 | **Relist detection** | same seller + fuzzy title = reposted item, new id |
| F11 | **Stale/motivated flags** | listed 21+ days, or two price drops = seller wants out |
| F12 | **Dismissal learning** | dismissed listings become negative examples in that hunt's prompt |
| F13 | Re-score on material change | price drop ≥15% re-opens a listing we already judged |

### v2 — what actually shipped

Push became **Discord**, two channels, not ntfy. The systemd timer, the second
source (Craigslist), the availability re-check, waking hours, and a settings
page that owns the wants list all shipped. Still not built: comparables from
our own price history (they need a month of observations), an auto-populated
seller blocklist, and any search over the archive.

**`docs/ARCHITECTURE.md` is authoritative for behaviour.** This file is the
feature and type reference, and where the two disagree it is this one that has
drifted.

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

Kept in sync with `dealbot/models.py`; the docstrings there carry the reasoning.

```python
@dataclass(frozen=True)
class Listing:                     # normalized, what the store holds
    id: str                        # f"{source}:{source_id}"
    source: str; source_id: str
    title: str; description: str | None
    price_cents: int | None        # 0 = free; None = no price shown. NOT the same
    currency: str; url: str
    previous_price_cents: int | None   # Facebook's strikethrough price
    city: str | None; lat: float | None; lng: float | None; distance_mi: float | None
    seller_id: str | None; seller_name: str | None
    images: tuple[str, ...]        # a listing with none of these is never judged
    category: str | None; posted_at: datetime | None
    raw: dict
    # computed: dup_key (same item cross-source), image_key (same item
    #           reposted), fingerprint (relists, inert)

@dataclass(frozen=True)
class Score:
    listing_id: str; hunt_id: str; model: str; scored_at: datetime
    match: str                     # "yes" | "no" | "unknown" -- three-way, not a bool
    deal_score: float              # 0-10, scored AS IF the unknowns resolve well
    est_value_cents: int | None; condition: str | None; matched_want: str | None
    worth_grabbing: bool           # independent of match; only a SWEEP acts on it
    unknowns: tuple[str, ...]      # what a human should check
    requirements: tuple[dict, ...] # [{req, met, evidence}] -- auditable
    red_flags: tuple[str, ...]; reasoning: str
    needs_images: bool; image_question: str | None; images_checked: bool
    input_tokens: int; output_tokens: int; cache_read_tokens: int; cost_usd: float

@dataclass(frozen=True)
class Candidate:                   # survived the gate, headed for the model
    listing: Listing
    reason: str                    # "new" | "price_drop" | "relist" | "backlog"

@dataclass(frozen=True)
class StoredWant:                  # a want as the DATABASE holds it
    want: Want
    origin: str                    # "config" when seeded from the file
    archived_at: str | None        # soft delete; the name stays taken
    created_at: str | None; updated_at: str | None
```

## Part 3 — Interfaces

```python
class Source(Protocol):
    name: str
    def search(self, hunt: Hunt) -> Iterator[RawListing]: ...
    def parse(self, raw: RawListing) -> Listing | None: ...   # None = unusable
    # optional: detail(listing) -> Listing | None, the enrichment fetch

class Throttled:                 # mixin: inherit it, do not retype it
    def reset_budget(self) -> None: ...        # one budget per PASS, not per hunt
    def _reserve(self, n: int) -> None: ...    # refuse a search that cannot finish
    def _await_slot(self) -> None: ...         # jittered wait; raises BudgetExhausted
    def _spend_slot(self) -> None: ...         # count a request that went out

class Scorer(Protocol):
    def triage(self, hunt, candidates) -> TriageResult: ...   # batched, coarse
    def appraise(self, hunt, candidates) -> list[Score]: ...  # one call each
    # ClaudeCodeScorer adds: resolve_with_images, check_available, begin_run,
    # drain_unbilled, overridden
    # optional: suggest_queries(name, description, requires) -> tuple[str, ...]
    #           drafts a want's search terms. The DASHBOARD calls this one.

class Notifier(Protocol):
    def notify(self, hunt: Hunt, surfaced: list[tuple[Listing, Score]]) -> None: ...
    # optional: notify_price_drop(hunt, listing, score, was_cents) -> bool
```

Scoring is **two calls, not one**: triage is batched and coarse because every
invocation pays a fixed ~2,500-token prefix whatever the prompt size, and
appraisal is per listing because its output is per listing. A third, the image
pass, runs only where the model asked for one.

`parse` lives on the source because parsing is source-specific, but it returns
the shared `Listing` and `validate()` checks it before anything is written. A
broken adapter yields garbage that gets rejected and logged; it cannot corrupt
the store.

`DashboardNotifier` is a near-no-op that flips status and stamps `notified_at`;
the dashboard reads the database directly. `DiscordNotifier` is the real one.

Rate limiting lives INSIDE the adapter so a caller cannot bypass it, but the
mechanism is shared: `Throttled` in `sources/base.py`. `SourceBlocked` means the
site withheld data; `BudgetExhausted` (a subclass) means we stopped asking.
`facebook.search` acts on the difference — another surface is worth trying when
the site gates one, and worth nothing when our own budget is spent.

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
run_hunt(store, hunt, source, scorer, notifiers, location):

  1. start_run                 every attempt gets a row, success or failure
  2. FETCH                     a dead source fails LOUDLY here, never quietly
  3. PARSE + validate
  4. STORE                     upsert, record_price, mark_matches, mark_gone
  5. GATE                      filters.gate(); rejections are RECORDED
  5a. CAP at max_results       newest first, by pipeline.freshness
  5b. TOP UP from the backlog  spare capacity goes to listings never judged
  6. ENRICH survivors          description, coordinates, all photos
  6a. re-check distance, age, exclude terms   (only knowable after enrichment)
  6b. drop no-photo listings   nothing for the image pass to open
  6c. cross-source duplicates
  7. TRIAGE                    one batched call, coarse keep/drop
  8. APPRAISE                  one call each, survivors only
  9. IMAGES                    where needs_images AND route() would bin it
 10. ROUTE                     route() -> wants / free finds / filed
 11. notifiers, thumbnails, finish_run
```

Stages 6a-6c are each a predicate handed to `pipeline._drop`, which partitions,
**records the rejection**, logs a count and returns the new `GateResult`. Adding
a post-enrichment check is a predicate plus one call; the recording — the part
that makes a drop explainable rather than a silent disappearance — cannot be
forgotten because it is not yours to write.

Stages 9 and 10 both ask `pipeline.route(score, hunt, listing) -> "wanted" |
"free_find" | None`. It is one function because it was once two — a boolean for
the image pass and a pair of comprehensions in stage 10 — kept in agreement by a
comment. Disagreement was silent and cost money: image passes spent on listings
nobody would be shown.

`docs/ARCHITECTURE.md` has the reasoning for each stage and the order.

Every step is idempotent. Re-running the same hunt five minutes later re-upserts the same
listings, appends identical price observations (cheap), gates out everything already scored
and unchanged, and makes zero API calls. That property is what makes a tight polling
interval safe.

### Step 5 in detail — the gate

This is the cost-control layer, so it's worth being explicit. A listing becomes a candidate
if **any** admit rule fires and **no** reject rule does.

```
REJECT (checked first, cheapest first):
  - status is dismissed/saved/grabbed for this hunt      → "triaged"
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

Candidates are appraised **sequentially**, one `claude -p` at a time. There is no
concurrency anywhere in this codebase and that is deliberate: the constraint is plan
quota shared with `otter` and the user's own interactive use, so the plan window is
re-read *between* listings and the run stands aside the moment it tightens. Parallelism
would spend past a ceiling it could no longer see.

Failure is handled at three different depths, and the distinctions are load-bearing:

- **Unparseable output** — one retry with a blunter instruction, then give up on that
  listing and write **no** `Score` row, so it stays `new` and is retried next run rather
  than being recorded as judged on output nobody could read. Its cost is still charged,
  via `drain_unbilled`.
- **A pause mid-batch** (`ScoringUnavailable`) — stop, but re-raise carrying
  `partial=scores`. Every appraisal already bought is kept. Discarding them threw away
  real money and re-charged for the same listings on the next run.
- **Anything else** — logged against that listing and skipped. One bad response must
  never end a batch.

### Status state machine (per hunt, per listing)

```
                  ┌── gate reject ──→ filtered ──(price drop)──┐
                  │                                             │
   new ───────────┤                                             ↓
                  └── gate admit ──→ scored ──≥ threshold──→ surfaced
                                        │                       │
                                        └─ below threshold      ├─→ saved ──→ grabbed
                                           (visible in "all")   └─→ dismissed ──→ feeds F12

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
- **Saved** (`/saved`) — `status IN (saved, grabbed)`. What you decided to act on,
  and what you went and got. A grabbed listing is marked and counted apart, not
  moved to a page of its own.
- **Skipped** (`/skipped`) — judged, then passed over. Renamed from `/near`,
  which read as "near me"; the old URL 308-redirects.
- **Hunt** (`/hunt/<id>`) — everything matched for one hunt, filtered by status,
  including rejected listings with their reason. This is how you tune a hunt.
- **Listing** (`/listing/<id>`) — images, full description, the judgement
  (score, reasoning, flags, requirements and unknowns) with earlier passes
  beneath it, price sparkline, link out, triage.
- **Runs** (`/runs`) — per run: fetched / new / candidates / scored / wanted /
  free / images / cost / error / warning. `error` means the FETCH failed and is
  the column `last_success_at` reads; a run that fetched but stood aside from
  the plan quota is a `warning`, or its cadence collapses onto the timer.
  Also carries the two pause switches (sweeps, and
  everything) and the list of hunts.

Triage is **one** POST endpoint, `/triage`, writing `hunt_matches.status`
(+ optional dismiss note). Recording a purchase is a second, `/grabbed`, because
it carries a figure and stamps the listing as well as the match; `/hunts/toggle`
and the settings writes are the others. There was a `contacted` status once,
meaning "I messaged the seller"; it was never offered, never used, and
contradicted the read-only boundary, so it went when `grabbed` arrived.

## Part 6 — CLI

```
dealbot once   [--hunt N] [--dry-run] [--no-score] [--due]  one pass
dealbot run    [--interval 15m]                             loop on each cadence
dealbot serve  [--host H] [--port 8080]                     dashboard, localhost
dealbot hunts                                               hunts, last run, the window
dealbot notify [--hunt N]                                   flush alerts; no fetch, no cost
dealbot recheck [--limit N] [--all]                         still for sale? requests, no quota
dealbot seed-demo [--db PATH]                               an offline database to develop on
dealbot prune-thumbs                                        drop cached photos no longer needed
```

`--due` is what the timer passes: each hunt decides whether enough time has
passed, and the whole pass is skipped outside the waking hours. `backfill` and
`fixtures capture` were planned here and never built.

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
match == no and worth_grabbing and the hunt is a SWEEP
                         and deal_score >= free_find_min_score -> free_find
otherwise                                                      -> scored (filed)
```

Two thresholds, because the bins answer different questions. "Is this the TV
stand I want" clears a high bar (7.0) because you will drive across town for it;
"is this free thing worth a look" is browsing (5.0). Holding both to 7.0 meant a
free working treadmill scored 5 and was never shown, which is the whole point of
the second bin.

Only a sweep fills the free bin. A want hunt that met an unrelated bargain used
to route it there too, which put seven priced items into a tab named for free
things. A listing also appears in exactly one bin: saved outranks wanted
outranks free find.

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

- **The tv-stand hunt asks an unanswerable question.** "At least 70 inches wide"
  is stated in 5 of 104 listings, so 49% of its judgements come back `unknown`
  and $2.17 of $5.63 of all appraisal spend went on them. Every want-hunt
  dismissal so far was an unconfirmed width. Either the requirement becomes
  something checkable or the bin is accepted as a "go and look at the photos"
  queue.
- **Dismissal learning is wired but unproven**, and measurably wrong on want
  hunts: 31 listings share the title "tv stand", so dismissing one teaches that
  hunt to suppress the thing it hunts for. Blocked words on `/free` are the
  auditable counterpart; the prompt block has no such account of itself.
- `max_price` on both wants is a guess (tv-stand $250, ottoman $150).
- The stacked-ottoman description is an interpretation of "3 round stacks" and
  should be checked against a real example.
