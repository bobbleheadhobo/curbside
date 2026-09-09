"""Pure extractor for `claude -p --output-format stream-json --verbose` output.

Event list in, facts dict out -- no subprocess, no I/O -- so it can be tested
against recorded streams. Every rule here was measured, not assumed:

  * Sum `total_cost_usd` across EVERY `result` event, never take the last. A
    resumed session emits one result per invocation, each carrying only its own
    turn's numbers. (36 of otter's 81 recorded streams had more than one.)
  * Read the FAILURE from the final result only, so a run that fails after
    successful turns is reported as the failure it ended on.
  * Never detect failure from `subtype`: across 174 observed results it was
    "success" every time, including the 9 where `is_error` was true. Use
    `is_error` AND `terminal_reason`.
  * Keep the result TEXT. When a run never reaches the API it burns ~10 minutes
    of retry backoff and ends `terminal_reason: "api_error"` -- and that string
    is the only place the cause is stated (expired OAuth reads nothing like a
    network failure, and conflating them produces confidently wrong alerts).
  * Token keys contain `_input_`: `cache_read_input_tokens`. Guessing
    `cache_read_tokens` silently yields zero.
  * Rate-limit state is camelCase and lives in its own event type, never in
    `result`. Newest wins for a snapshot; "ever used overage" is sticky, because
    a run can cross into overage mid-flight and end back inside the plan window.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class StreamFacts:
    text: str = ""                      # final result string
    is_error: bool = False
    terminal_reason: str | None = None
    stop_reason: str | None = None
    cost_usd: float = 0.0
    num_results: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    duration_ms: int = 0
    session_id: str | None = None
    # rate limits (newest snapshot)
    rate_limit_status: str | None = None
    rate_limit_type: str | None = None
    resets_at: int | None = None
    five_hour_utilization: float | None = None
    seven_day_utilization: float | None = None
    ever_used_overage: bool = False
    overage_status: str | None = None
    saw_result: bool = False            # distinguishes "no telemetry" from "never ran"
    api_retries: int = 0

    @property
    def failed(self) -> bool:
        return self.is_error or self.terminal_reason not in (None, "completed")


def parse_events(raw: str) -> list[dict[str, Any]]:
    """Tolerant JSONL parse. Three reasons a line won't decode, none an error:
    merged stderr, our own end markers, and a partial final line in a file a
    live session is still appending to."""
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def extract(events: Iterable[dict[str, Any]]) -> StreamFacts:
    facts = StreamFacts()
    final: dict[str, Any] | None = None

    for ev in events:
        etype = ev.get("type")

        if etype == "system" and ev.get("subtype") == "api_retry":
            facts.api_retries += 1

        elif etype == "rate_limit_event":
            info = ev.get("rate_limit_info", ev)
            facts.rate_limit_status = info.get("status", facts.rate_limit_status)
            facts.rate_limit_type = info.get("rateLimitType", facts.rate_limit_type)
            facts.resets_at = info.get("resetsAt", facts.resets_at)
            facts.overage_status = info.get("overageStatus", facts.overage_status)
            if info.get("isUsingOverage"):
                facts.ever_used_overage = True          # sticky, never cleared
            windows = info.get("unifiedWindows") or {}
            if (w := windows.get("five_hour")):
                facts.five_hour_utilization = w.get("utilization")
            if (w := windows.get("seven_day")):
                facts.seven_day_utilization = w.get("utilization")

        elif etype == "result":
            facts.saw_result = True
            facts.num_results += 1
            facts.cost_usd += float(ev.get("total_cost_usd") or 0.0)
            facts.duration_ms += int(ev.get("duration_ms") or 0)
            usage = ev.get("usage") or {}
            facts.input_tokens += int(usage.get("input_tokens") or 0)
            facts.output_tokens += int(usage.get("output_tokens") or 0)
            facts.cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
            facts.cache_creation_tokens += int(usage.get("cache_creation_input_tokens") or 0)
            final = ev

    if final is not None:
        facts.text = final.get("result") or ""
        facts.is_error = bool(final.get("is_error"))
        facts.terminal_reason = final.get("terminal_reason")
        facts.stop_reason = final.get("stop_reason")
        facts.session_id = final.get("session_id")

    return facts
