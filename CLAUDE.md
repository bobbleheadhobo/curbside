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
.venv/bin/python -m pytest tests/ -q                # 247 tests, all offline
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

**You have passwordless `sudo` for `systemctl` and `journalctl`**, for the
system manager and the full journal. You will rarely need it, since everything
Curbside runs is a user unit. Nothing else is passwordless.

**Every test must stay offline.** Sources are tested against recorded responses
in `fixtures/` — including a captured *throttled* Facebook page, which is the
failure mode that matters most. Never add a test that hits the network.

## The dashboard owns some of the config now

Three things used to require an ssh session and a service restart, and are set
from `/settings` instead:

* **the waking hours** — outside them a timer tick does *nothing*
* **each hunt's cadence** — `settings` rows keyed `hunt_interval:<hunt_id>`
* **the wants themselves** — a `wants` table, which `config.yaml` **seeds once**

The seed is a one-shot, remembered as `settings['wants.seeded']`. Seeding per
start would revert every edit made on the phone at the next tick, and seeding
per missing name would resurrect a want deleted from the web. After the first
open the table is the truth and the file is history.

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

**Fail open, never closed.** Every filter that cannot decide lets the listing
through. An unrecognised city, an undated listing, a listing whose detail fetch
failed — all pass, or get deferred. Failing closed silently drops the thing the
user wanted; failing open costs one request.

**The scorer runs with `--tools ""`.** The prompt is *entirely* stranger-written
text: titles and descriptions from marketplace sellers. The model gets no
capability at all. The image pass is the one exception and is narrow — `Read`,
`--restricted`, confined to a directory holding files we downloaded. Do not
relax this.

**Never trust model output.** It is coerced, not believed: `"$1,350"` parses,
scores clamp to 0-10, non-scalars never reach a TEXT column, and one bad
response cannot end a batch. A model writing `est_value_usd: "$350"` used to
kill an entire run, fetch included.

**Prompt prefix stability is money.** Caching is prefix-matched, so the prompt
is assembled most-stable-first: rubric, then wants, then a *daily* snapshot of
dismissed titles, then the listing. Rebuilding the dismissal block per run would
cost ~3x on every call forever. `prompts/rubric.md` is read once per process for
the same reason.

## The bug that keeps happening

**Adding a column means four edits, and the fourth gets forgotten.** SCHEMA, the
`_migrate` list, the INSERT, *and* the UPDATE in `upsert_listing` — the last one
with `COALESCE` so a thin index-only refresh cannot wipe enriched data.

This has been missed three times:

* coordinates — every lat/lng from the detail pass was silently discarded, while
  descriptions landed fine, so enrichment *looked* like it worked
* `posted_at` and `category` — every Craigslist listing had no age at all, so no
  "listed 12d ago", no motivated-seller flag, and nothing for the age filter
* nearly `dup_key`

Related: **an index on a migrated column must be created in `_migrate`, not in
SCHEMA.** `executescript` runs before the `ALTER TABLE`, so an index naming a
new column fails on every existing database.

## Other things learned the expensive way

* Facebook throttles **silently** — HTTP 200, a full-size page, no listings. The
  `feed_units` key is the discriminator and its absence must raise.
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
  anything in a bin.

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

## Before you change scoring behaviour

Most tuning is `prompts/rubric.md`, which is prose and needs no code change.
Check there first. If you do change the prompt shape, keep the stable-first
ordering, and remember the skipped view (`/skipped`, renamed from `/near`)
exists so the threshold can be judged rather than guessed at.
