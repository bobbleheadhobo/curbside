# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One user: the person who built and runs it. A hobbyist deal-hunter in
Albuquerque, technical, running the bot on their own hardware behind a
reverse proxy. There is no second audience — no sharing, no accounts, no
onboarding for strangers.

**Confirmed primary scene: a phone.** The dashboard is checked in spare
moments, most often away from a desk. Desktop is the secondary adaptation,
not the design target.

## Product Purpose

Curbside watches Facebook Marketplace and Craigslist around Albuquerque for
free and underpriced things, judges each survivor with Claude against a
written rubric, and puts what survives in front of one person so they can
decide whether it is worth driving to.

Success is a decision made in seconds without regret: the good thing gets
seen before it is gone, and the junk gets dismissed without being read.

## Positioning

Not a scraper and not an alert feed. Three things make it different and all
three must stay legible in the interface:

1. **It keeps everything, forever.** Rejected listings keep their reason;
   price observations are append-only. The history is the moat.
2. **It explains itself.** Every surfaced listing carries a model judgement:
   a score, reasoning, met/unmet requirements, unknowns, red flags.
3. **It shows its own health.** The runs view distinguishes "nothing good
   posted today" from "the scraper broke last Tuesday". An unexplained empty
   result is a tool you stop opening.

## Operating Context

A systemd timer runs the pipeline every 15 minutes and it spends real Claude
plan quota shared with the user's other tools. The dashboard is read-mostly
FastAPI + Jinja over the same SQLite file the pipeline writes; the only
writes are triage actions.

**Confirmed daily job:** skim new arrivals for anything worth driving to.
Price and photo are the first gate; the model's reasoning is read only once
those two look good. That ordering is the core interaction to design for —
judgement is progressive disclosure, not front-matter.

Live scale (2026-09-09): 790 listings, 126 runs, and per bin —
6 wanted, 21 free finds, 184 scored, 164 filtered, 241 new, 180 gone.
Bins are small; the archive is not. `PAGE_LIMIT` caps every view at 200.

## Capabilities and Constraints

- **Four bins are fixed destinations** and the user has confirmed they stay
  with their current meanings: `/` wants, `/free` free finds, `/saved`,
  `/skipped` judged and passed over (renamed from `/near`, which read as
  "near me"; the old URL redirects). Plus per-hunt views, a listing detail page, and runs.
- **`/settings` exists, added 2026-09-10 at the user's request.** It carries the
  waking hours, each hunt's cadence, and the wants list with add / edit /
  remove. It is reached by a gear in the top bar rather than a sixth tab: five
  is what fits across a phone, and this is set once a month, not skimmed daily.
- **The bot sleeps.** Requested window noon to 8pm, editable on that page.
  Asleep it does nothing at all — no fetching, no judging, no re-checks. That
  must be legible from any page or eight hours of deliberate silence is
  indistinguishable from a dead scraper.
- **Wants are edited on the site, not in the file.** `config.yaml` seeds the
  table once; after that the database owns them. A removed want is archived,
  never deleted, and its hunt view keeps working.
- **Installable.** Manifest, icons and a service worker, so it sits on the
  Android home screen. The worker caches photos and icons and **never a page** —
  half of what this finds is gone within the hour.
- **Read-only toward the world.** No messaging sellers, no offers, no
  posting. Triage is the only mutation: POST `/triage` with `hunt_id`,
  `listing_id`, `status` in saved/dismissed/contacted/wanted/free_find,
  optional `note`, and `back`. **The UI offers only Save and Dismiss.**
  Confirmed 2026-09-09: the user does not want to track whether they messaged
  a seller, so `contacted` is never offered, though the pipeline still honours
  it as an already-triaged state.
- **Dismissing is not cosmetic.** Dismissed titles become negative examples
  in that hunt's next prompt. The button is part of how the bot learns.
- **`match == "unknown"` is a first-class state**, not an error, and belongs
  beside confirmed matches rather than hidden.
- **Paused hunts must be announced on every page.** A bot switched off and
  forgotten looks exactly like a broken one.
- **Images go through `/thumb/{id}`**, never the source URL — Facebook's
  image URLs expire in about four days.
- `price_cents == 0` is *free*; `None` is *no price shown*. Not the same.
- No authentication in the app itself; auth is the reverse proxy's job. Do
  not add mutating endpoints casually.
- Server-rendered Jinja, no build step, no framework. Everything ships in
  templates the FastAPI app already serves.
- Every test is offline (`tests/test_web.py`), and every view must render on
  an empty database.

## Brand Commitments

The name **Curbside** is fixed. No logo and no wordmark **in the interface** —
the top bar says "Curbside" in the same face as everything else.

**An app icon exists as of 2026-09-10**, because the user asked to install it on
an Android home screen and the alternative was a grey default square. It is the
thing the product is named after: a box at the edge of a curb where the pavement
steps down, white on the accent blue, two shapes and no detail that dies at
32px. It is drawn by `tools/make_icons.py`, which emits the SVG and every PNG
from one geometry.

**It sits in the top bar beside the name as of 2026-09-10**, at the user's
request — the same `/static/icon.svg` the tab and the installed tile use, so
the three cannot drift. It is a mark, not a logo or a wordmark: the name is
still set in the interface face beside it. It is also the one solid block of
accent in the interface, which is the documented exception to "accent is never
decoration".

**Standing preference, confirmed 2026-09-09: the category standard, played
straight.** Offered a rolled visual direction (highway guide signage) and
three challengers, the user took the standing exit. Convention is the
commitment here, and future work executes it at full fidelity rather than
smuggling in a point of view.

The craft bar the user named is **Apple's structure with Stripe's
discipline**: grouped inset lists, thumb-sized rows and a bottom tab bar,
carrying tabular figures, hairlines over shadow, one restrained accent and
semantic colour used only for state. Not a compromise between two looks —
Apple decides the layout because the surface is a phone, Stripe decides the
typography because the content is data.

## Evidence on Hand

- `dealbot seed-demo` builds a realistic offline database (`config.demo.yaml`)
  covering the awkward states: a 140-character title, a listing with no
  photo, `$450 → $199` over three weeks, `$120 → free`, an unverified match,
  requirement evidence, red flags, something already saved, a 30-day-old
  listing. This is the development target; `data/dealbot.db` is not.
- `fixtures/` holds recorded real source responses, including a throttled
  Facebook page.
- Real model output is on disk: scores, reasoning, requirements, unknowns.
  Nothing about the judgement needs to be invented.

## Product Principles

1. **The photo and the price decide; the words confirm.** Design for a
   glance first and a read second, in that order.
2. **Nothing is deleted and nothing is hidden that explains a decision.**
   Rejection reasons, unknowns and red flags stay reachable.
3. **The tool must look alive.** Pauses, quiet days and breakage are
   different things and the interface must say which.
4. **Judgement is advisory.** A score is a number that ranks things for a
   human; it never triggers an action and must never look like it did.
5. **One user, one phone, no ceremony.** No onboarding, no features that exist
   to be shown to someone else. This ruled out settings screens until
   2026-09-10, when the user asked for the hours and the wants list to be
   editable from the phone. The rule it becomes: a setting earns a screen only
   when the alternative is an ssh session.

## Accessibility & Inclusion

No user-specific requirement established. Both a light and a dark theme are
real and in use, and both must stay correct — the dashboard is opened
outdoors in daylight and at night.
