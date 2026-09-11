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
from typing import Sequence

from .db import Store
from .filters import gate, matches_any
from .models import GateResult
from dataclasses import replace

from .models import Candidate, Hunt, Listing, Location, RunResult, Score
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


def _would_bin(score: Score, hunt: Hunt, listing: Listing) -> bool:
    """Whether this score alone puts a listing in front of you.

    Must agree with the routing in stage 6, or the image pass is spent looking
    at things that will not be shown either way.
    """
    if score.match in ("yes", "unknown"):
        return score.deal_score >= hunt.min_deal_score
    return (hunt.kind == "sweep"
            and bool(score.worth_grabbing)
            and score.deal_score >= hunt.free_find_min_score
            and _is_a_bargain(score, listing))


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
    )
    store.record_rejections(hunt.id, gr.rejected)

    # --- 4a. cap the batch ---------------------------------------------------
    # Cold start is the problem case: a first run against Craigslist's free
    # category sees ~192 listings, which would mean 192 detail fetches and 192
    # appraisals in one go. Cap to the newest N and simply LEAVE the rest alone
    # -- they keep status `new`, so the backlog drains over the next few runs
    # instead of arriving as one bill. Newest first because that is the half
    # most likely to still be available.
    if len(gr.candidates) > hunt.max_results:
        ordered = sorted(
            gr.candidates,
            key=lambda c: c.listing.posted_at or datetime.min.replace(
                tzinfo=timezone.utc),
            reverse=True)
        result.n_deferred = len(ordered) - hunt.max_results
        gr = GateResult(candidates=ordered[:hunt.max_results],
                        rejected=gr.rejected)
        # Recorded on the run so a backlog that keeps GROWING is visible: that
        # means arrivals are outrunning max_results_per_run and older listings
        # will never be reached.
        log.info("capped at %d candidates; %d deferred to a later run",
                 hunt.max_results, result.n_deferred)

    # --- 4b. enrich survivors ------------------------------------------------
    # The search feed is a cheap index -- no description, no coordinates. Detail
    # is fetched only for listings that already passed the gate, which is what
    # keeps the request count low enough to stay unremarkable.
    if hasattr(source, "detail") and gr.candidates:
        enriched: list = []
        failed = 0
        for cand in gr.candidates:
            try:
                full = source.detail(cand.listing)
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
        in_range, out_of_range = [], []
        for cand in enriched:
            d = cand.listing.distance_mi
            if d is not None and d > location.radius_miles:
                out_of_range.append((cand.listing.id, "too_far"))
            else:
                in_range.append(cand)
        store.record_rejections(hunt.id, out_of_range)
        gr = GateResult(candidates=in_range, rejected=gr.rejected + out_of_range)

    # --- 4b2. too old to bother judging --------------------------------------
    # Placed AFTER enrichment on purpose: Craigslist only reveals postedDate on
    # the item page, so before this point most listings have no age at all.
    # Enrichment is cheap (an HTTP request); appraisal is not.
    if hunt.max_age_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=hunt.max_age_days)
        fresh, stale = [], []
        for cand in gr.candidates:
            posted = cand.listing.posted_at
            if posted is not None and posted < cutoff:
                stale.append((cand.listing.id, "too_old"))
            else:
                fresh.append(cand)          # undated listings pass: fail open
        if stale:
            store.record_rejections(hunt.id, stale)
            log.info("%d listings older than %dd skipped", len(stale),
                     hunt.max_age_days)
        gr = GateResult(candidates=fresh, rejected=gr.rejected + stale)

    # --- 4b3. exclude keywords, now that there is a description --------------
    # The gate only ever sees the search feed, where Facebook supplies no
    # description at all and Craigslist hardcodes None -- so an exclude term
    # that appears only in the body cannot fire there, and the listing gets
    # paid for at triage AND at appraisal. Same reason `too_far` and `too_old`
    # are re-checked here.
    if hunt.exclude and gr.candidates:
        keep, excluded = [], []
        for cand in gr.candidates:
            if (hit := matches_any(cand.listing, hunt.exclude)):
                excluded.append((cand.listing.id, f"excluded_kw:{hit}"))
            else:
                keep.append(cand)
        if excluded:
            store.record_rejections(hunt.id, excluded)
            log.info("%d listings excluded on their description", len(excluded))
        gr = GateResult(candidates=keep, rejected=gr.rejected + excluded)

    # --- 4c. cross-source duplicates -----------------------------------------
    # People post the same thing to both marketplaces. This runs for EVERY
    # source, not only ones with a detail fetch -- it lived inside the
    # enrichment branch at first, which silently disabled it for any source
    # without a `detail` method. It is placed after enrichment because the key
    # needs coordinates, which Facebook only supplies at detail time.
    deduped, dupes, seen_keys = [], [], {}
    for cand in gr.candidates:
        key = cand.listing.dup_key
        if key:
            prior = store.scored_duplicate(hunt.id, key, cand.listing.id)
            if prior:
                dupes.append((cand.listing.id, f"duplicate_of:{prior}"))
                continue
            if key in seen_keys:
                dupes.append((cand.listing.id, f"duplicate_of:{seen_keys[key]}"))
                continue
            seen_keys[key] = cand.listing.id
        deduped.append(cand)
    if dupes:
        store.record_rejections(hunt.id, dupes)
        log.info("%d cross-source duplicates skipped", len(dupes))
    gr = GateResult(candidates=deduped, rejected=gr.rejected + dupes)

    result.n_candidates = len(gr.candidates)

    if no_score or not gr.candidates:
        # result.warning may already be set by a partial enrichment. The runs
        # table is the only place a degraded run is visible, so it has to go in
        # -- in `warning`, because `error` is what last_success_at reads.
        store.finish_run(run_id, n_fetched=result.n_fetched, n_new=result.n_new,
                         n_candidates=result.n_candidates, error=result.error,
                         warning=result.warning,
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
        result.error = f"scoring skipped: {exc}"
        store.finish_run(run_id, n_fetched=result.n_fetched, n_new=result.n_new,
                         n_candidates=result.n_candidates,
                         n_scored=result.n_scored, cost_usd=result.cost_usd,
                         error=result.error, warning=result.warning)
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
        result.error = f"scoring interrupted: {interrupted}"

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
            if not _would_bin(score, hunt, listing):
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
    # Sorted by WHY it is here, not by how sure we are. "Did you find my TV
    # stand" and "what free stuff is worth grabbing" are different questions and
    # want different pages; certainty is shown inside a card, not as its own bin.
    #
    # `deal_score` is scored as if unknowns resolve favourably, so one threshold
    # serves both bins and only the match field carries the uncertainty.
    wanted = [s for s in scores
              if s.match in ("yes", "unknown")
              and s.deal_score >= hunt.min_deal_score]
    # Only the SWEEP fills the free bin. A want hunt that stumbles on an
    # unrelated bargain used to put it there too, which is how a $40
    # entertainment centre ended up in a tab called "Free finds" -- seven of
    # the ten things in that bin were priced, and every one came from a want
    # hunt. The sweep is free-only by construction, so this makes the bin's
    # name true: everything in it is actually free. A want hunt's non-matches
    # stay `scored` and remain findable in /skipped.
    free_finds = [] if hunt.kind != "sweep" else [
        s for s in scores
        if s.match == "no" and s.worth_grabbing
        and s.deal_score >= hunt.free_find_min_score
        and _is_a_bargain(s, by_id[s.listing_id])]

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

    store.finish_run(run_id, error=result.error, warning=result.warning,
                     n_fetched=result.n_fetched, n_new=result.n_new,
                     n_candidates=result.n_candidates, n_scored=result.n_scored,
                     n_surfaced=result.n_surfaced, n_wanted=result.n_wanted,
                     n_free_find=result.n_free_find,
                     n_image_checks=result.n_image_checks,
                     n_deferred=result.n_deferred, cost_usd=result.cost_usd)
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
