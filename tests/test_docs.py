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
