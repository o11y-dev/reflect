"""Percentile computation for tool latencies."""
from __future__ import annotations

from math import ceil


def _percentile(sorted_values: list[float], p: float) -> float:
    """Compute the p-th percentile from a pre-sorted list."""
    if not sorted_values:
        return 0.0
    idx = ceil(len(sorted_values) * p / 100) - 1
    return sorted_values[max(0, min(idx, len(sorted_values) - 1))]


def compute_tool_percentiles(
    tool_durations_ms: dict[str, list[float]],
) -> list[dict]:
    """Compute p50/p90/p95/p99 per tool, sorted by call count descending."""
    results = []
    for tool, durations in tool_durations_ms.items():
        if not durations:
            continue
        s = sorted(durations)
        results.append({
            "tool": tool,
            "count": len(s),
            "p50": round(_percentile(s, 50), 1),
            "p90": round(_percentile(s, 90), 1),
            "p95": round(_percentile(s, 95), 1),
            "p99": round(_percentile(s, 99), 1),
        })
    results.sort(key=lambda r: r["count"], reverse=True)
    return results
