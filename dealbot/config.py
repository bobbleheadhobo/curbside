"""Config loading. One YAML file compiles into the runtime `Hunt` list.

`wants` and `sweeps` are separate in the file because they mean different things
to a person, but both become Hunts so the pipeline has exactly one kind of thing
to run. The compile step is where the sweep picks up every want -- that is what
lets a free listing match "tv stand" without a tv-stand keyword ever being
searched.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import yaml

from .models import Hunt, Location, Want
from .schedule import ScheduleDefaults, parse_hhmm

log = logging.getLogger("dealbot.config")


@dataclass(frozen=True)
class ScorerConfig:
    backend: str = "stub"                  # "stub" | "claude_code"
    triage_model: str = "sonnet"
    appraise_model: str = "sonnet"
    # Drafting a want's search terms. Sonnet, and MEASURED rather than assumed:
    # haiku looks like the cheap choice on the price card (half sonnet's input
    # rate) and is the more expensive one here, by ~6x.
    #
    #   haiku, cold cache   writes 5,602 tok, 1,492 out   $0.0197
    #   haiku, warm cache   reads  5,602 tok,   672 out   $0.0050
    #   sonnet, warm cache  reads  7,516 tok,    40 out   $0.0030
    #
    # Two reasons, both specific to how this bot calls the model. `claude -p`
    # prepends its own ~12k-token harness prompt, so cost is dominated by
    # whether that block is a cache WRITE or a cache READ -- and the cache is
    # per model. The poller runs sonnet every 15 minutes against a 1h TTL, so
    # sonnet's is permanently warm; drafting happens a few times a month, so a
    # second model's cache would be cold essentially every time. And haiku
    # spends 672-1,492 output tokens where sonnet spends 40, because it thinks
    # and pads around the JSON rather than just returning it.
    #
    # Keep the knob: the right answer here is a property of the call pattern,
    # not a fact about the models, and it changes if either one changes.
    suggest_model: str = "sonnet"
    claude_bin: str = "claude"
    timeout_seconds: int = 180
    batch_size: int = 20
    # A hard ceiling on the image pass per run, so a bad day cannot run away.
    max_image_checks: int = 10
    # Photos sent per image pass, downscaled to 512px. Three was arbitrary.
    # Roughly 260 tokens each, so this is about a third of what an image
    # appraisal costs over a text one. Two keeps a fallback for when the
    # seller's hero shot is a room photo or a close-up: a pass that fails to
    # resolve the unknown has cost money and learned nothing, which is worse
    # value than the photo it saved.
    images_per_check: int = 2
    # Resolved against the CONFIG FILE by `load`, for the same reason db_path
    # is: read relative to the working directory it silently falls back to the
    # built-in rubric, so the bot judges by criteria other than the ones in the
    # file you edited, and nothing looks wrong.
    rubric_path: Path | None = None
    # Hard ceiling on model spend per calendar day (UTC). Fetching continues
    # past it -- that costs no quota -- so the bot keeps collecting and simply
    # stops judging. The quota is shared with otter, and an unattended bot
    # draining a cold-start backlog is exactly how you would find that out the
    # hard way.
    daily_cost_limit_usd: float = 10.0
    # The real constraint is plan quota, not dollars, and it is shared with
    # otter's incident triage and your own interactive use. Claude Code reports
    # live utilisation in its rate_limit_event stream, so we can stand aside
    # BEFORE otter gets refused rather than after.
    max_five_hour_utilization: float = 0.70
    max_seven_day_utilization: float = 0.90
    # A utilisation reading only arrives with a model call, so pausing on it
    # would otherwise deadlock: no calls, no fresh number, no way back. A
    # reading older than this is treated as unknown and one run is let through
    # to refresh it.
    utilization_stale_minutes: int = 30
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
class RecheckConfig:
    """Asking the source whether a find is still there.

    Costs requests and no model quota, so it is on by default -- but it is
    requests against sources that throttle, hence the pacing."""
    enabled: bool = True
    every_hours: float = 6.0        # per listing, not per run
    max_per_run: int = 10


@dataclass(frozen=True)
class DiscordConfig:
    enabled: bool = False
    dashboard_url: str = "http://koda.taila4e463.ts.net:8477"
    mention_score: float = 8.0      # @mention in the muted free channel above this
    max_per_run: int = 10
    max_age_days: int = 7           # cold-start suppression
    pause_seconds: float = 1.2


@dataclass(frozen=True)
class HuntDefaults:
    min_deal_score: float = 7.0
    free_find_min_score: float = 5.0
    max_results: int = 60


@dataclass(frozen=True)
class SweepSpec:
    """A broad trawl, as the file describes it. Kept as a spec rather than
    compiled straight to a Hunt because a sweep carries every want, and the want
    list now changes at runtime -- see `Config.hunts`."""
    name: str
    queries: tuple[str, ...] = ()
    max_price_cents: int | None = None
    exclude: tuple[str, ...] = ()
    min_deal_score: float | None = None
    free_find_min_score: float | None = None
    interval_minutes: int = 15
    max_results: int | None = None
    max_age_days: int = 7
    enabled: bool = True


@dataclass(frozen=True)
class WantHuntSpec:
    """Per-want overrides for that want's own targeted hunt (`want_hunts:`)."""
    exclude: tuple[str, ...] = ()
    min_deal_score: float | None = None
    free_find_min_score: float | None = None
    # Priced items don't evaporate the way free ones do, so they are polled
    # hourly rather than every 15 minutes.
    interval_minutes: int = 60
    max_results: int | None = None
    max_age_days: int = 0
    enabled: bool = True


@dataclass(frozen=True)
class Config:
    location: Location
    wants: tuple[Want, ...]
    scorer: ScorerConfig
    db_path: Path
    sources: tuple[str, ...]
    facebook: FacebookConfig
    craigslist: CraigslistConfig
    discord: DiscordConfig
    recheck: RecheckConfig = RecheckConfig()
    sweeps: tuple[SweepSpec, ...] = ()
    want_hunts: Mapping[str, WantHuntSpec] = field(default_factory=dict)
    defaults: HuntDefaults = HuntDefaults()
    # Cadence set from the dashboard, keyed by hunt id. Overrides the file.
    interval_overrides: Mapping[str, int] = field(default_factory=dict)
    # Words blocked from the dashboard, keyed by hunt id. These ADD to the
    # file's `exclude`; a reviewed line in a committed file is not something a
    # tap should be able to delete.
    exclude_extra: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # True once the file's terms have been seeded into the table, after which
    # the table is the whole list and the file is no longer consulted -- so a
    # term removed on the dashboard stays removed.
    exclude_owned: bool = False
    schedule: ScheduleDefaults = ScheduleDefaults()

    def _exclude(self, hunt_id: str, from_file: tuple[str, ...]) -> tuple[str, ...]:
        if self.exclude_owned:
            return tuple(self.exclude_extra.get(hunt_id, ()))
        return tuple(dict.fromkeys(
            from_file + tuple(self.exclude_extra.get(hunt_id, ()))))

    @property
    def hunts(self) -> tuple[Hunt, ...]:
        """The runnable hunts, COMPUTED from the current want list.

        This used to be a field compiled once at load. It is derived now because
        wants are editable from the dashboard: adding one has to produce its
        hunt, and removing one has to take that hunt away, in a web process that
        has been up for weeks. Deriving costs a few object allocations per page
        and removes the whole class of bug where the file and the database
        disagree about what is running.
        """
        hunts: list[Hunt] = []

        # Sweeps carry EVERY want, so a free listing can match any of them --
        # that is what lets the sweep find a tv stand without ever searching for
        # one. Which also means adding a want changes the sweep's prompt.
        for s in self.sweeps:
            hunts.append(self._hunt(
                f"sweep:{s.name}", s.name, "sweep", s,
                queries=s.queries, max_price_cents=s.max_price_cents,
                wants=self.wants))

        # A want with queries also gets its own targeted hunt -- the sweep only
        # ever sees free items, so a want with a budget is invisible to it.
        for w in self.wants:
            if not w.queries:
                continue
            hunts.append(self._hunt(
                f"want:{w.name}", w.name, "want",
                self.want_hunts.get(w.name) or WantHuntSpec(),
                queries=w.queries, max_price_cents=w.max_price_cents,
                wants=(w,)))
        return tuple(hunts)

    def _hunt(self, hid: str, name: str, kind: str, spec, *,
              queries, max_price_cents, wants) -> Hunt:
        """One hunt, with every per-hunt override resolved against the defaults.

        Sweeps and want-hunts differ in six values and agreed on the other
        five, which were written out twice -- including four copies each of the
        `d.X if spec.X is None else spec.X` fallback. A new tunable on `Hunt`
        was a four-place edit that failed by half-working.
        """
        d = self.defaults

        def override(field: str):
            """A spec value of None means "no opinion, use the default"."""
            chosen = getattr(spec, field)
            return getattr(d, field) if chosen is None else chosen

        return Hunt(
            id=hid, name=name, kind=kind,
            queries=queries, max_price_cents=max_price_cents,
            exclude=self._exclude(hid, spec.exclude), wants=wants,
            min_deal_score=override("min_deal_score"),
            free_find_min_score=override("free_find_min_score"),
            # The cadence is the dashboard's to set, so it is looked up rather
            # than resolved against the file's default.
            interval_minutes=self.interval_overrides.get(
                hid, spec.interval_minutes),
            max_results=override("max_results"),
            max_age_days=spec.max_age_days, enabled=spec.enabled)


def with_store(cfg: Config, store) -> Config:
    """Overlay what the dashboard owns: the want list and the cadences.

    `config.yaml` seeds the wants table once and is then no longer consulted for
    them, so this is what makes an edit made on a phone take effect on the next
    timer tick with nothing to restart. Called per request in the dashboard and
    once per command in the CLI.
    """
    store.seed_wants(cfg.wants)
    # Keyed off the specs rather than `cfg.hunts`, which is computed from the
    # want list this function is in the middle of replacing.
    from_file = {f"sweep:{sw.name}": sw.exclude for sw in cfg.sweeps}
    from_file.update({f"want:{n}": spec.exclude
                      for n, spec in cfg.want_hunts.items()})
    store.seed_excludes(from_file)

    # The numbers the dashboard owns. Same shape as the cadences: the file
    # supplies the default and a settings row wins. Each one carries its own
    # destination in `Store.TUNING`, so adding a fifth is one line there rather
    # than a line there and a `replace` here.
    tune = store.tuning()
    landing: dict[str, dict[str, Any]] = {}
    for key, (*_, dest) in store.TUNING.items():
        if key in tune:
            landing.setdefault(dest, {})[key] = tune[key]
    tuned = {dest: replace(getattr(cfg, dest), **fields)
             for dest, fields in landing.items()}
    return replace(cfg, **tuned,
                   wants=tuple(s.want for s in store.wants()),
                   interval_overrides=store.hunt_intervals(),
                   exclude_extra=store.hunt_excludes(),
                   exclude_owned=True)


def _cents(value: Any) -> int | None:
    if value is None:
        return None
    return int(round(float(value) * 100))


def _schedule_defaults(raw: dict) -> ScheduleDefaults:
    """The STARTING window, and the timezone.

    Only the timezone really belongs in the file -- it is a fact about where you
    live, not a preference. The window is here so a fresh install has one, and
    is overridden by the `settings` table the moment it is set from the web.
    """
    tz = tz_name = None
    name = raw.get("timezone")
    if name:
        try:
            from zoneinfo import ZoneInfo
            tz, tz_name = ZoneInfo(str(name)), str(name)
        except Exception:                                  # noqa: BLE001
            # An unknown zone must not stop the bot. Falling back to the
            # machine's own clock is right far more often than refusing to run.
            log.warning("unknown schedule.timezone %r; using local time", name)
    start = parse_hhmm(raw.get("start")) 
    end = parse_hhmm(raw.get("end"))
    return ScheduleDefaults(
        enabled=bool(raw.get("enabled", False)),
        start_minute=12 * 60 if start is None else start,
        end_minute=20 * 60 if end is None else end,
        tz=tz, tz_name=tz_name)


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

    dflt = raw.get("defaults") or {}
    defaults = HuntDefaults(
        min_deal_score=float(dflt.get("min_deal_score", 7.0)),
        free_find_min_score=float(dflt.get("free_find_min_score", 5.0)),
        max_results=int(dflt.get("max_results_per_run", 60)),
    )

    # The file's wants are a SEED. `db.seed_wants` copies them in the first time
    # a database is opened and never again, after which the table is the truth
    # and the dashboard owns it -- see `with_store`.
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

    sweeps = tuple(
        SweepSpec(
            name=s["name"],
            queries=tuple(s.get("queries") or ()),
            max_price_cents=_cents(s.get("max_price")),
            exclude=tuple(s.get("exclude") or ()),
            min_deal_score=(None if s.get("min_deal_score") is None
                            else float(s["min_deal_score"])),
            free_find_min_score=(None if s.get("free_find_min_score") is None
                                 else float(s["free_find_min_score"])),
            interval_minutes=int(s.get("interval_minutes", 15)),
            max_results=(None if s.get("max_results") is None
                         else int(s["max_results"])),
            max_age_days=int(s.get("max_age_days", 7)),
            enabled=bool(s.get("enabled", True)),
        )
        for s in (raw.get("sweeps") or [])
    )

    want_hunts = {
        name: WantHuntSpec(
            exclude=tuple(c.get("exclude") or ()),
            min_deal_score=(None if c.get("min_deal_score") is None
                            else float(c["min_deal_score"])),
            free_find_min_score=(None if c.get("free_find_min_score") is None
                                 else float(c["free_find_min_score"])),
            interval_minutes=int(c.get("interval_minutes", 60)),
            max_results=(None if c.get("max_results") is None
                         else int(c["max_results"])),
            max_age_days=int(c.get("max_age_days", 0)),
            enabled=bool(c.get("enabled", True)),
        )
        for name, c in (raw.get("want_hunts") or {}).items()
    }

    schedule = _schedule_defaults(raw.get("schedule") or {})

    sc = raw.get("scorer") or {}
    scorer = ScorerConfig(
        backend=sc.get("backend", "stub"),
        triage_model=sc.get("triage_model", "sonnet"),
        suggest_model=sc.get("suggest_model", "sonnet"),
        appraise_model=sc.get("appraise_model", "sonnet"),
        claude_bin=sc.get("claude_bin", "claude"),
        timeout_seconds=int(sc.get("timeout_seconds", 180)),
        batch_size=int(sc.get("batch_size", 20)),
        max_image_checks=int(sc.get("max_image_checks", 10)),
        images_per_check=int(sc.get("images_per_check", 2)),
        daily_cost_limit_usd=float(sc.get("daily_cost_limit_usd", 10.0)),
        max_five_hour_utilization=float(sc.get("max_five_hour_utilization", 0.70)),
        max_seven_day_utilization=float(sc.get("max_seven_day_utilization", 0.90)),
        utilization_stale_minutes=int(sc.get("utilization_stale_minutes", 30)),
        rate_limit_fallback_seconds=int(sc.get("rate_limit_fallback_seconds", 5 * 3600)),
        rubric_path=(config_path.parent
                     / sc.get("rubric_path", "prompts/rubric.md")),
    )

    # A file-consistency check, and only that. It runs against the file's own
    # wants: once the table is the truth a want can be deleted from the
    # dashboard while the file still names it, and THAT must not raise -- an
    # exception here takes the bot off the air over a stale comment.
    unknown = set(want_hunts) - set(by_name)
    if unknown:
        raise ValueError(f"want_hunts references unknown wants: {sorted(unknown)}")

    rc = raw.get("recheck") or {}
    recheck = RecheckConfig(
        enabled=bool(rc.get("enabled", True)),
        every_hours=float(rc.get("every_hours", 6.0)),
        max_per_run=int(rc.get("max_per_run", 10)),
    )

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
        wants=wants,
        sweeps=sweeps,
        want_hunts=want_hunts,
        defaults=defaults,
        schedule=schedule,
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
        recheck=recheck,
    )
