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
* The window may **wrap midnight**, and the hours panel states how long it
  actually is: "Awake 21 hours a day. This window runs past midnight." Two
  clock times alone hide that — 11pm to 8pm reads like a night shift and is 21
  hours awake, the near-opposite — and a control you misread is a control that
  lies, which is the same failure the health pill exists to prevent.
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

**Which makes the `error` column load-bearing, and it bit.** A scoring
standdown — the plan window shutting before a batch (`scoring skipped`) or part
way through one (`scoring interrupted`) — was recorded there. The fetch had
succeeded in both cases; only the judging stood aside. But `last_success_at`
reads `error`, so those hunts came up due on *every* tick: measured on the live
box, the free sweep ran at a median 16 minutes against a configured 30, and
`want:tv-stand` at 15 against a configured 60. That is 2-4x the intended
requests, at two sources that throttle silently, retrying something no amount
of fetching can fix.

Both go in `warning` now. The rule: **`error` means the FETCH failed; anything
else that went wrong is a `warning`.** Note there are two standdown phrases and
fixing one and not the other is the easy mistake — a test greps `run_hunt` for
both.

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
REJECT   already saved/dismissed/grabbed     → "triaged"
         over the hunt's max_price            → "over_price"
         beyond location.radius_miles          → "too_far"
         city/state alone puts it out of range → "too_far_by_city"
         older than the hunt's max_age_days     → "too_old"
         no photo, and no description either    → "nothing_to_judge"
         no photo                               → "no_photo"
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

### The second half of the gate, after enrichment

Some rules cannot run in `filters.gate` at all, because the search feed does not
carry what they need: Facebook supplies no description and no coordinates until
the item page is fetched, and Craigslist supplies neither a description nor a
posting date. So `too_far`, `too_old` and `excluded_kw` are **re-checked** once
enrichment has filled those in, alongside the checks that only make sense there.

Each is a predicate handed to `pipeline._drop`, which partitions the candidates,
**records the rejections**, logs a count, and returns the new `GateResult`. They
were five hand-written copies of that loop until recently, and had drifted: the
distance one recorded unguarded and logged nothing, so the single stage
rejecting on a post-enrichment distance was the single stage whose rejections
never appeared in the journal. Recording is what separates "explainably empty"
from "silently broken", so it is no longer the caller's to remember.

- **Age**, after enrichment. Free things evaporate: a couch posted a fortnight
  ago is gone, and appraising it can only ever produce a wasted trip, so free
  sweeps skip anything over 7 days. Want hunts have **no** limit — priced things
  sit, and age there is a *buy* signal, which is what the motivated-seller flag
  is built on. Undated listings always pass; Craigslist's search feed omits the
  date, so failing closed would discard most of what it returns.
- **Cap** at `max_results_per_run` (5) per hunt per source, newest first — by
  `pipeline.freshness`, which falls back to `first_seen` when there is no
  posting date. Ordering on `posted_at` alone made this a no-op for Craigslist,
  whose search feed carries no date: every candidate tied on the `datetime.min`
  fallback and a stable sort handed the slots to feed order. The
  overflow stays `new` so a cold start arrives gradually rather than as one
  bill.

  **It does not drain by itself, and for a long time it did not drain at all.**
  Candidates came only from the *current* fetch, so anything capped out then
  fell off page one and was never seen again — 130 listings were stranded that
  way, the oldest 47 hours old, while every individual run looked healthy. A
  run with room under its own cap now tops up from `store.unjudged()`: still
  `new`, still this source, not confirmed sold, newest first. In steady state
  the backlog is empty and nothing changes, so re-running still costs nothing.
  The count per hunt is on `/runs`, because the one failure that view could not
  show was collecting things and never judging them.
- **A photograph is the minimum.** Without one there is nothing for the image
  pass to open, and on Craigslist a photoless post is usually not a listing at
  all: of the 51 collected, the titles run *"Gone"*, *"Free junk metal
  removal"*, *"I need HELP please"*, *"Anyone willing to donate bikes for
  kids"*. Wanted-ads and noise. Eight were appraised for $0.20 and not one ever
  reached a bin. Two reasons are recorded so the hunt page can tell an empty
  post (`nothing_to_judge`) from a photoless one (`no_photo`).

  Missing **text** is deliberately not disqualifying. On Facebook a bare
  listing usually means the seller let the photos do the talking, and those
  score *better* than the ones with words — 5.65 average against 4.89, 9 of 17
  above 7, one of them in the wants bin. Those listings carry an amber
  *no description* badge on the card instead, because the judgement rests
  entirely on the photographs.

  Both run after enrichment, because that is where both arrive: Craigslist's
  search feed carries no description at all, and Facebook's carries one photo.
- **Reposts.** A seller puts the same thing up twice, minutes apart, and
  `dup_key` cannot see it: it hashes the price exactly, and Craigslist spells
  "free" two ways (`-1` for no price shown, `0` for free). One gas stove was
  posted at $0 and then with no price at all, so it made two keys, was
  appraised twice and reached Discord twice. `image_key` — the photo's own id
  plus coordinates — catches these. Backfilling it over the collected data
  found 24 repost groups, one of them the same item posted four times, and
  eight of them had been appraised more than once.
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
dismissed titles         negative examples, WIRED         ← snapshotted daily
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

All of that is `pipeline.route(score, hunt, listing)`, returning `"wanted"`,
`"free_find"` or `None`. **One function, because it used to be two.** The image
pass needs the same question answered before spending on photos — it only pays
for listings that would already reach a bin — and it asked with its own boolean
copy of these conditions, kept in step with the routing by a comment. Nothing
breaks visibly when two copies disagree; you simply buy image passes for
listings nobody will be shown, or skip them on listings people will see.

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
| `listings.grabbed_at` / `paid_cents` | **you** went and got it | `mark_grabbed`, from the **Grabbed it** button on `/saved`. Also stamps `sold_at` with reason `grabbed`, which is what makes every existing `sold_at IS NULL` guard exclude it: no re-check request and no price alert is ever spent on a thing in your garage. `paid_cents` is nullable and 0 is a different answer -- 0 is free, NULL is "I did not note it". These two columns are the **only** ground truth in the database; everything else about value is the model's claim. `upsert_listing` deliberately does not know them, so no refresh can overwrite a purchase. |
| `scores.price_unclear` | the $0 is not real | Neither site has a "make me an offer" price, so a seller who wants one puts $0 and says so in the description ("Send me offers please over 50 wrenches"). The model sets this; the card then stops printing FREE, and `route` keeps it out of the free bin -- a price nobody knows cannot be weighed against the trip. It stays `scored` and shows on `/skipped`, marked. The flag decides the bin rather than the score, so the model can still say plainly whether the thing would be worth having. |

The re-check costs requests and no model quota, so it rides along with `once`:
one detail fetch per bin listing, capped per run, and only for statuses you
might act on. A `saved` listing is *marked*, never un-saved.

It runs at **two speeds**, because the two halves of a bin are not worth the
same. `saved` is the things you might be about to drive to and
there are only ever a handful, so they are asked about on **every pass** —
`grabbed` is in neither list, because the `sold_at` stamp already excludes it —
`recheck.saved_every_hours: 0.25`. `wanted` and `free_find` are candidates
nobody has decided on and the half that grows to dozens, so they stay at
**6 hours**: at the saved pace they would be a few hundred item-page fetches a
day at a site that throttles silently, to learn something `mark_gone` already
half-answers for free. The faster half is queued **first**, so a full candidate
bin can never spend the per-run cap before your own list is reached.

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

### The one scam the rubric names outright

Most bad listings cost a wasted trip. The **advance-fee** scam costs money: a
valuable thing is offered free or far under its worth, delivery is offered for a
fee, you pay the fee and nothing comes. It was found in the live data doing
exactly what it is designed to do — a free washer and dryer, "like new",
"delivery all depends on you", scored **9.0 with no red flags** and sat in the
free-finds bin. The image pass then *raised* its confidence, because the
photographs were real, as they usually are.

The rubric now names the pattern, and keys on the **combination** rather than on
delivery: valuable, free or far too cheap, delivery offered, usually "brand new"
or "like new" plus an invitation to message. Delivery for a fee on a **priced**
item is completely ordinary — six listings in the collected data do it, a $250
console with $250 delivery among them — and flagging that would bury legitimate
listings. It also says plainly that photographs cannot establish legitimacy,
only what the thing would be worth if real.

Re-scored against the same listings: the washer and dryer went 9.0 → **1.0**,
`worth_grabbing` false, with the pattern named in `red_flags`; the $250 console
stayed at 7.0 with no flags; and free flagstone whose seller wants their petrol
money covered came back "ordinary for a free heavy item, not a scam pattern".

Because `load_rubric` falls back to `base.RUBRIC` when the file is missing — and
only logs it, at info — the guidance lives in both and `tests/test_docs.py`
fails if either loses it.

## 6. Guards

| guard | behaviour |
|---|---|
| Plan quota | Stands aside at **70%** of the 5-hour window or **90%** of the 7-day one. Waiting for an outright rejection means otter has already been refused by the time we react; these numbers come from Claude Code's own `rate_limit_event` stream, so Curbside yields first. A reading past its own window's `resetsAt` is retired: the window it measured has rolled. Until then it HOLDS, however old it is, because utilisation does not fall before a window resets — re-asking cost $0.57 in six hours, one pass every 31 minutes against a 7-day window with 21 hours left on it. Only an *undated* reading older than 30 minutes is treated as unknown and lets one pass through, since with no expiry there is no other way to learn the window reopened. `read_plan_usage` decides all of it, and `/runs` draws exactly what it decided. **The hold is on judging alone**: the re-check and the price-drop alert are requests and no model call, so they keep running. |
| Daily ceiling | $10/day, a blunt backstop under the quota ceiling. Counts in-flight spend, not just finished runs. |
| Rate-limit pause | If refused anyway, resumes at `resetsAt`; never zero. |
| Connectivity preflight | A `claude -p` with no network burns ~10 min of retry backoff; a 200ms TCP probe avoids it. |
| **Override** | *Judge anyway for 2 hours*, on `/runs`. Lifts the three guards this project chose to stop at — the daily spend ceiling, the utilisation ceilings and a rate-limit pause it set itself — and nothing else. The connectivity probe still applies, because that is backoff rather than budget, and **the plan's own limit still refuses**: the override gets the call sent, not accepted. Time-boxed, because an override with no expiry is a guard you removed rather than one you overrode. |
| Silent throttling | Facebook answers 200 with a full page and no data. Absent `feed_units` raises. |
| Model output | Coerced, not trusted: `"$1,350"` parses, scores clamp to 0–10, non-scalars never reach TEXT columns. |
| Run record | Every fetch attempt, success or failure. |

## 7. What deduplication exists

| case | handled by |
|---|---|
| Same listing, later run | primary key `source:source_id`, upserted |
| Same listing, several queries in one run | in-run `seen` set |
| Same item cross-posted to both sources | `dup_key` (needs coordinates, so post-enrichment) |
| Same item reposted by its seller | `image_key` (the photo's id + coordinates) |
| Same listing matched by several hunts | **deliberately not merged** — independent triage state per hunt |
| Relist: same item, brand-new id | `fingerprint` — **inert**, since neither source exposes a seller id. Falls back to the listing's own id so relists are simply not detected rather than falsely detected. |

## 8. Learning from your triage

Dismissing something for a hunt adds its title to that hunt's negative examples,
which go into the scoring prompt ahead of the listing. So the hunt sharpens with
use instead of showing you the same category of junk daily.

The set is **snapshotted once a day**. Dismissals arrive continuously, and
rebuilding the block per run would change the cached prefix per run — costing
~3x on every call, forever, since everything after it is invalidated too.

## 8a. Price drops on things you already care about

The re-check pass refreshes the price of everything in a bin — saved things
every pass, candidates every six hours — and for a long time nothing read it. Alerts only fired when a listing *entered*
a bin, so a saved $200 credenza falling to $120 said nothing at all — with
every observation needed to notice it already on disk.

`announce_price_drops` closes that. It costs no quota and no requests: the
prices are already there.

The baseline is **the price you were last told**, kept in
`hunt_matches.alerted_price_cents` and falling back to the price the listing
carried when it was judged. So $200 → $180 → $160 announces once, at $160, and
the next alert needs a further real drop from there. The bar is the same 15%
the gate uses, because sellers nudge prices constantly. A drop to free counts,
and is the headline case.

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
| `/` wants | matches for your list, unverified ones flagged. The wants list is edited here, in a panel folded above the cards |
| `/free` free finds | worth collecting regardless of the list |
| `/saved` | what you decided to act on, and what you went and grabbed |
| `/skipped` | judged, then passed over. `/near` 308-redirects here |
| `/hunt/<id>` | everything one hunt matched, including rejections and why |
| `/listing/<id>` | detail, the judgement and every earlier pass, price sparkline |
| `/runs` | every fetch attempt: counts, cost, errors, and what the plan has left |
| `/settings` | waking hours, cadences, the tuned limits, and the blocked words |
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
That download is the same `images.fetch_downscaled` the vision pass uses, so the
hardening around a stranger's URL — byte cap enforced while reading,
content-type check, timeout, re-encode through Pillow — exists once.

**The dashboard is a reader.** It shares the SQLite file with the poller (WAL,
`busy_timeout=10000`), and WAL readers never block, so a page load is unaffected
by a run in flight — *as long as it does not write*. It no longer does.

The three bin queries are one query with the WHERE clause swapped, built by
`_queue_sql(where)`. They were previously derived from each other with
`str.replace`, which is a **silent** no-op when the needle stops matching:
editing the literal `"WHERE m.status = ?"` would have produced a valid query
against the wrong rows, with nothing raised anywhere.

Two indexes carry those views. The bin queries filter on `status` *alone*, with
no `hunt_id`, so the composite `ix_matches_status(hunt_id, status)` never
applied to them; and the one-row-per-listing subquery matches on `listing_id`,
the second column of the primary key. Every bin page therefore scanned
`hunt_matches` and sorted once per candidate row — quadratic in a table nothing
is ever deleted from. `ix_matches_bin(status)` and `ix_matches_listing(listing_id)`
fixed that: on the live database the tab counts went 6.6x faster and the card
query 2.2x, and the gap widens as history accrues.

## 11. Where configuration lives

Two writers on one committed file is how you lose a comment, or a whole want.
So anything the dashboard can change lives in SQLite and `config.yaml` is
either the seed or is not consulted at all:

| thing | home | file's role |
|---|---|---|
| wants | `wants` table | **seeds each name once** |
| cadences | `settings` `hunt_interval:<id>` | default |
| the two score bars, the per-run cap, the radius | `settings` `tune:<name>` | default |
| blocked words | `settings` `hunt_exclude:<id>` | **seeds each hunt once** |
| waking hours | `settings` `schedule.*` | starting values |
| timezone | `config.yaml` | the only home. It is a fact, not a preference |
| pause switches | `settings` `hunt_disabled:<id>` | `enabled:` still wins if false |

A want's **price cap of 0 means free ones only** — `over_price` drops anything
dearer, so the cap is how you say "I want one of these, but only if someone is
giving it away". Every want needs **at least one search term**: without one it
has no hunt, and only the free sweep sees it, which searches "free" rather than
the thing you asked for. The terms can be drafted by the *Suggest terms* button
on the want editor; that is the only thing the dashboard ever spends quota on,
it spends none unless the button is pressed, and it runs on `suggest_model`
(sonnet — see `ScorerConfig` for why the cheaper-looking model costs more).

Seeding is **per name and per hunt**, so something added to `config.yaml` after
a database's first open still arrives. It cannot resurrect a deletion: a deleted
want is archived rather than dropped, so its name stays taken (and its history
stays readable at `/hunt/want:<name>`), and a hunt that already has an exclude
list is left alone however short that list is. The one thing the file can no
longer do is append a term to a hunt that already has a list — it cannot tell a
new term from one you deleted, so that edit belongs on `/settings`.

Because of this, `Config.hunts` is a **computed property** rather than a field:
the web process stays up for weeks and adding a want has to produce its hunt
without a restart. It is genuinely recomputed, so read it *once* per request and
bind it — `ctx()` used to read `cfg.hunts` seven times per render and rebuild the
whole tuple each time, and `/settings` scanned it once per want, which is
quadratic in the one number on that page a person is expected to grow.

Each tuned number carries its **destination** in `Store.TUNING` —
`"min_deal_score": (float, 0.0, 10.0, "defaults")`, i.e. cast, floor, ceiling,
and the `Config` attribute it lands on — so `config.with_store` applies them
generically. `radius_miles` is the odd one out, landing on
`location` rather than `defaults`; before the destination was written down, that
meant a second hand-written `replace`, and a fifth number would have meant a
third. Adding one is now a line in `TUNING` plus its form field, which is
exactly what surfacing `max_image_checks` (destination `scorer`) cost.

`max_image_checks` is the image budget: how many listings per hunt per run may
have their photographs looked at, at `images_per_check` photos each. **It is
not the reason most unmet image requests go unmet.** An image pass is only
bought for a listing `route` would already put in a bin, so a low-scoring
listing that asks to be seen is declined by that test and never reaches the
budget at all. Raising the number buys looks only when one batch holds several
bin-bound listings at once.

**Seeding writes nothing on a read.** Both seed calls run inside `with_store`,
which the dashboard calls *per request*, so they must be no-ops once a database
is seeded. The per-name and per-hunt checks already guarantee that; two
`set_setting(...'seeded', '1')` rows that nothing ever read did not, and put a
write lock on the read path of every page. `tests/test_web.py` now fails if a
GET writes to an already-seeded database.

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
- **No search over the archive.** 800+ listings and the only ways in are the
  bins and the per-hunt views, both capped at 200.
