# Curbside — orientation for agents

A personal bot that watches Facebook Marketplace and Craigslist around
Albuquerque for free and underpriced things, judges them with Claude, and puts
what survives on a local dashboard and into two Discord channels.

**It is live.** A systemd timer runs it every 15 minutes and it spends real plan
quota. Read "Working on this safely" before running anything.

The shape, in one line:

> Fetch cheaply and often, filter ruthlessly with code, spend the model only on
> what survives, and route by *why* something is interesting rather than by how
> certain we are.

## Where things are

```
dealbot/
  cli.py          entry point and composition root
  config.py       config.yaml -> Hunt objects; secrets come from .env
  models.py       the dataclasses everything passes around
  db.py           SQLite. All schema lives here
  schedule.py     waking hours: when the bot is allowed to run at all
  filters.py      the gate: what is worth spending money on
  pipeline.py     run_hunt() — the whole flow, stage by stage
  sources/        facebook, craigslist, fixture
  scoring/        base (prompts+protocols), claude_code, stub, stream parser
  notify/         dashboard (no-op), discord
  web/            FastAPI dashboard + Jinja templates
  web/static/     icons, the manifest and the service worker (installable)
prompts/rubric.md how listings are judged. Editable, no code change needed
tools/make_icons.py  draws the app icon into every size. SVG and PNG from one
                  geometry, so the favicon and the home-screen tile cannot drift
fixtures/         recorded real responses — the offline test suite
docs/             DESIGN (why), FLOW (features+types), ARCHITECTURE (as built)
systemd/          the deployed units
```

**Changing the dashboard?** Read [`docs/UI.md`](docs/UI.md) — it covers getting
a realistic offline database to develop against (`dealbot seed-demo`), what a
card receives, and what not to break.

Read `docs/ARCHITECTURE.md` for what actually runs. `docs/DESIGN.md` explains
why the shape is what it is, including options that were rejected and why.

## Working on this safely

**`dealbot once` costs real money and real plan quota**, shared with the user's
`otter` incident-triage bot and their own interactive Claude Code use. The
default `config.yaml` points at live sources with the real scorer.

```bash
.venv/bin/python -m dealbot.cli once --no-score     # full pipeline, spends nothing
.venv/bin/python -m dealbot.cli once --dry-run      # fetch + gate, writes nothing
.venv/bin/python -m dealbot.cli notify              # flush alerts, no fetch, no cost
.venv/bin/python -m dealbot.cli recheck            # still for sale? requests, no quota
.venv/bin/python -m pytest tests/ -q                # 356 tests, all offline
```

To exercise the real thing without touching the live database, copy
`config.yaml`, point `db_path` somewhere else and set `scorer.backend: stub`.

### Watching the live service

The deployed units are **user** units, so none of this needs sudo:

```bash
systemctl --user list-timers curbside.timer          # when it next fires
systemctl --user status curbside.service
journalctl --user -u curbside.service -n 50          # what the last pass did
journalctl --user -u curbside-web.service -f
systemctl --user restart curbside-web.service        # after changing web code
```

The dashboard is a long-lived process: **web code changes need that restart.**
The timer does not — each `dealbot once` is a fresh process.

**Restart it yourself when you change web code.** Standing authorisation, given
2026-09-10 — do not stop and ask. It is a five-second bounce of a local read-
mostly service behind a proxy, and leaving it un-restarted means the user is
looking at last week's dashboard while being told the work is done. Check it
came back rather than assuming — and note that `is-active` says `active` the
instant the unit starts, seconds before uvicorn has bound the port, so poll it:

```bash
systemctl --user restart curbside-web.service
for i in 1 2 3 4 5; do curl -sf -o /dev/null localhost:8477/ && break; sleep 1; done
```

This covers restarts. Editing or disabling the units is still worth asking
about.

**You have passwordless `sudo` for `systemctl` and `journalctl`**, for the
system manager and the full journal. You will rarely need it, since everything
Curbside runs is a user unit. Nothing else is passwordless.

**The docs make checkable claims, and `tests/test_docs.py` checks them.** The
type reference in `docs/FLOW.md`, the CLI list, and the test count are all
asserted against the code. Three things in there were false before that test
existed: dismissal learning described as "not yet wired" when it was wired,
"notifications are not built yet" while Discord had been running for days, and
two CLI commands that never existed. A doc that lies is worse than no doc,
because it gets followed.

**Every test must stay offline.** Sources are tested against recorded responses
in `fixtures/` — including a captured *throttled* Facebook page, which is the
failure mode that matters most. Never add a test that hits the network.

## The dashboard owns some of the config now

Three things used to require an ssh session and a service restart, and are set
from `/settings` instead:

* **the waking hours** — outside them a timer tick does *nothing*
* **each hunt's cadence** — `settings` rows keyed `hunt_interval:<hunt_id>`
* **the wants themselves** — a `wants` table, which `config.yaml` **seeds once**
* **the blocked words** — `hunt_exclude:<id>`, also seeded once

Each seed is **per name / per hunt**, not one global flag: something added to
`config.yaml` later still arrives. It cannot resurrect a deletion, because
deleting a want archives the row and a hunt that already has an exclude list is
left alone. The one thing the file can no longer do is append a term to a hunt
that already has a list — it cannot tell a new term from one you deleted. Seeding per
start would revert every edit made on the phone at the next tick, and seeding
per missing name would resurrect a want deleted from the web. After the first
open the table is the truth and the file is history.

**There is deliberately no `seeded` marker row.** The per-name and per-hunt
checks *are* the one-shot, so a marker would be redundant — and two of them
used to be written on every call to `seed_wants`/`seed_excludes`. Since
`with_store` runs **per web request**, that put a WRITE on the read path of
every page: a GET taking a write lock on the file the poller writes, to store a
constant nothing ever read. Do not add one back. Every dashboard read path is
now verifiably write-free.

`Config.hunts` is therefore a **computed property**, not a field. The web
process stays up for weeks; adding a want has to produce its hunt without a
restart. Every CLI command goes through `cli._open()`, and the dashboard
recomputes per request (`app._live()`).

A deleted want is archived, never dropped — its hunt stops, and everything it
ever matched stays readable at `/hunt/want:<name>`.

## Invariants — break these and it fails quietly

**Re-running must cost nothing.** The gate's `unchanged` rule means a second run
in the same minute makes zero model calls. That is what makes a 15-minute poll
interval affordable. Anything that lets already-judged listings back through
turns a cheap loop into an expensive one, and nothing will look broken.

**Fetching is free; judgement is not.** Every pause — quota ceiling, rate limit,
spend ceiling, connectivity — stops the *judging* and lets the *collecting*
continue. Listings pile up as `new` and get judged when the window reopens. You
lose judgement for a few hours, never data.

**The two deliberate exceptions are the pause switches and the waking hours.**
Both stop the fetching as well, and both are *decisions* rather than
interruptions: nothing found at 3am can be collected at 3am. Only `once --due`
is gated by the hours, so a hand-run `dealbot once` always runs.

**Nothing is deleted.** Rejected listings keep their reason; listings keep their
raw source payload. Three separate parser bugs have been repaired from data
already on disk, with no re-fetching. Price observations are append-only.

**One rule fails closed, on purpose: `excluded_kw`.** A blocked listing is
dropped before anything reads it. Because these are now words typed on a phone,
matching is word-start with plurals (`bed` does not catch `bedroom`) and a term
matching one of your wants is refused outright. Everything else here:

**Fail open, never closed.** Every filter that cannot decide lets the listing
through. An unrecognised city, an undated listing, a listing whose detail fetch
failed — all pass, or get deferred. Failing closed silently drops the thing the
user wanted; failing open costs one request.

**A rejection must be recorded, never just dropped.** `store.record_rejections`
is what makes a drop explainable on `/hunt` instead of a listing silently
vanishing, and it is why an empty result is distinguishable from a broken
scraper. The post-enrichment checks all go through `pipeline._drop`, which does
the recording for you — when they were five hand-written copies of that loop,
the one rejecting on distance recorded unguarded and logged nothing at all.

**The dashboard reads; the poller writes.** A GET must not write. Both processes
share one SQLite file by design (WAL, `busy_timeout=10000`), and readers never
block — but a write on a read path can, and did. POSTs write freely; that is
what they are for.

**Cheap models are not automatically cheap here — measure.** `claude -p`
prepends its own ~12k-token harness prompt, so what a call costs is mostly
whether that block is a cache *write* or a cache *read*, and the cache is **per
model**. Drafting a want's search terms on haiku was tried and reverted: it cost
~6x sonnet. The poller runs sonnet every 15 minutes against a 1h TTL, so
sonnet's harness cache is permanently warm, while a second model's would be cold
nearly every time a rare call used it — and haiku spent 672-1,492 output tokens
where sonnet spent 40, thinking and padding around a JSON object rather than
just returning it. Measured: haiku cold $0.0197, haiku warm $0.0050, sonnet warm
$0.0030. **Introducing a second model for an infrequent call is usually a cost
increase**, whatever the price card says.

**The dashboard can spend quota, but only when asked.** `cmd_serve` hands
`create_app` a scorer for exactly one job: the *Suggest terms* button on the
want editor. It is a button rather than something that happens on save because
search terms become two requests per tick for as long as the want exists, so a
person should read them before committing to them — and because nothing should
quietly spend on a form submission. It goes through the same `check_available()`
ceilings and pauses as an appraisal, takes a shorter timeout (someone is waiting
on a form), and **fails open**: every way it can fail ends with the form handed
back intact and no terms. `create_app(cfg)` with no scorer is still a working
dashboard.

**The scorer runs with `--tools ""`.** The prompt is *entirely* stranger-written
text: titles and descriptions from marketplace sellers. The model gets no
capability at all. The image pass is the one exception and is narrow — `Read`,
`--restricted`, confined to a directory holding files we downloaded. Do not
relax this.

**Never trust model output.** It is coerced, not believed: `"$1,350"` parses,
scores clamp to 0-10, non-scalars never reach a TEXT column, and one bad
response cannot end a batch. A model writing `est_value_usd: "$350"` used to
kill an entire run, fetch included. All of that lives in **one** place —
`ClaudeCodeScorer._score_from` — because the text pass and the image pass both
build a `Score` from the same schema, and when they each spelled it out a field
wired into only the first would vanish from image-checked listings. Those are by
design the *high-scoring* ones headed for a bin, so nothing would look broken.

**Prompt prefix stability is money.** Caching is prefix-matched, so the prompt
is assembled most-stable-first: rubric, then wants, then a *daily* snapshot of
dismissed titles, then the listing. Rebuilding the dismissal block per run would
cost ~3x on every call forever. `prompts/rubric.md` is read once per process for
the same reason.

## The bug that keeps happening

**Adding a `listings` column means four edits, and the fourth gets forgotten.**
SCHEMA, the `_migrate` list, the INSERT, *and* the UPDATE in `upsert_listing` —
the last one with `COALESCE` so a thin index-only refresh cannot wipe enriched
data.

This has been missed three times:

* coordinates — every lat/lng from the detail pass was silently discarded, while
  descriptions landed fine, so enrichment *looked* like it worked
* `posted_at` and `category` — every Craigslist listing had no age at all, so no
  "listed 12d ago", no motivated-seller flag, and nothing for the age filter
* nearly `dup_key`

**`runs` no longer has this problem, and how it was fixed is the pattern to
copy.** That table had *five* copies — SCHEMA, `_migrate`, a hand-typed column
whitelist in `finish_run`, `RunResult`, and the kwargs at each call site. Now
`finish_run` asks the table (`PRAGMA table_info`, cached per connection) and the
pipeline hands it `**asdict(result)`, so adding a counter is SCHEMA + `_migrate`
+ the `RunResult` field and nothing else. The whitelist had already gone stale:
`n_worth_a_look` was carried through four of the five places and never assigned
anywhere, and `n_deferred` was omitted from two of the three `finish_run` calls,
so a `--no-score` pass or a quota pause recorded a backlog of zero — which is
precisely the number that counter exists to make visible.

**When a value has to be stated in more than one place, make one place derive
from the other.** Where that is not practical, make the drift a test failure;
`tests/test_docs.py` is that idea applied to the docs.

Related: **an index on a migrated column must be created in `_migrate`, not in
SCHEMA.** `executescript` runs before the `ALTER TABLE`, so an index naming a
new column fails on every existing database. `ix_listings_dup_key` is there for
exactly that reason. `ix_matches_listing` and `ix_matches_bin` name original
columns and so would have been legal in SCHEMA, but they sit in `_migrate` with
it: every index in one place is a rule you cannot get wrong, and an existing
database picks them up on its next open either way.

**Indexes are not optional here, because nothing is ever deleted.** The bin
views filter on `status` ALONE, with no `hunt_id`, so the composite
`ix_matches_status(hunt_id, status)` never applied to them; and the
one-row-per-listing subquery in `ONE_BIN` matches on `listing_id`, the *second*
column of the primary key. Every bin page therefore scanned `hunt_matches` and
sorted, once per candidate row — quadratic in a table that only grows. Adding a
query that filters on a new column means adding its index too.

## Other things learned the expensive way

* Facebook throttles **silently** — HTTP 200, a full-size page, no listings. The
  `feed_units` key is the discriminator and its absence must raise. **The item
  page needs the same guard** (`marketplace_product_details` /
  `marketplace_listing_title`): without it a throttled detail fetch reads as
  "this listing is gone", and the re-check pass retired saved listings out of
  every bin, permanently. A missing Facebook payload is now `unknown`, never
  `removed` — that source states `is_sold` and `is_live` when it answers at all.
* `Sec-Fetch-*` headers are mandatory on Facebook or you get a bodyless 400.
* Craigslist's search feed omits descriptions *and* the posting date; both only
  arrive from the item endpoint, which wants the **uuid** (field 13), not the
  numeric posting id.
* `claude -p` needs `--verbose` with `stream-json` or there is no stream at all,
  and `< /dev/null` or it stalls ~3s per launch.
* Plan quota is readable *only* from `rate_limit_event` records in the stream.
  There is no API for it. `subtype` says `"success"` even when `is_error` is
  true — check `is_error` and `terminal_reason`.
* A `claude -p` launched with no network does not fail fast; it burns ~10
  minutes of retry backoff. Hence the connectivity preflight.
* Facebook image URLs expire in ~4 days. Thumbnails are cached locally for
  anything in a bin. That download and the vision pass's are the SAME
  function — `images.fetch_downscaled` — so the hardening (size cap enforced
  while reading, content-type check, timeout, re-encode through Pillow) has one
  implementation rather than two kept in step by hand.
* **`str.replace` on a SQL string is a silent no-op when the needle misses.**
  The bin queries were built by replacing text in each other; editing the
  literal `"WHERE m.status = ?"` would have produced a perfectly valid query
  against the wrong rows, with nothing raised. They come from `_queue_sql(where)`
  now. Never build SQL by substring surgery on another query.

## Secrets

`.env` (gitignored, mode 600) holds the Discord webhooks, the user's home
coordinates and their Discord user id. `config.yaml` is committed and must stay
shareable — never put a secret in it, and never echo `.env` values into a
transcript.

## Deliberately not done

Read-only: no messaging sellers, no offers, no posting. That boundary is what
keeps this a personal tool.

Relist detection is **inert** — neither source exposes a seller id, so the
fingerprint falls back to the listing's own id. That is deliberate: title+price
matching produced false merges (four different "Curb alert" posts collapsing
into one). Inert beats confidently wrong.

Comparables from the bot's own price history need about a month of observations.
That is why the timer matters more than the polish — the data accrues with
wall-clock time, not with effort.

## Adding a source adapter

Implement `search`, `parse`, and optionally `detail` (see `sources/base.py`),
and **inherit `Throttled`**. It gives you `_init_budget`, `reset_budget`,
`_await_slot` and `_spend_slot` — the jittered interval and the per-pass request
cap. Rate limiting still lives *inside* the adapter, which is the invariant that
matters; it is simply no longer retyped per source.

Both existing adapters used to carry their own copy, and the copies had drifted
in the way that mattered: Facebook raised `BudgetExhausted` when it ran out of
*our* budget, Craigslist raised a plain `SourceBlocked`. That distinction is
load-bearing — `facebook.search` tries another surface when the *site* gates it
and gives up when our own budget is spent — and Craigslist could not express it.
`BudgetExhausted` subclasses `SourceBlocked`, so catching the base still works.

## Before you change scoring behaviour

Most tuning is `prompts/rubric.md`, which is prose and needs no code change.
Check there first. **It has a twin**: `base.RUBRIC` is the fallback when the
file is missing, and `load_rubric` only *logs* that substitution, at info — so
anything safety-relevant belongs in both, and `tests/test_docs.py` checks the
one that is (the advance-fee scam section).

**Judge a listing by the combination, not by a keyword.** The scam guidance is
the worked example: "offers delivery for a fee" reads like a clean signal and is
not one — six legitimate priced listings in the collected data say it, including
a $250 console with $250 delivery. What is diagnostic is valuable + free or far
too cheap + delivery offered + usually "like new". A keyword rule here would
have buried real listings and still missed the case that started it, whose
description never says "fee" at all. If you do change the prompt shape, keep the stable-first
ordering, and remember the skipped view (`/skipped`, renamed from `/near`)
exists so the threshold can be judged rather than guessed at.
