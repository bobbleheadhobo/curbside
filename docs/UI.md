# Working on the dashboard

Read `CLAUDE.md` first for the project as a whole. This is the part you need if
you are changing what it looks like.

## Get a database to work against, first

**Do not develop against `data/dealbot.db`.** A timer writes to it every fifteen
minutes, its bins hold a handful of rows, nothing has ever been saved, and half
the views render empty. You will design against the wrong thing and fight a
moving target.

```bash
.venv/bin/python -m dealbot.cli seed-demo
.venv/bin/python -m dealbot.cli --config config.demo.yaml serve --port 8478
```

That is offline, free, deterministic, and repeatable — fixture source, stub
scorer, notifications off. It runs the *real* pipeline, so hunt views and the
runs table look genuine, then adds the awkward cases you would otherwise wait
weeks to encounter naturally:

| case | why you need it on screen |
|---|---|
| a 140-character title | wrapping, truncation, card height |
| a listing with **no photo** | the placeholder is a real state, not an edge case |
| `$450 → $199` over three weeks | strikethrough, percentage, motivated-seller flag |
| `$120 → free` | the "now FREE" treatment |
| an **unverified** match | amber, requirement `?` marks, "worth checking" |
| requirement evidence | the ✅/❌/❓ block, which can run to five lines |
| red flags | warning chips |
| something already **saved** | `/saved` is empty in production |
| a 30-day-old listing | the "sitting 30d" and motivated-seller chips |

Re-run `seed-demo` any time; it rebuilds from scratch.

## Layout

```
dealbot/web/
  app.py            routes and the SQL behind them
  templates/
    base.html       shell, nav, all CSS, the paused banner
    _card.html      the listing card macro — used by four views
    wants.html      /        matches for the wants list
    free.html       /free    worth grabbing anyway; carries the pause switch
    saved.html      /saved   what you decided to act on
    near.html       /near    judged, but under the bar
    hunt.html       /hunt/<id>  everything one hunt matched, with rejections
    listing.html    /listing/<id>  detail, scores, price sparkline
    runs.html       /runs    every fetch attempt
```

**All CSS lives in `base.html`** as custom properties on `:root`, with a
`prefers-color-scheme` block. Both themes are real; check any change in both.
There is a `viewport` meta and the layout is flex — it is used on a phone.

**`_card.html` is the shared macro.** Four views render through it, so a change
there lands everywhere. It takes `(item, back_url)`; `back_url` is where the
triage buttons return to.

## What a card receives

Every field on `listings` and the latest `scores` row, plus three computed in
SQL. The ones the card actually reads:

| field | notes |
|---|---|
| `id` `title` `url` `source` `city` | `id` is `source:source_id`, e.g. `craigslist:abc` |
| `price_cents` | `0` is **free**; `None` is *no price shown*. Not the same thing |
| `previous_price_cents` | the seller's old price, when they dropped it |
| `priced_at_cents` | what it cost when we last scored it |
| `distance_mi` | straight-line, and ~61% of listings are snapped to a neighbourhood centre |
| `images` | JSON array, already decoded to a list |
| `age_days` `price_moves` | computed; drive the motivated-seller chip |
| `deal_score` `match` `matched_want` | `match` is `yes` / `no` / `unknown` |
| `est_value_cents` `condition` `reasoning` | |
| `unknowns` `requirements` `red_flags` | decoded lists; `requirements` is `[{req, met, evidence}]` |
| `images_checked` | whether the model looked at photos |
| `status` `filter_reason` | `filter_reason` only on hunt views |

Use `/thumb/{id}` for images, never `images[0]` directly — the route serves a
cached local copy and falls back to the source URL. Facebook's URLs expire after
about four days, so a card built on the raw URL rots.

## Things not to break

**There is no authentication, and `/triage` is a POST that mutates state.** It
sits behind a reverse proxy that provides auth. Do not add mutating endpoints
casually, and do not assume the browser is trusted.

**The triage contract**: POST `/triage` with `hunt_id`, `listing_id`, `status`,
optional `note`, and `back`. Status must be one of `saved` / `dismissed` /
`contacted` / `wanted` / `free_find`. **Dismissing is not cosmetic** — dismissed
titles become negative examples in that hunt's next prompt, so the button is
part of how the bot learns.

**`match == "unknown"` is a first-class state**, not an error. It means the
listing plausibly matches but something could not be verified from the text, and
it belongs *next to* confirmed matches rather than hidden — burying it is the
bug the `/near` view was built to expose.

**Paused hunts are announced on every page.** Keep that banner wherever you move
things; a bot switched off and forgotten looks exactly like a broken one.

**Queries are capped at `PAGE_LIMIT`** with a "showing N of M" line. Do not
remove the cap — this table grows forever by design.

## Testing

`tests/test_web.py` uses FastAPI's `TestClient` against a throwaway database;
`_client(tmp_path)` gives you one. Every view must render on an **empty**
database — there is a test for exactly that, because a fresh install shows empty
views first and a crash there is the worst possible first impression.

Keep every test offline.
