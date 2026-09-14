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
    stats.html      /stats   what it costs and what each hunt found for it
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

**A label says what the control does; a hint says what it costs.** Neither
explains why the feature exists, which is what these documents are for.

Both halves were got wrong in one day. First the copy explained itself at
length ("Asleep it does nothing at all. No searching, no judging, no checking
whether saved things are still there"). Cutting that left labels like *Wants
bar* and *Judge per run* — internal names, readable only by whoever built it —
and hints stating bare numbers with no idea what they measured. The rationale
was the right thing to cut and the wrong half to cut *first*.

So: **Show a want scoring at least**, not *Wants bar*. **How far you will
drive, in miles**, not *Radius*. Then the count of what it is currently costing.

**A number in copy comes from the data, never typed in.** The sweep row read
"Running every 15 minutes" for a week after the interval was changed to 30, and
the batch-cap hint said "up to 30 judged" from a hardcoded `* 6` that would
have gone wrong the moment a want was added.

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
status still exists and is still accepted, because `filters.TRIAGED` and
`recheck.KEEP_STATUS` both treat it as "already
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

`_card.html` also exports two smaller macros. `_money(cents)` renders `free` or
`$1,234`, and **`price(price_cents, was)`** renders the whole price block —
current price, the old price struck through, and the drop chip. `listing.html`
imports `price` rather than keeping its own version: it used to carry a copy
with `_money` inlined twice and the drop percentage recomputed, so changing how
a free or priced listing reads needed both files and only ever got one. `was` is
the caller's argument because the card and the detail page derive the old price
differently.

**Template context.** Every view gets `bin_counts` — the per-bin totals behind
the nav pips, deduplicated the same way the bins themselves are. There is no
separate `counts` on the bin pages; three of them used to pass one *in addition*
to `bin_counts`, computing the identical three queries twice per page load. The
hunt view has its own `counts`, which is a different thing: per-status totals for
that one hunt.

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

## The page-head stat line has a length budget

Reported from the phone: it wraps. `/runs` read "200 fetch attempts · 36
failed · $9.45 spent · 77 waiting to be judged" — 64 characters, which pushed
the page head onto three lines before a single listing was visible.

**40 rendered characters is the ceiling, and a test measures it** across every
page. Below 430px the figures also shrink a step.

Shortening the wording was not enough on its own, and the screenshot that came
back proved it: 64 characters became 42 and still wrapped on a 360px screen.
**Three labelled figures is what actually fits.** So `/runs` lost one
entirely, and the right one to lose was spend, because `/stats` now owns money
and `/runs` owns health. `runs` stayed as the denominator that makes `failed`
mean anything.

The wording cuts stand on their own merit as well: two labels were restating
their own heading. `/runs` said "fetch attempts" under a heading reading Runs,
and `/skipped` said "judged and passed over" directly above a sentence reading
"Judged, then passed over."

Adding a fourth figure to a page head will fail that test rather than the
phone, which is the point: this line is the first thing on every screen and
the easiest one to quietly overfill.

**The way into `/stats` is a `.btn`, never `.primary`.** Filled accent means
"the action to take now" throughout this interface: resume the sweeps, judge
anyway. A link to another page is not that, however much it wants noticing, so
it takes the soft accent treatment and lets the icon carry the colour.

## `back` is reader-supplied, and looking local is not being local

Triage buttons carry a `back`, and since the detail page takes one as a query
parameter it is now reader-supplied and ends up in a `Location` header.
`_safe_back` is the one place that decides, and `_answer` runs everything
through it.

Rejecting `//host` is not enough. Both holes it had were the same mistake,
judging the string handed over rather than the URL a browser resolves:

* **Tab, newline and carriage return are stripped before parsing**, so
  `/<tab>/evil.test` is not a path starting `/t`. It is `//evil.test`.
* **A backslash is normalised to a forward slash**, so `/\evil.test` is
  `//evil.test` by the time it is resolved.

Strip the first three characters, then treat `/` and `\` as off-site in
second place. A test walks both shapes.

## A day starts where the user is

The daily spend ceiling compared against **UTC midnight**, which in Albuquerque
is 6pm — inside the waking window on every day of the year. Two consequences,
and the second is the worse one:

* `/stats` reported yesterday evening as today. Measured at 10:25 on a Monday
  morning, "Today" read $1.23 and every cent of it had been spent before 6pm
  the previous evening.
* The counter meant to **cap a day's spending reset in the middle of the
  evening**, so a day that had already spent its allowance got a second full
  one for the rest of the night. It never bound in practice — daily spend runs
  $3-7 against a $10 ceiling — but it was a ceiling that could be exceeded by
  design.

`schedule.local_day_start(tz, now)` is the single definition, and both
`ClaudeCodeScorer.check_available` and `/stats` call it. The zone comes from
`schedule.timezone` in `config.yaml`, copied onto `ScorerConfig.timezone` so
the scorer does not become a second place to configure one. With no zone set
it falls back to the machine's local time, not to UTC, like every other
fail-open default here.

**The daily bars are bucketed the same way**, with the offset as it stands now.
In the week around a daylight-saving change an hour of runs can land in the
neighbouring bar; both switches happen at 2am, which is an hour the bot is
generally asleep for.

## Blocking a word works from a card OR the listing page

It lived only on a free-sweep card, so opening a listing to look at it properly
took the control away — which is the wrong way round, since the listing page is
where you go when the card did not tell you enough.

**One implementation serves both.** `app.js` finds its context with
`closest("[data-hunt][data-listing]")` rather than `closest("article.card")`,
and both the card and the panel on the listing page state the same four things
as data attributes: `data-hunt`, `data-listing`, `data-status` and
`data-title`. The title moved onto the card as an attribute for this — the word
suggestions used to be read out of the card's `.info h2`, an element the detail
page does not have.

**A card folds; the listing page leaves.** There is nothing to fold there and
nothing to stay for once the listing is dismissed, so it returns to
`data-back`, the same journey Save and Dismiss make from that page. The undo
travels in `sessionStorage` like theirs, and carries the term as well: undoing
a block has to **unblock the word first**, or the restored listing is filtered
out again on the next run and the undo looks like it worked without having.

**The control is offered only where a sweep matched.** Blocked words are what
stop the free trawl dragging a category back every half hour; a want hunt
searches its own terms instead. A listing matched only by a want gets no block
control, and the one that is shown acts on that sweep's list.

Like the card's, this is JavaScript-only — the `addterm` form has no action and
nothing to fall back to. That was already true of the card and is the one place
in this interface where it is.

## The photo pass has three states, and the page must admit to all of them

Discord's footer says "photos checked" and the listing page carried the same
chip, below the score table and the requirements. Both only ever spoke in the
positive, so **"judged on the text alone" and "asked for a look and never got
one" were the same blank space** — which is how a reader concludes the site
does not say at all.

They are not the same thing. 207 of the collected scores have `needs_images`
set and `images_checked` clear: the model saying it could not settle the
listing without seeing the photographs, and `max_image_checks` saying no. That
state is amber on the page, because it is a caution about how much the
judgement rests on rather than a fault.

`photo_verdict` in `app.py` computes the three, and the line sits at the TOP of
the Scores panel rather than under the table.

**Where it did look, show the score before.** Both rows are kept for exactly
this reason — the text judgement stays next to the one that looked — and it
was never displayed. Every sampled pair moved, and they move in both
directions: one went from 7.0 down to 6.0 when the photographs showed a corner
unit. The "before" is matched on the HUNT as well as the listing, because one
listing can be judged by several and their scores answer different questions.

**`image_question` is shown too.** The model writes down what it wanted the
photographs to answer ("Does the media console appear at least 70 inches wide,
or is there any reference like a TV"), and on a listing nobody looked at, that
question is the exact thing a person can settle in two seconds.

## /stats has no tab, on purpose

Five tabs is what fits across a phone, and the sixth thing was already spent
on the settings gear. `/stats` is read monthly and acted on by rewording a
want or slowing it down, so it is signposted from `/runs` and `/settings`
rather than taking a tab from a page read daily. A test asserts it stays out
of the tab bar.

**It answers a different question from `/runs`.** `/runs` is "is it working
right now" — the pause switches, the backlog, the last hundred fetch attempts.
`/stats` is "is it worth running", which wants months rather than minutes.
Merging them was considered and rejected: one page carrying both horizons
means every number needs a qualifier.

**Every figure comes from `runs`, never from `scores`.** Measured on the fifth
day of live running, `runs.cost_usd` totalled $24.52 and `scores.cost_usd`
totalled $17.10. The gap is the triage pass, which is written to its score row with
`cost_usd = 0` and only ever counted at the run level, so summing score rows
loses about a third of the money. `runs` also carries `hunt_id` and `source`,
so every split on the page is attributable as well as complete. The one place
the page shows the remainder — the "triage" figure under Where it goes — says
`(derived)` next to it.

**The stage split is classified on `scores.model`, whose spellings are set in
two other files.** `"<model>:triage"` comes from `pipeline`, `"<model>+images"`
from `ClaudeCodeScorer.resolve_with_images`, and a bare model name means an
appraisal. Triage is matched explicitly rather than left to fall in with
appraisal: those rows cost 0 today, so lumping them together is harmless right
now and would double-count the day anyone bills them, once under appraisal and
once inside the derived remainder. A test pins the spellings and another
asserts the three figures still add up to what was spent.

**The outcome columns in `spend_by_hunt` are subqueries, not a join.** Joining
`runs` to `hunt_matches` fans the run rows out once per match before `SUM` sees
them, so every cost on the page would be multiplied by however many listings
that hunt matched.

**The Today panel answers the Spend panel.** The figure says how much, the
panel says what on: per hunt, per stage, per source, and the dearest few
listings judged. That last list is the one that catches a single odd listing
eating an afternoon, because an image pass runs about 15x a text appraisal.
Its costs are **appraisal only** and the panel says so: triage is billed per
batch and written to its score row as 0, so a per-listing figure is a floor.

**Three numbers on this page can lie if nobody watches them.** Each has a
test:

* **`n_fetched` counts repeats.** Every run re-reads the whole feed, so it
  read 85,210 index rows against 1,387 listings on file. The funnel says
  "index rows read", not "listings seen", and states the distinct count
  underneath.
* **A young database repeats itself.** With five days of history, "last 7
  days", "last 30 days" and "all time" are one number, and three identical
  figures read as a bug. Any window covering the whole history says "all of
  it so far" instead.
* **"Today" is the user's day, and so is the spend ceiling.** Both call
  `schedule.local_day_start`, so neither can drift from the other or from
  what the word means to the person reading it. It used to be the UTC day on
  both sides, which is 6pm in Albuquerque: read at 10:25 on a Monday morning,
  "Today" covered everything since 6pm on the Sunday. See the section below.

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

**The hours panel says how long the window is, not just its two ends.**
`Schedule.span_label`: "Awake 21 hours a day. This window runs past midnight." Clock times alone hide
a wrapping window: 11pm to 8pm reads like a night shift and is in fact 21 hours
awake, the near-opposite. That was reported as a bug in the code, and the code
was right at every layer — form names, parsing, the save round trip, the wrap
arithmetic. The defect was that the interface described the window in the one
way that concealed what it meant, which is the same class of problem as a pause
switch nobody can see.

Two boundaries are easy to get wrong and are tested: a window that legitimately
wraps (8pm to 6am is 10 hours, not 14), and `start == end`, which is all day
rather than zero.

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

**"Suggest terms" is a button, and it fills the field rather than saving.**
Describing what you want and naming it the way a *seller* would are different
skills, and the second is the one people are bad at — "tv stand" and "media
console" are the same object and share no word. So the model drafts them, but
into the textarea, on a form handed straight back: those terms become two
searches per tick for as long as the want exists, and nobody should be
committed to words they have not read. It is a plain submit button carrying
`action=suggest`, so it needs no JavaScript, and the browser's own `required`
on the description enforces the one input the drafting actually needs. Every
failure — no scorer, quota paused, unparseable output — comes back as the form,
intact, with a note.

**The price cap of 0 is a real answer.** It means free ones only, because
`over_price` drops anything dearer than the cap. It used to be refused as a
mistake, which left "free only" with no way to say it except leaving the search
terms blank — one field quietly controlling two unrelated things. The settings
row says "Free ones only" rather than "Up to $0".

**A want cannot be saved without at least one search term.** Without one it has
no hunt of its own, so only the free sweep sees it — and the sweep searches
"free", not the thing you asked for. That is far less than it looks like, and it
used to be creatable by leaving a box empty. To stop searching for a want you
already have, pause its hunt; that control is on the same page and says what it
does. `config.yaml` can still seed a want with no terms and older ones predate
the rule, so the settings row labels those "no search terms".

**Search terms are pills, not lines of a textarea.** Each term is a whole
separate search, and a textarea does not say that: "tv stand media console"
typed on one line is one bad search that finds nothing and looks identical to
two good ones. `app.js` builds the pill editor over the real `<textarea>`, which
stays the field that posts and is rewritten on every change — so with the script
absent you get a textarea, one term per line, and everything still works. Enter
commits a term rather than submitting the form (submitting a half-filled want is
exactly what it would otherwise do); backspace on an empty box pulls the last
pill back in to edit rather than deleting it outright; and a term still sitting
in the box when you submit is committed rather than lost. The pill is the same
`.term` component as the blocked words on `/settings`, because it is the same
kind of thing.

**The service worker must never cache HTML.** Photos and icons only. Half of
what this shows is gone within the hour.

**The card's action row holds two labelled buttons and nothing else.** Save
and Dismiss, each `flex:1`. Blocking is an icon-only button after them.

**The card has two destinations: the card opens the detail page, the title
opens the marketplace.** The link out used to live only on the listing page, on
the reasoning that you look there before driving anywhere — but the title is
the thing you reach for when you want to see the actual advert, and making that
a second tap was wrong. Links do not nest, so this cannot be one wrapping
anchor: `.card-open` is an overlay stretched across `.card-main`, and the title
sits above it on `z-index` with the `#i-open` icon after it, which is the only
thing saying that one tap leaves the app. It opens in a new tab — on a phone
that is usually the Marketplace or Craigslist app, and navigating away would
lose your place in the bin. `tests/test_web.py` fails if the anchors ever nest.

**Swipe right to save, left to dismiss.** Same directions the buttons sit in,
same colours the verdict badge uses, so the gesture is the buttons rather than
a second vocabulary. It is an **addition**: a gesture is invisible,
undiscoverable and unavailable without a touchscreen, so nothing may ever be
reachable only that way — both buttons stay exactly where they were, and the
same undo toast covers both.

The parts that matter are the ones that fail quietly, and they are tested in
`tests/js/swipe_harness.mjs`:

* `touch-action: pan-y` on the card gives the browser the vertical axis and
  keeps the horizontal, so a swipe never fights the list.
* A drag whose vertical travel dominates is a **scroll** and is abandoned, and
  it cannot become a swipe later in the same drag. Test this with a *diagonal*
  drag — a straight vertical one proves nothing, because its horizontal travel
  never reaches the slop threshold and it would be ignored even with the axis
  check deleted. That mistake was in the first version of the test.
* 72px of travel before it commits, so a short drag springs back. Dismissing is
  not cosmetic — the gate never spends on that listing again — so an accidental
  one is expensive.
* A swipe towards an action the card does not offer barely moves and does
  nothing: `/saved` has only Dismiss, and the hunt views have neither.
* The click that a touch ends in is swallowed once **and then expires**. A
  drag usually suppresses the click by itself, so a listener that simply waits
  for one sits there and eats the *next* real tap on that card instead.
* A drag towards an action the card does not have is still a drag, and must
  still swallow its click — otherwise a dead-direction swipe released over the
  title opens the marketplace.

**Letting go.** A swipe that does not commit springs home on a long easeOut
(`cubic-bezier(.22,1,.36,1)`), which settles rather than stopping dead, and the
hint fades as it goes. A swipe that *does* commit carries on out the way it was
already going while the verdict badge lands over it — yanking the card back to
centre and then folding it from the middle was the whole of the jank.

Two things make that work, and both are easy to get wrong:

* The fling offset is a **class**, not the inline `--sx` variable. Undo restores
  a card by removing `going` and the status class, so a class-based transform
  disappears with them; an inline one would survive and an undone card would
  come back sitting off-screen. There is a test for exactly that.
* `.card.going` has to list `transform` in its `transition`. It sits after the
  swipe rules at equal specificity, so a bare `transition: opacity` wins and
  silently drops the fling's animation — the card jumped instead of leaving.

**The toast can be swiped away too.** It holds an Undo for seven seconds, and
after a run of triage it is the thing in your way -- sideways or downwards, the
two directions that mean "off" given it lives at the bottom of the screen. An
upward drag is damped to a quarter and never reaches the threshold, because
there is nothing up there to go to.

It only ever **dismisses**. The save or the dismissal stays applied, exactly as
when the timer runs out; Undo is the button, and a swipe past it must not press
it, which is the second click-swallow in this file. `--tx`/`--ty` ride along
with the `-50%` centring in *both* the up and the hidden state, so a flicked
toast carries on out from where the finger left it rather than snapping back to
centre first. It takes `touch-action: none` rather than `pan-y`: it is a fixed
bar over the list and owns both axes.

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
