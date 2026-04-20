"""Recommendation signal functions. No unconditional entries."""
from __future__ import annotations

from reflect.models import TelemetryStats
from reflect.utils import _safe_ratio

from ..types import DataProfile, Insight, Severity, confidence_for


def signal_rec_prompt_contract(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    total_sessions = len(stats.sessions_seen)
    completed = sum(1 for v in stats.session_goal_completed.values() if v)
    completion_rate = 100 * _safe_ratio(completed, total_sessions) if total_sessions else 100

    ratio = _safe_ratio(pre_tool, prompts) if prompts else 0.0
    dist = profile.tools_per_prompt

    low_completion = completion_rate < 60
    low_ratio = (not dist.is_sparse() and dist.is_outlier_low(ratio)) or (dist.is_sparse() and prompts >= 5 and ratio < 1.5)

    if not low_completion and not low_ratio:
        return None
    return Insight(
        kind="recommendation", title="Use a fixed prompt contract for non-trivial requests",
        body="Goal, Context, Constraints, Output, Done-when. "
             "This structure reduces ambiguity and helps the AI plan before executing.",
        category="workflow", severity=Severity.HIGH,
        confidence=confidence_for(dist),
        evidence={"completion_rate": round(completion_rate, 1), "ratio": round(ratio, 1)},
    )


def signal_rec_pin_files(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    reads = stats.events_by_type.get("BeforeReadFile", 0)
    if prompts == 0:
        return None

    ratio = _safe_ratio(pre_tool, prompts)
    rpp = _safe_ratio(reads, prompts)
    dist_t = profile.tools_per_prompt
    dist_r = profile.reads_per_prompt

    high_tools = (not dist_t.is_sparse() and dist_t.is_outlier_high(ratio)) or (dist_t.is_sparse() and ratio >= 3.0)
    high_reads = (not dist_r.is_sparse() and dist_r.is_outlier_high(rpp)) or (dist_r.is_sparse() and rpp >= 3.0)

    if not high_tools and not high_reads:
        return None
    return Insight(
        kind="recommendation", title="Pin relevant files in the first prompt",
        body="Pin relevant files/folders in the first prompt to reduce exploratory tool churn.",
        category="efficiency", severity=Severity.MEDIUM,
        confidence=max(confidence_for(dist_t), confidence_for(dist_r)),
        evidence={"tool_ratio": round(ratio, 1), "reads_per_prompt": round(rpp, 1)},
    )


def signal_rec_schema_check(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    failures = stats.events_by_type.get("PostToolUseFailure", 0)
    if failures == 0:
        return None
    return Insight(
        kind="recommendation",
        title="Require schema/path check before execution",
        body="For MCP and path-sensitive tasks, require schema/path check as step one before execution.",
        category="reliability", severity=Severity.MEDIUM, confidence=0.8,
        evidence={"failures": failures},
    )


def signal_rec_structured_handoff(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    stops = stats.events_by_type.get("Stop", 0)
    if len(stats.sessions_seen) <= 3 or prompts == 0 or stops < prompts:
        return None
    return Insight(
        kind="recommendation", title="Close tasks with a structured handoff",
        body="Close each major task with a structured handoff: changes, validations, "
             "residual risk, and next command.",
        category="workflow", severity=Severity.LOW, confidence=0.7,
        evidence={"stops": stops, "prompts": prompts},
    )


def signal_rec_two_phases(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    economy = profile.token_economy
    dist = profile.tokens_per_tool
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    total_tokens = economy.get("total_tokens", 0)

    if not dist.is_sparse():
        if dist.median <= dist.p75 * 0.8:
            return None  # not consistently high
    else:
        if total_tokens < 500_000 or prompts == 0:
            return None

    return Insight(
        kind="recommendation", title="Use two-phase execution for complex tasks",
        body="For medium/large tasks, request two phases explicitly: plan first, execute second.",
        category="workflow", severity=Severity.MEDIUM,
        confidence=confidence_for(dist),
        evidence={"total_tokens": total_tokens},
    )


def signal_rec_model_routing(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    if profile.heavy_model_share < 50 or stats.total_events < 100:
        return None
    return Insight(
        kind="recommendation", title="Route models by task phase",
        body="Use heavy models for planning and analysis — then switch to a balanced model for implementation.",
        category="cost", severity=Severity.MEDIUM, confidence=0.8,
        evidence={"heavy_model_share": round(profile.heavy_model_share, 1)},
    )


def signal_rec_session_splitting(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    economy = profile.token_economy
    share = economy.get("top_session_share", 0)
    high_ctx = economy.get("high_context_sessions", 0)
    dist = profile.session_token_share

    high_share = (not dist.is_sparse() and dist.is_outlier_high(share)) or (dist.is_sparse() and share >= 25)

    if not high_share and high_ctx == 0:
        return None
    return Insight(
        kind="recommendation", title="Split large tasks into smaller sessions",
        body="Split large tasks into smaller user stories and start a fresh session "
             "after each completed milestone to control context accumulation.",
        category="cost", severity=Severity.HIGH, confidence=0.8,
        evidence={"top_session_share": round(share, 1), "high_context_sessions": high_ctx},
    )


def signal_rec_cache_hygiene(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    if prompts == 0 or stats.total_input_tokens <= 1_000_000:
        return None
    if profile.cache_reuse_ratio >= 0.05:
        return None
    return Insight(
        kind="recommendation", title="Compact context to improve cache reuse",
        body="Compact or summarize after task completion instead of carrying a swollen context forward; "
             "this also improves prompt-cache reuse.",
        category="cost", severity=Severity.MEDIUM, confidence=0.7,
        evidence={"cache_reuse_ratio": round(profile.cache_reuse_ratio, 3)},
    )


def signal_rec_reduce_reads(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    reads = stats.events_by_type.get("BeforeReadFile", 0)
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    if prompts == 0:
        return None
    rpp = _safe_ratio(reads, prompts)
    dist = profile.reads_per_prompt

    if not dist.is_sparse():
        if not dist.is_outlier_high(rpp):
            return None
    else:
        if rpp < 3.0:
            return None

    return Insight(
        kind="recommendation",
        title="Reduce file-read churn",
        body="Reduce file-read churn by pinning the exact files, functions, and examples "
             "in the first prompt.",
        category="efficiency", severity=Severity.MEDIUM,
        confidence=confidence_for(dist),
        evidence={"reads_per_prompt": round(rpp, 1)},
    )


def signal_rec_mcp_discipline(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    economy = profile.token_economy
    if economy.get("mcp_per_prompt", 0) < 0.5:
        return None
    return Insight(
        kind="recommendation", title="Reduce MCP context bloat",
        body="Turn off unnecessary MCPs and prefer skills/scripts for deterministic tasks "
             "so tool descriptions and large responses do not bloat context.",
        category="context_hygiene", severity=Severity.MEDIUM, confidence=0.8,
        evidence={"mcp_per_prompt": round(economy.get("mcp_per_prompt", 0), 1)},
    )


def signal_rec_subagent_format(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    if not stats.subagent_types:
        return None
    return Insight(
        kind="recommendation", title="Specify subagent output format",
        body="When delegating to subagents, specify output format (table, markdown, JSON) "
             "to avoid manual reformatting.",
        category="delegation", severity=Severity.LOW, confidence=0.7,
        evidence={"subagent_types": len(stats.subagent_types)},
    )


SIGNALS = [
    signal_rec_prompt_contract,
    signal_rec_pin_files,
    signal_rec_schema_check,
    signal_rec_structured_handoff,
    signal_rec_two_phases,
    signal_rec_model_routing,
    signal_rec_session_splitting,
    signal_rec_cache_hygiene,
    signal_rec_reduce_reads,
    signal_rec_mcp_discipline,
    signal_rec_subagent_format,
]
