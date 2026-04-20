"""Distribution-aware session quality scoring."""
from __future__ import annotations

from reflect.utils import _safe_ratio

from .types import DataProfile


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
    score = 0.0

    # ── 1. Completion (25 pts) ──────────────────────────────────
    events = [s.get("event", "") for s in spans]
    has_stop = any(e in ("Stop", "SessionEnd") for e in events)
    has_subagent_stop = any(e == "SubagentStop" for e in events)
    if has_stop:
        score += 25.0
    elif has_subagent_stop:
        score += 15.0

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
        elif tpt > threshold_mild:
            efficiency -= 7.0

        if profile and not profile.session_tool_count.is_sparse():
            if profile.session_tool_count.is_outlier_high(float(tool_uses)):
                efficiency -= 5.0
        else:
            if tool_uses > 30:
                efficiency -= 5.0
    else:
        if profile and not profile.session_total_tokens.is_sparse():
            if profile.session_total_tokens.is_outlier_high(float(total_tokens)):
                efficiency -= 10.0
        else:
            if total_tokens > 50000:
                efficiency -= 10.0
            elif total_tokens > 20000:
                efficiency -= 5.0
    score += max(0.0, efficiency)

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
            score += max(0.0, 15.0 - (fail_rate * 100))
        elif failures == 0:
            score += 15.0
        else:
            score += 15.0 * (1.0 - fail_rate / threshold)
    else:
        score += 15.0

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
    score += 10.0 - loop_penalty

    # ── 5. Duration Health (10 pts) ─────────────────────────────
    timestamps = [s["t"] for s in spans if s.get("t")]
    if len(timestamps) >= 2:
        duration_ms = (max(timestamps) - min(timestamps)) / 1e6
        duration_score = 10.0

        if duration_ms < 30_000:
            duration_score -= 3.0
        elif profile and not profile.session_duration_ms.is_sparse():
            if profile.session_duration_ms.is_outlier_high(duration_ms):
                excess = duration_ms - profile.session_duration_ms.upper_fence()
                max_excess = profile.session_duration_ms.max_val - profile.session_duration_ms.upper_fence()
                if max_excess > 0:
                    duration_score -= 7.0 * min(1.0, excess / max_excess)
                else:
                    duration_score -= 3.0
        else:
            if duration_ms > 1_800_000:
                duration_score -= 5.0
        score += max(0.0, duration_score)
    else:
        score += 5.0

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
        score += 7.0
    elif recovered > 0:
        recovery_rate = recovered / failures
        score += 10.0 * min(1.0, recovery_rate)
    # else: failures with no recovery → 0

    # ── 7. Tool Diversity (5 pts) ───────────────────────────────
    distinct_tools = len({s["tool"] for s in spans if s.get("tool")})
    if distinct_tools >= 5:
        score += 5.0
    elif distinct_tools >= 3:
        score += 3.0
    elif distinct_tools >= 1:
        score += 1.0

    # ── 8. Edit Productivity (5 pts) ────────────────────────────
    edits = sum(1 for s in spans if s.get("event") == "AfterFileEdit")
    reads = sum(1 for s in spans if s.get("event") == "BeforeReadFile")
    if edits > 0:
        if reads > 0:
            edit_ratio = edits / reads
            score += 5.0 * min(1.0, edit_ratio / 0.5)
        else:
            score += 5.0
    elif reads > 0:
        score += 1.0

    return min(100.0, max(0.0, score))
