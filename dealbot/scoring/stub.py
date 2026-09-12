"""Deterministic stand-in for the real scorer.

Exists so P0 runs end to end with no network and no quota. It is a crude keyword
heuristic and is NOT meant to be good -- it is meant to be *predictable*, so the
pipeline, the store and the dashboard can be exercised and tested without
spending anything.

It is also a useful demonstration of the gap. It will happily match "Free
entertainment center" against the tv-stand want and miss that the listing says
40 inches wide when the want demands 70 -- exactly the judgement the model is
there to supply.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from ..models import Candidate, Hunt, Score, Want
from .base import TriageResult

JUNK = ("not working", "doesn't spin", "for parts", "parts only", "needs repair",
        "broken", "as is", "damaged")
SERVICE = ("free estimate", "licensed and insured", "call now", "free quote",
           "free consultation")


def _want_terms(want: Want) -> list[str]:
    terms = {want.name.replace("-", " ")}
    terms.update(q.lower() for q in want.queries)
    return sorted(terms)


class StubScorer:
    name = "stub"

    def triage(self, hunt: Hunt, candidates: Sequence[Candidate]) -> TriageResult:
        kept, notes = [], {}
        for c in candidates:
            hay = f"{c.listing.title}\n{c.listing.description or ''}".lower()
            if any(s in hay for s in SERVICE):
                notes[c.listing.id] = "looks like a service ad"
                continue
            hit_want = any(t in hay for w in hunt.wants for t in _want_terms(w))
            is_free = c.listing.price_cents == 0
            if hit_want or is_free:
                kept.append(c)
            else:
                notes[c.listing.id] = "no want keyword and not free"
        return TriageResult(kept, notes)

    def suggest_queries(self, name: str, description: str,
                        requires: Sequence[str] = ()) -> tuple[str, ...]:
        """The name, and nothing cleverer.

        Predictable rather than good, like the rest of this class: it makes the
        blank-queries path exercisable offline and gives `backend: stub` a want
        that actually searches for something. The real scorer is what knows
        that "media console" and "credenza" are the same object as "tv stand".
        """
        term = " ".join(name.replace("-", " ").split())
        return (term,) if term else ()

    def appraise(self, hunt: Hunt, candidates: Sequence[Candidate]) -> list[Score]:
        now = datetime.now(timezone.utc)
        out: list[Score] = []
        for c in candidates:
            l = c.listing
            hay = f"{l.title}\n{l.description or ''}".lower()

            score = 0.0
            matched: str | None = None
            flags: list[str] = []

            for want in hunt.wants:
                terms = _want_terms(want)
                if any(t in l.title.lower() for t in terms):
                    score += 5; matched = want.name; break
                if any(t in hay for t in terms):
                    score += 2; matched = want.name; break

            if l.price_cents == 0:
                score += 2
            if any(j in hay for j in JUNK):
                score -= 4; flags.append("described as broken or for parts")
            if any(s in hay for s in SERVICE):
                score -= 5; flags.append("looks like a service ad, not an item")
            if l.images:
                score += 1
            else:
                flags.append("no photos")
            if l.distance_mi is not None and l.distance_mi <= 10:
                score += 1

            score = max(0.0, min(10.0, score))
            # The stub cannot evaluate a hard requirement, so it never claims
            # "yes" for a want that has any -- it reports the honest "unknown"
            # and lets the score decide where it lands: the wants bin if it
            # clears the bar (flagged amber), /skipped if it does not.
            want = next((w for w in hunt.wants if w.name == matched), None)
            if matched is None:
                match = "no" if score < 4 else "unknown"
            elif want and want.requires:
                match = "unknown"
            else:
                match = "yes"
            out.append(Score(
                listing_id=l.id, hunt_id=hunt.id, model="stub", scored_at=now,
                match=match, deal_score=score,
                worth_grabbing=(matched is None and score >= 4),
                est_value_cents=None, condition=None, matched_want=matched,
                unknowns=tuple(want.requires) if (want and want.requires) else (),
                requirements=tuple(
                    {"req": r, "met": "unknown", "evidence": "stub cannot judge"}
                    for r in (want.requires if want else ())),
                red_flags=tuple(flags),
                reasoning=("stub scorer: keyword heuristic only, no real judgement "
                           f"(matched_want={matched})"),
            ))
        return out
