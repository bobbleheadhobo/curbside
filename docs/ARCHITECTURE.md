# How deal_bot works

What actually runs, as built. DESIGN.md covers *why*; this covers *what*.

## 1. The clock

A systemd **user** timer fires every **15 minutes** and runs one command:

```
dealbot once --due
```

That is the only scheduled thing. Each hunt then decides for itself whether it
is due, so one timer serves every cadence:

| hunt | cadence | why |
|---|---|---|
| `sweep:free-nearby` | 15 min | free things evaporate in minutes |
| `want:tv-stand` | 60 min | priced items sit for days |
| `want:stacked-ottoman` | 60 min | same |

Each hunt runs **once per source**, so there are 6 hunt×source combinations. A
run counts toward the cadence only if it finished, had no error, and actually
scored — a `--no-score` pass fetches but does not reset the clock.

Units live in `~/.config/systemd/user/`: `curbside.timer` → `curbside.service`,
plus `curbside-web.service` for the dashboard (bound to localhost).

## 2. Fetch — two stages

**Stage one, the index.** One HTTP request per query. Facebook tries the
*category* page first (`/marketplace/albuquerque/free`, which returns more) and
falls back to the search page; Craigslist calls its JSON API. This yields title,
price, city and one thumbnail — **no descriptions**, and on Facebook no
coordinates.

**Stage two, the detail fetch** — description, exact coordinates, all photos —
runs **only for listings that survive the gate**. That is why the gate sits in
the middle of fetching rather than after it, and it is what keeps the request
count low enough to stay unremarkable.

Rate limiting lives inside each adapter so a caller cannot bypass it:

| source | between requests | per run | notes |
|---|---|---|---|
| Facebook | 15s | 25 | throttles silently; absent `feed_units` raises |
| Craigslist | 4s | 40 | JSON both ends; `?format=rss` is blocked |

## 3. The gate — what must be true before anything costs money

Pure arithmetic and string matching. Cheapest checks first, and **every
rejection is stored with its reason** so an empty result is explainable.

```
REJECT   already saved/dismissed/contacted   → "triaged"
         seller on the blocklist              → "blocked_seller"
         over the hunt's max_price            → "over_price"
         beyond location.radius_miles          → "too_far"
         city/state alone puts it out of range → "too_far_by_city"
         matches an exclude phrase             → "excluded_kw"
         already scored, nothing changed       → "unchanged"

ADMIT    never scored for this hunt            → "new"
         price fell ≥15% since last score      → "price_drop"
         relist of something already seen      → "relist"
```

`too_far_by_city` exists because Facebook gives no coordinates until the item
page is fetched, and its results are not confined to your area at all — a single
"free" search returned listings from Kansas City, Amarillo, Sacramento and
Findlay OH. Two rules run in order: a state that is not yours is definitively
too far, and a known city gets its centroid distance. Anything unrecognised
fails **open** — failing closed silently drops a listing that might be the one
you wanted, whereas failing open costs one detail fetch. On collected data this
rejects 14% before paying for detail, ~6 minutes of rate-limited fetching per
pass.

`unchanged` is what makes a tight poll interval affordable: in steady state
almost everything hits it, so **most ticks make zero model calls**.

Then two more filters:

- **Cap** at `max_results_per_run` (5) per hunt per source, newest first. The
  overflow stays `new` and drains over later runs, so a cold start arrives
  gradually rather than as one bill. The deferred count is recorded per run —
  if it keeps growing, the cap is too low.
- **Cross-source duplicates.** People post the same thing to both marketplaces.
  After enrichment (when both sources have coordinates) a `dup_key` of
  normalised title + exact price + coordinates rounded to ~1km identifies the
  same physical item. A duplicate of something this hunt already *paid to
  appraise* is skipped as `duplicate_of:<id>`.

## 4. The model

**Sonnet for everything** — triage, appraisal and the image pass — through
`claude -p` against the Pro plan, never the API.

```
5 candidates
  │
  ├─ TRIAGE     1 call, all 5 in one prompt, coarse keep/drop + a reason
  │             batched because every invocation pays a fixed ~2,500-token
  │             Claude Code prefix regardless of prompt size
  ↓
~3 survivors
  │
  ├─ APPRAISE   1 call each → match yes/no/unknown, per-requirement evidence,
  │             unknowns, deal_score, worth_grabbing, est_value
  ↓
  ├─ IMAGES     1 more call, ONLY where the model itself set needs_images.
  │             We download and downscale the photos; it gets `Read` scoped to
  │             that directory and nothing else.
  ↓
routing
```

One hunt×source is **1 + N + M calls** — worst case 11 with the cap at 5. All
six combinations due at once is ~66; steady state is usually zero.

Every scoring call runs with **`--tools ""`**: the prompt is entirely
stranger-written text, so the model gets no capability at all. The image pass is
the single narrow exception.

### The prompt

Assembled stable-first, because caching is prefix-matched and a byte change
invalidates everything after it:

```
prompts/rubric.md        editable; how to judge          ← stable for weeks
hunt wants + requires    what you want                   ← stable until you edit config
(dismissed examples)     designed, not yet wired         ← daily snapshot when built
the listing              volatile
```

`prompts/rubric.md` is read **once per process**, so editing it mid-run cannot
split the cache. Missing or empty falls back to the built-in default.

## 5. Routing — two bins, two bars

```
match = no,  worth_grabbing, score ≥ 5.0   → free finds
match = yes or unknown,      score ≥ 7.0   → wants
everything else                            → filed, still searchable
```

`unknown` lands in **wants**, flagged — a 9.0 unconfirmed TV stand belongs next
to a confirmed one. `deal_score` is judged *as if* unknowns resolve favourably,
so one threshold serves both bins and the match field carries the uncertainty.

## 6. Guards

| guard | behaviour |
|---|---|
| Daily ceiling | $3.00/day. Fetching continues (no quota cost); only judging stops. Counts in-flight spend, not just finished runs. |
| Rate-limit pause | Resumes at `resetsAt`; never zero. |
| Connectivity preflight | A `claude -p` with no network burns ~10 min of retry backoff; a 200ms TCP probe avoids it. |
| Silent throttling | Facebook answers 200 with a full page and no data. Absent `feed_units` raises. |
| Model output | Coerced, not trusted: `"$1,350"` parses, scores clamp to 0–10, non-scalars never reach TEXT columns. |
| Run record | Every fetch attempt, success or failure. |

## 7. What deduplication exists

| case | handled by |
|---|---|
| Same listing, later run | primary key `source:source_id`, upserted |
| Same listing, several queries in one run | in-run `seen` set |
| Same item cross-posted to both sources | `dup_key` (needs coordinates, so post-enrichment) |
| Same listing matched by several hunts | **deliberately not merged** — independent triage state per hunt |
| Relist: same item, brand-new id | `fingerprint` — **inert**, since neither source exposes a seller id. Falls back to the listing's own id so relists are simply not detected rather than falsely detected. |

## 8. Learning from your triage

Dismissing something for a hunt adds its title to that hunt's negative examples,
which go into the scoring prompt ahead of the listing. So the hunt sharpens with
use instead of showing you the same category of junk daily.

The set is **snapshotted once a day**. Dismissals arrive continuously, and
rebuilding the block per run would change the cached prefix per run — costing
~3x on every call, forever, since everything after it is invalidated too.

## 9. Signals surfaced in the dashboard

All of this was collected and none of it was visible:

- **Old price.** Facebook sends `strikethrough_price` on ~28% of listings. Shown
  struck through on the card ("$500 → now FREE") **and put in front of the
  model**, because a thing that was $500 and is now free is a different
  proposition from a thing that was always free.
- **Age** from `posted_at`, and **price moves** from the observation history.
- **"Motivated seller"** — 14+ days old *and* repriced. The one signal no
  keyword filter can express.
- **Price sparkline** on the listing page, coloured when the trend is down.
- **Rejections by reason** on the hunt page. "118 rejected on `over_price`" says
  your cap is wrong far faster than reading listings one at a time.

## 10. The dashboard

| view | what it holds |
|---|---|
| `/` wants | matches for your list, unverified ones flagged |
| `/free` free finds | worth collecting regardless of the list |
| `/saved` | what you decided to act on (saved + contacted) |
| `/near` near misses | judged, but under the bar |
| `/hunt/<id>` | everything one hunt matched, including rejections and why |
| `/listing/<id>` | detail, score history, price sparkline |
| `/runs` | every fetch attempt: counts, cost, errors |

**Near misses exist so the threshold is falsifiable.** `deal_score` assumes
anything unverified resolves favourably, so a 6 means "even at its best,
mediocre" -- but a bar you can never see over cannot be calibrated. If good
things keep appearing there, 7.0 is too high.

**Photos are cached locally** for listings that reach a bin, at 512px and around
34KB each. Facebook's image URLs carry an expiry token and die after roughly
four days -- 225 of 619 listings with photos are Facebook -- so a browsing UI
built on the source URLs would rot a third of its images every week. `/thumb/<id>`
serves the local copy and falls back to the source while one exists.
`dealbot prune-thumbs` drops copies for listings no longer in a bin or triaged.

## 11. Known gaps

- **Relist detection** is inert without seller ids.
- **Comparables from our own price history** need a month of observations; that
  is why the timer matters more than the polish.
- **`worth_grabbing` runs conservative** — a free piano scored 2.0. Tune it in
  `prompts/rubric.md`.
- **Notifications** are not built yet (Discord, two channels).
