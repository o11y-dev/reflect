"""Per-session signal functions — compare one session against the user's baseline."""
from __future__ import annotations

from reflect.models import TelemetryStats
from reflect.utils import _safe_ratio

from ..types import DataProfile, Insight, Severity, confidence_for

SessionSignalFn = type(lambda sid, spans, tokens, stats, profile: None)


def _fmt_duration(ms: float) -> str:
    if ms < 60_000:
        return f"{ms / 1000:.0f}s"
    elif ms < 3_600_000:
        return f"{ms / 60_000:.1f}m"
    else:
        return f"{ms / 3_600_000:.1f}h"


def signal_session_token_outlier(
    sid: str, spans: list[dict], tokens: dict, stats: TelemetryStats, profile: DataProfile,
) -> Insight | None:
    total = tokens.get("input", 0) + tokens.get("output", 0) + tokens.get("cache_creation", 0) + tokens.get("cache_read", 0)
    dist = profile.session_total_tokens

    if not dist.is_sparse():
        if not dist.is_outlier_high(float(total)):
            return None
        if total > dist.p95:
            sev = Severity.CRITICAL if dist.count >= 10 and total > dist.max_val * 0.9 else Severity.HIGH
        else:
            sev = Severity.MEDIUM
        z = total / dist.median if dist.median > 0 else 0
    else:
        if total < 500_000:
            return None
        sev = Severity.HIGH if total > 1_000_000 else Severity.MEDIUM
        z = 0

    return Insight(
        kind="observation", title="Token usage outlier",
        body=(f"This session used {total:,} tokens"
              + (f" — {z:.1f}x your typical session median ({dist.median:,.0f})" if z > 0 else "")
              + ". Long-lived context accumulation is the likely driver."),
        category="cost", severity=sev,
        confidence=confidence_for(dist),
        evidence={"total_tokens": total, "median": dist.median, "z": round(z, 1)},
    )


def signal_session_efficiency(
    sid: str, spans: list[dict], tokens: dict, stats: TelemetryStats, profile: DataProfile,
) -> Insight | None:
    tool_count = sum(1 for s in spans if s.get("tool"))
    if tool_count == 0:
        return None
    total = tokens.get("input", 0) + tokens.get("output", 0) + tokens.get("cache_creation", 0) + tokens.get("cache_read", 0)
    tpt = total / tool_count
    dist = profile.tokens_per_tool

    if not dist.is_sparse():
        fence = dist.upper_fence(1.5)
        if tpt <= fence:
            return None
    else:
        if tpt <= 25_000:
            return None
        fence = 25_000

    return Insight(
        kind="observation", title="High token cost per tool action",
        body=(f"Token cost per tool action ({tpt:,.0f}) is above your typical ceiling ({fence:,.0f}). "
              "Large context, verbose reasoning, or repeated retries may be inflating cost."),
        category="efficiency", severity=Severity.MEDIUM,
        confidence=confidence_for(dist),
        evidence={"tokens_per_tool": round(tpt), "fence": round(fence)},
    )


def signal_session_failure_rate(
    sid: str, spans: list[dict], tokens: dict, stats: TelemetryStats, profile: DataProfile,
) -> Insight | None:
    tool_count = sum(1 for s in spans if s.get("tool"))
    failures = sum(1 for s in spans if not s.get("ok", True))
    if failures == 0 or tool_count == 0:
        return None
    fail_rate = failures / tool_count

    if not profile.session_failure_count.is_sparse() and not profile.session_tool_count.is_sparse():
        baseline = _safe_ratio(profile.session_failure_count.median, profile.session_tool_count.median)
        threshold = max(baseline * 2.5, 0.10)
    else:
        threshold = 0.10

    if fail_rate <= threshold:
        return None

    recovered = stats.session_recovered_failures.get(sid, 0)
    if fail_rate > 0.30:
        sev = Severity.CRITICAL
    elif fail_rate > 0.15:
        sev = Severity.HIGH
    else:
        sev = Severity.MEDIUM

    return Insight(
        kind="observation", title="Elevated failure rate",
        body=(f"{failures} of {tool_count} tool calls failed ({fail_rate:.0%}). "
              f"{recovered} were recovered. Schema and path pre-validation would reduce retry cost."),
        category="reliability", severity=sev, confidence=0.9,
        evidence={"failures": failures, "tool_count": tool_count, "fail_rate": round(fail_rate, 2), "recovered": recovered},
    )


def signal_session_zero_failures(
    sid: str, spans: list[dict], tokens: dict, stats: TelemetryStats, profile: DataProfile,
) -> Insight | None:
    tool_count = sum(1 for s in spans if s.get("tool"))
    failures = sum(1 for s in spans if not s.get("ok", True))
    if tool_count <= 5 or failures > 0:
        return None
    return Insight(
        kind="strength", title="Clean execution",
        body=f"{tool_count} tool calls completed with no failures.",
        category="reliability", severity=Severity.LOW, confidence=0.9,
        evidence={"tool_count": tool_count},
    )


def signal_session_loop_detected(
    sid: str, spans: list[dict], tokens: dict, stats: TelemetryStats, profile: DataProfile,
) -> Insight | None:
    tool_seq = [s["tool"] for s in spans if s.get("tool")]
    if len(tool_seq) < 3:
        return None

    runs: list[tuple[str, int]] = []
    current_tool: str | None = None
    current_run = 0
    for t in tool_seq:
        if t == current_tool:
            current_run += 1
        else:
            if current_run >= 3 and current_tool is not None:
                runs.append((current_tool, current_run))
            current_tool = t
            current_run = 1
    if current_run >= 3 and current_tool is not None:
        runs.append((current_tool, current_run))

    if not runs:
        return None

    longest = max(runs, key=lambda r: r[1])
    sev = Severity.HIGH if longest[1] >= 5 else Severity.MEDIUM

    return Insight(
        kind="observation", title="Tool loop detected",
        body=(f"{longest[0]} repeated {longest[1]} times consecutively. "
              "This usually means the AI is stuck retrying. "
              "An explicit constraint or path change would break the cycle."),
        category="efficiency", severity=sev, confidence=0.9,
        evidence={"tool": longest[0], "run_length": longest[1], "total_loops": len(runs)},
    )


def signal_session_duration_outlier(
    sid: str, spans: list[dict], tokens: dict, stats: TelemetryStats, profile: DataProfile,
) -> Insight | None:
    timestamps = [s["t"] for s in spans if s.get("t")]
    if len(timestamps) < 2:
        return None
    duration_ms = (max(timestamps) - min(timestamps)) / 1e6
    dist = profile.session_duration_ms

    if not dist.is_sparse():
        fence = dist.upper_fence(1.5)
        if duration_ms <= fence:
            return None
    else:
        if duration_ms <= 1_800_000:
            return None
        fence = 1_800_000

    return Insight(
        kind="observation", title="Long session",
        body=(f"Session lasted {_fmt_duration(duration_ms)} — above your typical ceiling of "
              f"{_fmt_duration(fence)}. Consider splitting into milestones with context resets."),
        category="cost", severity=Severity.MEDIUM,
        confidence=confidence_for(dist),
        evidence={"duration_ms": round(duration_ms), "fence_ms": round(fence)},
    )


def signal_session_aborted(
    sid: str, spans: list[dict], tokens: dict, stats: TelemetryStats, profile: DataProfile,
) -> Insight | None:
    timestamps = [s["t"] for s in spans if s.get("t")]
    if len(timestamps) < 2:
        return None
    duration_ms = (max(timestamps) - min(timestamps)) / 1e6
    if duration_ms >= 30_000:
        return None
    events = [s.get("event", "") for s in spans]
    if any(e in ("Stop", "SessionEnd", "SubagentStop") for e in events):
        return None
    tool_count = sum(1 for s in spans if s.get("tool"))
    if tool_count >= 3:
        return None
    return Insight(
        kind="observation", title="Session appears aborted",
        body=f"Session lasted {duration_ms / 1000:.0f}s with no completion signal and minimal tool activity.",
        category="workflow", severity=Severity.LOW, confidence=0.7,
        evidence={"duration_ms": round(duration_ms), "tool_count": tool_count},
    )


def signal_session_completion(
    sid: str, spans: list[dict], tokens: dict, stats: TelemetryStats, profile: DataProfile,
) -> Insight | None:
    events = [s.get("event", "") for s in spans]
    tool_count = sum(1 for s in spans if s.get("tool"))

    if any(e in ("Stop", "SessionEnd") for e in events):
        return Insight(
            kind="strength", title="Session completed",
            body="Session completed successfully with a clear termination signal.",
            category="workflow", severity=Severity.LOW, confidence=0.9,
            evidence={"completed": True},
        )
    elif any(e == "SubagentStop" for e in events):
        return Insight(
            kind="observation", title="Subagent-only completion",
            body="Session ended via subagent completion but no explicit Stop signal was observed.",
            category="workflow", severity=Severity.LOW, confidence=0.7,
            evidence={"completed": False, "subagent_stop": True},
        )
    elif tool_count > 5:
        return Insight(
            kind="observation", title="No completion signal",
            body=(f"No completion signal detected despite {tool_count} tool actions. "
                  "Adding a done-criteria to prompts helps the AI know when to stop."),
            category="workflow", severity=Severity.MEDIUM, confidence=0.8,
            evidence={"completed": False, "tool_count": tool_count},
        )
    return None


def signal_session_recovery(
    sid: str, spans: list[dict], tokens: dict, stats: TelemetryStats, profile: DataProfile,
) -> Insight | None:
    recovered = stats.session_recovered_failures.get(sid, 0)
    if recovered == 0:
        return None
    failures = sum(1 for s in spans if not s.get("ok", True))
    return Insight(
        kind="strength", title="Failure recovery",
        body=(f"Recovered from {recovered} failure(s) during this session — the AI successfully "
              "adapted its approach after initial errors."),
        category="reliability", severity=Severity.LOW, confidence=0.9,
        evidence={"recovered": recovered, "failures": failures},
    )


def signal_session_heavy_reads(
    sid: str, spans: list[dict], tokens: dict, stats: TelemetryStats, profile: DataProfile,
) -> Insight | None:
    reads = sum(1 for s in spans if s.get("event") == "BeforeReadFile")
    prompts = sum(1 for s in spans if s.get("event") == "UserPromptSubmit")
    if prompts == 0 or reads == 0:
        return None
    rpp = reads / prompts
    dist = profile.reads_per_prompt

    if not dist.is_sparse():
        fence = dist.upper_fence(1.5)
        if rpp <= fence:
            return None
    else:
        if reads < max(8, prompts * 4):
            return None
        fence = max(8.0 / prompts, 4.0)

    return Insight(
        kind="observation", title="Heavy file reads",
        body=(f"{reads} file reads across {prompts} prompts ({rpp:.1f}/prompt). "
              "Pinning relevant files in the initial prompt could cut exploration overhead."),
        category="context_hygiene", severity=Severity.MEDIUM,
        confidence=confidence_for(dist),
        evidence={"reads": reads, "prompts": prompts, "reads_per_prompt": round(rpp, 1)},
    )


def signal_session_productive_edits(
    sid: str, spans: list[dict], tokens: dict, stats: TelemetryStats, profile: DataProfile,
) -> Insight | None:
    edits = sum(1 for s in spans if s.get("event") == "AfterFileEdit")
    reads = sum(1 for s in spans if s.get("event") == "BeforeReadFile")
    if edits == 0:
        return None
    ratio = edits / max(reads, 1)
    if ratio < 0.3:
        return None
    return Insight(
        kind="strength", title="Productive editing",
        body=(f"{edits} file edits from {reads} reads ({ratio:.0%} edit rate). "
              "Prompts translated directly into code changes."),
        category="efficiency", severity=Severity.LOW, confidence=0.8,
        evidence={"edits": edits, "reads": reads, "ratio": round(ratio, 2)},
    )


def signal_session_model_mix(
    sid: str, spans: list[dict], tokens: dict, stats: TelemetryStats, profile: DataProfile,
) -> Insight | None:
    models = stats.session_models.get(sid)
    if not models or len(models) <= 1:
        return None
    primary, primary_count = models.most_common(1)[0]
    total = sum(models.values())
    pct = 100 * primary_count / total if total else 0
    model_list = ", ".join(m for m, _ in models.most_common(3))
    return Insight(
        kind="observation", title="Multi-model session",
        body=(f"Session used {len(models)} models: {model_list}. "
              f"Primary: {primary} ({pct:.0f}% of model events)."),
        category="workflow", severity=Severity.LOW, confidence=0.9,
        evidence={"model_count": len(models), "primary": primary, "primary_pct": round(pct)},
    )


def signal_session_cache_utilization(
    sid: str, spans: list[dict], tokens: dict, stats: TelemetryStats, profile: DataProfile,
) -> Insight | None:
    inp = tokens.get("input", 0)
    cr = tokens.get("cache_read", 0)
    if inp < 100_000:
        return None
    cache_ratio = _safe_ratio(cr, inp)

    if cache_ratio >= 0.20:
        return Insight(
            kind="strength", title="Good cache reuse",
            body=f"Cache read ratio of {cache_ratio:.0%} is helping reduce resend cost in this session.",
            category="cost", severity=Severity.LOW, confidence=0.8,
            evidence={"cache_ratio": round(cache_ratio, 2), "input_tokens": inp},
        )
    elif cache_ratio < 0.03 and inp > 500_000:
        return Insight(
            kind="observation", title="Weak cache reuse",
            body=(f"Cache read ratio is only {cache_ratio:.0%} despite {inp:,} input tokens. "
                  "Context churn or variable prefixes may be preventing cache hits."),
            category="cost", severity=Severity.MEDIUM, confidence=0.7,
            evidence={"cache_ratio": round(cache_ratio, 3), "input_tokens": inp},
        )
    return None


SESSION_SIGNALS = [
    signal_session_token_outlier,
    signal_session_efficiency,
    signal_session_failure_rate,
    signal_session_zero_failures,
    signal_session_loop_detected,
    signal_session_duration_outlier,
    signal_session_aborted,
    signal_session_completion,
    signal_session_recovery,
    signal_session_heavy_reads,
    signal_session_productive_edits,
    signal_session_model_mix,
    signal_session_cache_utilization,
]


def run_session_signals(
    session_id: str,
    spans: list[dict],
    tokens: dict[str, int],
    stats: TelemetryStats,
    profile: DataProfile,
) -> list[Insight]:
    """Run all session signals, return sorted by priority."""
    results: list[Insight] = []
    for fn in SESSION_SIGNALS:
        insight = fn(session_id, spans, tokens, stats, profile)
        if insight is not None:
            results.append(insight)
    results.sort(key=lambda i: i.priority, reverse=True)
    return results
