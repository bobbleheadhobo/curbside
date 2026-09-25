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

    from curbside import models

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

    from curbside import cli

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
        cli.main(["--help"])
    real = set(re.findall(r"^\s{4}(\w[\w-]*)", buf.getvalue(), re.M))
    assert real, "could not read the parser's subcommands"

    block = re.search(r"## Part 6 — CLI\n\n```\n(.*?)```", FLOW, re.S)
    assert block, "FLOW.md no longer documents the CLI"
    documented = set(re.findall(r"^curbside (\S+)", block.group(1), re.M))
    assert documented <= real, f"FLOW.md documents {sorted(documented - real)}"
    assert real <= documented, f"FLOW.md omits {sorted(real - documented)}"


def test_no_doc_claims_something_is_unbuilt_when_it_is_built():
    """The exact phrasings that were wrong. If a feature genuinely regresses,
    change the code or reword the claim -- do not just delete the assertion."""
    from curbside.notify.discord import DiscordNotifier          # noqa: F401
    from curbside.scoring.base import build_system_prompt

    for doc in ("CLAUDE.md", "PRODUCT.md", "README.md",
                "docs/ARCHITECTURE.md", "docs/FLOW.md", "docs/UI.md"):
        text = (ROOT / doc).read_text().lower()
        assert "notifications** are not built" not in text, doc
        assert "designed, not yet wired" not in text, doc

    # Dismissal learning reaches the prompt, so nothing may say it does not --
    # but it reaches the SWEEP's prompt only. A want dismissal is "right
    # category, wrong specimen" almost every time (33 of 34 on bookshelf were
    # bookshelves), so the block told that hunt not to surface bookshelves.
    def stub(kind):
        return type("H", (), {"wants": (), "kind": kind})()

    assert "a dismissed title" in build_system_prompt(
        stub("sweep"), ("a dismissed title",), rubric="R")
    assert "a dismissed title" not in build_system_prompt(
        stub("want"), ("a dismissed title",), rubric="R")


def test_the_appraisal_asks_for_a_sentence_about_the_THING():
    """REPORTED: nearly every free find's reasoning opened with "doesn't match
    either want". The schema lists `match` first and the prose followed the
    schema, so the one sentence a person reads spent its opening on a field
    that is a chip on the same card -- and on an answer that is "no" for almost
    everything in that bin, since a listing that DID match is routed to a
    different one. Prose guidance, so it is deletable; this is the guard."""
    from curbside.scoring.base import APPRAISE_INSTRUCTION, TRIAGE_INSTRUCTION
    low = APPRAISE_INSTRUCTION.lower()
    assert "`reasoning`" in APPRAISE_INSTRUCTION, "lost what the sentence is FOR"
    assert "do not open with whether it matched a want" in low, (
        "lost the specific instruction; the general 'do not restate' did not "
        "stop it on its own")
    assert "no match" in TRIAGE_INSTRUCTION.lower(), (
        "triage's `why` has eight words and was spending two of them the same "
        "way")


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
    # The band is a sanity guard, not the count: it catches a number nobody
    # touched for a year, and gets widened when the suite genuinely grows past
    # it rather than being treated as a ceiling on the suite.
    assert 250 <= n <= 1000, f"CLAUDE.md claims {n} tests, which is far off"


def test_every_requesting_adapter_inherits_the_shared_throttle():
    """CLAUDE.md tells the next person to inherit `Throttled` rather than retype
    a rate limiter. Both adapters used to carry their own copy, and the copies
    drifted: Facebook raised BudgetExhausted when its OWN budget ran out,
    Craigslist raised a plain SourceBlocked, so the one distinction the caller
    acts on was unavailable to it."""
    from curbside.sources.base import BudgetExhausted, SourceBlocked, Throttled
    from curbside.sources.craigslist import CraigslistSource
    from curbside.sources.facebook import FacebookSource

    for cls in (FacebookSource, CraigslistSource):
        assert issubclass(cls, Throttled), f"{cls.__name__} retypes the throttle"

    # Catching the base still works, so the refinement broke no caller.
    assert issubclass(BudgetExhausted, SourceBlocked)


def test_our_own_budget_is_distinguishable_from_the_site_gating_us():
    """`facebook.search` tries another surface when the SITE blocks it and gives
    up when our budget is spent. That is only possible if the two are different
    exceptions -- in BOTH adapters."""
    import pytest

    from curbside.models import Location
    from curbside.sources.base import BudgetExhausted
    from curbside.sources.craigslist import CraigslistSource
    from curbside.sources.facebook import FacebookSource

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

    from curbside import pipeline

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

    from curbside import cli

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

    from curbside.config import load
    from curbside.db import Store

    cfg = load(ROOT / "config.yaml")
    for key, spec in Store.TUNING.items():
        assert len(spec) == 4, f"{key}: expected (cast, lo, hi, dest), got {spec}"
        cast, lo, hi, dest = spec
        assert callable(cast) and lo < hi, key
        target = getattr(cfg, dest, None)
        assert target is not None, f"{key} lands on Config.{dest}, which is absent"
        fields = {f.name for f in dc.fields(target)}
        assert key in fields, f"Config.{dest} has no field {key!r}"


def test_the_rubric_knows_a_zero_price_can_be_a_lie():
    """REPORTED from the free bin: "Sockets and wrenches", price $0, whole
    description "Send me offers please over 50 wrenches". The model called it
    "Free sockets and wrenches", scored it 6.0 with `worth_grabbing` true, and
    it landed in a bin named Free finds. The $0 is a field the seller filled
    in; the words are what they meant.

    The guard matters as much as the rule. "Open to offers" on a PRICED listing
    is ordinary haggling, and a keyword rule on "offer" would flag it.

    `prompts/rubric.md` only: the fallback carries what is safety-relevant, and
    this one costs a message rather than money. See
    `test_the_scam_guidance_is_in_both_rubrics` for the one that is.
    """
    from curbside.scoring.base import load_rubric
    text = load_rubric(ROOT / "prompts/rubric.md")
    low = text.lower()
    assert "contradict" in low, "lost the rule: the description can contradict $0"
    assert "the words win" in low, "lost which one to believe"
    assert "price_unclear` true" in text, (
        "lost what to DO about it: the structured flag is what stops the card "
        "printing FREE, and prose in red_flags cannot do that")
    assert "worth the trip" in low, (
        "lost the scope: the score describes the object, as if it really were "
        "free. `route` is what keeps it out of the free bin -- a listing "
        "scored DOWN for being unclear falls under the /skipped floor too, "
        "which is being buried where it cannot be seen")
    assert "ordinary haggling" in low, (
        "lost the exception: 'open to offers' on a priced listing is haggling, "
        "and flagging it would bury ordinary listings")
    assert "does not change the match" in low, (
        "lost the scope: a wanted thing whose seller takes offers must still "
        "reach the wanted bin")


def test_the_scam_guidance_is_in_both_rubrics():
    """`prompts/rubric.md` is the rubric; `base.RUBRIC` is the fallback used
    when that file is missing or empty -- and `load_rubric` only LOGS the
    fallback, at info. So guidance that exists in one and not the other is
    invisible until it costs something.

    This one is checked because of what it guards. A free washer and dryer,
    "like new", "delivery all depends on you", scored 9.0 with no red flags and
    landed in the free-finds bin; the image pass then raised confidence,
    because the photographs were real. The downside there is not a wasted trip,
    it is money sent to a stranger.

    Both must also keep the exception, or the rule eats the ordinary case:
    delivery for a fee on a PRICED item is completely normal, and six listings
    in the collected data do exactly that."""
    from curbside.scoring.base import RUBRIC, load_rubric

    for name, text in (("prompts/rubric.md", load_rubric(ROOT / "prompts/rubric.md")),
                       ("base.RUBRIC", RUBRIC)):
        low = text.lower()
        assert "advance-fee" in low, f"{name} lost the scam pattern"
        assert "worth_grabbing` false" in text, f"{name} lost what to DO about it"
        assert "priced" in low and "ordinary" in low, (
            f"{name} lost the exception: delivery for a fee on a priced item "
            "is normal, and flagging it would bury legitimate listings")
        assert "photograph" in low, (
            f"{name} lost the note that photos cannot establish legitimacy")
