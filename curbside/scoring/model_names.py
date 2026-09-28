"""Which Claude model a run used, and what to call it.

`--model sonnet` is an alias that Claude Code resolves, and each release points
it at that family's newest model. So "always the latest model" is two things:
pass the FAMILY, never a pinned ID, and keep Claude Code updated
(systemd/claude-update.timer). Nothing here knows the version in advance. A run
reports it in its stream (`StreamFacts.model`), and `learn_model` remembers the
newest one seen per family so the settings page can say "Sonnet 5.5".
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("curbside.scoring")

MODEL_FAMILIES = ("haiku", "sonnet", "opus")
DEFAULT_FAMILY = "sonnet"
# The family chosen on /settings. It drives triage, appraisal, the image pass
# AND drafting, because the harness cache is per model: drafting on anything
# but what the poller keeps warm is the ~6x cost ScorerConfig measured.
MODEL_SETTING = "scorer_model"
SEEN_PREFIX = "model_seen:"

_FAMILY_RE = re.compile(r"^claude-(haiku|sonnet|opus)(?:-|$)")
_MODEL_ID_RE = re.compile(r"^claude-([a-z]+)-(\d+(?:-\d{1,2})?)(?:-\d{8})?$")


def resolve_model(m) -> str | None:
    """Family for an alias or a full ID ('Sonnet', 'claude-sonnet-5' ->
    'sonnet'); None if unknown."""
    m = str(m or "").strip().lower()
    if m in MODEL_FAMILIES:
        return m
    hit = _FAMILY_RE.match(m)
    return hit.group(1) if hit else None


def model_name(m: str | None) -> str:
    """'claude-sonnet-5-5' -> 'Sonnet 5.5', 'claude-haiku-4-5-20251001' ->
    'Haiku 4.5', 'sonnet' -> 'Sonnet'. An ID in an unexpected shape is shown
    raw, not guessed at."""
    m = (m or "").strip()
    hit = _MODEL_ID_RE.match(m)
    if hit:
        return f"{hit.group(1).capitalize()} {hit.group(2).replace('-', '.')}"
    return m.capitalize() if m in MODEL_FAMILIES else m


def model_version(m: str | None) -> tuple[int, ...] | None:
    hit = _MODEL_ID_RE.match(m or "")
    return tuple(int(x) for x in hit.group(2).split("-")) if hit else None


def seen_models(store) -> dict[str, str]:
    """Family -> newest model ID a run has reported. Read-only."""
    out = {}
    for fam in MODEL_FAMILIES:
        if seen := store.get_setting(SEEN_PREFIX + fam):
            out[fam] = seen
    return out


def learn_model(store, family: str | None, resolved: str | None) -> str | None:
    """Record the model a run launched as `family` actually used.

    Returns a notice only when the family moved to a NEWER model. Three things
    are stored or ignored quietly, each for a reason:

      * another family: Claude Code can fall back from Opus to Sonnet when the
        Opus allowance runs out, and relabelling "Opus" as "Sonnet 5" would
        flip back on the next run
      * an older version: a run that started before an update can finish after
        one on the new model
      * the first sighting, or the same version under a dated ID
    """
    family = resolve_model(family)
    if not family or not resolved or resolve_model(resolved) != family:
        return None
    key = SEEN_PREFIX + family
    prev = store.get_setting(key)
    if prev == resolved:
        return None
    new_v, old_v = model_version(resolved), model_version(prev)
    if prev and new_v and old_v and new_v < old_v:
        return None
    store.set_setting(key, resolved)
    if not prev or (new_v and new_v == old_v):
        return None
    notice = (f"{family.capitalize()} runs are now on {model_name(resolved)} "
              f"(was {model_name(prev)}).")
    log.warning(notice)
    return notice
