"""Command line entry point."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from . import config as config_mod
from .db import Store
from .images import FixtureImageProvider, HttpImageProvider
from .notify.dashboard import DashboardNotifier
from .pipeline import dry_run, run_hunt
from .scoring.claude_code import ClaudeCodeScorer
from .scoring.stub import StubScorer
from .sources.craigslist import CraigslistSource
from .sources.facebook import FacebookSource
from .sources.fixture import FixtureSource


def _build_image_provider(cfg: config_mod.Config, source_name: str):
    if source_name == "fixture":
        return FixtureImageProvider()
    return HttpImageProvider()


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


def _build_scorer(cfg: config_mod.Config, store):
    if cfg.scorer.backend == "stub":
        return StubScorer()
    if cfg.scorer.backend == "claude_code":
        return ClaudeCodeScorer(cfg.scorer, store)
    raise SystemExit(f"unknown scorer backend {cfg.scorer.backend!r}")


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


def _hunts(cfg: config_mod.Config, name: str | None):
    hunts = [h for h in cfg.hunts if h.enabled]
    if name:
        hunts = [h for h in hunts if h.name == name or h.id == name]
        if not hunts:
            raise SystemExit(f"no enabled hunt matching {name!r}")
    return hunts


def cmd_once(args) -> int:
    cfg = config_mod.load(args.config)
    store = Store(cfg.db_path)
    sources, scorer = _build_sources(cfg), _build_scorer(cfg, store)
    notifiers = [DashboardNotifier(store)]

    for hunt in _hunts(cfg, args.hunt):
        for name, source in sources:
            if args.due and not _is_due(store, hunt, name):
                continue
            if hasattr(source, "reset_budget"):
                source.reset_budget()
            if args.dry_run:
                print(json.dumps(dry_run(store, hunt, source, cfg.location),
                                 indent=2))
                continue
            r = run_hunt(store, hunt, source, scorer, notifiers, cfg.location,
                         no_score=args.no_score,
                         image_provider=None if args.no_images
                         else _build_image_provider(cfg, name),
                         max_image_checks=cfg.scorer.max_image_checks)
            status = f"ERROR {r.error}" if r.error else (
                f"fetched={r.n_fetched} new={r.n_new} cand={r.n_candidates} "
                f"scored={r.n_scored} wanted={r.n_wanted} free={r.n_free_find} "
                f"imgs={r.n_image_checks} cost=${r.cost_usd:.4f}")
            print(f"{hunt.id:24} {name:11} {status}")
    store.close()
    return 0


def cmd_hunts(args) -> int:
    cfg = config_mod.load(args.config)
    store = Store(cfg.db_path)
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
        print(f"{h.id:28} {h.kind:6} {h.interval_minutes:>5}m  {cap:>6}  {last}")
    store.close()
    return 0


def cmd_serve(args) -> int:
    import uvicorn
    from .web.app import create_app
    cfg = config_mod.load(args.config)
    uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_run(args) -> int:
    """Poll loop. Each hunt runs on its own cadence -- free sweeps every 15
    minutes because free items evaporate, want searches hourly because priced
    ones do not."""
    cfg = config_mod.load(args.config)
    store = Store(cfg.db_path)
    sources, scorer = _build_sources(cfg), _build_scorer(cfg, store)
    notifiers = [DashboardNotifier(store)]
    next_due: dict[str, float] = {}

    print("polling; ctrl-c to stop")
    try:
        while True:
            now = time.time()
            for hunt in _hunts(cfg, args.hunt):
                if now < next_due.get(hunt.id, 0):
                    continue
                for name, source in sources:
                    if hasattr(source, "reset_budget"):
                        source.reset_budget()
                    r = run_hunt(store, hunt, source, scorer, notifiers,
                                 cfg.location,
                                 image_provider=_build_image_provider(cfg, name),
                                 max_image_checks=cfg.scorer.max_image_checks)
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

    h = sub.add_parser("hunts", help="list hunts and last run")
    h.set_defaults(func=cmd_hunts)

    s = sub.add_parser("serve", help="dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
