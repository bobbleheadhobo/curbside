# How deal_bot works

What actually runs, as built. DESIGN.md covers *why*; this covers *what*.

## 1. The clock

### Waking hours

The bot is asleep outside a window, **noon to 8pm** by default. Outside it,
`dealbot once --due` does **nothing at all** — no fetch, no judgement, no
re-check — and leaves no `runs` row.

That makes it the one pause here that can lose data, which is deliberate.
Nothing found at 3am can be collected at 3am, and every hour awake is requests
against two sources that throttle silently. Every *other* pause (quota, rate
limit, spend, connectivity) stops the judging and keeps collecting, because
those are interruptions rather than decisions.

Three rules keep it from becoming a mystery:

* Only `--due` is gated. A hand-run `dealbot once` always runs.
* `start == end` means **always on**, not never on — it fails open like every
  other filter here.
* Being asleep is stated in the health pill on every page (`Asleep till 12pm`),
  and the first half hour after waking never reads as "quiet", because the last
  run is legitimately as old as the night.

The window lives in `settings` and is edited at `/settings`; `config.yaml`
supplies the starting values and the timezone.

### The timer

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

Those are defaults. Each cadence is editable from `/settings` (a
`hunt_interval:<hunt_id>` settings row) and clamped to 15 minutes at the low
end, since the timer cannot fire faster than that anyway.

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
         older than the hunt's max_age_days     → "too_old"
         matches an exclude phrase             → "excluded_kw:<term>"
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

`excluded_kw` is the **only rule here that fails closed** — a blocked listing is
dropped without ever being read. Since these became words you add from a phone
(*Never show* on a free card, or the list on `/settings`) rather than reviewed
lines in a committed file, two guards hold it:

* **Word-start matching, plurals allowed.** `bed` no longer matches *bedroom
  set*; `mattress` still matches *mattresses*. Substring matching was wrong in
  both directions.
* **A term that matches one of your wants is refused.** Blocking `console` on
  the sweep would drop the free media console the tv-stand hunt exists to find,
  and nothing in the interface would ever say so.

Every term is counted on `/settings` (its own **Never show me** panel, anchored
at `#blocked` and linked from the head of `/free`) as the number of listings it
has dropped — the difference between a preference you can audit and one you
have to trust.

`config.yaml` **seeds** the terms on a database's first open and is not read for
them again, exactly like wants. Merging the file in forever would mean four of
the entries on that page could never be deleted, which is not a list you can
edit.

Then two more filters:

- **Age**, after enrichment. Free things evaporate: a couch posted a fortnight
  ago is gone, and appraising it can only ever produce a wasted trip, so free
  sweeps skip anything over 7 days. Want hunts have **no** limit — priced things
  sit, and age there is a *buy* signal, which is what the motivated-seller flag
  is built on. Undated listings always pass; Craigslist's search feed omits the
  date, so failing closed would discard most of what it returns.
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
  ├─ IMAGES     1 more call, where the model set needs_images AND the text
  │             score already puts the listing in a bin. An image pass costs
  │             about twice a text appraisal, and two thirds were being spent
  │             confirming that 3/10 listings are indeed poor. Its value is at
  │             the top: a 9 that photos reveal to be junk saves a wasted trip.
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
match = yes or unknown,       score ≥ 7.0                  → wants
match = no, worth_grabbing,   score ≥ 5.0, a bargain,
                              AND the hunt is a SWEEP      → free finds
everything else                                            → filed, still searchable
```

**Only a sweep fills the free bin.** A want hunt that met an unrelated bargain
used to route it there too: seven of the ten entries in the live bin were
priced items from want hunts, including a $40 entertainment centre carrying a
green tv-stand chip in a tab called "Free finds". The sweep is free-only by
construction, so this makes the bin's name true. A want hunt's non-matches stay
`scored` and are still findable in `/skipped`.

**And a listing appears in exactly one bin.** `hunt_matches` remains per
(hunt, listing) — independent triage state is worth keeping — but the views
pick one row per listing: saved outranks wanted outranks free find, ties inside
a bin going to the first hunt by name so the choice is stable between loads.

`unknown` lands in **wants**, flagged — a 9.0 unconfirmed TV stand belongs next
to a confirmed one. `deal_score` is judged *as if* unknowns resolve favourably,
so one threshold serves both bins and the match field carries the uncertainty.

**"A bargain" only bites on things that cost money.** `worth_grabbing` asks
whether a sensible person would collect this at this price, which for a fairly
priced thing is a low bar — a $140 chair the model valued at $160 cleared it and
was announced in a tab called free finds. A priced listing now needs
`est_value ≥ 1.4 × price` (`FREE_FIND_VALUE_MULTIPLE`); free listings and ones
the model would not value pass untouched. In the live bin the genuine finds ran
3–5×, the filler sat at 1.1–1.2×.

### Leaving a bin

Two different facts, deliberately kept apart:

| | what it means | how |
|---|---|---|
| `gone` | stopped **appearing** in results | `mark_gone`, after 3 consecutive misses of that same source — 45 min on the free sweep, 3 h on an hourly want hunt. Reversible: seen again, the status is restored. |
| `sold_at` / `sold_reason` | **confirmed** off the market | `dealbot recheck` asks the source: Facebook's item payload carries `is_sold` and `is_live`, and a removed Craigslist posting stops returning a detail payload at all. `sold` is the source saying so; `removed` is only the page no longer resolving. |

The re-check costs requests and no model quota, so it rides along with `once`:
one detail fetch per bin listing every 6 hours, capped per run, and only for
statuses you might act on. A `saved` listing is *marked*, never un-saved.

It fails open, and three things make that true rather than aspirational:

* **A missing Facebook payload is `unknown`, not `removed`.** A throttled item
  page and a deleted one are the same 200 with no listing data, and retiring on
  it cannot be undone — `mark_sold` COALESCEs, `due_for_recheck` skips anything
  stamped, `mark_seen` will not un-`gone` it. Facebook says `is_sold` and
  `is_live` outright when it answers, so nothing is lost by requiring that.
  `parse_detail_html` also raises on a page carrying no product structure at
  all, the same discriminator the search path uses.
* **A blocked source stops being asked; the others carry on.** Recheck runs
  last and shares the pass's single request budget, so Facebook is routinely
  spent by the sweep before it. Breaking outright skipped every Craigslist
  listing queued behind it.
* **A listing in two bins is asked about once.** The queue is one row per
  (hunt, listing). Seeing a sold listing again does not resurrect it, in either the
upsert or the miss counter: Facebook keeps showing sold items in search.

## 6. Guards

| guard | behaviour |
|---|---|
| Plan quota | Stands aside at **70%** of the 5-hour window or **90%** of the 7-day one. Waiting for an outright rejection means otter has already been refused by the time we react; these numbers come from Claude Code's own `rate_limit_event` stream, so Curbside yields first. A reading older than 30 minutes is treated as unknown and one run is let through — enforcing a stale number is self-sealing, since no calls means no fresh number. |
| Daily ceiling | $10/day, a blunt backstop under the quota ceiling. Counts in-flight spend, not just finished runs. |
| Rate-limit pause | If refused anyway, resumes at `resetsAt`; never zero. |
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
| `/skipped` | judged, then passed over. `/near` 308-redirects here |
| `/hunt/<id>` | everything one hunt matched, including rejections and why |
| `/listing/<id>` | detail, score history, price sparkline |
| `/runs` | every fetch attempt: counts, cost, errors |
| `/settings` | waking hours, cadences, and the wants list |
| `/wants/<name>` | one want: what it looks for, its budget, its cadence |

The runs page carries two **pause switches**: one for the sweeps, one for
everything. Unlike every other pause here, they stop the *fetching* as well as
the judging, so nothing at all is collected for a paused hunt. That is
deliberate — the sweep switch exists to stop spending on free stuff rather than
to quieten it, and the second one exists for when you want the whole thing to
stop. They live in the `settings` table rather than in `config.yaml`, so the
dashboard and a hand edit are never fighting over one file, and they are an
override *on top of* config: a hunt disabled in config stays disabled.

A paused hunt is announced in a banner on **every** page, and marked `[PAUSED]`
in `dealbot hunts`. A bot switched off and forgotten looks exactly like a broken
one.

**The skipped view exists so the threshold is falsifiable.** `deal_score` assumes
anything unverified resolves favourably, so a 6 means "even at its best,
mediocre" -- but a bar you can never see over cannot be calibrated. If good
things keep appearing there, 7.0 is too high. It was called "near misses" at
`/near` until that read as "near me", which is the one thing it never meant.

**Photos are cached locally** for listings that reach a bin, at 512px and around
34KB each. Facebook's image URLs carry an expiry token and die after roughly
four days -- 225 of 619 listings with photos are Facebook -- so a browsing UI
built on the source URLs would rot a third of its images every week. `/thumb/<id>`
serves the local copy and falls back to the source while one exists.
`dealbot prune-thumbs` drops copies for listings no longer in a bin or triaged.

## 11. Where configuration lives

Two writers on one committed file is how you lose a comment, or a whole want.
So anything the dashboard can change lives in SQLite and `config.yaml` is
either the seed or is not consulted at all:

| thing | home | file's role |
|---|---|---|
| wants | `wants` table | **seeds each name once** |
| cadences | `settings` `hunt_interval:<id>` | default |
| blocked words | `settings` `hunt_exclude:<id>` | **seeds each hunt once** |
| waking hours | `settings` `schedule.*` | starting values |
| timezone | `config.yaml` | the only home. It is a fact, not a preference |
| pause switches | `settings` `hunt_disabled:<id>` | `enabled:` still wins if false |

Seeding is **per name and per hunt**, so something added to `config.yaml` after
a database's first open still arrives. It cannot resurrect a deletion: a deleted
want is archived rather than dropped, so its name stays taken (and its history
stays readable at `/hunt/want:<name>`), and a hunt that already has an exclude
list is left alone however short that list is. The one thing the file can no
longer do is append a term to a hunt that already has a list — it cannot tell a
new term from one you deleted, so that edit belongs on `/settings`.

Because of this, `Config.hunts` is a **computed property** rather than a field:
the web process stays up for weeks and adding a want has to produce its hunt
without a restart.

## 12. Installable

Manifest, three icon sizes (including a maskable one), and a service worker
served from the root so its scope covers the whole site. Android then offers to
add it to the home screen; the reverse proxy already provides the HTTPS that
requires.

The worker caches `/static/` and `/thumb/` and **never any HTML**. Half of what
this bot surfaces is gone within the hour, and a cached card claiming a free
sofa is still on the kerb sends someone across town for nothing — so pages are
network-only, with a plain offline notice when there is no connection.

The icon is drawn by `tools/make_icons.py`, which emits the SVG and every PNG
from one geometry so the browser tab and the home-screen tile cannot drift.

## 13. Known gaps

- **Relist detection** is inert without seller ids.
- **Comparables from our own price history** need a month of observations; that
  is why the timer matters more than the polish.
- **`worth_grabbing` runs conservative** — a free piano scored 2.0. Tune it in
  `prompts/rubric.md`.
- **Notifications** are not built yet (Discord, two channels).
