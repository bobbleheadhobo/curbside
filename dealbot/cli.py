"""Command line entry point."""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as config_mod
from . import schedule as schedule_mod
from .db import Store
from .env import load_env
from .images import FixtureImageProvider, HttpImageProvider
from .notify.dashboard import DashboardNotifier
from .notify.discord import DiscordNotifier
from .pipeline import announce_price_drops, dry_run, run_hunt
from .recheck import recheck
from .scoring.claude_code import ClaudeCodeScorer
from .scoring.stub import StubScorer
from .sources.craigslist import CraigslistSource
from .sources.facebook import FacebookSource
from .sources.fixture import FixtureSource
from .thumbs import ThumbnailStore

log = logging.getLogger("dealbot.cli")


def _build_image_provider(source_name: str):
    if source_name == "fixture":
        return FixtureImageProvider()
    return HttpImageProvider()


def _reset_budgets(sources) -> None:
    """ONE budget for the whole pass, not per hunt.

    Reset inside the hunt loop, `max_requests_per_run: 25` quietly meant 25 PER
    HUNT -- 75 requests in a single `once` against a source that starts
    throttling, silently, after about five rapid ones.
    """
    for _, source in sources:
        if hasattr(source, "reset_budget"):
            source.reset_budget()


def _build_one_source(cfg: config_mod.Config, name: str):
    if name == "fixture":
        return FixtureSource(cfg.location)
    if name == "facebook":
        return FacebookSource(
            cfg.location, city=cfg.facebook.city,
            min_interval_seconds=cfg.facebook.min_interval_seconds,
            max_requests_per_run=cfg.facebook.max_requests_per_run)
    if name == "craigslist":
        return CraigslistSource(
            cfg.location, area_id=cfg.craigslist.area_id,
            min_interval_seconds=cfg.craigslist.min_interval_seconds,
            max_requests_per_run=cfg.craigslist.max_requests_per_run)
    raise SystemExit(f"unknown source {name!r}")


def _build_sources(cfg: config_mod.Config):
    """Each source gets its own run per hunt, so one being throttled is visible
    in the runs table instead of quietly halving the results."""
    return [(n, _build_one_source(cfg, n)) for n in cfg.sources]


def _build_notifiers(cfg: config_mod.Config, store):
    notifiers = [DashboardNotifier(store)]
    if cfg.discord.enabled:
        d = cfg.discord
        notifiers.append(DiscordNotifier(
            store, dashboard_url=d.dashboard_url, mention_score=d.mention_score,
            max_per_run=d.max_per_run, max_age_days=d.max_age_days,
            pause_seconds=d.pause_seconds))
    return notifiers


def _build_scorer(cfg: config_mod.Config, store):
    if cfg.scorer.backend == "stub":
        return StubScorer()
    if cfg.scorer.backend == "claude_code":
        return ClaudeCodeScorer(cfg.scorer, store)
    raise SystemExit(f"unknown scorer backend {cfg.scorer.backend!r}")


def _open(args):
    """Config from the file, with everything the dashboard owns laid over it.

    `config.yaml` seeds the wants table on a database's first open and is not
    consulted for wants again -- so this is the single point where a want added
    on a phone becomes a hunt that runs. Every command goes through it.
    """
    cfg = config_mod.load(args.config)
    store = Store(cfg.db_path)
    return config_mod.with_store(cfg, store), store


def _is_due(store, hunt, source_name: str) -> bool:
    """Cadence for timer-driven runs. The timer fires on a fixed period; each
    hunt decides for itself whether enough time has passed -- free sweeps every
    15 minutes because free things evaporate, want searches hourly because
    priced ones do not."""
    last = store.last_success_at(hunt.id, source_name)
    if last is None:
        return True
    try:
        when = datetime.fromisoformat(last)
    except (ValueError, TypeError):
        return True
    if when.tzinfo is None:
        # Everything we write is timezone-aware, but a hand-edited or imported
        # row would otherwise raise TypeError here and kill the whole run.
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when >= timedelta(
        minutes=hunt.interval_minutes)


def _hunts(cfg: config_mod.Config, name: str | None, store=None):
    """Enabled in config AND not switched off from the dashboard."""
    hunts = [h for h in cfg.hunts if h.enabled]
    if store is not None:
        off = store.disabled_hunts()
        hunts = [h for h in hunts if h.id not in off]
    if name:
        hunts = [h for h in hunts if h.name == name or h.id == name]
        if not hunts:
            raise SystemExit(f"no enabled hunt matching {name!r}")
    return hunts


def cmd_once(args) -> int:
    cfg, store = _open(args)

    # Outside its waking hours the timer's pass does NOTHING -- no fetch, no
    # judgement, no re-check. Unlike the quota and rate-limit pauses, which
    # keep collecting because collecting is free, this is a decision rather
    # than an interruption: nothing found at 3am can be collected at 3am.
    #
    # Only `--due` is gated. A bare `dealbot once` is a person at a keyboard
    # asking for a pass, and refusing that would be obstinate rather than
    # thrifty.
    sched = schedule_mod.load(store, cfg.schedule)
    if args.due and not sched.is_open():
        opens = sched.opens_at()
        print(f"asleep until {schedule_mod.fmt_clock(sched.start_minute)}"
              + (f" ({opens:%H:%M})" if opens else "")
              + f"; awake {sched.window_label}")
        store.close()
        return 0
    sources, scorer = _build_sources(cfg), _build_scorer(cfg, store)
    notifiers = _build_notifiers(cfg, store)

    _reset_budgets(sources)

    for hunt in _hunts(cfg, args.hunt, store):
        for name, source in sources:
            if args.due and not _is_due(store, hunt, name):
                continue
            if args.dry_run:
                print(json.dumps(dry_run(store, hunt, source, cfg.location),
                                 indent=2))
                continue
            r = run_hunt(store, hunt, source, scorer, notifiers, cfg.location,
                         no_score=args.no_score,
                         image_provider=None if args.no_images
                         else _build_image_provider(name),
                         max_image_checks=cfg.scorer.max_image_checks,
                         thumbnails=ThumbnailStore(cfg.db_path.parent / "thumbs"))
            status = f"ERROR {r.error}" if r.error else (
                f"fetched={r.n_fetched} new={r.n_new} cand={r.n_candidates} "
                f"scored={r.n_scored} wanted={r.n_wanted} free={r.n_free_find} "
                f"imgs={r.n_image_checks} cost=${r.cost_usd:.4f}")
            print(f"{hunt.id:24} {name:11} {status}")

    # Rides along with the pass that is already running, and is paced per
    # listing, so most ticks it does nothing. It costs requests and no quota.
    if cfg.recheck.enabled and not args.dry_run:
        rc = recheck(store, sources, every_hours=cfg.recheck.every_hours,
                     max_per_run=cfg.recheck.max_per_run)
        if rc.n_checked or rc.error:
            print(f"{'recheck':24} {'':11} checked={rc.n_checked} "
                  f"sold={rc.n_sold} removed={rc.n_removed} "
                  f"listed={rc.n_listed}" + (f" {rc.error}" if rc.error else ""))
    # Rides along after the re-check that just refreshed those prices. No
    # quota, no requests: it reads what is already on disk.
    if not args.dry_run:
        announce_price_drops(store, notifiers)

    store.close()
    return 0


def cmd_recheck(args) -> int:
    """Ask each source whether the things in your bins are still there.

    No search, no model calls: one detail fetch per listing, paced. `mark_gone`
    only ever knew that a listing had stopped APPEARING, which for a 15-minute
    sweep is 45 minutes off page one -- this is the difference between that and
    a listing that actually sold."""
    cfg, store = _open(args)
    sources = _build_sources(cfg)
    _reset_budgets(sources)
    rc = recheck(store, sources,
                 every_hours=0.0 if args.all else cfg.recheck.every_hours,
                 max_per_run=args.limit or cfg.recheck.max_per_run)
    print(f"checked {rc.n_checked}: {rc.n_sold} sold, {rc.n_removed} removed, "
          f"{rc.n_listed} still listed")
    if rc.error:
        print(f"  {rc.error}")
    store.close()
    return 0


def cmd_notify(args) -> int:
    """Flush pending notifications without fetching or scoring anything.

    Catch-up otherwise rides along with a hunt's own cadence, so something stuck
    under an hourly want search waits up to an hour. This costs nothing -- no
    requests, no model calls -- and is the way to drain a backlog on demand."""
    cfg, store = _open(args)
    notifiers = _build_notifiers(cfg, store)
    hunts = _hunts(cfg, args.hunt, store)   # `cfg.hunts` is computed: bind once
    total = 0
    for hunt in hunts:
        pending = store.pending_notifications(hunt.id)
        if not pending:
            continue
        print(f"{hunt.id:24} {len(pending)} pending")
        for n in notifiers:
            try:
                n.notify(hunt, [])
            except Exception:                              # noqa: BLE001
                log.exception("notifier %s failed", n.name)
        total += len(pending)
    total += announce_price_drops(store, notifiers)
    still = sum(len(store.pending_notifications(h.id)) for h in hunts)
    print(f"queued {total}, {still} still pending (per-run caps defer the rest)")
    store.close()
    return 0


def cmd_seed_demo(args) -> int:
    """Build a dashboard-development database. Offline, free, deterministic."""
    from .demo import build
    cfg = config_mod.load(args.config)
    store = build(cfg, args.db)
    counts = {r["status"]: r["n"] for r in store.conn.execute(
        "SELECT status, COUNT(*) n FROM hunt_matches GROUP BY status")}

    # Write the config too. db_path resolves against the CONFIG FILE, so a
    # hand-rolled one in /tmp silently points at a database that does not exist
    # and every view renders empty -- which looks like a bug in the dashboard.
    src = Path(args.config).resolve()
    out = src.parent / "config.demo.yaml"
    text = src.read_text()
    text = re.sub(r"^db_path:.*$", f"db_path: {args.db}", text, flags=re.M)
    text = re.sub(r"^(\s+)backend:.*$", r"\1backend: stub", text, flags=re.M)
    text = re.sub(r"^(\s+)enabled: true.*$", r"\1enabled: false", text, flags=re.M)
    out.write_text("# Generated by `dealbot seed-demo`. Offline: fixture source,\n"
                   "# stub scorer, notifications off. Safe to delete.\n" + text)

    print(f"built {args.db}")
    print("  " + " · ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print(f"\n  serve it:\n"
          f"    .venv/bin/python -m dealbot.cli --config {out.name} serve --port 8478")
    store.close()
    return 0


def cmd_prune_thumbs(args) -> int:
    """Drop cached photos for listings no longer in a bin or triaged."""
    cfg, store = _open(args)
    thumbs = ThumbnailStore(cfg.db_path.parent / "thumbs")
    keep = {r["listing_id"] for r in store.conn.execute(
        """SELECT listing_id FROM hunt_matches WHERE status IN
           ('wanted','free_find','saved','contacted')""")}
    before = thumbs.disk_usage_mb()
    removed = thumbs.prune(keep)
    print(f"kept {len(keep)}, removed {removed} "
          f"({before:.1f}MB -> {thumbs.disk_usage_mb():.1f}MB)")
    store.close()
    return 0


def cmd_hunts(args) -> int:
    cfg, store = _open(args)
    off = store.disabled_hunts()
    sched = schedule_mod.load(store, cfg.schedule)
    if not sched.always_on:
        print(f"awake {sched.window_label}"
              f"{' -- ASLEEP NOW' if not sched.is_open() else ''}"
              f"{f' ({sched.tz_name})' if sched.tz_name else ''}\n")
    print(f"{'hunt':28} {'kind':6} {'every':>6}  {'max$':>6}  last run")
    for h in cfg.hunts:
        row = store.conn.execute(
            "SELECT started_at, n_surfaced, error FROM runs WHERE hunt_id=? "
            "ORDER BY id DESC LIMIT 1", (h.id,)).fetchone()
        cap = "-" if h.max_price_cents is None else f"{h.max_price_cents/100:.0f}"
        last = "never"
        if row:
            last = f"{row['started_at']} surfaced={row['n_surfaced']}"
            if row["error"]:
                last += f" ERROR: {row['error']}"
        state = "  [PAUSED]" if h.id in off else ""
        print(f"{h.id:28} {h.kind:6} {h.interval_minutes:>5}m  {cap:>6}  {last}{state}")
    store.close()
    return 0


def cmd_serve(args) -> int:
    import uvicorn
    from .web.app import create_app
    cfg = config_mod.load(args.config)
    # The dashboard gets a scorer for exactly ONE job: drafting search terms
    # for a want whose author left them blank. Built here rather than in the
    # web module because this is the composition root, and passed in so the
    # tests can hand it a fake -- or nothing, which simply turns the drafting
    # off. It spends through the same ceilings and pauses as the poller.
    scorer = _build_scorer(cfg, Store(cfg.db_path))
    uvicorn.run(create_app(cfg, scorer=scorer),
                host=args.host, port=args.port, log_level="info")
    return 0


def cmd_run(args) -> int:
    """Poll loop. Each hunt runs on its own cadence -- free sweeps every 15
    minutes because free items evaporate, want searches hourly because priced
    ones do not."""
    cfg, store = _open(args)
    sources, scorer = _build_sources(cfg), _build_scorer(cfg, store)
    notifiers = _build_notifiers(cfg, store)
    next_due: dict[str, float] = {}

    sched = schedule_mod.load(store, cfg.schedule)
    print("polling; ctrl-c to stop"
          + ("" if sched.always_on else f" (awake {sched.window_label})"))
    asleep = False
    try:
        while True:
            now = time.time()
            # Re-read every pass rather than caching: the hours and the want
            # list are edited from the dashboard, and a loop that has been up
            # for a week should not be running last week's hunts to last
            # week's hours.
            cfg = config_mod.with_store(cfg, store)
            sched = schedule_mod.load(store, cfg.schedule)
            if not sched.is_open():
                if not asleep:
                    print(f"asleep until "
                          f"{schedule_mod.fmt_clock(sched.start_minute)}")
                    asleep = True
                time.sleep(30)
                continue
            asleep = False
            # Reset once per pass, and only when a pass actually runs
            # something: per hunt it multiplies the request budget by the
            # number of hunts, and unconditionally every ten seconds it stops
            # being a budget at all.
            budget_is_fresh = False
            for hunt in _hunts(cfg, args.hunt, store):
                if now < next_due.get(hunt.id, 0):
                    continue
                if not budget_is_fresh:
                    _reset_budgets(sources)
                    budget_is_fresh = True
                for name, source in sources:
                    r = run_hunt(store, hunt, source, scorer, notifiers,
                                 cfg.location,
                                 image_provider=_build_image_provider(name),
                                 max_image_checks=cfg.scorer.max_image_checks,
                                 thumbnails=ThumbnailStore(
                                     cfg.db_path.parent / "thumbs"))
                    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(f"[{stamp}] {hunt.id:24} {name:11} "
                          + (f"ERROR {r.error}" if r.error
                             else f"wanted={r.n_wanted} free={r.n_free_find}"))
                next_due[hunt.id] = now + hunt.interval_minutes * 60
            time.sleep(10)
    except KeyboardInterrupt:
        print("\nstopped")
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dealbot")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("once", help="one pass over every hunt")
    o.add_argument("--hunt")
    o.add_argument("--dry-run", action="store_true",
                   help="fetch and gate, write nothing")
    o.add_argument("--no-score", action="store_true",
                   help="full pipeline but stop before the model; spends nothing")
    o.add_argument("--no-images", action="store_true",
                   help="skip the image pass even where the model asks for it")
    o.add_argument("--due", action="store_true",
                   help="only run hunts whose interval has elapsed (for timers)")
    o.set_defaults(func=cmd_once)

    r = sub.add_parser("run", help="poll on each hunt's cadence")
    r.add_argument("--hunt")
    r.set_defaults(func=cmd_run)

    rk = sub.add_parser("recheck",
                        help="confirm bin listings are still for sale; no cost")
    rk.add_argument("--limit", type=int, default=0,
                    help="how many to check (default: config max_per_run)")
    rk.add_argument("--all", action="store_true",
                    help="ignore the per-listing interval and check the oldest")
    rk.set_defaults(func=cmd_recheck)

    nt = sub.add_parser("notify", help="send pending alerts; no fetching, no cost")
    nt.add_argument("--hunt")
    nt.set_defaults(func=cmd_notify)

    sd = sub.add_parser("seed-demo",
                        help="build an offline database for UI development")
    sd.add_argument("--db", default="data/demo.db")
    sd.set_defaults(func=cmd_seed_demo)

    pr = sub.add_parser("prune-thumbs", help="drop cached photos no longer needed")
    pr.set_defaults(func=cmd_prune_thumbs)

    h = sub.add_parser("hunts", help="list hunts and last run")
    h.set_defaults(func=cmd_hunts)

    s = sub.add_parser("serve", help="dashboard")
    # Localhost by default. There is no authentication in the app and
    # there are now half a dozen mutating endpoints; the systemd unit
    # passes --host 0.0.0.0 explicitly, so a wide default buys nothing
    # and hands anyone on the wifi a button to delete your wants.
    s.add_argument("--host", default="127.0.0.1",
                   help="bind address (default: 127.0.0.1). The deployed unit "
                        "passes 0.0.0.0 explicitly, behind a trusted network.")
    s.add_argument("--port", type=int, default=8080,
                   help="(default: 8080; the deployed unit uses 8477)")
    s.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    # Next to the config file first, working directory second. Resolved only
    # against the CWD, a run started from anywhere else silently loses the
    # Discord webhooks and the home coordinates -- `load_env` uses setdefault,
    # so the first file to define a key wins and this order is deliberate.
    load_env(Path(args.config).resolve().parent / ".env")
    load_env()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
