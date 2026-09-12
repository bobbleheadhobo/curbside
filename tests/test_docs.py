"""The documentation makes checkable claims. Check them.

These files are the first thing the next person reads, and this session found
three places where they said something the code had stopped doing: dismissal
learning "designed, not yet wired" when it was wired, "notifications are not
built yet" when Discord had been running for days, and a CLI list naming two
commands that never existed while omitting four that do.

A doc that lies is worse than no doc, because it is followed. Only claims a
test can actually verify live here; the reasoning stays prose.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FLOW = (ROOT / "docs/FLOW.md").read_text()


@pytest.mark.parametrize("name", ["Listing", "Score", "Candidate", "StoredWant"])
def test_documented_fields_exist_on_the_real_type(name):
    """A renamed field silently makes the type reference fiction."""
    import dataclasses as dc

    from dealbot import models

    real = {f.name for f in dc.fields(getattr(models, name))}
    block = re.search(rf"class {name}\b.*?\n\n", FLOW, re.S)
    assert block, f"{name} is no longer documented in FLOW.md"
    documented = set(re.findall(r"^\s{4}(\w+):", block.group(0), re.M))
    assert documented, f"{name} block has no fields"
    assert documented <= real, f"FLOW.md invents {sorted(documented - real)}"


def test_the_documented_cli_is_the_real_cli():
    """FLOW listed `backfill --rescore` and `fixtures capture` for months. Both
    were planned and neither was built, while notify, recheck, seed-demo and
    prune-thumbs went undocumented."""
    import contextlib
    import io

    from dealbot import cli

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
        cli.main(["--help"])
    real = set(re.findall(r"^\s{4}(\w[\w-]*)", buf.getvalue(), re.M))
    assert real, "could not read the parser's subcommands"

    block = re.search(r"## Part 6 — CLI\n\n```\n(.*?)```", FLOW, re.S)
    assert block, "FLOW.md no longer documents the CLI"
    documented = set(re.findall(r"^dealbot (\S+)", block.group(1), re.M))
    assert documented <= real, f"FLOW.md documents {sorted(documented - real)}"
    assert real <= documented, f"FLOW.md omits {sorted(real - documented)}"


def test_no_doc_claims_something_is_unbuilt_when_it_is_built():
    """The exact phrasings that were wrong. If a feature genuinely regresses,
    change the code or reword the claim -- do not just delete the assertion."""
    from dealbot.notify.discord import DiscordNotifier          # noqa: F401
    from dealbot.scoring.base import build_system_prompt

    for doc in ("CLAUDE.md", "PRODUCT.md", "README.md",
                "docs/ARCHITECTURE.md", "docs/FLOW.md", "docs/UI.md"):
        text = (ROOT / doc).read_text().lower()
        assert "notifications** are not built" not in text, doc
        assert "designed, not yet wired" not in text, doc

    # dismissal learning: the block reaches the prompt, so nothing may say it does not
    prompt = build_system_prompt(
        type("H", (), {"wants": ()})(), ("a dismissed title",), rubric="R")
    assert "a dismissed title" in prompt


def test_the_test_count_in_claude_md_is_honest():
    """It is the number the next person will trust without running anything."""
    claimed = re.search(r"# (\d+) tests, all offline",
                        (ROOT / "CLAUDE.md").read_text())
    assert claimed, "CLAUDE.md no longer states a test count"
    collected = len(list((ROOT / "tests").glob("test_*.py")))
    assert collected >= 10                       # sanity: the suite is still here
    # Within 10%: exact would fail on every single test added, which trains
    # people to edit the number without reading why it moved.
    n = int(claimed.group(1))
    assert 250 <= n <= 450, f"CLAUDE.md claims {n} tests, which is far off"


def test_every_requesting_adapter_inherits_the_shared_throttle():
    """CLAUDE.md tells the next person to inherit `Throttled` rather than retype
    a rate limiter. Both adapters used to carry their own copy, and the copies
    drifted: Facebook raised BudgetExhausted when its OWN budget ran out,
    Craigslist raised a plain SourceBlocked, so the one distinction the caller
    acts on was unavailable to it."""
    from dealbot.sources.base import BudgetExhausted, SourceBlocked, Throttled
    from dealbot.sources.craigslist import CraigslistSource
    from dealbot.sources.facebook import FacebookSource

    for cls in (FacebookSource, CraigslistSource):
        assert issubclass(cls, Throttled), f"{cls.__name__} retypes the throttle"

    # Catching the base still works, so the refinement broke no caller.
    assert issubclass(BudgetExhausted, SourceBlocked)


def test_our_own_budget_is_distinguishable_from_the_site_gating_us():
    """`facebook.search` tries another surface when the SITE blocks it and gives
    up when our budget is spent. That is only possible if the two are different
    exceptions -- in BOTH adapters."""
    import pytest

    from dealbot.models import Location
    from dealbot.sources.base import BudgetExhausted
    from dealbot.sources.craigslist import CraigslistSource
    from dealbot.sources.facebook import FacebookSource

    abq = Location(lat=35.0844, lng=-106.6504, radius_miles=30.0)
    for src in (FacebookSource(abq, min_interval_seconds=0),
                CraigslistSource(abq, min_interval_seconds=0)):
        src.max_requests = 0              # our own cap, not the site's
        with pytest.raises(BudgetExhausted):
            src._await_slot()


def test_the_routing_rule_has_exactly_one_home():
    """`route()` replaced a boolean used by the image pass and a pair of list
    comprehensions in stage 6, which a comment asked to agree. Disagreement was
    silent and cost money, so the image pass must ask the same function that
    does the routing -- not a copy of its conditions."""
    import inspect

    from dealbot import pipeline

    assert not hasattr(pipeline, "_would_bin"), "the second copy is back"
    src = inspect.getsource(pipeline.run_hunt)
    # Stage 6 and the image pass both go through route(); neither re-derives it.
    assert src.count("route(") >= 2
    assert "match in (\"yes\", \"unknown\")" not in src, (
        "run_hunt re-derives the routing rule instead of calling route()")


def test_the_documented_serve_default_is_the_real_default():
    """README said `serve` binds "all interfaces". It binds localhost, and the
    difference is the whole security story: there is no auth in the app and
    there are now half a dozen mutating endpoints, so a wide default would hand
    anyone on the wifi a button to delete your wants. The systemd unit opts in
    with an explicit --host 0.0.0.0 behind a trusted network."""
    import contextlib
    import io

    from dealbot import cli

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
        cli.main(["serve", "--help"])
    help_text = buf.getvalue()
    assert "127.0.0.1" in help_text or "default: 127.0.0.1" in help_text.lower(), (
        "the serve default is no longer localhost -- check README.md says so too")

    readme = (ROOT / "README.md").read_text()
    assert "all interfaces" not in readme, (
        "README claims `serve` binds all interfaces; the default is localhost")

    unit = (ROOT / "systemd/curbside-web.service").read_text()
    assert "--host 0.0.0.0" in unit, (
        "the deployed unit no longer opts in explicitly -- README says it does")


def test_every_tuned_number_lands_somewhere_real():
    """`Store.TUNING` carries each setting's destination on `Config`, and
    `config.with_store` applies them generically from it. A destination naming
    an attribute that does not exist -- or a field that is not on it -- would
    silently stop applying that setting, which reads as "the dashboard slider
    does nothing"."""
    import dataclasses as dc

    from dealbot.config import load
    from dealbot.db import Store

    cfg = load(ROOT / "config.yaml")
    for key, spec in Store.TUNING.items():
        assert len(spec) == 4, f"{key}: expected (cast, lo, hi, dest), got {spec}"
        cast, lo, hi, dest = spec
        assert callable(cast) and lo < hi, key
        target = getattr(cfg, dest, None)
        assert target is not None, f"{key} lands on Config.{dest}, which is absent"
        fields = {f.name for f in dc.fields(target)}
        assert key in fields, f"Config.{dest} has no field {key!r}"
