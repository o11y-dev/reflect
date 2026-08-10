"""Build the distributions consumed by session-quality rules."""

from __future__ import annotations

from reflect.models import TelemetryStats

from .types import DataProfile, compute_distribution


def build_data_profile(stats: TelemetryStats) -> DataProfile:
    total_tokens: list[float] = []
    tool_counts: list[float] = []
    failure_counts: list[float] = []
    durations_ms: list[float] = []
    tokens_per_tool: list[float] = []

    for session_id in stats.sessions_seen:
        tokens = stats.session_tokens.get(session_id, {})
        total = sum(
            int(tokens.get(field, 0) or 0)
            for field in ("input", "output", "cache_creation", "cache_read")
        )
        spans = stats.session_span_details.get(session_id, [])
        tool_count = sum(bool(span.get("tool")) for span in spans)
        timestamps = [span["t"] for span in spans if span.get("t")]

        total_tokens.append(float(total))
        tool_counts.append(float(tool_count))
        failure_counts.append(float(sum(not span.get("ok", True) for span in spans)))
        durations_ms.append(
            (max(timestamps) - min(timestamps)) / 1e6
            if len(timestamps) >= 2
            else 0.0
        )
        if tool_count:
            tokens_per_tool.append(total / tool_count)

    return DataProfile(
        session_total_tokens=compute_distribution(total_tokens),
        session_tool_count=compute_distribution(tool_counts),
        session_failure_count=compute_distribution(failure_counts),
        session_duration_ms=compute_distribution(durations_ms),
        tokens_per_tool=compute_distribution(tokens_per_tool),
    )
