# Curbside

Watches Facebook Marketplace and Craigslist around Albuquerque for free and
underpriced things, judges them with Claude, and puts what survives on a local
dashboard and into two Discord channels.

**Working on this?** Start with [`CLAUDE.md`](CLAUDE.md) — orientation, the
invariants that fail quietly when broken, and how to run things without spending
plan quota.

| doc | what it answers |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | how to work on it safely; what not to break |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | what actually runs, as built |
| [`docs/DESIGN.md`](docs/DESIGN.md) | why the shape is what it is |
| [`docs/FLOW.md`](docs/FLOW.md) | features, types, control flow |
| [`docs/UI.md`](docs/UI.md) | changing the dashboard |
| [`PRODUCT.md`](PRODUCT.md) | who the dashboard is for and what it must feel like |
| [`DESIGN.md`](DESIGN.md) | the dashboard's visual system: colours, type, components |
| [`systemd/README.md`](systemd/README.md) | deployment |

## Status: live

A systemd timer runs a pass every 15 minutes during waking hours, against both
live sources, with the real scorer. It spends real plan quota.

The whole pipeline also runs end to end against recorded fixtures, with **no
network and no Claude quota**, and the test suite does nothing else. That ordering is deliberate: every dead scraper project on GitHub
died because the fetch layer broke and took the project with it. Here the
pipeline is provably fine and a Facebook change is a one-file repair.

| Phase | | |
|---|---|---|
| **P0** | config, schema, pipeline, gate, fixture source, dashboard, stub scorer | **done** |
| **P2** | `claude -p` scorer — triage + appraise, three-way match | **done** |
| **P2.5** | two lists of picks; model-requested image pass | **done** |
| **P1** | live Facebook + Craigslist sources, two-stage fetch | **done** |
| **P3** | dismissal learning, price-drop / stale flags, sparkline, rejection view | **done** |
| **P4** | Discord notifications (two channels), scheduled runs, still-for-sale re-checks | **done** |
| P5 | comparables from our own price history | needs about a month of data |
| | ntfy | not built |

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m curbside.cli once --no-score   # one pass, spends nothing
.venv/bin/python -m curbside.cli serve     # dashboard on 127.0.0.1:8080
.venv/bin/python -m pytest tests/ -q
```

A bare `curbside once` judges with the real scorer and **spends plan quota**. Use
`--no-score` or `--dry-run` unless that is what you mean.

```
curbside once --hunt free-nearby --dry-run   fetch and gate, write nothing
curbside once --no-score                     full pipeline, spend nothing
curbside once --due                          the timer's pass; respects the hours
curbside run                                 poll on each hunt's cadence
curbside hunts                               hunts, their last run, the window
curbside notify                              send pending alerts; no fetch, no cost
curbside recheck                             is it still for sale? requests, no quota
curbside seed-demo                           a realistic offline database to develop against
curbside prune-thumbs                        drop cached photos no longer needed
```

`serve` binds localhost by default. The app has no login, so the deployed unit
opts in to `--host 0.0.0.0 --port 8477` explicitly, behind a trusted network.

**It sleeps.** Noon to 8pm by default, editable at `/settings`. Outside the
window a `--due` pass does nothing at all — no fetching, no judging, no
re-checking — because nothing found at 3am can be collected at 3am. A hand-run
`curbside once` ignores the window, so you can always force a pass.

## How it works

```
sources/   fixture | facebook | craigslist  → RawListing
           ↓ parse + validate
store/     sqlite — nothing is ever deleted
           ↓
filter/    cheap deterministic gates    ← keeps model cost near zero
           ↓
score/     triage (batched) → appraise (individual)
           ↓
notify/    dashboard | discord
```

Configure in `config.yaml`. A **want** is something you want, with a budget and a
plain-language description; a **sweep** is a broad trawl for free stuff that gets
scored against every want *plus* a general "is this obviously valuable" test — so
it can surface things you never thought to search for.

Wants need both `queries` and a `description` for a reason: keyword search cannot
know that "media console" means "tv stand", and a description cannot be typed
into a search box. The queries cast the net; the description does the judging.

**`config.yaml` seeds the wants once and is then out of the loop.** After a
database's first open, wants live in SQLite and are added, edited and removed
from the Wants page (`/`), in a panel folded away above the cards. The file and
the dashboard are never fighting over one file. A removed want is archived, not
deleted: its hunt stops, its history stays.

**Every search term is a request.** Each term costs one request per source per
pass, from the same per-pass budget that fetches descriptions, so a want may
have at most six. Its editor shows what each term has found that no other term
did, which is how to decide which one to cut.

The waking hours, each hunt's cadence, the blocked words and the tuned limits
(the two score bars, the batch cap, the radius, the photo budget) are set on
`/settings`, and the same seed-once rule applies to the blocked words.

## The dashboard

Built for a phone, read many times a day. Five tabs along the bottom:

| page | what it holds |
|---|---|
| **Wants** `/` | picks that match something on your list, and the list itself |
| **Free** `/free` | free finds: picks that match nothing but are worth collecting |
| **Saved** `/saved` | what you kept; **Grabbed it** records what you paid |
| **Skipped** `/skipped` | judged and passed over, so the bar can be checked |
| **Runs** `/runs` | what is running now, and why judging is held if it is |

`/stats` says what the bot spends and what each hunt found for it, `/settings`
holds the controls above, and `/hunt/<id>` shows what one hunt judged and why
anything was dropped. Each page has its own hue, so you can tell where you are
at a glance. [`DESIGN.md`](DESIGN.md) is the
system, and [`docs/UI.md`](docs/UI.md) is how to work on it.

## On your phone

The dashboard ships as an installable web app — manifest, icons and a service
worker — so Chrome on Android offers **Add to home screen** and it opens
without browser chrome. The worker caches the photos and the icons and
deliberately **never a page**: half of what this finds is gone within the hour,
and a stale card is worse than no card. Icons are generated:

```bash
.venv/bin/python tools/make_icons.py
```

## Sources

Both are live, and each gets **its own run per hunt** — so one being throttled
shows up in the runs table rather than quietly halving the results.

| | fetch | coordinates | descriptions | images |
|---|---|---|---|---|
| **Craigslist** | JSON API, both ends | in search results | detail fetch | in search results |
| **Facebook** | HTML + embedded JSON | detail fetch | detail fetch | 1 thumb, rest on detail |

**Craigslist is much the friendlier of the two.** `sapi.craigslist.org` serves
search *and* detail as JSON, so there is no HTML parsing anywhere. Two things had
to be worked out: search items are positional arrays
(`[id, pidOffset, catId, price, "1:locIdx~lat~lon", imgPrefix, [code,…], …, title]`,
where price is whole dollars and `-1` means none), and the detail endpoint wants
the **uuid** (field code 13), not the numeric posting id. `?format=rss` is
blocked outright. Because coordinates arrive in the search feed, the radius
filter works on the first pass — no detail fetch needed to know how far away
something is.

Albuquerque is `areaId=50`, and Craigslist's own coordinates for it
(35.0844, -106.651) match our configured location exactly.

## The Facebook source

Logged out. No cookies, no login, no browser — the public pages still embed their
GraphQL payload in `<script type="application/json">` blocks. Three things had to
be learned by trying:

**`Sec-Fetch-*` headers are mandatory.** Without `Sec-Fetch-Site: none` and
friends you get a bodyless HTTP 400. The response says so if you read it:
`vary: Sec-Fetch-Site, Sec-Fetch-Mode`.

**The search feed has no descriptions and no coordinates.** It carries title,
price, city, one thumbnail, and the id — and real titles are frequently just
"Free". So fetching is two-stage, mirroring the scoring: search is a cheap index,
and the item page (which does have `redacted_description` and exact lat/lng) is
fetched **only for listings that survive the gate**. That is what keeps the
request count low enough to stay unremarkable.

**Throttling is silent, and this is the dangerous one.** After roughly five rapid
requests Facebook keeps answering `200` with ~590KB of perfectly valid HTML that
contains no listing data whatsoever. A naive adapter reads that as "quiet day"
— forever, while looking healthy. The `feed_units` key is the discriminator:
present means a real feed (possibly legitimately empty), absent means we were cut
off, which raises and lands in the runs table.

**Two surfaces, best first.** The same feed is reachable through more than one
page, and when one is gated the other frequently is not. For a browse query the
category page is also simply richer: `/marketplace/albuquerque/free` returned 24
listings where `/search?query=free` returned 15. So category is tried first and
search is the fallback; only when *every* surface is gated does the run fail.

Rate limiting lives inside the adapter so a caller cannot bypass it, and the
defaults (15s between requests, 25 per run) are set for never being throttled
rather than for speed. Both adapters get it from one `Throttled` mixin, so
"the site is gating us" (`SourceBlocked`) stays distinguishable from "we stopped
asking" (`BudgetExhausted`) — trying another surface helps in the first case and
is pointless in the second. Running out of budget mid-enrichment is not fatal:
what was enriched gets judged, the rest stay `new` for the next run. Judging a listing
titled "Free" with no description would waste the one chance to score it.

`fixtures/html/` holds real captured pages — a good search, a throttled search,
and an item page — so the parser is testable with no network. When the adapter
breaks, those tests say whether it was us or Facebook.

## Two lists of picks

The dashboard sorts finds by **why they are there**, not by how sure we are:

- **Wants** (`/`) — matches for something on your list. Unverified matches sit
  here too, flagged, rather than on a list of their own: a 9.0 unconfirmed TV
  stand belongs next to a 9.0 confirmed one.
- **Free finds** (`/free`) — nothing on your list, but worth collecting anyway.

That second list exists because `worth_grabbing` is a **separate axis** from
`match`. Without it the sweep can only ever return things you already thought to
ask for, which defeats the point of trawling free listings at all. It is also
where the first version quietly failed: triage was told to keep anything "worth
much more than it costs", which is vacuously true of everything free, so it
collapsed to "does it match a want" and dropped a working treadmill and a clean
sectional before either was appraised.

## Certainty and quality are separate questions

The model returns `match: yes | no | unknown` alongside `deal_score`, and each
hard requirement comes back individually with its evidence:

```
[yes    ]  9.0  Long low credenza, mid century   $220
    OK at least 70 inches wide   | "six feet long" = 72 inches
    OK not a corner unit         | described as a "long low credenza"

[unknown]  7.0  Tiered ottoman                   $95
    ?  teal or blue in colour    | No colour mentioned; 4 photos not described
    ?  three stacked round tiers | "Tiered" / "Layered design" implies stacking
    needs checking: Colour; whether it has exactly three round tiers
```

That split exists because most sellers do not state dimensions or colour. With a
single score, "I cannot verify this" collapses to about 4.5 — under the threshold,
never surfaced, silently gone. Since that is the *common* case, the feed would
quietly contain only listings that happen to put a number in the title.

`deal_score` is scored as though the unknowns resolve favourably, so one
threshold does all the work and only the match field carries the uncertainty.
An `unknown` is then routed by score like anything else:

- **at or above the bar it stays in Wants**, flagged amber — a 9.0 unconfirmed
  TV stand belongs next to a 9.0 confirmed one, not in a queue of its own;
- **below the bar it lands in Skipped** (`/skipped`), alongside everything else
  that was judged and passed over.

So the two halves of "close, but not quite" live in different places on purpose:
an unverified requirement on a promising listing is a *flag in Wants*, and
anything that missed on value is a *row in Skipped*. The model cannot see the
photos; you can, in about five seconds, which is what the amber flag is asking
you to do.

Hard requirements live in `requires:`, deliberately outside the prose — prose
describes character, the list carries pass/fail, and the evidence strings are how
you audit whether a description is working.

## The model asks for photos when they would help

After the text pass the model returns `needs_images` and an `image_question`. If
it asks, we fetch the photos, downscale them, and hand it a second look with
`Read` scoped to a directory holding only those files.

Letting the model opt in per listing is what makes this affordable, and it maps
onto the wants exactly as you would hope: **the ottoman asks** (colour and tier
count are plainly visible in a photo) and **the TV stand does not** (a photograph
has no reference scale, so a 70-inch minimum stays unknowable however many
pictures there are). No blanket policy gets that right; the model deciding does.

Both scores are kept, so "the photos changed my mind" is visible in the history
rather than overwriting the text judgement. `--no-images` skips the pass, and
`scorer.max_image_checks` caps it per run.

## Three properties worth not breaking

**Re-running costs nothing.** Every stage is idempotent — a second run in the
same minute re-upserts the same listings, appends price observations, gates out
everything unchanged, and makes zero model calls. That is what makes a
15-minute poll interval safe rather than expensive.

**Cold start drains gradually.** A first run against Craigslist's free category
sees ~192 listings. `max_results_per_run` caps the batch at the newest N and
leaves the overflow at status `new`, so the backlog arrives over several runs
instead of as one bill.

**Nothing is deleted.** Rejected listings are kept with their reason; price
observations are append-only. Today's junk is next month's answer to "what does
this actually go for here", and it cannot be backfilled later.

**An empty result is explainable.** Every fetch attempt gets a `runs` row.
"Fetched 200, rejected all on `over_price`" and "fetched 0" mean completely
different things, and only one of them is a broken scraper.

## Cost

Measured, not estimated. Scoring runs on `claude -p` against the Pro
subscription, so the real currency is plan quota, and `total_cost_usd` from the
stream is what the same work would have cost on the API.

- 20 fixture listings across 3 hunts: **$0.058**, ~59s
- Every invocation carries a ~2,500-token Claude Code system prefix, so batched
  triage matters far more than prompt length
- A byte-identical prefix is reused across separate invocations —
  **$0.0078 → $0.0025**, about 3× — which is why the prompt is layered stable-first
  and the dismissal-example block is snapshotted daily rather than rebuilt per run
- An image costs ~1,200 tokens / ~$0.006 at 1024×768, ~$0.0013 at 512px —
  so ~$7/mo to caption an entire free sweep at 512px, and far less in practice
  because only the listings the model asks about get a second look

`--tools ""` is a security decision, not a speed one: the prompt is entirely
stranger-written text, so the model gets no capability at all. The image pass is
the one exception, and a narrow one — `Read` only, `--restricted`, confined to a
scratch directory holding files **we** downloaded. No attacker-controlled URL
ever becomes a model capability. The output is also advisory by construction — a
number in a table a human reads — so the worst a malicious listing can do is be
scored wrongly.

The `stub` backend remains available (`scorer.backend: stub` in config) for
running the pipeline with no network and no quota. It is deliberately not good.

Automated collection is against Meta's ToS. This is a personal hobby tool with
conservative rate limits; rate limiting lives inside the source adapter so it
cannot be bypassed by accident. Read-only — it never messages, offers, or posts.
