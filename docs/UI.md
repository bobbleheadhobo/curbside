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

**Restart the server after touching Python. Jinja will lie to you.** Templates
are re-read on every request, but uvicorn does not reload `app.py`. A
long-running `dealbot serve` therefore shows your new templates running against
the *old* module: context variables silently render empty and new routes 404,
while the page looks broadly right. This bit twice in one session, once on the
demo server and once on production, where `/skipped` had been 404ing from the
nav for hours. `systemctl --user restart curbside-web` after any Python change.

## Layout

```
dealbot/web/
  app.py            routes and the SQL behind them
  templates/
    base.html       shell, nav, all CSS, the icon sprite, the paused banner
    _card.html      the listing card macro — used by four views
    wants.html      /        matches for the wants list
    free.html       /free    worth grabbing anyway
    saved.html      /saved   what you decided to act on
    skipped.html    /skipped judged, then passed over (/near redirects here)
    hunt.html       /hunt/<id>  everything one hunt matched, with rejections
    listing.html    /listing/<id>  detail, scores, price sparkline
    error.html      404 / 400 / 500, in the normal shell
    runs.html       /runs    the Searching panel, every fetch attempt, the hunts
    settings.html   /settings  waking hours, sweep cadence, the wants list
    want_form.html  /wants/new and /wants/<name>  add or edit one want
  static/
    app.css         ALL the CSS. Two themes, one file, no build step
    app.js          the interaction layer. Progressive enhancement only
    sw.js           service worker; caches assets and photos, never a page
    icons, manifest.webmanifest, offline.html
```

`static/` is generated except for `sw.js`, `offline.html` and the manifest —
run `tools/make_icons.py` rather than editing an icon by hand.

**Each section owns a hue.** `<body data-page="free">` selects `--tint` /
`--tint-soft`, spent on the current tab, its count pip, and the live numbers in
the page head — never on a button, which stays `--accent`, and never on
anything semantic. Every hue is declared three times (light, system dark,
forced dark) and a test asserts it; a light-only hue silently outranks the dark
`body` rule on specificity, so a half-built colour looks fine until dusk.

**Say "your call", not "triage".** The word appeared once, as the heading on
the listing page, and the user did not recognise it. `/triage` stays as the
endpoint and the internal noun.

**One listing, one bin.** `ONE_BIN` in `app.py` is appended to every bin query
*and* to the counts behind them. Keep those two together: the counts drifting
from the cards is how "12 waiting" ends up over ten of them.

**All CSS lives in `static/app.css`** as custom properties on `:root`, with a
`prefers-color-scheme` block *and* a `[data-theme=dark]` block carrying the
same tokens.

It lived inside `base.html` until that template reached 1112 lines — 693 of
them CSS and 302 JavaScript, against 92 of actual markup. The JavaScript was
the worse half: inline in a Jinja template nothing could lint it, syntax-check
it or diff it sensibly, and two of its bugs were found only by reading. Both
are plain static files now — still no build step, still one file each — and
`tests/test_web.py` runs `node --check` over the script.

Both are linked with `?v={{ assets }}`, the newest mtime under `static/`. That
one number busts three caches together: the browser's, the service worker's own
name, and therefore everything the worker holds cache-first. Without it a
regenerated icon or a CSS fix never reaches a phone with the app installed. Both themes are real; check any change in both. To screenshot
dark, temporarily add `data-theme="dark"` to the `<html>` tag — headless
Chrome has no reliable flag for the media query.

**A hint states a fact, not a rationale.** The settings copy drifted into
explaining *why* each control exists, which is what these documents are for.
"Asleep it does nothing at all" earns its place; the list of what "nothing"
covers does not. Where a number appears in copy it comes from the data, never
typed in: the sweep row said "Running every 15 minutes" for a week after the
interval was changed to 30.

**Every page head is a live stat line, not prose.** `h1` carries the name
alone; under it `.stats` states what is actually waiting — counts in tabular
figures, the one that needs a decision tinted amber (`.stats .hi`). The
explanatory sentence sits below that and stays short. A paragraph of teaching
copy belongs in the empty state, where it is read, not above a list that is
opened twenty times a day.

**`/near` is `/skipped`.** "Near" read as "near me", which is the one thing it
never meant. The route, the template, the nav label and the icon all changed;
`/near` 308-redirects and is covered by a test so an old phone bookmark still
lands.

**Triage buttons must name a real status.** `/triage` accepts exactly
`saved` / `dismissed` / `contacted` / `wanted` / `free_find`. The listing page
used to offer a **Surface** button posting `surfaced`, which is not on that
list, so it posted, redirected and changed nothing. If you add a button here,
add the status to the endpoint in the same change. The panel carries a
`.triage-help` line explaining what each button does, Dismiss in particular,
because it teaches the hunt and that is not guessable from the word.

**The UI offers Save and Dismiss only.** `contacted` is no longer surfaced
anywhere: the user does not want to track whether they messaged a seller. The
status still exists and is still accepted, because `filters.TRIAGED`,
`recheck.KEEP_STATUS` and `db.TERMINAL_TRIAGE` all treat it as "already
triaged, do not spend money judging this again", and `SAVED_SQL` still matches
it so any row already carrying it stays visible. Do not add the button back.

**Stranger-written text must never overflow.** `body` sets
`overflow-wrap:anywhere`. Titles, descriptions, red flags and want names all
come from sellers or from the model, and one unbroken 200-character token used
to run out of the card, off the page, and through the score. Chips wrap rather
than truncate: a chip is part of the reason a listing is on screen, so cutting
it hides the reason.

**Never fabricate a number to fill a slot.** `distance_mi` is often unknown, and
`'%.0f'|format(i.distance_mi or 0)` rendered **"0 mi"**, which told you the item
was at your front door. Print nothing instead. The same rule applies to any
field the sources leave empty.

**Colour is checked by arithmetic, not by eye.** Every foreground/background
token pair meets WCAG AA (4.5:1) in *both* themes; `--faint` was 2.78:1 and was
carrying real data, including every score under 5.0. If you change a colour
token, recompute the ratios rather than judging by eye, and remember `--faint`
is text, not decoration.

**Touch targets come from `--tap` (44px).** Anything tappable below 560px meets
it; the card's triage buttons, the disclosure row and the tab bar all do. Link
chips get at least 26px. Compacting a row may take its padding, never its
targets.

**Reduced motion is not a blanket kill.** The block disables real movement only.
Colour and background transitions stay, because they are the only feedback a tap
gets on a server-rendered page and removing them makes the UI feel broken rather
than calm.

**Errors render in the normal shell.** `error.html` plus three handlers in
`app.py` cover 404, a bad query string (400) and an unhandled exception (500).
The nav and the paused banner keep working, so a wrong URL is a wrong turn
rather than a dead end. `_error_page` falls back to plain text if the database
is itself the problem, and the 500 handler logs the exception rather than
swallowing it.

**Copy rule: short, and no em dashes.** Page descriptions are one sentence,
two at most. Long teaching copy belongs in the empty state, where it is read.
Use a colon, a full stop or brackets where you reach for an em dash. The only
surviving `&mdash;` is the "no value" marker in table cells, which is a data
convention rather than prose.

**Mobile is the design target, not the adaptation.** The primary scene is a
phone in a spare moment, away from a desk. Navigation is a fixed bottom tab
bar under 900px and moves into the top bar above it; the per-hunt views are
reached from the Hunts panel on `/runs`, not from the tab bar.

**The card is progressive disclosure by design.** Photo, price and place get
the whole top row because they are the first gate; the model's judgement —
reasoning, requirements, unknowns — lives in a `<details>` whose summary says
what is inside ("Why it scored 8.4 · 3 things to check"). Nothing that
explains a decision is hidden, but nothing pushes the next listing off the
screen either. Do not un-fold it by default and do not drop fields into it
without adding them to the summary count.

**Icons are drawn, never typed.** `base.html` carries an inline SVG sprite
(`#i-wants`, `#i-check`, `#i-alert`, …) at 24×24 with a 1.75 stroke. Use
`<svg class="i"><use href="#i-name"/></svg>`. No emoji and no Unicode glyphs
standing in for icons — ✓/✗/? in the requirements list are `#i-check`,
`#i-x` and `#i-help`.

**A photo that fails to load is a real state, not an edge case.** Facebook's
image URLs expire after about four days, so the card renders the "photo
expired" placeholder *underneath* the `<img>` and the image hides itself on
`onerror`. `.shot[hidden]` needs its own rule — `.shot` sets `display:block`,
which outranks the UA `[hidden]` stylesheet.

**Wide tables stack on a phone.** `<table class="responsive">` with
`data-label` on every `<td>` turns into label/value pairs in a two-column
grid under 720px; `.lead` and `.span2` span the full width and `.empty-note`
disappears. Runs and the listing's scores table both use it — the scores
table's reasoning column used to sit off the right edge behind a scrollbar.

**`_card.html` is the shared macro.** Four views render through it, so a change
there lands everywhere. It takes `(item, back_url, actions, show_status)`;
`back_url` is where the triage buttons return to, `actions` are the triage
statuses offered inline (`['saved','dismissed']`, except `/saved` which offers
`['dismissed']` on `/saved`), and `show_status` is off everywhere but the hunt
views — the bins are named after their status, so repeating it on every row is
noise.

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

**A listing with no description wears an amber `no description` chip.** It is
a caution, not a fault: those listings score *better* than the ones with words,
and the badge says only that every judgement on the card rests on the
photographs. A listing with no *photograph* never reaches a card at all.

**`match == "unknown"` is a first-class state**, not an error. It means the
listing plausibly matches but something could not be verified from the text.
It is routed by score exactly like `yes`: over `min_deal_score` it sits in Wants
flagged amber, next to confirmed matches rather than hidden; under it, it stays
`scored` and appears in `/skipped`. Do not give it a bin of its own, and do not
let it be filtered out of Wants: a 9.0 unconfirmed listing is worth the five
seconds it takes to look at the photos.

**Judging has its own row on `/runs`, separate from the two pause switches.**
Every other pause here stops the fetching as well; the quota guards stop only
the spending, and the health pill says "Judging paused" and links here. The row
states what is stopping it and offers the override.

**The pause switches live on `/runs`.** Both of them: *Pause free-stuff
searches* (`kind=sweep`) and *Pause all searching* (`kind=all`). They used to
sit on `/free`, which was the wrong page. `/runs` is where you go to ask
whether the bot is working, so it is where you answer it. `.control-row` is the
same component as `.triage-row`; the row was generalised rather than copied.

**The health pill is the only announcement of a pause, and it is on every
page.** There used to be a banner as well; it said what the pill says, took a
block of every screen to say it, and pushed the first listing below the fold.

`_health()` picks one label from several simultaneously-true facts, and the
ORDER is the design. Most of the time the bot is asleep *and* paused *and*
quiet; picking the wrong one is how the pill starts lying, which it did —
reporting "Asleep till 12pm" over a bot with every hunt switched off.

```
1  every hunt off   → "Paused"          nothing runs, so nothing else explains it
2  last run errored → "Fetch failing"
3  some hunts off   → "2 hunts off"     indefinite; only you undo it
4  no runs at all   → "No runs yet"
5  outside hours    → "Asleep till 12pm"  self-resolving, so it yields to a pause
6  just woken       → "Just woke"       the last run is as old as the night
7  over an hour     → "Quiet 3h ago"
8  otherwise        → "12m ago"
```

The `title` carries what the label could not, and the pill links to `/runs`,
which is where the switch to undo any of it lives.

**`tests/test_web.py` asserts on markup.** The strikethrough treatment is
found by the class name `strike`, the sweep switch by the words "pause
free-stuff searches", and the empty-view check by `"count num">0`. Rename
those and the tests tell you.

**`health()` is a pure function** taking the latest `runs` row, the paused
hunts, all hunts and the schedule. It is out at module level so every branch of
its precedence ladder is unit-testable — it has been wrong twice, and a ladder
you can only exercise through HTTP is one nobody checks all of.

**Asleep is a state the interface has to admit to.** The health pill reports
it (`Asleep till 12pm`) ahead of "quiet" and behind "Fetch failing", and the
first half hour after waking is never a warning. Without that, eight hours of
deliberate silence looks exactly like a scraper that died on Tuesday, which is
the one thing this dashboard exists to rule out.

**The mark in the top bar is `/static/icon.svg`**, the same file the browser
tab and the installed home-screen tile use. Do not inline a copy of it into
`base.html`; regenerate with `tools/make_icons.py` instead, or the three drift.
It is the only solid block of accent in the interface.

**`/` carries the add button for wants** (`.addbtn`, a `+` beside the title,
44px because it is a phone). Settings has one too, but reaching it only through
the gear reads as configuration; `/` is the page you are on when you think "I
should look for one of those". The empty state links there as well.

**Settings is a gear in the top bar, not a sixth tab.** Five tabs is what fits
across a phone. Hours and wants are set once a month; the bins are skimmed
daily. If you add another destination, it goes in the top bar too.

**A want's name is frozen once it exists.** It is the hunt id, the URL of that
hunt's view, and the key every score and triage decision is filed under.
The edit form does not offer it, and `/wants/save` ignores a `name` field when
`existing` is set. Renaming would orphan the lot, silently.

**The form keeps what was typed when it rejects something.** A description is a
paragraph of prose written on a phone; losing it to a mistyped price cap would
be unforgivable. `_want_form` re-renders with `values`, and there is a test.

**The service worker must never cache HTML.** Photos and icons only. Half of
what this shows is gone within the hour.

**The card's action row holds two labelled buttons and nothing else.** Save
and Dismiss, each `flex:1`. Blocking is an icon-only button after them, and
the link out to the marketplace was removed entirely — it lives on the listing
page, which is one tap away and where you look before driving anywhere.

Three labelled buttons do not fit a phone: `body{overflow-wrap:anywhere}` is
there for stranger-written titles and it will happily break *Dismiss* one
letter per line. `.actions button` carries `white-space:nowrap` so that failure
mode is loud (overflow) rather than silent (a column of letters).

**Nothing that saves should throw you back to the top of the page.** Every
mutating form outside the card is marked `data-inplace`, and every endpoint
behind one answers **204 or JSON to a fetch and 303 to a form post** (see
`_answer` in `app.py`). With the script absent they all still work, they just
reload.

Rather than patching the DOM by hand for each — a toggle changes the health
pill, a blocked word changes a count, saving hours changes a sentence — the
handler re-fetches the current page and swaps in the affected panel, plus
`.health`. The server stays the single source of truth for every label, and
the scroll position never moves. `data-inplace` takes an optional selector and
defaults to the closest `.panel`; `data-toast` adds a confirmation, and
`data-refocus` names an input to clear and re-focus afterwards.

`tests/test_excludes.py` asserts both halves: every listed endpoint answers a
fetch *and* a form post, and every `data-inplace` form in every template posts
to one of them. A form marked `data-inplace` whose endpoint still redirected
would fetch the redirect, download a page, and appear to do nothing.

**The two forms that SHOULD navigate are on the want editor**: saving a want
and removing the want you are editing both end that page's job.

**Triage is a progressive enhancement, and must stay one.** Every Save and
Dismiss is a real `<form method=post action="/triage">`. A script intercepts
the submit, posts it with `X-Requested-With: fetch`, gets a 204 instead of a
303, shows which button you pressed on the card, folds the card away and
offers an undo. With JavaScript off, every button still works -- it just
reloads and drops you at the top of the list, which is what made going through
twenty listings miserable.

The script finds things by selector, so a rename in a template breaks the
interaction *silently* (the buttons keep working, they just reload again).
`tests/test_excludes.py::test_the_script_and_the_markup_still_agree` asserts
the contract in both directions; keep it honest rather than deleting it.

**Undo matters more than the animation.** Dismissing is not cosmetic -- the
gate never spends on that listing again -- so a mis-tap on a phone was
unrecoverable. The card carries `data-status` for exactly this: undo writes
back the status the card actually had. `/triage` accepts `scored` only so that
a card on `/skipped` can be put back.

**Counts are decremented client-side after an action.** "12 waiting" over
eleven cards reads as a bug. `[data-bincount]` in the page head and the `.pip`
on the current tab are the two places.

**The blocked-word list has its own panel**, `#blocked` on `/settings`, and
the head of `/free` links to it with a count. It was a field at the bottom of
the sweep panel and nobody found it — which is the test that matters for a
list whose entire job is being auditable.

**"Never show" is only on `/free`.** A blocked word on a want hunt would block
the thing you are hunting -- the guard in `/settings/exclude` refuses a term
that matches any want's name or queries, but the affordance is absent there
anyway. The chips are candidate words from the listing's own title, computed in
the browser; typed entry covers the rest.

**Queries are capped at `PAGE_LIMIT`** with a "showing N of M" line. Do not
remove the cap — this table grows forever by design.

## Testing

`tests/test_web.py` uses FastAPI's `TestClient` against a throwaway database;
`_client(tmp_path)` gives you one. Every view must render on an **empty**
database — there is a test for exactly that, because a fresh install shows empty
views first and a crash there is the worst possible first impression.

Keep every test offline.

## When you change the look

`DESIGN.md` at the repo root records the shipped design system: tokens, the type
ramp, the spacing rhythm, components and their states, the icon scale, and the
named rules. `.impeccable/design.json` is its machine-readable sidecar. **They
are generated from the built code, not written by hand** — if you change a token
or a component, regenerate them (`/impeccable document`) rather than editing
them, or the two will disagree.

`PRODUCT.md` holds product truth and the standing design preference: the
category standard played straight, with Apple's structure and Stripe's
discipline. That preference was a decision, not a default. Read it before
proposing a new look.
