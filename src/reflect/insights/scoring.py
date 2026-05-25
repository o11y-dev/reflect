"""Distribution-aware session quality scoring."""
from __future__ import annotations

from reflect.utils import _safe_ratio

from .types import DataProfile


def _breakdown_item(
    name: str,
    earned: float,
    max_points: float,
    summary: str,
    metrics: dict[str, object] | None = None,
) -> dict[str, object]:
    metric_items = metrics or {}
    return {
        "name": name,
        "earned": round(max(0.0, min(max_points, earned)), 2),
        "max": max_points,
        "summary": summary,
        "metrics": metric_items,
        "inputs": [
            {"name": key.replace("_", " "), "value": value}
            for key, value in metric_items.items()
        ],
    }


def compute_session_quality(
    session_id: str,
    spans: list[dict],
    tokens: dict[str, int],
    profile: DataProfile | None = None,
) -> float:
    """Heuristic quality score (0-100) based on signals in spans.

    When *profile* is provided, thresholds adapt to the user's own
    data distribution.  Without it, conservative cold-start defaults
    are used.
    """
    score = sum(float(item["earned"]) for item in compute_session_quality_breakdown(session_id, spans, tokens, profile))
    return min(100.0, max(0.0, score))


def compute_session_quality_breakdown(
    session_id: str,
    spans: list[dict],
    tokens: dict[str, int],
    profile: DataProfile | None = None,
) -> list[dict[str, object]]:
    """Return the rule-by-rule score contribution for a session."""
    breakdown: list[dict[str, object]] = []

    # ── 1. Completion (25 pts) ──────────────────────────────────
    events = [s.get("event", "") for s in spans]
    has_stop = any(e in ("Stop", "SessionEnd") for e in events)
    has_subagent_stop = any(e == "SubagentStop" for e in events)
    if has_stop:
        completion = 25.0
        completion_summary = "Found a normal session completion event."
    elif has_subagent_stop:
        completion = 15.0
        completion_summary = "Found a subagent completion event, but no full session stop."
    else:
        completion = 0.0
        completion_summary = "No completion event was found in the scored spans."
    breakdown.append(_breakdown_item(
        "Completion",
        completion,
        25.0,
        completion_summary,
        {"has_stop": has_stop, "has_subagent_stop": has_subagent_stop},
    ))

    # ── 2. Efficiency (20 pts) ──────────────────────────────────
    tool_uses = sum(1 for s in spans if s.get("tool"))
    total_tokens = tokens.get("input", 0) + tokens.get("output", 0)

    efficiency = 20.0
    if tool_uses > 0:
        tpt = total_tokens / tool_uses

        if profile and not profile.tokens_per_tool.is_sparse():
            threshold_severe = profile.tokens_per_tool.p95
            threshold_mild = profile.tokens_per_tool.p75
        else:
            threshold_severe = 25000
            threshold_mild = 10000

        if tpt > threshold_severe:
            efficiency -= 15.0
            efficiency_summary = "Tokens per tool exceeded the severe threshold."
        elif tpt > threshold_mild:
            efficiency -= 7.0
            efficiency_summary = "Tokens per tool exceeded the mild threshold."
        else:
            efficiency_summary = "Tokens per tool stayed within the expected range."

        if profile and not profile.session_tool_count.is_sparse():
            if profile.session_tool_count.is_outlier_high(float(tool_uses)):
                efficiency -= 5.0
                efficiency_summary += " Tool count was also high for this local profile."
        else:
            if tool_uses > 30:
                efficiency -= 5.0
                efficiency_summary += " Tool count exceeded the cold-start threshold."
        efficiency_metrics = {
            "tool_uses": tool_uses,
            "total_tokens": total_tokens,
            "tokens_per_tool": round(tpt, 2),
            "mild_threshold": round(float(threshold_mild), 2),
            "severe_threshold": round(float(threshold_severe), 2),
        }
    else:
        efficiency_summary = "No tool calls were present; scoring used total token volume."
        if profile and not profile.session_total_tokens.is_sparse():
            if profile.session_total_tokens.is_outlier_high(float(total_tokens)):
                efficiency -= 10.0
                efficiency_summary = "No tool calls were present and total tokens were high for this local profile."
        else:
            if total_tokens > 50000:
                efficiency -= 10.0
                efficiency_summary = "No tool calls were present and total tokens exceeded 50k."
            elif total_tokens > 20000:
                efficiency -= 5.0
                efficiency_summary = "No tool calls were present and total tokens exceeded 20k."
        efficiency_metrics = {"tool_uses": tool_uses, "total_tokens": total_tokens}
    breakdown.append(_breakdown_item("Efficiency", efficiency, 20.0, efficiency_summary, efficiency_metrics))

    # ── 3. Tool Reliability (15 pts) ────────────────────────────
    failures = sum(1 for s in spans if not s.get("ok", True))
    if tool_uses > 0:
        fail_rate = failures / tool_uses

        if profile and not profile.session_failure_count.is_sparse():
            normal_rate = _safe_ratio(
                profile.session_failure_count.median,
                profile.session_tool_count.median,
            )
            threshold = max(normal_rate * 3.0, 0.10)
        else:
            threshold = 0.15

        if fail_rate > threshold:
            reliability = max(0.0, 15.0 - (fail_rate * 100))
            reliability_summary = "Failure rate exceeded the threshold."
        elif failures == 0:
            reliability = 15.0
            reliability_summary = "No failed tool calls were observed."
        else:
            reliability = 15.0 * (1.0 - fail_rate / threshold)
            reliability_summary = "Failures were present but stayed under the threshold."
        reliability_metrics = {
            "failures": failures,
            "tool_uses": tool_uses,
            "failure_rate": round(fail_rate, 4),
            "threshold": round(float(threshold), 4),
        }
    else:
        reliability = 15.0
        reliability_summary = "No tool calls were present, so no tool failures were observed."
        reliability_metrics = {"failures": failures, "tool_uses": tool_uses}
    breakdown.append(_breakdown_item("Tool reliability", reliability, 15.0, reliability_summary, reliability_metrics))

    # ── 4. Loop Detection (10 pts) ──────────────────────────────
    tool_seq = [s["tool"] for s in spans if s.get("tool")]
    consecutive_pairs = 0
    consecutive_triples = 0
    for i in range(len(tool_seq) - 1):
        if tool_seq[i] == tool_seq[i + 1]:
            consecutive_pairs += 1
            if i + 2 < len(tool_seq) and tool_seq[i] == tool_seq[i + 2]:
                consecutive_triples += 1
    loop_penalty = min(10.0, consecutive_pairs * 2.0 + consecutive_triples * 1.0)
    loop_score = 10.0 - loop_penalty
    breakdown.append(_breakdown_item(
        "Loop detection",
        loop_score,
        10.0,
        "Repeated consecutive tool calls reduced this score." if loop_penalty else "No repeated consecutive tool loops were detected.",
        {
            "consecutive_pairs": consecutive_pairs,
            "consecutive_triples": consecutive_triples,
            "penalty": round(loop_penalty, 2),
        },
    ))

    # ── 5. Duration Health (10 pts) ─────────────────────────────
    timestamps = [s["t"] for s in spans if s.get("t")]
    if len(timestamps) >= 2:
        duration_ms = (max(timestamps) - min(timestamps)) / 1e6
        duration_score = 10.0
        duration_summary = "Duration stayed within the expected range."

        if duration_ms < 30_000:
            duration_score -= 3.0
            duration_summary = "Session was very short, so duration health was reduced."
        elif profile and not profile.session_duration_ms.is_sparse():
            if profile.session_duration_ms.is_outlier_high(duration_ms):
                excess = duration_ms - profile.session_duration_ms.upper_fence()
                max_excess = profile.session_duration_ms.max_val - profile.session_duration_ms.upper_fence()
                if max_excess > 0:
                    duration_score -= 7.0 * min(1.0, excess / max_excess)
                else:
                    duration_score -= 3.0
                duration_summary = "Session duration was high for this local profile."
        else:
            if duration_ms > 1_800_000:
                duration_score -= 5.0
                duration_summary = "Session exceeded the 30 minute cold-start duration threshold."
        duration_metrics = {"duration_ms": round(duration_ms, 1), "timestamp_count": len(timestamps)}
    else:
        duration_score = 5.0
        duration_summary = "Only partial timing data was available."
        duration_metrics = {"timestamp_count": len(timestamps)}
    breakdown.append(_breakdown_item("Duration health", duration_score, 10.0, duration_summary, duration_metrics))

    # ── 6. Error Recovery (10 pts) ──────────────────────────────
    recovered = 0
    last_failed = False
    for s in spans:
        if s.get("event") == "PostToolUseFailure":
            last_failed = True
        elif last_failed and s.get("ok", True) and s.get("event") == "PostToolUse":
            recovered += 1
            last_failed = False
        elif s.get("ok", True):
            last_failed = False

    if failures == 0:
        recovery = 7.0
        recovery_summary = "No failures were observed, so recovery gets baseline credit."
    elif recovered > 0:
        recovery_rate = recovered / failures
        recovery = 10.0 * min(1.0, recovery_rate)
        recovery_summary = "Failures were followed by successful tool results."
    else:
        recovery = 0.0
        recovery_summary = "Failures were observed without a matching successful recovery."
    breakdown.append(_breakdown_item(
        "Error recovery",
        recovery,
        10.0,
        recovery_summary,
        {"failures": failures, "recovered": recovered},
    ))

    # ── 7. Tool Diversity (5 pts) ───────────────────────────────
    distinct_tools = len({s["tool"] for s in spans if s.get("tool")})
    if distinct_tools >= 5:
        diversity = 5.0
    elif distinct_tools >= 3:
        diversity = 3.0
    elif distinct_tools >= 1:
        diversity = 1.0
    else:
        diversity = 0.0
    breakdown.append(_breakdown_item(
        "Tool diversity",
        diversity,
        5.0,
        f"Observed {distinct_tools} distinct tool(s).",
        {"distinct_tools": distinct_tools},
    ))

    # ── 8. Edit Productivity (5 pts) ────────────────────────────
    edits = sum(1 for s in spans if s.get("event") == "AfterFileEdit")
    reads = sum(1 for s in spans if s.get("event") == "BeforeReadFile")
    if edits > 0:
        if reads > 0:
            edit_ratio = edits / reads
            edit_productivity = 5.0 * min(1.0, edit_ratio / 0.5)
            edit_summary = "Edits were scored against the edit-to-read ratio."
        else:
            edit_productivity = 5.0
            edit_summary = "Edits were present without read-heavy exploration."
    elif reads > 0:
        edit_productivity = 1.0
        edit_summary = "Reads were present, but no edit events were captured."
    else:
        edit_productivity = 0.0
        edit_summary = "No read or edit productivity signal was captured."
    edit_metrics = {"edits": edits, "reads": reads}
    if reads > 0:
        edit_metrics["edit_to_read_ratio"] = round(edits / reads, 4)
    breakdown.append(_breakdown_item(
        "Edit productivity",
        edit_productivity,
        5.0,
        edit_summary,
        edit_metrics,
    ))

    return breakdown
