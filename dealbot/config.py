"""Config loading. One YAML file compiles into the runtime `Hunt` list.

`wants` and `sweeps` are separate in the file because they mean different things
to a person, but both become Hunts so the pipeline has exactly one kind of thing
to run. The compile step is where the sweep picks up every want -- that is what
lets a free listing match "tv stand" without a tv-stand keyword ever being
searched.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .models import Hunt, Location, Want


@dataclass(frozen=True)
class ScorerConfig:
    backend: str = "stub"                  # "stub" | "claude_code"
    triage_model: str = "sonnet"
    appraise_model: str = "sonnet"
    claude_bin: str = "claude"
    timeout_seconds: int = 180
    batch_size: int = 20
    # A hard ceiling on the image pass per run, so a bad day cannot run away.
    max_image_checks: int = 10
    # Hard ceiling on model spend per calendar day (UTC). Fetching continues
    # past it -- that costs no quota -- so the bot keeps collecting and simply
    # stops judging. The quota is shared with otter, and an unattended bot
    # draining a cold-start backlog is exactly how you would find that out the
    # hard way.
    daily_cost_limit_usd: float = 3.0
    # Pause scoring when Claude Code rejects us, and resume at resetsAt. Never 0:
    # a falsy resume deadline reads as "no deadline, resume now", which would make
    # the pause a silent no-op. (Lesson borrowed from otter.)
    rate_limit_fallback_seconds: int = 5 * 3600


@dataclass(frozen=True)
class FacebookConfig:
    city: str = "albuquerque"
    # Deliberately generous. Throttling is silent -- Facebook keeps answering
    # 200 with a full-size page containing no data -- so the interval is set for
    # never noticing it rather than for speed.
    min_interval_seconds: float = 15.0
    max_requests_per_run: int = 25
    fetch_details: bool = True


@dataclass(frozen=True)
class CraigslistConfig:
    area_id: int = 50                       # 50 = albuquerque
    min_interval_seconds: float = 4.0
    max_requests_per_run: int = 40


@dataclass(frozen=True)
class DiscordConfig:
    enabled: bool = False
    dashboard_url: str = "http://koda.taila4e463.ts.net:8477"
    mention_score: float = 8.0      # @mention in the muted free channel above this
    max_per_run: int = 10
    max_age_days: int = 7           # cold-start suppression
    pause_seconds: float = 1.2


@dataclass(frozen=True)
class Config:
    location: Location
    hunts: tuple[Hunt, ...]
    wants: tuple[Want, ...]
    scorer: ScorerConfig
    db_path: Path
    sources: tuple[str, ...]
    facebook: FacebookConfig
    craigslist: CraigslistConfig
    discord: DiscordConfig

    @property
    def source(self) -> str:
        """Back-compat for single-source callers and tests."""
        return self.sources[0]


def _cents(value: Any) -> int | None:
    if value is None:
        return None
    return int(round(float(value) * 100))


def load(path: str | os.PathLike[str] = "config.yaml") -> Config:
    config_path = Path(path).resolve()
    raw = yaml.safe_load(config_path.read_text()) or {}

    # Your actual coordinates go in .env (gitignored); config.yaml keeps a
    # rounded city-level fallback so it stays shareable. Precision is capped by
    # the marketplaces anyway -- 61% of listings are snapped to a neighbourhood
    # centre -- but measuring from your street instead of downtown removes a
    # systematic bias from every distance.
    loc = raw.get("location") or {}
    location = Location(
        lat=float(os.environ.get("HOME_LAT") or loc["lat"]),
        lng=float(os.environ.get("HOME_LNG") or loc["lng"]),
        radius_miles=float(os.environ.get("RADIUS_MILES")
                           or loc.get("radius_miles", 25)),
    )

    defaults = raw.get("defaults") or {}
    default_score = float(defaults.get("min_deal_score", 7.0))
    default_free_score = float(defaults.get("free_find_min_score", 5.0))
    default_max_results = int(defaults.get("max_results_per_run", 60))

    wants = tuple(
        Want(
            name=w["name"],
            description=w["description"].strip(),
            max_price_cents=_cents(w["max_price"]) or 0,
            queries=tuple(w.get("queries") or ()),
            requires=tuple(w.get("requires") or ()),
        )
        for w in (raw.get("wants") or [])
    )
    by_name = {w.name: w for w in wants}

    hunts: list[Hunt] = []

    # Sweeps carry every want, so the model can match a free listing against the
    # whole list at once.
    for s in raw.get("sweeps") or []:
        hunts.append(Hunt(
            id=f"sweep:{s['name']}",
            name=s["name"],
            kind="sweep",
            queries=tuple(s.get("queries") or ()),
            max_price_cents=_cents(s.get("max_price")),
            exclude=tuple(s.get("exclude") or ()),
            wants=wants,
            min_deal_score=float(s.get("min_deal_score", default_score)),
            free_find_min_score=float(
                s.get("free_find_min_score", default_free_score)),
            interval_minutes=int(s.get("interval_minutes", 15)),
            max_results=int(s.get("max_results", default_max_results)),
            max_age_days=int(s.get("max_age_days", 7)),
            enabled=bool(s.get("enabled", True)),
        ))

    # A want with queries also gets its own targeted hunt -- the sweep only ever
    # sees free items, so a want with a budget is invisible to it.
    for w in wants:
        if not w.queries:
            continue
        cfg = (raw.get("want_hunts") or {}).get(w.name, {})
        hunts.append(Hunt(
            id=f"want:{w.name}",
            name=w.name,
            kind="want",
            queries=w.queries,
            max_price_cents=w.max_price_cents,
            exclude=tuple(cfg.get("exclude") or ()),
            wants=(w,),
            min_deal_score=float(cfg.get("min_deal_score", default_score)),
            free_find_min_score=float(
                cfg.get("free_find_min_score", default_free_score)),
            # Priced items don't evaporate the way free ones do, so they are
            # polled hourly rather than every 15 minutes. Keeps request volume
            # down without costing anything real.
            interval_minutes=int(cfg.get("interval_minutes", 60)),
            max_results=int(cfg.get("max_results", default_max_results)),
            max_age_days=int(cfg.get("max_age_days", 0)),
            enabled=bool(cfg.get("enabled", True)),
        ))

    sc = raw.get("scorer") or {}
    scorer = ScorerConfig(
        backend=sc.get("backend", "stub"),
        triage_model=sc.get("triage_model", "sonnet"),
        appraise_model=sc.get("appraise_model", "sonnet"),
        claude_bin=sc.get("claude_bin", "claude"),
        timeout_seconds=int(sc.get("timeout_seconds", 180)),
        batch_size=int(sc.get("batch_size", 20)),
        max_image_checks=int(sc.get("max_image_checks", 10)),
        daily_cost_limit_usd=float(sc.get("daily_cost_limit_usd", 3.0)),
        rate_limit_fallback_seconds=int(sc.get("rate_limit_fallback_seconds", 5 * 3600)),
    )

    unknown = set((raw.get("want_hunts") or {})) - set(by_name)
    if unknown:
        raise ValueError(f"want_hunts references unknown wants: {sorted(unknown)}")

    dc = raw.get("discord") or {}
    discord = DiscordConfig(
        enabled=bool(dc.get("enabled", False)),
        dashboard_url=dc.get("dashboard_url", "http://koda.taila4e463.ts.net:8477"),
        mention_score=float(dc.get("mention_score", 8.0)),
        max_per_run=int(dc.get("max_per_run", 10)),
        max_age_days=int(dc.get("max_age_days", 7)),
        pause_seconds=float(dc.get("pause_seconds", 1.2)),
    )

    cl = raw.get("craigslist") or {}
    craigslist = CraigslistConfig(
        area_id=int(cl.get("area_id", 50)),
        min_interval_seconds=float(cl.get("min_interval_seconds", 4.0)),
        max_requests_per_run=int(cl.get("max_requests_per_run", 40)),
    )

    fb = raw.get("facebook") or {}
    facebook = FacebookConfig(
        city=fb.get("city", "albuquerque"),
        min_interval_seconds=float(fb.get("min_interval_seconds", 15.0)),
        max_requests_per_run=int(fb.get("max_requests_per_run", 25)),
        fetch_details=bool(fb.get("fetch_details", True)),
    )

    return Config(
        location=location,
        hunts=tuple(hunts),
        wants=wants,
        scorer=scorer,
        # Resolved against the CONFIG FILE, not the working directory. A
        # relative db_path interpreted per-CWD is how the data ended up split
        # across three databases -- one of which held the best find so far.
        db_path=(config_path.parent / raw.get("db_path", "data/dealbot.db")),
        # `sources` is the list; `source` remains accepted as a single value.
        sources=tuple(raw.get("sources") or [raw.get("source", "fixture")]),
        facebook=facebook,
        craigslist=craigslist,
        discord=discord,
    )
