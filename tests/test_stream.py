"""Extractor tests, run against a REAL captured `claude -p` stream
(fixtures/streams/minimal-result.jsonl, captured on koda 2026-09-08).

The field names here were all wrong in at least one plausible guess, so these
tests exist to catch a silent zero rather than a crash.
"""
from pathlib import Path

import pytest

from dealbot.scoring.stream import extract, parse_events

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/streams/minimal-result.jsonl"


def _facts():
    return extract(parse_events(FIXTURE.read_text()))


def test_reads_a_real_capture():
    f = _facts()
    assert f.saw_result and f.num_results == 1
    assert not f.failed
    assert f.terminal_reason == "completed"
    assert f.text == '{"ok":true}'


def test_cache_token_keys_are_the_underscore_input_ones():
    """`cache_read_tokens` would silently yield zero forever."""
    f = _facts()
    assert f.cache_creation_tokens == 2514
    assert f.output_tokens == 9


def test_rate_limit_comes_from_its_own_event_not_the_result():
    f = _facts()
    assert f.rate_limit_type == "five_hour"
    assert f.five_hour_utilization == 0.41
    assert f.seven_day_utilization == 0.17     # the window otter does not watch
    assert f.resets_at is not None


def test_cost_sums_across_every_result_never_just_the_last():
    ev = parse_events(FIXTURE.read_text())
    doubled = ev + [e for e in ev if e.get("type") == "result"]
    assert extract(doubled).cost_usd == pytest.approx(extract(ev).cost_usd * 2)


def test_subtype_success_does_not_mean_success():
    """Observed 9 times in otter's history: is_error true, subtype 'success'."""
    f = extract([{"type": "result", "subtype": "success", "is_error": True,
                  "terminal_reason": "api_error", "total_cost_usd": 0,
                  "result": "Failed to authenticate: OAuth session expired"}])
    assert f.failed
    assert "OAuth" in f.text          # the only place the cause is stated


def test_tolerates_stderr_markers_and_a_half_written_line():
    raw = ('warning: something\n'
           '{"type":"result","subtype":"success","is_error":false,'
           '"terminal_reason":"completed","total_cost_usd":0.5,"result":"hi"}\n'
           '--- session complete ---\n'
           '{"type":"result","total_cost')          # truncated mid-write
    f = extract(parse_events(raw))
    assert f.num_results == 1 and f.cost_usd == 0.5


def test_never_ran_is_distinguishable_from_no_telemetry():
    assert extract([]).saw_result is False


def test_overage_is_sticky_across_the_run():
    """A run can cross into overage and end back inside the plan window."""
    f = extract([
        {"type": "rate_limit_event", "rate_limit_info": {"isUsingOverage": True}},
        {"type": "rate_limit_event", "rate_limit_info": {"isUsingOverage": False}},
    ])
    assert f.ever_used_overage
