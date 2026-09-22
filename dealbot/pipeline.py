"""The run loop: fetch -> parse -> store -> gate -> triage -> appraise -> surface.

Every stage is idempotent. Re-running a hunt five minutes later re-upserts the
same listings, appends identical price observations (cheap), gates out everything
already scored and unchanged, and makes ZERO model calls. That property is what
makes a 15-minute poll interval safe rather than expensive, so be careful not to
break it.

Scoring is deliberately separable from fetching. When the plan window is
exhausted, fetching continues -- it costs no quota -- and listings pile up with
status `new` until scoring resumes. A rate limit therefore costs you judgement
for a few hours, never data.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

from .db import Store
from .filters import gate, matches_any
from .models import GateResult
from dataclasses import asdict, replace

from .models import (Candidate, Hunt, Listing, Location, RunResult, Score,
                     UpsertResult)
from .notify.base import Notifier
from .scoring.base import Scorer, TriageResult
from .scoring.claude_code import ScoringUnavailable
from .sources.base import Source, SourceBlocked, validate

log = logging.getLogger("dealbot.pipeline")

# How far under its value a PRICED listing has to be before the free-finds bin
# will take it. `worth_grabbing` asks "would a sensible person collect this at
# this price?", which for a fairly priced thing is a low bar -- a $140 chair
# estimated at $160 cleared it, and "fair value, nothing special" is not a find.
# The estimate is the model's, so this is a floor rather than a judgement:
# genuine bargains in the live bin run 3-5x, the filler sits at 1.1-1.2x.
# Free listings are unaffected -- free costs a drive, not money.
FREE_FIND_VALUE_MULTIPLE = 1.4


def _is_a_bargain(score: Score, listing: Listing) -> bool:
    """Whether a priced listing is far enough under its value to be a find.

    Fails OPEN, twice over: a free listing (or one with no price shown) and a
    listing the model would not put a value on both pass. An unpriced find is
    the thing this bot exists for, and a missing estimate is not evidence
    against a listing.
    """
    price = listing.price_cents
    if not price:                       # free, or no price shown
        return True
    if score.est_value_cents is None:   # no estimate: not a reason to drop it
        return True
    return score.est_value_cents >= FREE_FIND_VALUE_MULTIPLE * price


def route(score: Score, hunt: Hunt, listing: Listing) -> str | None:
    """Which bin this score puts a listing in, or None for neither.

    THE routing rule, in one place. It used to be written twice -- once here as
    a boolean for the image pass, once in stage 6 as a pair of list
    comprehensions -- with a comment asking the two to agree. They agreed only
    as long as someone remembered, and disagreement is silent: the image pass
    is spent on listings nobody will be shown, or skipped on ones they will.

    Sorted by WHY a listing is here, not by how sure we are. "Did you find my
    TV stand" and "what free stuff is worth grabbing" are different questions
    and want different pages; certainty is shown inside a card, not as its own
    bin. `deal_score` is scored as if unknowns resolve favourably, so one
    threshold serves both bins and only the match field carries uncertainty.

    Only the SWEEP fills the free bin. A want hunt that stumbles on an
    unrelated bargain used to put it there too, which is how a $40
    entertainment centre ended up in a tab called "Free finds" -- seven of the
    ten things in that bin were priced, and every one came from a want hunt.
    The sweep is free-only by construction, so this makes the bin's name true.
    A want hunt's non-matches stay `scored` and remain findable in /skipped.

    `price_unclear` closes the free bin, and ONLY the free bin. That bin means
    "worth grabbing for nothing", and a $0 the seller contradicted in the words
    is not a price at all -- there is no figure to weigh the trip against. It
    stays `scored` and shows on /skipped saying so, where the score the model
    gave it (judged as if the thing really were free) keeps it above the floor.
    The decision is here rather than in the rubric because the model writes that
    flag, and the thing that writes a flag is not the thing that should be
    trusted to act on it. A wanted thing is untouched: a bookshelf whose seller
    takes offers is still the bookshelf you asked for.
    """
    if score.match in ("yes", "unknown"):
        return "wanted" if score.deal_score >= hunt.min_deal_score else None
    if (hunt.kind == "sweep"
            and not score.price_unclear
            and bool(score.worth_grabbing)
            and score.deal_score >= hunt.free_find_min_score
            and _is_a_bargain(score, listing)):
        return "free_find"
    return None


def _drop(store: Store, hunt: Hunt, gr: GateResult,
          reason_for, note: str) -> GateResult:
    """Reject the candidates `reason_for` names a reason for, and keep the rest.

    The five post-enrichment checks were five copies of this loop, and they had
    already drifted: the radius one recorded its rejections unguarded and
    logged nothing, so the only stage rejecting on a post-enrichment distance
    was the only stage whose rejections were invisible in the journal.

    Recording the rejection is the part that must not be forgotten -- it is
    what makes a drop auditable on the hunt page instead of a listing quietly
    vanishing. Keeping it here means a sixth check cannot omit it.
    """
    keep: list[Candidate] = []
    dropped: list[tuple[str, str]] = []
    for cand in gr.candidates:
        reason = reason_for(cand)
        if reason:
            dropped.append((cand.listing.id, reason))
        else:
            keep.append(cand)
    if dropped:
        store.record_rejections(hunt.id, dropped)
        log.info(note, len(dropped))
    return GateResult(candidates=keep, rejected=gr.rejected + dropped)


def freshness(cand: Candidate, upserts: Mapping[str, UpsertResult]) -> datetime:
    """How new a listing is, by the best date we actually hold.

    The batch cap orders by this, and it used to read `posted_at` alone with
    `datetime.min` for anything undated. **Craigslist's search feed carries no
    posting date** -- it only arrives from the item endpoint, after enrichment,
    which happens after the cap. So every Craigslist candidate tied on
    `datetime.min`, the sort became a no-op, and a stable sort handed the slots
    to whatever order the feed returned. On the free sweep -- the one hunt that
    overflows its cap, by hundreds -- "newest first" was picking nothing of the
    kind. 83 of the 84 listings stranded at the time had no date at all.

    `first_seen` is the fallback because the store always has it. It is not on
    the `Listing`: that comes off a search feed, not out of the database, so it
    rides back on the `UpsertResult` from stage 3.

    Fails OPEN, to the epoch. An unparseable date sorts last rather than
    raising, because one bad row must not take a whole run's cap with it.
    """
    posted = cand.listing.posted_at
    if posted is not None:
        return posted if posted.tzinfo else posted.replace(tzinfo=timezone.utc)
    up = upserts.get(cand.listing.id)
    if up is not None and up.first_seen:
        try:
            seen = datetime.fromisoformat(up.first_seen)
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)
        return seen if seen.tzinfo else seen.replace(tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def _record_triage_drops(store: Store, hunt: Hunt, dropped: Sequence[Candidate],
                         notes: dict[str, str], model_name: str) -> int:
    """Record a triage drop as a SCORE, not as a filter rejection.

    Without this the gate has no memory of it -- `last_scores` stays empty, the
    listing is re-admitted as "new" on the very next run, and it is re-triaged
    forever. Storing it also means a later price drop reopens it, which is
    exactly what should happen.
    """
    now = datetime.now(timezone.utc)
    model = f"{model_name}:triage"
    for cand in dropped:
        score = Score(
            listing_id=cand.listing.id, hunt_id=hunt.id, model=model,
            scored_at=now, match="no", deal_score=0.0,
            est_value_cents=None, condition=None, matched_want=None,
            worth_grabbing=False, unknowns=(), requirements=(), red_flags=(),
            reasoning="dropped in triage: "
                      + (notes.get(cand.listing.id) or "no reason given"))
        store.save_score(score, priced_at_cents=cand.listing.price_cents)
        store.set_status(hunt.id, cand.listing.id, "scored")
    return len(dropped)


def _with_stamp(listing: Listing, upserts: dict[str, UpsertResult]) -> Listing:
    """The source's own version stamp for this listing, as stored.

    `upsert_listing` reports what it HELD before the write, which is what a
    freshly fetched item page has to be compared against.
    """
    prior = upserts.get(listing.id)
    stamp = prior.source_updated_at if prior else None
    if not stamp:
        return listing
    return replace(listing, source_updated_at=datetime.fromisoformat(stamp))


def run_hunt(
    store: Store,
    hunt: Hunt,
    source: Source,
    scorer: Scorer,
    notifiers: Sequence[Notifier],
    location: Location,
    *,
    no_score: bool = False,
    image_provider=None,
    max_image_checks: int = 10,
    thumbnails=None,
) -> RunResult:
    """One hunt against one source, end to end.

    Stages: fetch -> parse -> store -> gate -> cap -> enrich -> age -> dedupe
    -> triage -> appraise -> images -> route.

    Two invariants worth protecting:

    * **Idempotent.** Running twice in a minute re-upserts the same listings and
      makes ZERO model calls, because the gate rejects everything `unchanged`.
      Anything that breaks this makes the poll interval expensive.
    * **Fetching is free, judgement is not.** Every pause here -- quota ceiling,
      rate limit, spend ceiling, connectivity -- stops the judging and lets the
      collecting continue. You lose judgement for a while, never data.

    Nothing is deleted. A rejected listing keeps its reason and its raw payload,
    which is how three separate parser bugs were repaired from data already on
    disk without re-fetching anything.
    """
    run_id = store.start_run(hunt, source.name)
    result = RunResult(run_id=run_id, hunt_id=hunt.id)

    # The previous run's spend is in the runs table by now, so the scorer's
    # in-memory counter must start over -- otherwise `cost_since` and that
    # counter hold the same dollars and the daily ceiling arrives at twice the
    # real spend, stopping judgement at about half the configured limit.
    if hasattr(scorer, "begin_run"):
        scorer.begin_run()

    # --- 1. fetch -----------------------------------------------------------
    # A dead source must fail LOUDLY in the runs table. Silently returning zero
    # rows is indistinguishable from a quiet day, and that is how these bots die
    # without anyone noticing.
    try:
        raws = list(source.search(hunt))
    except Exception as exc:                              # noqa: BLE001
        log.exception("fetch failed for %s", hunt.id)
        result.error = f"{type(exc).__name__}: {exc}"
        store.finish_run(run_id, error=result.error)
        return result

    # --- 2. parse -----------------------------------------------------------
    listings: list[Listing] = []
    for raw in raws:
        try:
            parsed = validate(source.parse(raw))
        except Exception as exc:                          # noqa: BLE001
            log.warning("parse failed for %s:%s -- %s", raw.source, raw.source_id, exc)
            continue
        if parsed is not None:
            listings.append(parsed)
    result.n_fetched = len(listings)

    if len(raws) and not listings:
        # Fetched rows but parsed none: that is a broken parser, not a quiet day.
        result.error = f"parsed 0 of {len(raws)} fetched listings"
        store.finish_run(run_id, n_fetched=0, error=result.error)
        return result

    # --- 3. store -----------------------------------------------------------
    upserts = {}
    for listing in listings:
        up = store.upsert_listing(listing)
        upserts[listing.id] = up
        store.record_price(listing.id, listing.price_cents)   # always, append-only
        if up.is_new:
            result.n_new += 1
    store.mark_matches(hunt.id, listings)
    store.mark_gone(hunt.id, source.name, [l.id for l in listings])

    # --- 4. gate ------------------------------------------------------------
    gr = gate(
        hunt, listings, location,
        statuses=store.statuses(hunt.id),
        last_scores=store.last_scores(hunt.id),
        upserts=upserts,
        filtered=store.filter_reasons(hunt.id),
    )
    store.record_rejections(hunt.id, gr.rejected)

    # --- 4a. cap the batch ---------------------------------------------------
    # Cold start is the problem case: a first run against Craigslist's free
    # category sees ~192 listings, which would mean 192 detail fetches and 192
    # appraisals in one go. Cap to the newest N and simply LEAVE the rest alone
    # -- they keep status `new`, so the backlog drains over the next few runs
    # instead of arriving as one bill. Newest first because that is the half
    # most likely to still be available.
    #
    # See `freshness` for why this is not just `posted_at`: Craigslist has no
    # posting date at this stage, so ordering on that alone silently handed the
    # slots to feed order on the one hunt that actually overflows.
    if len(gr.candidates) > hunt.max_results:
        ordered = sorted(gr.candidates,
                         key=lambda c: freshness(c, upserts), reverse=True)
        result.n_deferred = len(ordered) - hunt.max_results
        gr = GateResult(candidates=ordered[:hunt.max_results],
                        rejected=gr.rejected)
        # Recorded on the run so a backlog that keeps GROWING is visible: that
        # means arrivals are outrunning max_results_per_run and older listings
        # will never be reached.
        log.info("capped at %d candidates; %d deferred to a later run",
                 hunt.max_results, result.n_deferred)

    # --- 4a2. top up from what was collected and never judged ----------------
    # The cap above says the overflow "drains over the next few runs". It did
    # not. Candidates only ever came from the current fetch, so a listing that
    # was capped out and then fell off page one was never fetched again and
    # never judged -- 130 of them, the oldest 47 hours old, while every
    # individual run looked healthy.
    #
    # So when a run has room under its own cap, it spends it on the backlog
    # rather than on nothing. In steady state the backlog is empty and this
    # changes nothing, which keeps "re-running costs nothing" intact.
    room = hunt.max_results - len(gr.candidates)
    if room > 0:
        already = {c.listing.id for c in gr.candidates}
        older = [Candidate(l, "backlog")
                 for l in store.unjudged(hunt.id, source.name, room + len(already))
                 if l.id not in already][:room]
        if older:
            log.info("topping up with %d listings collected earlier and never "
                     "judged", len(older))
            gr = GateResult(candidates=gr.candidates + older,
                            rejected=gr.rejected)

    # --- 4b. enrich survivors ------------------------------------------------
    # The search feed is a cheap index -- no description, no coordinates. Detail
    # is fetched only for listings that already passed the gate, which is what
    # keeps the request count low enough to stay unremarkable.
    if hasattr(source, "detail") and gr.candidates:
        enriched: list = []
        failed = 0
        for cand in gr.candidates:
            try:
                # A listing parsed off a search feed cannot know the version
                # stamp we already hold for it, and without it a source cannot
                # recognise a stale cached copy of its own item page. The
                # store just told us, one stage ago.
                full = source.detail(_with_stamp(cand.listing, upserts))
            except SourceBlocked as exc:
                # Blocked or out of request budget. Stop enriching, but do NOT
                # kill the run: what was already enriched is good, and the rest
                # simply stay `new` and are retried next time. Same principle as
                # a scoring outage -- lose progress, never data.
                log.warning("detail fetch stopped at %s: %s", cand.listing.id, exc)
                result.warning = f"detail fetch stopped: {type(exc).__name__}: {exc}"
                break
            except Exception as exc:                      # noqa: BLE001
                # ONE payload we could not parse -- an image entry that is not a
                # string, an attribute that is not a dict. Skipping costs this
                # listing a deferral; breaking used to cost every candidate
                # behind it, which is the difference between one listing waiting
                # and a whole run going unjudged.
                log.warning("detail failed for %s: %s", cand.listing.id, exc)
                failed += 1
                continue
            if full is None:
                # Detail unavailable (removed, or the parser missed it). Judging
                # the index-level record means judging a title like "Free" with
                # no description -- worse than useless, and it would burn the one
                # chance to score it. Leave it `new` and try again next run.
                log.info("no detail for %s; deferring", cand.listing.id)
                continue
            store.upsert_listing(full)
            enriched.append(replace(cand, listing=full))

        # Judging an un-enriched listing means judging a title like "Free" with
        # no description -- worse than useless, and it would burn the one chance
        # to score it. Leave them `new` for the next run instead.
        skipped = len(gr.candidates) - len(enriched)
        if skipped:
            log.info("%d candidates left unenriched for next run", skipped)
        if failed and result.warning is None:
            result.warning = f"{failed} detail fetches failed"

        # Distance is only knowable after enrichment, so the radius check has to
        # run again here rather than in the gate.
        def _too_far(cand: Candidate) -> str | None:
            d = cand.listing.distance_mi
            return "too_far" if d is not None and d > location.radius_miles else None

        gr = _drop(store, hunt, GateResult(enriched, gr.rejected), _too_far,
                   "%d candidates dropped outside the radius")

    # --- 4b2. too old to bother judging --------------------------------------
    # Placed AFTER enrichment on purpose: Craigslist only reveals postedDate on
    # the item page, so before this point most listings have no age at all.
    # Enrichment is cheap (an HTTP request); appraisal is not.
    if hunt.max_age_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=hunt.max_age_days)

        def _too_old(cand: Candidate) -> str | None:
            posted = cand.listing.posted_at
            # undated listings pass: fail open
            return "too_old" if posted is not None and posted < cutoff else None

        gr = _drop(store, hunt, gr, _too_old,
                   f"%d listings older than {hunt.max_age_days}d skipped")

    # --- 4b3. exclude keywords, now that there is a description --------------
    # The gate only ever sees the search feed, where Facebook supplies no
    # description at all and Craigslist hardcodes None -- so an exclude term
    # that appears only in the body cannot fire there, and the listing gets
    # paid for at triage AND at appraisal. Same reason `too_far` and `too_old`
    # are re-checked here.
    if hunt.exclude and gr.candidates:
        def _excluded(cand: Candidate) -> str | None:
            hit = matches_any(cand.listing, hunt.exclude)
            return f"excluded_kw:{hit}" if hit else None

        gr = _drop(store, hunt, gr, _excluded,
                   "%d listings excluded on their description")

    # --- 4b4. nothing to go on -----------------------------------------------
    # A photograph is the minimum. Without one there is nothing for the image
    # pass to open, and on Craigslist a photoless post is usually not a listing
    # at all: of the 51 collected, the titles run "Gone", "Free junk metal
    # removal", "I need HELP please", "Anyone willing to donate bikes for kids".
    # Wanted-ads and noise. Eight were appraised for $0.20 and not one ever
    # reached a bin.
    #
    # Missing TEXT is not disqualifying on its own -- on Facebook a bare listing
    # usually means the seller let the photos do the talking, and those score
    # better than the ones with words. The two reasons stay separate so the
    # hunt page can tell an empty post from a photoless one.
    #
    # After enrichment, because that is where both arrive: Craigslist's search
    # feed carries no description at all, and Facebook's carries one photo.
    if gr.candidates:
        def _unjudgeable(cand: Candidate) -> str | None:
            if cand.listing.images:
                return None
            has_words = bool((cand.listing.description or "").strip())
            return "no_photo" if has_words else "nothing_to_judge"

        gr = _drop(store, hunt, gr, _unjudgeable,
                   "%d listings had no photograph")

    # --- 4c. cross-source duplicates -----------------------------------------
    # People post the same thing to both marketplaces. This runs for EVERY
    # source, not only ones with a detail fetch -- it lived inside the
    # enrichment branch at first, which silently disabled it for any source
    # without a `detail` method. It is placed after enrichment because the key
    # needs coordinates, which Facebook only supplies at detail time.
    seen_keys: dict[str, str] = {}

    def _duplicate(cand: Candidate) -> str | None:
        # TWO identities, and either one is enough. `dup_key` is
        # title+price+place and catches the cross-post; `img_key` is
        # photo+place and catches the repost. A seller put the same gas stove
        # up twice three minutes apart and both reached Discord, because
        # Craigslist reported one at $0 and the other with no price at all and
        # `dup_key` hashes the price exactly.
        keys = [k for k in (cand.listing.dup_key, cand.listing.image_key) if k]
        if not keys:
            return None
        prior = store.scored_duplicate(hunt.id, cand.listing.dup_key,
                                       cand.listing.id, cand.listing.image_key)
        if prior:
            return f"duplicate_of:{prior}"
        for key in keys:
            if key in seen_keys:
                return f"duplicate_of:{seen_keys[key]}"
        # Both keys point at this listing, so a later candidate matching
        # EITHER one is caught. Registering only the key that happened to be
        # checked first would make the batch-local check weaker than the
        # stored one for no reason.
        for key in keys:
            seen_keys[key] = cand.listing.id
        return None

    gr = _drop(store, hunt, gr, _duplicate,
               "%d cross-source duplicates skipped")

    result.n_candidates = len(gr.candidates)

    if no_score or not gr.candidates:
        # result.warning may already be set by a partial enrichment. The runs
        # table is the only place a degraded run is visible, so it has to go in
        # -- in `warning`, because `error` is what last_success_at reads.
        store.finish_run(**asdict(result),
                         full_pass=0 if no_score else 1)
        return result

    # --- 5. triage (cheap, batched) then appraise (expensive, individual) ----
    # Fetching costs no quota and has already happened; only judgement can be
    # unavailable. Listings stay `new` and get scored when the window reopens,
    # so a rate limit or an outage costs judgement for a few hours, never data.
    try:
        triaged = scorer.triage(hunt, gr.candidates)
    except ScoringUnavailable as exc:
        log.warning("scoring unavailable for %s: %s", hunt.id, exc)
        # Chunks that completed before the pause were BILLED, so their cost has
        # to reach `runs.cost_usd` -- the only thing the daily ceiling reads.
        #
        # Their DROPS are recorded as scores, which is the load-bearing half:
        # without it the gate re-admits them as `new` and they are re-triaged
        # and re-appraised forever. Their KEEPS are not, and cannot be: the
        # only honest record for a listing that was kept but never appraised is
        # "not judged yet". Those are re-triaged next run, costing part of one
        # batched call -- the cheap half of the bill, deliberately paid twice
        # rather than recorded as a judgement that never happened.
        done = getattr(exc, "partial", None)
        if isinstance(done, TriageResult):
            result.cost_usd += done.cost_usd
            result.n_scored += _record_triage_drops(
                store, hunt,
                [c for c in gr.candidates if c.listing.id in done.notes],
                done.notes, scorer.name)
        if hasattr(scorer, "drain_unbilled"):
            result.cost_usd += scorer.drain_unbilled()
        # `warning`, NOT `error`. The fetch succeeded -- this run collected
        # everything it was going to collect and only the judging stood aside.
        # `last_success_at` reads `error`, so recording it there made the hunt
        # "due" on every single tick: the sweep ran every ~16 minutes against a
        # configured 30, and want:tv-stand every ~15 against a configured 60.
        # That is 2-4x the intended request volume at two sources that throttle
        # silently, to retry something no amount of fetching can fix. The
        # backlog top-up is what resumes judging once the window reopens.
        note = f"scoring skipped: {exc}"
        result.warning = f"{result.warning}; {note}" if result.warning else note
        store.finish_run(**asdict(result))
        return result

    survivors, triage_notes = triaged.kept, triaged.notes
    result.cost_usd += triaged.cost_usd
    kept_ids = {c.listing.id for c in survivors}
    n_dropped = _record_triage_drops(
        store, hunt, [c for c in gr.candidates if c.listing.id not in kept_ids],
        triage_notes, scorer.name)

    interrupted = None
    try:
        scores: list[Score] = scorer.appraise(hunt, survivors) if survivors else []
    except ScoringUnavailable as exc:
        # Keep whatever was already appraised -- it has been paid for. The rest
        # stay `new` and are picked up when the window reopens.
        log.warning("appraisal interrupted for %s: %s", hunt.id, exc)
        scores, interrupted = list(getattr(exc, "partial", []) or []), exc

    # Calls that produced no Score still cost money. They reach the run here,
    # or they reach nothing: `runs.cost_usd` is what the daily ceiling reads on
    # every later run, and it under-counted by exactly the listings that burn
    # the most tokens.
    if hasattr(scorer, "drain_unbilled"):
        result.cost_usd += scorer.drain_unbilled()

    by_id = {c.listing.id: c.listing for c in survivors}
    for score in scores:
        store.save_score(score, priced_at_cents=by_id[score.listing_id].price_cents)
        store.set_status(hunt.id, score.listing_id, "scored")
        result.cost_usd += score.cost_usd
    result.n_scored = len(scores) + n_dropped

    if interrupted is not None:
        # Recorded, but NOT returned on. Everything appraised before the pause
        # is saved with status `scored`; if it never reaches a bin, the gate
        # rejects it as `unchanged` on every later run and it is never surfaced
        # and never announced -- the same silent loss that left two TV stands
        # sitting un-announced. So routing and notification below still run,
        # and only the image pass is skipped.
        # `warning` for the same reason as the standdown above, and this case
        # has the stronger claim: the run fetched, judged part of the batch,
        # routed it and announced it. The `warning` column's schema comment
        # names this exact situation -- "appraisal interrupted by a rate
        # limit" -- and putting it in `error` made the hunt due on the next
        # tick, collapsing its cadence onto the timer period.
        note = f"scoring interrupted: {interrupted}"
        result.warning = f"{result.warning}; {note}" if result.warning else note

    # --- 5b. image pass, only where the model asked for one -----------------
    # Both scores are kept. The text judgement stays in the history next to the
    # one that looked, so "the photos changed my mind" is visible rather than
    # overwritten.
    if (interrupted is None and image_provider is not None
            and hasattr(scorer, "resolve_with_images")):
        for i, score in enumerate(scores):
            if not score.needs_images or result.n_image_checks >= max_image_checks:
                continue
            # Only look at photos for something that would ALREADY reach a bin on
            # its text score. An image pass costs about twice a text appraisal,
            # and two thirds of them were being spent confirming that things
            # scoring 3/10 are indeed poor. Its real value is at the top -- a 9
            # that photos reveal to be junk saves a wasted trip -- and that case
            # is preserved, because such a listing is in a bin already.
            listing = by_id[score.listing_id]
            if route(score, hunt, listing) is None:
                log.debug("skipping image pass for %s (scored %.0f)",
                          score.listing_id, score.deal_score)
                continue
            try:
                better = scorer.resolve_with_images(hunt, listing, score,
                                                    image_provider)
            except ScoringUnavailable as exc:
                log.warning("image pass unavailable: %s", exc)
                break
            if better is None:
                continue
            store.save_score(better, priced_at_cents=listing.price_cents)
            scores[i] = better
            result.n_image_checks += 1
            result.cost_usd += better.cost_usd

    # --- 6. route into the two bins -----------------------------------------
    # `route` is the rule; this is only the bookkeeping around it.
    wanted, free_finds = [], []
    for s in scores:
        bin_name = route(s, hunt, by_id[s.listing_id])
        if bin_name == "wanted":
            wanted.append(s)
        elif bin_name == "free_find":
            free_finds.append(s)

    for s in wanted:
        store.set_status(hunt.id, s.listing_id, "wanted")
    for s in free_finds:
        store.set_status(hunt.id, s.listing_id, "free_find")
    result.n_wanted, result.n_free_find = len(wanted), len(free_finds)

    # Keep a local copy of the photo for anything you will actually browse.
    # Facebook's URLs expire in about four days; the dashboard outlives them.
    if thumbnails is not None:
        for sc in wanted + free_finds:
            thumbnails.store(by_id[sc.listing_id])
    surfaced = [(by_id[s.listing_id], s) for s in wanted + free_finds]
    for notifier in notifiers:
        try:
            notifier.notify(hunt, surfaced)
        except Exception:                                  # noqa: BLE001
            log.exception("notifier %s failed", notifier.name)
    result.n_surfaced = len(surfaced)

    store.finish_run(**asdict(result))
    return result


def dry_run(store: Store, hunt: Hunt, source: Source, location: Location) -> dict:
    """Fetch and gate against current state, write nothing. The everyday
    debugging path: see what the gate would admit without spending anything or
    mutating the store. Relist detection is unavailable here, since that needs
    the upsert."""
    raws = list(source.search(hunt))
    listings = [l for r in raws if (l := validate(source.parse(r)))]
    gr = gate(hunt, listings, location,
              statuses=store.statuses(hunt.id),
              last_scores=store.last_scores(hunt.id),
              upserts={})
    return {
        "hunt": hunt.id,
        "fetched": len(listings),
        "candidates": [(c.listing.id, c.listing.title, c.reason) for c in gr.candidates],
        "rejected": gr.rejected,
    }


def announce_price_drops(store: Store, notifiers: Sequence[Notifier], *,
                         threshold: float = 0.15, limit: int = 20) -> int:
    """Tell the user when something already in a bin got materially cheaper.

    Costs no quota and no requests: the prices were collected by the re-check
    pass, which is already running, and every observation needed to spot this
    has been on disk since day one with nothing reading it.

    The baseline is the price last announced, so a listing sliding down in
    steps announces each real drop once rather than every run.
    """
    sent = 0
    for hunt_id, listing, score, was in store.price_drops(threshold, limit=limit):
        hunt = Hunt(id=hunt_id, name=hunt_id.split(":", 1)[-1],
                    kind=hunt_id.split(":", 1)[0], queries=(),
                    max_price_cents=None, exclude=(), wants=(),
                    min_deal_score=0.0, free_find_min_score=0.0,
                    interval_minutes=0, max_results=0)
        told = False
        for n in notifiers:
            if not hasattr(n, "notify_price_drop"):
                continue
            try:
                told = n.notify_price_drop(hunt, listing, score, was) or told
            except Exception:                              # noqa: BLE001
                log.exception("price-drop notice failed via %s", n.name)
        # Stamped either way: a webhook that is off should not leave the same
        # drop queued to announce itself on every future run.
        store.mark_price_alerted(hunt_id, listing.id, listing.price_cents or 0)
        sent += bool(told)
    if sent:
        log.info("announced %d price drop(s)", sent)
    return sent
