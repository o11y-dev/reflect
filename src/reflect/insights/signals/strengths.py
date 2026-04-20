"""Strength signal functions."""
from __future__ import annotations

from reflect.models import TelemetryStats
from reflect.utils import _safe_ratio

from ..types import DataProfile, Insight, Severity, confidence_for


def signal_strength_leverage(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    if prompts == 0:
        return None
    ratio = _safe_ratio(pre_tool, prompts)

    dist = profile.tools_per_prompt
    if not dist.is_sparse():
        if ratio > dist.p95:
            tier, sev, ref = "p95", Severity.HIGH, f"{dist.p95:.1f}"
        elif ratio > dist.p75:
            tier, sev, ref = "p75", Severity.MEDIUM, f"{dist.p75:.1f}"
        elif ratio > dist.median and ratio >= 2.0:
            tier, sev, ref = "median", Severity.LOW, f"{dist.median:.1f}"
        else:
            return None
    else:
        if ratio >= 10.0:
            tier, sev, ref = "10:1", Severity.HIGH, "10.0"
        elif ratio >= 5.0:
            tier, sev, ref = "5:1", Severity.MEDIUM, "5.0"
        elif ratio >= 3.0:
            tier, sev, ref = "3:1", Severity.LOW, "3.0"
        else:
            return None

    labels = {"p95": "Exceptional leverage per prompt", "p75": "High leverage per prompt",
              "median": "Good prompt-to-action ratio", "10:1": "Exceptional leverage per prompt",
              "5:1": "High leverage per prompt", "3:1": "Good prompt-to-action ratio"}

    return Insight(
        kind="strength", title=labels[tier],
        body=(f"Each prompt drives {ratio:.1f} tool actions (above your {tier} at {ref}), "
              "showing well-scoped, actionable requests."),
        category="efficiency", severity=sev,
        confidence=confidence_for(dist),
        evidence={"ratio": round(ratio, 1), "tier": tier, "ref": ref},
    )


def signal_strength_low_failure_rate(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    failures = stats.events_by_type.get("PostToolUseFailure", 0)
    if pre_tool <= 10:
        return None
    fail_rate = _safe_ratio(failures, pre_tool)
    if fail_rate >= 0.05:
        return None
    return Insight(
        kind="strength", title="Low tool failure rate",
        body=(f"{failures} failures out of {pre_tool} tool calls ({fail_rate:.1%}) — "
              "indicates well-formed requests with clear context."),
        category="reliability", severity=Severity.MEDIUM, confidence=0.9,
        evidence={"failures": failures, "pre_tool": pre_tool, "fail_rate": round(fail_rate, 4)},
    )


def signal_strength_subagent_delegation(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    starts = stats.events_by_type.get("SubagentStart", 0)
    if starts == 0:
        return None
    types = len(stats.subagent_types)
    return Insight(
        kind="strength", title="Effective subagent delegation",
        body=(f"Used {starts} subagent launches across {types} agent type(s), "
              "keeping the main conversation focused and enabling parallel investigation."),
        category="delegation", severity=Severity.LOW, confidence=0.9,
        evidence={"starts": starts, "types": types},
    )


def signal_strength_clean_mcp(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    mcp_before = stats.events_by_type.get("BeforeMCPExecution", 0)
    mcp_after = stats.events_by_type.get("AfterMCPExecution", 0)
    if mcp_before == 0:
        return None
    if abs(mcp_before - mcp_after) > 2:
        return None
    return Insight(
        kind="strength", title="Clean MCP integration",
        body=(f"All {mcp_before} MCP executions completed successfully "
              "(before/after counts match), showing proper schema usage."),
        category="tooling", severity=Severity.LOW, confidence=0.9,
        evidence={"mcp_before": mcp_before, "mcp_after": mcp_after},
    )


def signal_strength_productive_editing(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    edits = stats.events_by_type.get("AfterFileEdit", 0)
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    if edits == 0 or prompts == 0:
        return None
    epp = _safe_ratio(edits, prompts)

    if epp >= 0.5:
        return Insight(
            kind="strength", title="Highly productive editing sessions",
            body=(f"{epp:.1f} file edits per prompt. Prompts translate directly "
                  "into code changes, showing clear intent."),
            category="efficiency", severity=Severity.MEDIUM, confidence=0.8,
            evidence={"edits_per_prompt": round(epp, 2)},
        )
    elif epp >= 0.2:
        return Insight(
            kind="strength", title="Productive editing sessions",
            body="Prompts lead to actual code changes, not just exploration. "
                 "This shows clear intent in requests.",
            category="efficiency", severity=Severity.LOW, confidence=0.7,
            evidence={"edits_per_prompt": round(epp, 2)},
        )
    return None


def signal_strength_efficient_context(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    reads = stats.events_by_type.get("BeforeReadFile", 0)
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    if reads <= 0 or prompts <= 0:
        return None
    if reads >= prompts * 3:
        return None
    return Insight(
        kind="strength", title="Efficient context gathering",
        body="File reads are proportionate to prompt volume, avoiding unnecessary exploration overhead.",
        category="efficiency", severity=Severity.LOW, confidence=0.7,
        evidence={"reads": reads, "prompts": prompts},
    )


def signal_strength_shell_usage(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    shells = stats.events_by_type.get("BeforeShellExecution", 0)
    if shells == 0:
        return None
    return Insight(
        kind="strength", title="Active shell usage",
        body=f"{shells} shell executions for validation, builds, and scripting alongside AI assistance.",
        category="workflow", severity=Severity.LOW, confidence=0.9,
        evidence={"shell_executions": shells},
    )


def signal_strength_sustained_usage(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    if len(stats.sessions_seen) > 5:
        return Insight(
            kind="strength", title="Sustained usage",
            body=(f"{len(stats.sessions_seen)} unique sessions over {stats.days_active} "
                  "active days, showing consistent and productive AI collaboration."),
            category="workflow", severity=Severity.LOW, confidence=0.9,
            evidence={"sessions": len(stats.sessions_seen), "days": stats.days_active},
        )
    elif stats.session_files > 1:
        return Insight(
            kind="strength", title="Multiple focused sessions",
            body=(f"{stats.session_files} sessions suggest breaking work into discrete, "
                  "manageable chunks rather than one monolithic chat."),
            category="workflow", severity=Severity.LOW, confidence=0.7,
            evidence={"session_files": stats.session_files},
        )
    return None


SIGNALS = [
    signal_strength_leverage,
    signal_strength_low_failure_rate,
    signal_strength_subagent_delegation,
    signal_strength_clean_mcp,
    signal_strength_productive_editing,
    signal_strength_efficient_context,
    signal_strength_shell_usage,
    signal_strength_sustained_usage,
]
