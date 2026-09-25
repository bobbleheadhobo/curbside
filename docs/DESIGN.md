# Curbside — design

A personal bot that watches Facebook Marketplace (and later other sources) for free
and underpriced items near me, scores them, and surfaces them on a local dashboard.

Decisions made up front:
- **Own core, pluggable sources.** Not a fork. Techniques borrowed from prior art, not code.
- **Python 3.13**, SQLite, FastAPI dashboard.
- **Dashboard first**, push notifications (ntfy) later behind the same `Notifier` interface.

## 1. Why not just use the MCP server

`jdcodes1/facebook-marketplace-mcp` replays Facebook's internal GraphQL API using cookies
lifted from Chrome. Fast and clean, but: macOS-only (Keychain), 4 commits, and `doc_id`
values rotate on every Facebook deploy. More fundamentally, **MCP is the wrong shape for an
unattended cron job** — it exists so an interactive LLM client can call tools. Our bot is
mostly deterministic code that calls an LLM occasionally.

So: fetching is a **library**. If we later want to sit in Claude Code and ask "what's on
Marketplace right now", we add an MCP face over the same library. That's a 100-line adapter,
not an architecture.

## 2. The actual thesis

Every scraper project on GitHub nails "get listings" and then does something naive —
keyword match, or blast every hit at an LLM. The scraper is the commodity part and it
*will* break. The value is in the layers after it:

1. **History is the moat.** Every listing we ever see goes to SQLite, including ones we
   filter out. After a month we can answer "what does a used X actually go for here" from
   our own data instead of asking a model to guess. Costs nothing to start collecting now,
   impossible to backfill later. **Never delete listings.**
2. **The LLM is a funnel, not a filter.** Deterministic gates (keywords, price cap, radius,
   seen-before, blocklist) cut 95%+ before anything reaches the API. Skip this and we burn
   real money scoring 400 daily listings for "free stuff."
3. **Price drops are signal.** Because observations are append-only, a listing that goes
   $200 → $60, or one that's been sitting three weeks, tells us the seller is motivated.
   No other project here does this.

## 3. Pipeline

```
sources/     fb_logged_out | fb_graphql | craigslist | offerup   → RawListing
normalize/   → Listing{id, title, price_cents, url, posted_at, lat/lng, images, seller}
store/       sqlite: upsert listing, append price observation, mark hunt match
filter/      cheap deterministic gates  ← keeps LLM cost near zero
score/       LLM on survivors only → {deal_score, est_value, condition, red_flags, reasoning}
notify/      dashboard (v1) | ntfy (v2) — same interface, dedupe + rate limit
```

Each stage is independently testable and takes/returns plain dataclasses.

## 4. Schema

```sql
listings(
  id TEXT PRIMARY KEY,            -- "fb:1234567890"
  source TEXT, source_id TEXT,
  title TEXT, description TEXT,
  price_cents INTEGER, currency TEXT DEFAULT 'USD',
  url TEXT, category TEXT,
  city TEXT, lat REAL, lng REAL, distance_mi REAL,
  seller_id TEXT, seller_name TEXT,
  images TEXT,                    -- json array
  posted_at TEXT, first_seen TEXT, last_seen TEXT,
  is_active INTEGER DEFAULT 1,
  raw TEXT                        -- source payload, for re-parsing after schema changes
);

price_observations(listing_id, observed_at, price_cents);   -- append-only

hunt_matches(
  hunt_id TEXT, listing_id TEXT, matched_at TEXT,
  status TEXT,                    -- new|scored|surfaced|dismissed|saved|grabbed|gone
  PRIMARY KEY (hunt_id, listing_id)
);

scores(
  id INTEGER PRIMARY KEY, listing_id TEXT, hunt_id TEXT,
  model TEXT, scored_at TEXT,
  deal_score REAL, est_value_cents INTEGER, condition TEXT,
  reasoning TEXT, red_flags TEXT, -- json array
  input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL
);

runs(id, hunt_id, source, started_at, finished_at, n_fetched, n_new, n_scored, error);
```

`hunt_matches` deliberately separates "we saw this listing" from "it matched hunt X" — one
listing can match several hunts with independent triage state. `raw` means a parser bug or
a new field doesn't cost us the data. `runs` exists so the dashboard can show *why* nothing
showed up today (scraper broken vs. genuinely quiet) — the failure mode every dead repo has.

## 5. Source interface

```python
class Source(Protocol):
    name: str
    def search(self, hunt: Hunt) -> Iterator[RawListing]: ...
```

Adapters own their own rate limiting and return raw payloads; normalization lives outside
them so a broken adapter can't corrupt the store.

**Auth strategy: logged-out, and it works.** (Confirmed 2026-09-08 against live
Facebook: 15 real Albuquerque listings, descriptions and coordinates from item
pages, no credentials.)

**Original reasoning:** `secondhand-mcp` demonstrates the logged-out Marketplace
search page still carries a full first page of results — no credentials, no ban risk, no
headless-Chrome-in-a-container problem. For free/cheap hunting, first page sorted by recency
is most of the value. Escalate to a cookie/GraphQL path only if results prove too thin, and
if we do, never point it at an account that matters.

## 6. Scoring

Two stages, because a broad free sweep cannot gate on keywords -- the whole point
is not knowing what you are looking for -- so something has to cut volume before
the expensive judgement:

```
 60 free listings
  |  TRIAGE   batched ~20 per call, one line of verdict each
  ~6 survivors
  |  APPRAISE one call each, full rubric, value estimate, red flags
  ~2 surfaced
```

**Not the Claude API -- `claude -p` on the Pro subscription.** `Scorer` is a
Protocol, so this is an implementation choice and nothing upstream changes.

```bash
claude -p "<listing payload>" \
  --system-prompt "<rubric + wants + output schema>" \
  --tools "" --strict-mcp-config --disable-slash-commands \
  --model sonnet --output-format stream-json --verbose \
  --no-session-persistence < /dev/null
```

`--verbose` is **required** with `stream-json` under `-p` (no stream without it),
and `< /dev/null` avoids a ~3s stall waiting on stdin. Plan quota is readable
*only* from `rate_limit_event` records in the stream -- there is no API for it --
which is why the single-JSON output format is not enough. Sonnet for both stages.

**`--tools ""` is a security decision.** The prompt is entirely stranger-written
text: listing titles and descriptions. With no tools there is no file access, no
bash, no web, so a malicious listing's best case is a wrong score. The stronger
control is architectural: the scorer's output is advisory by construction -- a
number in a table that ranks things for a human -- and never triggers an action.

**Cost floor, measured on koda 2026-09-08.** Every invocation carries a
~2,500-token Claude Code system prefix billed as cache creation, so a trivial
one-line prompt still cost $0.0101. Batching is therefore not a micro-
optimisation: twenty listings in one call pay that floor once. The 15-minute poll
interval also sits inside the 1-hour cache TTL, so a byte-stable prefix turns
that creation cost into a cache read. Verify with `cache_read_input_tokens`.

**Quota policy.** Curbside and otter share one account, so they share the
five-hour window. Policy is to run freely and pause only on an actual rejection,
resuming at `resetsAt` (falling back to 5h when absent -- never 0, since a falsy
deadline reads as "resume now" and makes the pause a silent no-op). Utilization
is logged from both windows, `five_hour` and `seven_day`, so crowding shows up as
data rather than as a surprise during an incident.

Because fetch and score are separate stages with SQLite between them, a rate
limit **degrades gracefully**: fetching costs no quota and continues, listings
pile up as `new`, and they get scored when the window resets. You lose judgement
for a few hours, never data.

Cold start: v1 leans on the model's own price prior. Once `price_observations`
has a month of data we inject comparables and the estimate stops being a guess.

## 7. Config

One YAML file. A "hunt" is a saved search plus plain-language criteria.

```yaml
location: { lat: 00.0, lng: -00.0, radius_miles: 20 }
defaults: { min_deal_score: 7, max_results_per_run: 60 }

hunts:
  - name: free-stuff
    queries: ["free"]
    max_price: 0
    exclude: ["free estimate", "free delivery"]
    criteria: |
      Anything genuinely useful and worth the drive. Skip junk, broken
      appliances, and listings that are actually services.

  - name: power-tools
    queries: ["dewalt", "milwaukee", "makita"]
    max_price: 100
    exclude: ["broken", "for parts", "repair", "as is"]
    criteria: |
      Cordless tools on the 20V MAX or M18 platform. Bare tools fine.
      A kit with batteries and charger under $100 is a strong buy.
```

`criteria` is what goes into the scoring prompt. Everything above it is a free
deterministic gate.

## 8. Layout

```
curbside/
  config.py      models.py      db.py       pipeline.py     filters.py
  sources/  base.py  fixture.py  facebook.py
  scoring/  base.py  llm.py  heuristics.py
  notify/   base.py  dashboard.py  ntfy.py
  web/      app.py  templates/
  cli.py         # curbside once | run | serve | hunts | backfill
docs/DESIGN.md
data/curbside.db
```

## 9. Build order

- **P0 — skeleton + fixture source.** Config, schema, pipeline, dashboard shell, driven by
  recorded JSON fixtures. Full end-to-end run without touching Facebook once. This is the
  most important phase: it means scraper breakage never blocks pipeline work, and the
  fixtures double as the test suite.
- **P1 — real Facebook source.** Logged-out fetch, normalize, dedupe, price observations.
  Record real responses as new fixtures.
- **P2 — filters + LLM scoring.** Deterministic gates, structured-output scoring, cost
  accounting in `scores`.
- **P3 — dashboard triage.** Browse by score, save/dismiss, price-drop and stale-listing
  flags, run health.
- **P4 — ntfy + scheduling.** `Notifier` implementation, systemd timer or loop.
- **P5 — comparables from our own history; second source adapter.**

## 10. Notes

Automated collection violates Meta's ToS; every project in this space carries that notice.
Personal-scale hobby use with conservative rate limits and a throwaway account is the
normal posture. Rate limiting lives in the source adapter, not the caller, so it can't be
bypassed by accident.
