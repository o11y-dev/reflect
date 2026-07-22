"""Adapters from Reflect telemetry and SQLite rows into session-rule context."""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from .base import SessionRuleContext

if TYPE_CHECKING:
    from reflect.insights.types import DataProfile


def context_from_spans(
    session_id: str,
    spans: list[dict],
    tokens: dict[str, int],
    profile: DataProfile | None = None,
) -> SessionRuleContext:
    """Normalize detailed telemetry for the session-rule scorer."""
    events = [str(span.get("event") or "") for span in spans]
    tool_sequence = [str(span["tool"]) for span in spans if span.get("tool")]
    consecutive_pairs = 0
    consecutive_triples = 0
    for index in range(len(tool_sequence) - 1):
        if tool_sequence[index] == tool_sequence[index + 1]:
            consecutive_pairs += 1
            if (
                index + 2 < len(tool_sequence)
                and tool_sequence[index] == tool_sequence[index + 2]
            ):
                consecutive_triples += 1

    timestamps = [span["t"] for span in spans if span.get("t")]
    timing_available = len(timestamps) >= 2
    duration_ms = (
        float(max(timestamps) - min(timestamps)) / 1e6
        if timing_available
        else 0.0
    )

    recovered = 0
    last_failed = False
    for span in spans:
        if span.get("event") == "PostToolUseFailure":
            last_failed = True
        elif (
            last_failed
            and span.get("ok", True)
            and span.get("event") == "PostToolUse"
        ):
            recovered += 1
            last_failed = False
        elif span.get("ok", True):
            last_failed = False

    return SessionRuleContext(
        session_id=session_id,
        source="spans",
        profile=profile,
        has_stop=any(event in ("Stop", "SessionEnd") for event in events),
        has_subagent_stop=any(event == "SubagentStop" for event in events),
        tool_uses=len(tool_sequence),
        total_tokens=int(tokens.get("input", 0)) + int(tokens.get("output", 0)),
        failures=sum(1 for span in spans if not span.get("ok", True)),
        consecutive_pairs=consecutive_pairs,
        consecutive_triples=consecutive_triples,
        duration_ms=duration_ms,
        timing_available=timing_available,
        timestamp_count=len(timestamps),
        recovered=recovered,
        distinct_tools=len(set(tool_sequence)),
        edits=sum(1 for span in spans if span.get("event") == "AfterFileEdit"),
        reads=sum(1 for span in spans if span.get("event") == "BeforeReadFile"),
    )


def context_from_summary(
    row: Mapping[str, object],
    *,
    recovered: int = 0,
) -> SessionRuleContext:
    """Normalize a SQLite/dashboard summary row without inventing absent signals."""
    duration_ms = float(row.get("duration_ms") or 0)
    return SessionRuleContext(
        session_id=str(row.get("id") or row.get("session_id") or "unknown"),
        source="summary",
        status=str(row.get("status") or "unknown"),
        tool_uses=int(row.get("tool_call_count") or row.get("tool_calls") or 0),
        total_tokens=(
            int(row.get("input_tokens") or 0)
            + int(row.get("output_tokens") or 0)
        ),
        failures=int(row.get("failure_count") or row.get("failures") or 0),
        duration_ms=duration_ms,
        timing_available=duration_ms > 0,
        recovered=recovered,
    )


__all__ = ["context_from_spans", "context_from_summary"]
