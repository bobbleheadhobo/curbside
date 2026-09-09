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
from datetime import datetime, timezone
from typing import Sequence

from .db import Store
from .filters import gate
from .models import GateResult
from dataclasses import replace

from .models import Hunt, Listing, Location, RunResult, Score
from .notify.base import Notifier
from .scoring.base import Scorer
from .scoring.claude_code import ScoringUnavailable
from .sources.base import Source, validate

log = logging.getLogger("dealbot.pipeline")


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
) -> RunResult:
    run_id = store.start_run(hunt, source.name)
    result = RunResult(run_id=run_id, hunt_id=hunt.id)

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
        for cand in gr.candidates:
            try:
                full = source.detail(cand.listing)
            except Exception as exc:                      # noqa: BLE001
                # Blocked or out of request budget. Stop enriching, but do NOT
                # kill the run: what was already enriched is good, and the rest
                # simply stay `new` and are retried next time. Same principle as
                # a scoring outage -- lose progress, never data.
                log.warning("detail fetch stopped at %s: %s", cand.listing.id, exc)
                result.error = f"detail fetch stopped: {type(exc).__name__}: {exc}"
                break
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
        # result.error may already be set by a partial enrichment. The runs table
        # is the only place a degraded run is visible, so it has to go in.
        store.finish_run(run_id, n_fetched=result.n_fetched, n_new=result.n_new,
                         n_candidates=result.n_candidates, error=result.error,
                         full_pass=0 if no_score else 1)
        return result

    # --- 5. triage (cheap, batched) then appraise (expensive, individual) ----
    # Fetching costs no quota and has already happened; only judgement can be
    # unavailable. Listings stay `new` and get scored when the window reopens,
    # so a rate limit or an outage costs judgement for a few hours, never data.
    try:
        triaged = scorer.triage(hunt, gr.candidates)
        survivors, triage_notes = triaged.kept, triaged.notes
        result.cost_usd += triaged.cost_usd
    except ScoringUnavailable as exc:
        log.warning("scoring unavailable for %s: %s", hunt.id, exc)
        store.finish_run(run_id, n_fetched=result.n_fetched, n_new=result.n_new,
                         n_candidates=result.n_candidates,
                         error=f"scoring skipped: {exc}")
        result.error = f"scoring skipped: {exc}"
        return result

    kept_ids = {c.listing.id for c in survivors}

    # A triage drop is a judgement, not a filter outcome, so it is recorded as a
    # score. Without this the gate has no memory of it -- `last_scores` stays
    # empty, the listing is re-admitted as "new" on the very next run, and it is
    # re-triaged forever. Storing it also means a later price drop reopens it,
    # which is exactly what should happen.
    now = datetime.now(timezone.utc)
    triage_model = f"{scorer.name}:triage"
    dropped_scores = [
        Score(listing_id=c.listing.id, hunt_id=hunt.id, model=triage_model,
              scored_at=now, match="no", deal_score=0.0,
              est_value_cents=None, condition=None, matched_want=None,
              worth_grabbing=False, unknowns=(), requirements=(), red_flags=(),
              reasoning="dropped in triage: "
                        + (triage_notes.get(c.listing.id) or "no reason given"))
        for c in gr.candidates if c.listing.id not in kept_ids
    ]
    for score in dropped_scores:
        listing = next(c.listing for c in gr.candidates
                       if c.listing.id == score.listing_id)
        store.save_score(score, priced_at_cents=listing.price_cents)
        store.set_status(hunt.id, score.listing_id, "scored")

    interrupted = None
    try:
        scores: list[Score] = scorer.appraise(hunt, survivors) if survivors else []
    except ScoringUnavailable as exc:
        # Keep whatever was already appraised -- it has been paid for. The rest
        # stay `new` and are picked up when the window reopens.
        log.warning("appraisal interrupted for %s: %s", hunt.id, exc)
        scores, interrupted = list(getattr(exc, "partial", []) or []), exc

    by_id = {c.listing.id: c.listing for c in survivors}
    for score in scores:
        store.save_score(score, priced_at_cents=by_id[score.listing_id].price_cents)
        store.set_status(hunt.id, score.listing_id, "scored")
        result.cost_usd += score.cost_usd
    result.n_scored = len(scores) + len(dropped_scores)

    if interrupted is not None:
        result.error = f"scoring interrupted: {interrupted}"
        store.finish_run(run_id, error=result.error, n_fetched=result.n_fetched,
                         n_new=result.n_new, n_candidates=result.n_candidates,
                         n_scored=result.n_scored, cost_usd=result.cost_usd)
        return result

    # --- 5b. image pass, only where the model asked for one -----------------
    # Both scores are kept. The text judgement stays in the history next to the
    # one that looked, so "the photos changed my mind" is visible rather than
    # overwritten.
    if image_provider is not None and hasattr(scorer, "resolve_with_images"):
        for i, score in enumerate(scores):
            if not score.needs_images or result.n_image_checks >= max_image_checks:
                continue
            listing = by_id[score.listing_id]
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
    free_finds = [s for s in scores
                  if s.match == "no" and s.worth_grabbing
                  and s.deal_score >= hunt.free_find_min_score]

    for s in wanted:
        store.set_status(hunt.id, s.listing_id, "wanted")
    for s in free_finds:
        store.set_status(hunt.id, s.listing_id, "free_find")
    result.n_wanted, result.n_free_find = len(wanted), len(free_finds)
    surfaced = [(by_id[s.listing_id], s) for s in wanted + free_finds]
    for notifier in notifiers:
        try:
            notifier.notify(hunt, surfaced)
        except Exception:                                  # noqa: BLE001
            log.exception("notifier %s failed", notifier.name)
    result.n_surfaced = len(surfaced)

    store.finish_run(run_id, error=result.error,
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
