---
name: Curbside
description: A phone-first marketplace triage dashboard — grouped inset lists on a tinted ground, a hue per page for where you are, one blue for what you can do, semantic colour only for state.
colors:
  bg: "#f4f4f6"
  card: "#ffffff"
  raised: "#ffffff"
  fg: "#101014"
  dim: "#5f5f6a"
  faint: "#6a6a75"
  line: "#e5e5ea"
  line-soft: "#eeeef2"
  accent: "#2b64d6"
  accent-soft: "#eaf0fe"
  on-accent: "#ffffff"
  good: "#0f7040"
  good-soft: "#e6f4ec"
  warn: "#8a5300"
  warn-soft: "#fdf1de"
  bad: "#b52424"
  bad-soft: "#fdeceb"
  wants-hue: "#2b64d6"
  wants-hue-soft: "#eaf0fe"
  free-hue: "#0d7a4a"
  free-hue-soft: "#e4f5ec"
  saved-hue: "#6b3fb5"
  saved-hue-soft: "#f0eafc"
  skipped-hue: "#a3316f"
  skipped-hue-soft: "#fbe9f2"
  runs-hue: "#0f6d78"
  runs-hue-soft: "#e2f2f4"
  settings-hue: "#8a5300"
  settings-hue-soft: "#fdf1de"
typography:
  page-title:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI Variable Text', 'Segoe UI', system-ui, Roboto, 'Helvetica Neue', Arial, sans-serif"
    fontSize: "30px"
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: "-0.03em"
  page-title-desktop:
    fontSize: "34px"
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: "-0.03em"
  stat-value:
    fontSize: "15px"
    fontWeight: 640
    letterSpacing: "-0.01em"
    fontFeature: "tnum 1"
  stat-label:
    fontSize: "13.5px"
    fontWeight: 450
    lineHeight: 1.5
  brand:
    fontSize: "16px"
    fontWeight: 650
    letterSpacing: "-0.015em"
  figure:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI Variable Text', 'Segoe UI', system-ui, Roboto, 'Helvetica Neue', Arial, sans-serif"
    fontSize: "17px"
    fontWeight: 680
    lineHeight: 1
    letterSpacing: "-0.02em"
    fontFeature: "tnum 1"
  row-title:
    fontSize: "15px"
    fontWeight: 600
    lineHeight: 1.32
    letterSpacing: "-0.012em"
  body:
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  prose:
    fontSize: "14.5px"
    fontWeight: 400
    lineHeight: 1.65
  secondary:
    fontSize: "13.5px"
    fontWeight: 400
    lineHeight: 1.55
  meta:
    fontSize: "12.5px"
    fontWeight: 400
    lineHeight: 1.5
  chip:
    fontSize: "11.5px"
    fontWeight: 550
    lineHeight: 1.45
  section-label:
    fontSize: "12px"
    fontWeight: 620
    letterSpacing: "0.04em"
    textTransform: "uppercase"
  table-header:
    fontSize: "11px"
    fontWeight: 550
    letterSpacing: "0.03em"
    textTransform: "uppercase"
  tab-label:
    fontSize: "11px"
    fontWeight: 550
    letterSpacing: "0.005em"
  score-unit:
    fontSize: "11px"
    fontWeight: 600
    letterSpacing: "0.03em"
rounded:
  focus: "6px"
  chip: "6px"
  sm: "8px"
  thumb: "10px"
  image: "12px"
  md: "14px"
  pill: "99px"
spacing:
  xs: "4px"
  sm: "6px"
  gutter: "8px"
  stack: "10px"
  card: "12px"
  row: "13px 15px"
  page: "16px"
  block: "18px"
  section: "22px"
  tap: "44px"
  icon-chip: "12px"
  icon-inline: "15px"
  icon-notice: "17px"
  icon-tab: "22px"
  icon-empty: "30px"
components:
  button:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.fg}"
    rounded: "{rounded.sm}"
    padding: "0 13px"
    height: "34px"
    typography: "{typography.secondary}"
  button-hover:
    backgroundColor: "{colors.line-soft}"
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    rounded: "{rounded.sm}"
    padding: "0 13px"
    height: "34px"
  button-triage:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.fg}"
    rounded: "{rounded.sm}"
    height: "{spacing.tap}"
    width: "100%"
  stats:
    textColor: "{colors.dim}"
    typography: "{typography.stat-label}"
  stats-value:
    textColor: "{colors.fg}"
    typography: "{typography.stat-value}"
  stats-value-attention:
    textColor: "{colors.warn}"
  button-triage-row:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.fg}"
    rounded: "{rounded.sm}"
    height: "{spacing.tap}"
    width: "100%"
  triage-help:
    backgroundColor: "{colors.bg}"
    textColor: "{colors.dim}"
    padding: "13px 15px"
    typography: "{typography.meta}"
  chip:
    backgroundColor: "{colors.line-soft}"
    textColor: "{colors.dim}"
    rounded: "{rounded.chip}"
    padding: "2px 7px"
    width: "max-width: 100%"
    typography: "{typography.chip}"
  chip-link:
    backgroundColor: "{colors.line-soft}"
    textColor: "{colors.dim}"
    rounded: "{rounded.chip}"
    padding: "8px 10px"
    height: "32px"
  chip-unknown:
    backgroundColor: "{colors.warn-soft}"
    textColor: "{colors.warn}"
  chip-flag:
    backgroundColor: "{colors.bad-soft}"
    textColor: "{colors.bad}"
    padding: "3px 7px"
  chip-selected:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
  card:
    backgroundColor: "{colors.card}"
    textColor: "{colors.fg}"
    rounded: "{rounded.md}"
    padding: "12px"
  panel:
    backgroundColor: "{colors.card}"
    rounded: "{rounded.md}"
    padding: "0"
  health-pill:
    backgroundColor: "{colors.card}"
    textColor: "{colors.dim}"
    rounded: "{rounded.pill}"
    padding: "0 11px"
    height: "30px"
  tab-item:
    backgroundColor: "{colors.card}"
    textColor: "{colors.faint}"
    height: "56px"
    typography: "{typography.tab-label}"
  tab-item-current:
    textColor: "{colors.wants-hue}"
  notice:
    backgroundColor: "{colors.warn-soft}"
    textColor: "{colors.warn}"
    rounded: "{rounded.md}"
    padding: "13px 15px"
  status-lead-warn:
    backgroundColor: "{colors.warn-soft}"
    textColor: "{colors.warn}"
    padding: "16px 15px"
  status-lead-ok:
    backgroundColor: "{colors.good-soft}"
    textColor: "{colors.good}"
    padding: "16px 15px"
  status-lead-bad:
    backgroundColor: "{colors.bad-soft}"
    textColor: "{colors.bad}"
    padding: "16px 15px"
  period-chip:
    backgroundColor: "{colors.card}"
    textColor: "{colors.dim}"
    rounded: "{rounded.pill}"
    padding: "0 13px"
    height: "36px"
  period-chip-current:
    backgroundColor: "{colors.wants-hue-soft}"
    textColor: "{colors.wants-hue}"
  field-input:
    backgroundColor: "{colors.card}"
    textColor: "{colors.fg}"
    rounded: "{rounded.sm}"
    height: "{spacing.tap}"
  button-field-save:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.fg}"
    rounded: "{rounded.sm}"
    height: "{spacing.tap}"
    padding: "0 16px"
---

# Design System: Curbside

## Overview

**Creative North Star: "The Standard, Played Straight"**

Curbside is a phone held in a spare moment, deciding in two seconds whether a
thing is worth driving across town for. The system does not have a point of
view about itself — it spends all of its character on legibility and none on
personality. That is the commitment, not a compromise: the category standard
executed at full fidelity. Apple decides the layout because the surface is a
phone (a tinted ground, inset white cards, thumb-sized rows, a fixed
five-tab bar at the bottom). Stripe decides the typography because the content
is data (the platform system sans, tabular figures everywhere a number can be
compared, hairlines instead of shadows, one restrained blue).

Density is medium and honest. A card is a 128px photo, a title clamped to two
lines, a price, a place, a row of state chips and a score — everything the
first gate needs, and nothing else. The page head above it is built the same
way: a large plain title and a line of live facts about what is waiting, not a
paragraph explaining what the view is. The model's judgement is real content but
it is read second, so it folds behind a disclosure whose summary counts what
is inside. Colour carries no decoration at all, and each colour has one job:
a page's own hue says where you are, blue says what you can do, green/amber/red
mean a state the data is in, and every other surface is grey.

Both themes are real and equally weighted — this is opened outdoors in daylight
and in bed at night. Every colour is a custom property with a
`prefers-color-scheme` override and a matching `[data-theme=dark]` block, and
neither theme is a filter over the other.

**Key Characteristics:**
- Grouped inset cards on a tinted ground; the ground is never white
- A hue per page for location (the current tab, its pip, the head's figures); one blue (`#2b64d6` / `#6f9dff`) for action and selection
- Semantic green/amber/red used exclusively for state, never for emphasis
- Tabular figures on every comparable number
- Hairlines (1px `--line`) do the structural work; shadow is a whisper
- Drawn 24×24 SVG icons at 1.75 stroke; no emoji, no glyph stand-ins
- Phone-first: bottom tab bar under 900px, top-bar nav above it
- Page heads report state, not identity: a title and a live stats line

## Colors

A near-neutral grey system with two chromatic jobs kept apart — a hue per page
that says where you are, and one blue that says what you can do — plus three
semantic signals that are only ever allowed to describe a state.

### Primary
- **Marketplace Blue** (`#2b64d6` light / `#6f9dff` dark): the action colour,
  darkened from its first value so it clears AA against the card surface. It
  fills the primary button, colours in-prose links, draws the focus ring and a
  focused field's border, and fills the current filter chip. Nothing
  decorative is ever blue.
- **Blue Wash** (`#eaf0fe` light / `#1a2237` dark): the quiet form of the
  accent, behind the "Stats and spend" signpost and the add button.

### Secondary (page hues)
Each destination owns a hue, declared as `--tint` and `--tint-soft` on
`body[data-page=…]` in both themes. It marks location only: the current bottom
tab and its count pip, the current desktop nav item (hue on its soft wash), the
figures in the page head's stats line, and the current Stats period.
- **Wants Blue** (`#2b64d6` / `#6f9dff`, wash `#eaf0fe`): the same value as the
  accent. On Wants, location and action share a colour.
- **Free Green** (`#0d7a4a` / `#4bd08a`, wash `#e4f5ec`): the thing is free.
- **Saved Violet** (`#6b3fb5` / `#b79bff`, wash `#f0eafc`): it is yours.
- **Skipped Rose** (`#a3316f` / `#e882bd`, wash `#fbe9f2`): the pile you walked
  past. It was slate until 2026-09-24, and its active tab could not be told from
  the grey inactive ones.
- **Runs Teal** (`#0f6d78` / `#57c7d4`, wash `#e2f2f4`): machinery, not
  merchandise.
- **Settings Amber** (`#8a5300` / `#e8b155`, wash `#fdf1de`): the same value
  as Attention Amber below. On Settings a page figure and a caution therefore
  look alike. The overlap is known and unresolved; do not copy it to a new page.

### Tertiary (semantic state)
- **Confirmation Green** (`#0f7040` light / `#4bd08a` dark): "free" prices, a
  met requirement, a high score (≥7), a falling price sparkline, a healthy dot.
- **Attention Amber** (`#8a5300` light / `#e8b155` dark): the unverified match,
  unknowns that need checking, a mid score (5–7), a paused or quiet bot.
- **Problem Red** (`#b52424` light / `#f08b84` dark): red flags, failed
  requirements, failing fetch rows. Each ships with a `-soft` companion
  (`#e6f4ec` / `#fdf1de` / `#fdeceb`) used as the fill behind the text colour.

### Neutral
- **Tinted Ground** (`#f4f4f6` light / `#0f1012` dark): the page and the
  sticky top bar. Cards sit on it; it is what makes the list read as grouped.
- **Card White** (`#ffffff` light / `#17181b` dark) and **Raised**
  (`#ffffff` light / `#1d1e22` dark): card and panel bodies; `raised` separates
  the action footer from the card body — identical in light, distinct in dark.
- **Ink** (`#101014` / `#edeef1`): all primary text.
- **Dim** (`#5f5f6a` / `#9b9daa`): secondary text — place, meta, table headers,
  disclosure summaries.
- **Faint** (`#6a6a75` / `#8b8d99`): tertiary — the `/10` beside a score,
  struck-through old prices, "no photo", "no price", and every score under 5.0.
  Darker than a placeholder grey looks like it should be, because all of that
  is data a person reads.
- **Hairline** (`#e5e5ea` / `#26272c`) and **Soft Hairline**
  (`#eeeef2` / `#1f2024`): every border and divider. The soft one divides
  *inside* a card or panel; the hard one bounds it.

### Named Rules
**The Where-Versus-What Rule.** A page's hue says where you are; blue says
what you can do. The hue marks the current tab, its pip, the head's figures and
the current period, and never an action. Blue marks the primary button, links,
the focus ring and the current filter chip, and never a page. On Wants the two
happen to share a value; everywhere else they must not be swapped.

**The Hue-Is-Not-Grey Rule.** A page hue has to be a hue, because the inactive
tabs beside it are grey. Slate at saturation 0.17 made the Skipped tab read as
one more inactive icon. `tests/test_web.py` fails any page hue under 0.35
saturation.

**The State-Only Rule.** Green, amber and red describe a fact about the data —
free, confirmed, unverified, flagged, failing. They are never used to make
something look important. Green means confirmed: a score of 7 or better, the
word FREE, a want the model confirmed, a price that dropped, and "Working" on
the status card. The same want chip turns amber when the match is unverified.

**The Faint-Is-Text Rule.** `--faint` carries real content — the `/10` unit,
struck-through prices, "no photo", every score under 5.0 — so it is held to a
text contrast ratio, not a decoration one. Every foreground/background pair in
both themes clears WCAG AA, verified by computing the ratio rather than judging
it by eye. The palette drifted to 2.78:1 once by looking fine, and to 4.42:1 on
`--line-soft` a second time, which is why `tests/test_web.py` now computes
`--dim` and `--faint` against every surface token in both themes straight from
the stylesheet.

**The Two Real Themes Rule.** Every colour is a custom property with both a
`prefers-color-scheme` and a `[data-theme=dark]` value. No hardcoded hex may
enter a template, and any change is checked in both themes before it ships.

**The Derive-Don't-Declare Rule.** A pressed or hovered variant of a token is
mixed from that token, never declared as a new literal. The primary button
darkens with `color-mix(in srgb, var(--accent) 88%, #000)`; `#000` there is a
mixing operand, not a palette entry, and the result stays correct in both
themes because the accent it derives from differs per theme. A separate
`--accent-strong` would be a second value to keep in sync for one state, so the
mix is the system's way to shade a token.

## Typography

**Body Font:** the platform system sans — `-apple-system`,
`BlinkMacSystemFont`, `Segoe UI Variable Text`, `Segoe UI`, `system-ui`,
`Roboto`, `Helvetica Neue`, `Arial`. There is no second family, no webfont, and
no monospace.

**Character:** Invisible on purpose. The type has no voice of its own; it does
its work through weight, tightened tracking on large text, and tabular figures.
Weights are taken from the variable range rather than the classic steps — 550,
620, 650, 680 all appear, so headings read as firm rather than heavy.

### Hierarchy
- **Page Title** (700, 30px → 34px at 900px, 1.15, `-0.03em`): one per page, in
  `.pagehead`, plain and unaccompanied — the count that used to sit beside it
  moved into the stats line below.
- **Stat Value / Stat Label** (640, 15px, `-0.01em`, tabular / 450, 13.5px):
  the paired figures and words of the page-head stats line.
- **Wordmark** (650, 16px, `-0.015em`): "Curbside" in the top bar. The only
  place this step appears.
- **Figure** (680, 17px, `-0.02em`, tabular): the current price and the deal
  score. The two numbers that decide, set at the same size and weight so they
  read as a pair.
- **Row Title** (600, 15px, 1.32, `-0.012em`): the listing title in a card,
  clamped to two lines.
- **Body** (400, 15px, 1.5): the document default.
- **Prose** (400, 14.5px, 1.65, max 68ch): seller descriptions on the detail
  page, preserving source whitespace.
- **Secondary** (400–550, 13.5px, 1.55, max 62ch): page-head explanations,
  buttons, notices, disclosure bodies.
- **Meta** (400, 12.5px, 1.5): distance, city, age, disclosure summaries.
- **Chip** (550, 11.5px, 1.45): all state chips, and the plan meters' captions.
- **Section Label** (620, 12px, `+0.04em`, uppercase): detail-page section
  headings.
- **Table Header** (550, 11px, `+0.03em`, uppercase): column headers, and the
  `data-label` pseudo-element that replaces them on a phone.
- **Tab Label** (550, 11px, `+0.005em`): the five bottom tabs, and — at the
  same size, 600 weight, `+0.03em` — the `/10` unit beside a deal score. Both
  were 10.5px until 2026-09-24.

### Icon Sizes

Icons are `1em` square, so `font-size` on `svg.i` *is* the icon size control and
is not a step on the type ramp. Seven sizes are in use, each chosen against the
text it sits beside: **12px** in a chip, **15px** in a button, a requirement or
flag row, the unknowns callout and a hunts-list row, **17px** in a notice and
the empty-photo placeholder, **22px** in a bottom tab, and **30px** in an empty
state. Five steps, no near-duplicates. Stroke weight moves inversely — 2.0–2.1
at 12–15px so small icons do not go grey, 1.4–1.5 at 17–30px so large ones stay
quiet.

### Named Rules
**The 11px Floor Rule.** Nothing a person reads is set under 11px: not a tab
label, not a count pip, not the `/10` unit, not a placeholder. The smallest
steps on the ramp are Table Header and Tab Label at exactly 11px.

**The Icons-Are-Sized-Not-Typeset Rule.** An `svg.i` `font-size` is an icon
size, never a new type step. Pick from the icon scale to match the adjacent
text; do not introduce a new step to split a difference.

**The Tabular Figures Rule.** Any number a person might compare down a column
or across a state change carries `.num` (`font-variant-numeric: tabular-nums`).
Prices, scores, distances, counts, timestamps, pips. Prose numbers do not.

**The Uppercase-Is-For-Labels Rule.** Uppercase with positive tracking marks a
label about content — table headers, section headings, the word "free". It is
never applied to content itself and never to a heading a person reads as a
sentence.

**The Two-Line Clamp Rule.** Listing titles clamp at two lines, as does a red
flag on the card — the flag is shown in full inside the disclosure, so a warning
is shortened but never silently cut. A 140-character seller title must not
change the height of its row.

**The Stranger-Text Rule.** Every string on screen — titles, descriptions, want
names, red flags, the model's own sentences — was written by a marketplace
seller or by the model, and none of it is length-checked or space-checked.
`body { overflow-wrap: anywhere }` is therefore global: one unbroken
200-character token must break rather than run out of the card, off the page or
through the score. Any new container inherits this and must not opt out.

## Layout

**The model is a single centred column of grouped cards.** `main` is capped at
720px (1100px with `.wide`, used by Runs, All runs and Stats) and padded 20px/16px,
rising to 28px/24px above 900px. Cards stack with a 10px gap; nothing is ever
in a multi-column grid.

**One breakpoint decides the shell (900px)** and two smaller ones tune
components. Under 900px, navigation is a fixed five-column bottom tab bar
(56px rows plus `env(safe-area-inset-bottom)`) and `body` carries
`padding-bottom: calc(64px + safe-area)`; at 900px the tab bar disappears,
the nav moves into the sticky top bar, and the body padding drops to zero.
At 720px and below wide tables restack (see the responsive-table pattern).
At 560px and up the card thumbnail grows 128px → 148px, the gallery grows
220px → 300px, and the card's action buttons stop being full-width.

**Safe areas are respected on all four edges**: `viewport-fit=cover`, top-bar
padding from `safe-area-inset-top`, tab-bar padding from
`safe-area-inset-bottom`.

**The spacing rhythm is a fine one, not a strict 8px grid.** The recurring
values are 4/6/8/10/12/16/18/22 with a 13px×15px inset for list rows and
panel headers, which is the Apple grouped-row inset rather than an arbitrary
value. Card interiors are 12px; the disclosure summary is 7px×12px; the action
footer is 8px×12px; block elements clear 16–18px; detail sections clear 22px.
One value in the scale is not rhythm but reach: `--tap: 44px` is the minimum
touch target, and the card's inline triage controls take their height and their
icon-button width straight from it.

### Named Rules
**The Head-Reports-State Rule.** A page head states what is *waiting*, not what
the view *is*: a plain title, then a stats line of live counts, then at most one
or two lines of explanation. Teaching copy belongs in the empty state, where a
person has time to read it. If a fact in the head does not change between
visits, it does not belong there.

**The Thumb-Reach Rule.** Primary navigation lives at the bottom of the screen
under 900px, and every triage control sits inside the card it acts on. Nothing
a person taps repeatedly is placed in the top bar.

**The 44px Minimum Rule.** `--tap: 44px` is the floor for a tappable control on
a phone, and controls reference the token rather than a literal. The card's
Save, Dismiss and open-on-marketplace buttons, every triage- and control-row
button, and the card's disclosure summary are all `var(--tap)` tall; the open
button is `var(--tap)` wide. Compacting a row may take its padding, never its
targets: at 560px and up, where the surface is a cursor, they relax to 36px.
One exception is named rather than assumed — a filter chip is an inline element
in a wrapping row of chips, not a control in a control row, and sits at 32px on
`8px 10px` padding. It is the only target under the floor, and adding a second
requires the same kind of argument.

**The Every-Target-Has-A-Height Rule.** Anything tappable gets a deliberate
minimum height, never whatever its text happens to give it. A 12.5px summary
line and a 11.5px chip label are the two places this is easiest to forget, and
both were once sized by their text alone. Measure a new target before shipping
it; a height that arrived by accident is not a height.

**The Mobile-Is-The-Target Rule.** The phone layout is the design; the ≥900px
layout is the adaptation. A new component is drawn at 390px first, and it may
grow at 560/900px — never the reverse.

## Elevation & Depth

**This system is essentially flat and gets its depth from tone and hairlines.**
A card is separated from the page by being lighter than the tinted ground and
by a 1px `--line` border. Shadow exists but is deliberately below the threshold
of notice: it grounds the card against the tint rather than lifting it.

### Shadow Vocabulary
- **Resting** (`0 1px 2px rgba(16,16,20,.06), 0 1px 1px rgba(16,16,20,.04)`;
  dark: `0 1px 2px rgba(0,0,0,.5), 0 1px 1px rgba(0,0,0,.35)`): every card,
  panel and empty state. The only shadow actually in use.
- **Lift** (`0 4px 12px rgba(16,16,20,.08), 0 1px 2px rgba(16,16,20,.06)`;
  dark: `0 6px 18px rgba(0,0,0,.55), 0 1px 2px rgba(0,0,0,.4)`): defined as the
  system's one step up, reserved for a genuinely floating surface.

### Named Rules
**The Hairline-Over-Shadow Rule.** If a boundary needs to be visible, draw a
1px line. Shadow is never the thing that communicates a boundary; if the border
were removed and the card became hard to find, the fix is the border, not a
bigger shadow.

**The Feedback-Is-Tonal Rule.** Hover and press change background tone
(`--line-soft`, then `--line`), never elevation, never scale, never translate.
Transitions are 0.15s on background/border/colour and 0.2s on the disclosure
chevron rotation.

**The Colour-Is-Not-Motion Rule.** `prefers-reduced-motion: reduce` is not a
blanket transition kill. It disables animation and the two real movements — the
chevron's rotation and the card's press transform — and leaves colour and
background transitions running, because on a server-rendered page with no
client JS they are the only feedback a tap gets. Removing them makes the
interface feel broken rather than calm. Anything a future rule adds under
reduced motion must be actual movement.

## Shapes

Soft rectangles at three scales, plus one pill. Containers — cards, panels,
notices, empty states — use a generous 14px radius (`--r`). Interactive
controls and inner blocks use 8px (`--r-sm`): buttons, the desktop nav item,
the unknowns callout. Chips are tighter at 6px, as is the focus ring; card thumbnails
10px; gallery images 12px. Fully round (99px) is kept for status and for a
choice of view: the health pill, the tab count pip, the status card's tone dot
and the Stats period switch. Roundness never means "button".

Everything is bordered rather than borderless: `1px solid var(--line)` on
containers, `var(--line-soft)` on internal dividers. The one dashed border in
the system is the empty-photo placeholder — dashed says "absent", solid says
"present". Icons are 24×24 line drawings sized in `em`, `stroke-width: 1.75`
with round caps and joins; the stroke thickens to 2–2.1 only when an icon
shrinks below ~13px so it does not go grey. The sprite is not additive by default: `#i-empty` was
deleted once nothing referenced it. Three definitions are deliberately kept
unreferenced as reserves rather than rules — `#i-near` (the bullseye the
descending arrow `#i-skipped` replaced when `/near` became `/skipped`) and
`--shadow-lift`. A reserve is recorded here; anything not recorded here is
deleted when it goes unused.

## Components

### Page Head Stats
- **Character:** The head of every list view. A plain title, then a single wrapping line of live facts about what is in the view right now.
- **Structure:** `<p class="stats">` of `<i>` groups, each a tabular value in `.stats b` (15px/640 ink) followed by its word at 13.5px/450 dim — "11 waiting", "10 need checking", "2 free", "showing top 40". Groups are separated by a hairline-coloured middot drawn as `.stats i + i::before`, so the separator belongs to the item that follows it and wraps with that item instead of stranding at the end of a line. Baseline-aligned, wrapping with a 2px/10px gap.
- **Attention:** `.stats .hi b` tints its value amber — the count that is asking for a decision, never more than one per line.
- **Below it:** the explanatory paragraph survives at 13.5px/1.55 dim, capped at 62ch and one or two lines. Anything longer belongs in the empty state.

### Buttons
- **Shape:** Softly rounded (8px), 1px bordered, 34px minimum height, 13.5px/550 label with an optional 15px leading icon.
- **Default:** Raised surface on a hairline border, ink text. Hover fills with the soft hairline and darkens the border to faint; press fills with the hard hairline.
- **Primary:** Solid accent, on-accent text, accent border. Hover mixes the accent 88% with black. Reserved for the one action that resumes, restores or saves something, and **one per card or panel**: on the status card only the lead fact's action is filled, and a Limits field's Save is a default button.
- **Disabled:** 45% opacity, pointer events off.
- **In a card footer** the triage buttons go full-width at the 44px `--tap` minimum on a phone, and revert to auto-width 36px at 560px.

### Chips
- **Style:** 6px radius, 11.5px/550, soft-hairline fill with dim text by default, with a 12px icon at 2.0 stroke.
- **Wrapping:** `white-space: normal`, `max-width: 100%`, left-aligned. A chip wraps rather than clips, because a want name or a filter reason is part of *why* the listing is on screen and truncating it hides the reason.
- **Semantic variants:** a confirmed want and a price drop are green on good-soft, an unverified want is amber on warn-soft, and a red flag is red on bad-soft. Everything else is the neutral fill.
- **Red flag:** a sentence the model wrote, so it gets `align-items: flex-start`, 1.4 line-height and `3px 7px` padding, with its text clamped to two lines on the card and shown in full in the disclosure.
- **Filter chips** are links, so they are tap targets: `8px 10px` padding and a 32px minimum height — the system's one named exception to the 44px floor, because a chip is inline in a wrapping row rather than a control in a control row. They hover to a firmer grey, and the current one (`aria-current=true`) inverts to solid accent. Their labels are plain words, never a database value: "Too far", "Blocked: couch", "Judged", not `too_far_by_city` or `scored`.

### Cards / Containers
- **Corner Style:** 14px, with `overflow: hidden` so the three internal bands clip to the corner.
- **Structure:** three bands — a tappable main row, an optional disclosure, and an action footer on the raised surface. The title is an `<h2>` (`.info h2`) so the page's heading order runs h1 → h2 without a skipped level. They are divided by soft hairlines and bounded by a hard one.
- **Background:** card white on the tinted ground; the whole main row tints to soft hairline on hover and on `:active`.
- **Shadow:** resting only (see Elevation).
- **Internal Padding:** 12px, 13px gap between photo and text column.
- **Actions by state:** a card offers only what its state allows. Save and Dismiss while undecided; Dismiss alone once saved; **Put back** (undo glyph) once dismissed, returning it to the list its score earns; nothing once gone or rejected. A button that would do nothing is not rendered.
- **Photo:** 128px square (148px ≥560px), 10px radius, `object-fit: cover`. The "no photo / photo expired" placeholder is rendered *underneath* the image and the image hides itself on error, so an expired URL degrades to a labelled dashed state rather than a broken-image glyph.

### Navigation
- **Destinations:** Wants, Free, Saved, Skipped, Runs. Each has a drawn icon; the label is the view's own word, not a category name.
- **Bottom tab bar (<900px):** fixed, five equal columns, card surface over a hairline top border, 56px rows, 22px icon over an 11px label. Faint by default; the current tab (`aria-current=page`) turns the page's own hue — colour is the only affordance, there is no pill or underline, which is why the hue must be a real hue. A count pip (page-hue fill, card-coloured text, 99px, 17px tall, 11px/700) rides the icon's top-right.
- **Top bar:** sticky, on the page ground rather than the card surface, 52px plus the top safe area, with the wordmark at 16px/650. Above 900px the same five destinations appear as 32px text-and-icon items that hover to soft hairline and take the page hue on its wash when current.
- **Health pill:** always at the right of the top bar, a 30px bordered pill with a 7px status dot — green (ok), amber (paused/quiet), red (fetch failing), faint (no runs yet) — linking to `/runs`, whose status card finishes its sentence.

### Control Row / Triage Row
- **Character:** A settings-like control row inside a panel. `.control-row` is the general form — the pause switches in Settings' Running panel, the want editor's hunt controls — and `.triage-row` is the same component carrying a listing's triage actions; they share every rule.
- **Action width:** a triage row's forms are `min-width: 110px`; a control row's are `min-width: 180px`, because a switch is labelled with a sentence ("Pause free-stuff searches") rather than a verb.
- **Layout:** The label (`.who`, 13.5px/600 with a 12px dim sub-line) takes its own full-width line, and `.triage-actions` sits under it as a wrapping group where every form is `flex: 1 1 0` with a `min-width: 110px`, so buttons share the width evenly instead of breaking two-and-two. Buttons are full-width at `var(--tap)`.
- **≥560px:** the label returns to sharing the row (`flex: 1 1 auto`), the action group shrinks to its content, and the buttons relax to auto-width and 36px.
- **Help text:** `.triage-help` is a separate block below the row on the page ground (`--bg`) behind a soft top hairline, 12.5px/1.6 dim with ink bold run-ins. Explanation is never squeezed into the row itself.

### Tables
- **Style:** 13px, full-bleed inside a 14px panel, soft-hairline row rules, no rule after the last row. Headers are sticky, uppercase 11px/550 dim on the page ground. Numeric cells are right-aligned and tabular. A failed row fills bad-soft and carries a 1px inset accent bar of red on its first cell.
- **Responsive pattern:** `<table class="responsive">` with a `data-label` on every `<td>`. Below 720px the head is hidden, each row becomes a 2-column grid of label/value pairs — the label drawn from `data-label` as an uppercase micro-caption — with `.lead` as a bold full-width title line, `.span2`/`.wide` spanning both columns, and `.empty-note` cells dropping out entirely.

### Empty States
Centred in a card-shaped container: a 30px faint icon at 1.4 stroke, a 15.5px/620 ink headline, and 14px dim explanation capped at 44ch. Every view has one, and it explains what would put content here rather than apologising.

### Error States
- **Character:** An error is a wrong turn, not a dead end. 404, a query string the route cannot parse (400) and an unhandled 500 all render `error.html` inside the normal shell, so the top bar, the health pill and both navs still work.
- **Composition:** no new visual vocabulary — a `.pagehead` h1 carrying the heading ("Not found", "Bad link", "Something went wrong") over an `.empty` block with the 30px alert icon, the specific detail in ink bold, the recovery sentence in dim, and a link back to Wants.
- **Fallback:** if the shell itself cannot render — the database may be what is broken — the handler drops to plain text carrying the same status and detail. The design degrades to words rather than to a stack trace.

### Inputs / Fields
- **Style:** card surface, 1px hairline, 8px radius, at least `var(--tap)` tall, and **16px text** — anything smaller makes iOS zoom the page on focus and leave it zoomed.
- **Focus:** the border turns accent with a 3px accent ring at 22% (`color-mix`), no outline.
- **A value with its own Save** (Settings' Limits): the field and a 44px default button share a row, and each form saves and refreshes only itself, so an edit waiting in the next field is never wiped. A value the server cannot read is refused in the toast by name ("The radius has to be a number."); one it clamps says what it saved ("The radius saved as 200 miles, the most it allows."). A form never toasts "Saved." over a value that did not save.

### Status Card (signature)
The top of `/runs`, and the rest of the health pill's sentence. Every fact true
right now, most actionable first in the pill's own order, each on its own line
with the **one** action it calls for: *Judge anyway* when judging is held,
*Resume* when something is paused, *Change hours* when asleep.
- **Lead line:** the pill's state as a 17px/620 title over a 13px ink sentence, filled with its tone's soft colour — good-soft for Working, warn-soft for a pause or hold, bad-soft for a failed fetch, the page ground for asleep. It is the panel's one filled block, and its action is the panel's one filled button.
- **Other lines:** plain card surface, a 14px/620 title led by an 8px round dot in the tone colour, a 13px dim sentence indented under it.
- **Footer:** "Last pass 5m ago" — an age, like the pill beside it, with the date in `title`.

### Pass Rows
A pass is one `<details>` row, not ten run cards. The summary is the pass's time
(14px/600) with its duration right-aligned in faint tabular figures, a dim
"725 found · 7 new · 0 judged · 0 picked" line, then each distinct note in plain
words — amber for a warning, red for a failure — with the raw journal text kept
in `title`. Opened, it lists its runs on the page ground, one line each, the
hunt name linking to that hunt's runs. The same one-line shape is how All runs
lists runs on a phone; the table returns at 720px.

### Period Switch
Stats' one window: Today, 7 days, 30 days, All time as round 36px links on the
card surface, the current one on the page hue's wash in the page hue. Everything
below the spend panel follows it. Each bar in the spend chart is a link to its
own day, with a transparent hit area the full height of the chart, because a
$0.40 day draws a bar two pixels tall.

### The Judgement Disclosure (signature)
The card's second band is a `<details>` whose summary states its own contents —
"Why it scored 8.4 · 3 things to check" — with a help icon when there are
unknowns and a chevron that rotates 180° over 0.2s on open. Inside: an
amber "Needs checking" callout, then a requirements list where each item is a
green check, red x or amber question mark against the requirement and its
evidence in dim roman, then the model's reasoning.

The summary is a control, so it carries the full `var(--tap)` minimum height
rather than taking its height from 12.5px text — the same target as Save and
Dismiss, because it is tapped as often. Below the reasoning sits `.flags`, the red
flags in full as a bad-coloured list with 15px alert icons — the card shows two
lines of each, this shows all of it.

**The Earned-Expansion Rule.** The photo, price, place and score get the whole
top row because they are the first gate. Anything that explains a decision is
one tap away, never removed — and never open by default. A new field added
inside the disclosure must also be reflected in the summary's count.

## Do's and Don'ts

### Do:
- **Do** put every colour through a custom property with both a `prefers-color-scheme` override and a `[data-theme=dark]` value, and check any change in both themes.
- **Do** mark every comparable number with `.num` so tabular figures line up.
- **Do** use `<svg class="i"><use href="#i-name"/></svg>` from the sprite in `base.html`; add a new 24×24, 1.75-stroke symbol there when an icon is missing.
- **Do** separate surfaces with a 1px hairline (`--line` to bound, `--line-soft` to divide inside).
- **Do** render `<table class="responsive">` with a `data-label` on every cell whenever a table has more than three columns.
- **Do** design the 390px view first and let 560px and 900px add room.
- **Do** size every phone-tappable control from `var(--tap)` (44px), never a literal.
- **Do** give every view an empty state that says what would fill it, and put the long teaching copy there rather than in the page head.
- **Do** open a list view with a title and a `.stats` line of live counts, marking at most one group `.hi` when it needs a decision.
- **Do** size icons from the icon scale via `font-size` on `svg.i`, matching the text beside them.
- **Do** derive a hover or pressed colour with `color-mix` from the token it shades, rather than declaring a new one.
- **Do** recompute contrast for both themes when any colour changes; `--dim` and `--faint` carry text and are held to AA.
- **Do** let chips wrap. A want name or a flag is part of the reason a listing is on screen.
- **Do** give every tappable element a deliberate minimum height from `var(--tap)`, filter chips excepted at 32px.
- **Do** render errors in the normal shell with the nav intact, using `.pagehead` and `.empty`.
- **Do** delete a symbol or token that nothing references, unless it is recorded here as a reserve.
- **Do** keep the accent under a tenth of any screen — primary action, links, selection, focus ring.
- **Do** mark location with the page's hue and action with blue, and give a new page a hue of its own, saturated enough to beat the grey inactive tabs.
- **Do** keep one filled button per card or panel: the action of the lead fact.
- **Do** set every form field at 16px or larger, and give a single value its own Save.
- **Do** say in words what a run warning or a rejection reason means; keep the raw text in `title`.

### Don't:
- **Don't** introduce a second typeface, a webfont, or a monospace face. The platform system sans is the whole type system.
- **Don't** use emoji or Unicode glyphs (✓ ✗ ? ⚠) as icons; they are `#i-check`, `#i-x`, `#i-help` and `#i-alert`.
- **Don't** communicate structure with shadow. If a boundary is hard to see, the answer is a border or a tone step, never a larger shadow.
- **Don't** use green, amber or red for emphasis, ranking or decoration — only to name a state the data is in.
- **Don't** open the judgement disclosure by default, and don't add a field inside it without adding it to the summary's count.
- **Don't** hardcode a hex value in a template.
- **Don't** animate position, scale or elevation on interaction; feedback is a background-tone change at 0.15s.
- **Don't** put a repeatedly-tapped control in the top bar on a phone; the bottom tab bar and the in-card footer are where the thumb is.
- **Don't** let buttons in a control row wrap into an uneven group; give the label its own line and let the actions share the width (`flex: 1 1 0`, `min-width: 110px`).
- **Don't** state what a view *is* in its page head when you could state what is *waiting* in it.
- **Don't** read an `svg.i` `font-size` as a type step, and don't add a new one to split a difference between existing icon sizes.
- **Don't** kill colour and background transitions under `prefers-reduced-motion`; disable animation and real movement only.
- **Don't** colour a chip that is merely present. Green is a score of 7+, the word FREE, and the want check glyph — nothing else.
- **Don't** assume any on-screen string is short or space-separated; it was written by a stranger and `overflow-wrap: anywhere` is what keeps it inside its card.
- **Don't** skip a heading level: the card title is an `<h2>` under the page's `<h1>`.
- **Don't** set anything a person reads under 11px.
- **Don't** make a page hue a grey, or reuse a semantic colour as a new page's hue.
- **Don't** toast success the server did not confirm, and don't clamp a value silently.
- **Don't** offer a button that would do nothing — Dismiss on a dismissed card, Save on a gone one.
- **Don't** show a database value (`free_find`, `filtered`, `duplicate_of:…`) where a person reads it.
