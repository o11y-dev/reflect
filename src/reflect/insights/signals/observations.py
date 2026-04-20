"""Observation signal functions. Every signal has a real triggering condition."""
from __future__ import annotations

from reflect.models import TelemetryStats
from reflect.utils import _safe_ratio

from ..types import DataProfile, Insight, Severity, confidence_for


def signal_execution_heavy(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    if prompts == 0:
        return None
    ratio = _safe_ratio(pre_tool, prompts)
    dist = profile.tools_per_prompt

    if not dist.is_sparse():
        fence = dist.upper_fence(1.5)
        if ratio <= fence:
            return None
        ref = f"p75: {dist.p75:.1f}:1"
    else:
        if ratio < 5.0:
            return None
        fence = 5.0
        ref = "baseline 5.0:1"

    return Insight(
        kind="observation", title="Execution-heavy usage",
        body=(f"Tool activity ({ratio:.1f}:1) is significantly above your typical range ({ref}). "
              "Ensure tasks have clear done-criteria so the AI knows when to stop."),
        category="efficiency", severity=Severity.MEDIUM,
        confidence=confidence_for(dist),
        evidence={"ratio": round(ratio, 1), "fence": round(fence, 1)},
    )


def signal_clarification_churn(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    if prompts < 10:
        return None
    ratio = _safe_ratio(pre_tool, prompts)
    dist = profile.tools_per_prompt

    if not dist.is_sparse():
        if ratio >= dist.p25:
            return None
    else:
        if ratio > 1.0:
            return None

    return Insight(
        kind="observation", title="Clarification churn",
        body=(f"Prompt volume is high relative to execution ({ratio:.1f} tool calls per prompt). "
              "That usually signals vague asks or too much back-and-forth before action."),
        category="efficiency", severity=Severity.HIGH,
        confidence=confidence_for(dist),
        evidence={"ratio": round(ratio, 1), "prompts": prompts},
    )


def signal_heavy_context_gathering(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    reads = stats.events_by_type.get("BeforeReadFile", 0)
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    if prompts == 0 or reads == 0:
        return None
    rpp = _safe_ratio(reads, prompts)
    dist = profile.reads_per_prompt

    if not dist.is_sparse():
        fence = dist.upper_fence(1.5)
        if rpp <= fence:
            return None
    else:
        if reads < max(8, prompts * 4):
            return None
        fence = max(8.0 / prompts, 4.0) if prompts else 4.0

    return Insight(
        kind="observation", title="Heavy context gathering",
        body=(f"{reads} file reads across {prompts} prompts ({rpp:.1f} reads/prompt). "
              "Early context pinning could reduce overhead."),
        category="context_hygiene", severity=Severity.MEDIUM,
        confidence=confidence_for(dist),
        evidence={"reads": reads, "prompts": prompts, "reads_per_prompt": round(rpp, 1)},
    )


def signal_tool_failures(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    failures = stats.events_by_type.get("PostToolUseFailure", 0)
    if failures == 0:
        return None
    fail_rate = _safe_ratio(failures, pre_tool) if pre_tool > 0 else 0.0

    if not profile.tools_per_prompt.is_sparse() and profile.total_tool_calls > 0:
        baseline = _safe_ratio(profile.total_failures, profile.total_tool_calls)
        threshold = max(baseline * 2.5, 0.05)
    else:
        threshold = 0.05

    if failures < 5 and fail_rate < threshold:
        return None

    if fail_rate > 0.20:
        sev = Severity.CRITICAL
    elif fail_rate > 0.10:
        sev = Severity.HIGH
    else:
        sev = Severity.MEDIUM

    return Insight(
        kind="observation", title="Tool failures detected",
        body=(f"{failures} tool failures ({fail_rate:.1%} of tool calls). "
              "Path and schema validation up front can reduce iteration cost."),
        category="reliability", severity=sev, confidence=0.9,
        evidence={"failures": failures, "pre_tool": pre_tool, "fail_rate": round(fail_rate, 4)},
    )


def signal_subagent_usage(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    starts = stats.events_by_type.get("SubagentStart", 0)
    if starts == 0:
        return None
    types = len(stats.subagent_types)
    return Insight(
        kind="observation", title="Subagent workflows active",
        body=(f"Subagents are used for deeper workflows ({starts} launches across {types} types). "
              "Provide explicit deliverable formats in subagent prompts for cleaner outputs."),
        category="delegation", severity=Severity.LOW, confidence=0.9,
        evidence={"starts": starts, "types": types},
    )


def signal_top_session_dominance(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    economy = profile.token_economy
    share = economy.get("top_session_share", 0)
    dist = profile.session_token_share

    if not dist.is_sparse():
        if not dist.is_outlier_high(share):
            return None
    else:
        if share < 30:
            return None

    return Insight(
        kind="observation", title="Token spend dominated by one session",
        body=(f"One session accounts for {share:.1f}% of all observed tokens. "
              "Context accumulation is dominating cost rather than isolated prompts."),
        category="cost", severity=Severity.HIGH,
        confidence=confidence_for(dist),
        evidence={"top_session_share": round(share, 1)},
    )


def signal_high_context_sessions(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    count = profile.token_economy.get("high_context_sessions", 0)
    if count == 0:
        return None
    return Insight(
        kind="observation", title="Context-heavy sessions detected",
        body=(f"{count} session(s) are context-heavy. Long-lived sessions are where "
              "token accumulation quietly compounds."),
        category="cost", severity=Severity.MEDIUM, confidence=0.8,
        evidence={"count": count},
    )


def signal_context_hygiene_pressure(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    economy = profile.token_economy
    rpp = economy.get("reads_per_prompt", 0)
    mpp = economy.get("mcp_per_prompt", 0)
    dist = profile.reads_per_prompt

    rpp_high = (not dist.is_sparse() and rpp > dist.p90) or (dist.is_sparse() and rpp >= 3.0)
    mpp_high = mpp >= 0.5

    if not rpp_high and not mpp_high:
        return None
    return Insight(
        kind="observation", title="Context hygiene pressure elevated",
        body=(f"{rpp:.1f} file reads/prompt and {mpp:.1f} MCP calls/prompt. "
              "Tool and MCP metadata can bloat context even before real work starts."),
        category="context_hygiene", severity=Severity.MEDIUM,
        confidence=confidence_for(dist, base=0.7),
        evidence={"reads_per_prompt": round(rpp, 1), "mcp_per_prompt": round(mpp, 1)},
    )


def signal_weak_cache_reuse(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    if prompts == 0 or stats.total_input_tokens <= 1_000_000:
        return None
    economy = profile.token_economy
    if economy.get("cache_reuse_ratio", 1.0) >= 0.05:
        return None
    return Insight(
        kind="observation", title="Weak prompt cache reuse",
        body=(f"Prompt caching leverage is weak ({economy.get('cache_hit_pct', 0):.1f}% effective hit rate). "
              "Frequent context resets or variable prefixes may be forcing expensive re-sends."),
        category="cost", severity=Severity.HIGH, confidence=0.7,
        evidence={"cache_hit_pct": round(economy.get("cache_hit_pct", 0), 1)},
    )


def signal_strong_cache_reuse(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    economy = profile.token_economy
    ratio = economy.get("cache_reuse_ratio", 0)
    if ratio < 0.15:
        return None
    if ratio > 1.0:
        body = (f"Prompt cache reuse is materially helping ({ratio:.1f}x cached read volume vs fresh input). "
                "Stable prefixes and repeated working patterns are reducing resend cost.")
    else:
        body = (f"Prompt cache reuse is materially helping ({economy.get('cache_hit_pct', 0):.1f}% effective hit rate). "
                "Stable prefixes and repeated working patterns are reducing resend cost.")
    return Insight(
        kind="observation", title="Strong cache reuse",
        body=body, category="cost", severity=Severity.LOW, confidence=0.9,
        evidence={"cache_reuse_ratio": round(ratio, 2)},
    )


def signal_expensive_prompts(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    economy = profile.token_economy
    avg_in = economy.get("avg_input_per_prompt", 0)
    if avg_in < 20_000:
        return None
    return Insight(
        kind="observation", title="Average prompt cost very high",
        body=(f"Average prompt cost is very high ({avg_in:.0f} input tokens per prompt). "
              "Large pasted context, repeated session history, or oversized MCP/tool descriptions "
              "are likely driving spend."),
        category="cost", severity=Severity.HIGH,
        confidence=confidence_for(profile.session_input_tokens),
        evidence={"avg_input_per_prompt": round(avg_in)},
    )


def signal_output_heavy(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    if stats.total_output_tokens == 0 or stats.total_input_tokens == 0:
        return None
    ratio = _safe_ratio(stats.total_output_tokens, stats.total_input_tokens)
    if ratio < 1.2:
        return None
    return Insight(
        kind="observation", title="Output-heavy sessions amplifying cost",
        body=(f"Output/input ratio ({ratio:.2f}) is high. "
              "Long reasoning traces and verbose drafts are expensive because output tokens cost more."),
        category="cost", severity=Severity.MEDIUM, confidence=0.8,
        evidence={"output_input_ratio": round(ratio, 2)},
    )


def signal_premium_model_concentration(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    share = profile.heavy_model_share
    if stats.total_events < 200:
        return None
    if share >= 60:
        return Insight(
            kind="observation", title="Premium model concentration high",
            body=(f"Premium model usage is {share:.1f}% of model events. "
                  "That is often correct for planning or deep analysis, but expensive for "
                  "repetitive implementation loops."),
            category="cost", severity=Severity.MEDIUM, confidence=0.8,
            evidence={"heavy_model_share": round(share, 1)},
        )
    elif share <= 20:
        return Insight(
            kind="observation", title="Heavy-model usage restrained",
            body=(f"Heavy-model usage is restrained ({share:.1f}% of model events), "
                  "which usually helps contain cost on routine implementation work."),
            category="cost", severity=Severity.LOW, confidence=0.8,
            evidence={"heavy_model_share": round(share, 1)},
        )
    return None


def signal_multi_agent_spread(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    if len(stats.agents) < 3:
        return None
    return Insight(
        kind="observation", title="Multi-agent spread",
        body=(f"Work is spread across {len(stats.agents)} agents. Cross-agent workflows can be powerful, "
              "but they also make model/tool discipline and context hygiene more important."),
        category="workflow", severity=Severity.LOW, confidence=0.9,
        evidence={"agent_count": len(stats.agents)},
    )


def signal_strong_validation(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    shells = stats.events_by_type.get("BeforeShellExecution", 0)
    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    if shells < max(10, pre_tool * 0.2):
        return None
    return Insight(
        kind="observation", title="Strong validation activity",
        body="Validation activity is strong relative to execution. That usually means the workflow "
             "is grounded in real shell checks, not just generated edits.",
        category="workflow", severity=Severity.LOW, confidence=0.9,
        evidence={"shell_executions": shells, "pre_tool": pre_tool},
    )


def signal_interrupted_tools(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    post_tool = stats.events_by_type.get("PostToolUse", 0)
    if pre_tool == 0:
        return None
    gap = pre_tool - post_tool
    if gap <= max(5, pre_tool * 0.05):
        return None
    return Insight(
        kind="observation", title="Interrupted tool calls",
        body=(f"{gap} tool calls have no matching PostToolUse — some may be interrupted. "
              "Consider explicit validation/checkpoint steps."),
        category="reliability", severity=Severity.MEDIUM, confidence=0.7,
        evidence={"gap": gap, "pre_tool": pre_tool, "post_tool": post_tool},
    )


SIGNALS = [
    signal_execution_heavy,
    signal_clarification_churn,
    signal_heavy_context_gathering,
    signal_tool_failures,
    signal_subagent_usage,
    signal_top_session_dominance,
    signal_high_context_sessions,
    signal_context_hygiene_pressure,
    signal_weak_cache_reuse,
    signal_strong_cache_reuse,
    signal_expensive_prompts,
    signal_output_heavy,
    signal_premium_model_concentration,
    signal_multi_agent_spread,
    signal_strong_validation,
    signal_interrupted_tools,
]
