"""Practical example signal functions — domain-agnostic."""
from __future__ import annotations

from reflect.models import TelemetryStats
from reflect.utils import _safe_ratio

from ..types import DataProfile, Insight, Severity, confidence_for


def signal_example_pin_context(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
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
        if reads < max(8, prompts * 4):
            return None

    return Insight(
        kind="example",
        title="Reduce file exploration by pinning context upfront",
        body="Pin the exact files and lines relevant to the task in your first prompt.",
        category="efficiency", severity=Severity.MEDIUM,
        confidence=confidence_for(dist),
        before="Fix the bug in the payment service",
        after=(
            "@src/services/payment.py @src/models/transaction.py "
            "Fix the timeout bug in process_payment — the DB query on line 84 "
            "is missing a connection pool timeout"
        ),
    )


def signal_example_prevent_failures(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    failures = stats.events_by_type.get("PostToolUseFailure", 0)
    if failures == 0:
        return None

    top_mcp = stats.mcp_servers.most_common(1)
    if top_mcp:
        server = top_mcp[0][0]
        after = (
            f"Use the {server} MCP to check pipeline status for project <your-project>. "
            "Read the MCP schema first."
        )
        before = f"Run the {server} MCP to check the pipeline"
    else:
        after = (
            "Verify the tool schema and parameters before executing. "
            "Check paths exist and match the expected format."
        )
        before = "Run the tool to check the pipeline"

    return Insight(
        kind="example",
        title="Prevent tool failures with explicit paths and schemas",
        body="Validate schemas and paths before execution to avoid retry cycles.",
        category="reliability", severity=Severity.MEDIUM, confidence=0.8,
        before=before, after=after,
    )


def signal_example_structured_review(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    if prompts == 0:
        return None
    ratio = _safe_ratio(pre_tool, prompts)
    dist = profile.tools_per_prompt

    if not dist.is_sparse():
        if ratio <= dist.p75:
            return None
    else:
        if ratio < 5.0:
            return None

    return Insight(
        kind="example",
        title="Add structure to high-leverage prompts",
        body="Structure high-leverage prompts with Goal, Context, Constraints, Output, Done-when.",
        category="efficiency", severity=Severity.MEDIUM, confidence=0.8,
        before="Review this PR and check everything",
        after=(
            "Goal: Review PR #NNN for security and correctness\n"
            "Context: @.cursor/rules/standards.mdc\n"
            "Constraints: Focus on auth and input validation\n"
            "Output: Structured review with severity ratings\n"
            "Done when: All findings documented"
        ),
    )


def signal_example_model_routing(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    if profile.heavy_model_share < 50:
        return None
    return Insight(
        kind="example",
        title="Use a heavy model for planning, then switch for implementation",
        body="Split heavy-model work into a planning phase and a cheaper execution phase.",
        category="cost", severity=Severity.MEDIUM, confidence=0.8,
        before="Use the biggest model to read, design, implement, test, and write the PR.",
        after=(
            "Phase 1 (heavy model): inspect and produce a concrete plan.\n"
            "Done when: the plan lists files, edge cases, and validation steps.\n"
            "Then start a fresh session with a balanced model and execute only that plan."
        ),
    )


def signal_example_context_reset(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    economy = profile.token_economy
    share = economy.get("top_session_share", 0)
    high_ctx = economy.get("high_context_sessions", 0)
    if share < 25 and high_ctx == 0:
        return None
    return Insight(
        kind="example",
        title="Reset or compact context between milestones",
        body="Start a fresh session after each completed milestone.",
        category="cost", severity=Severity.MEDIUM, confidence=0.8,
        before="Keep working in the same session until the whole feature is done.",
        after=(
            "Finish one user story at a time.\n"
            "At the end of each milestone: summarize changes, list remaining tasks, "
            "then start a fresh session."
        ),
    )


def signal_example_scope_mcp(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    mcp_before = stats.events_by_type.get("BeforeMCPExecution", 0)
    if mcp_before == 0:
        return None

    top = stats.mcp_servers.most_common(1)
    server = top[0][0] if top else "your-mcp-server"

    return Insight(
        kind="example",
        title="Scope MCP calls with specific parameters",
        body="Narrow MCP calls to specific resources, time ranges, and filters.",
        category="context_hygiene", severity=Severity.LOW, confidence=0.8,
        before=f"Check what's happening in {server}",
        after=(
            f"Query {server} for ERROR-level logs from your-service "
            "in the last 2 hours, filtering by region. "
            "Summarize the top 5 error patterns."
        ),
    )


def signal_example_subagent_deliverables(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    starts = stats.events_by_type.get("SubagentStart", 0)
    if starts == 0:
        return None
    return Insight(
        kind="example",
        title="Guide subagents with clear deliverables",
        body="Specify the exact output format when delegating to subagents.",
        category="delegation", severity=Severity.LOW, confidence=0.8,
        before="Use the code reviewer to look at my changes",
        after=(
            "Use the code-reviewer subagent to review changes in src/services/auth.py — "
            "focus on input validation, error handling, and missing test coverage. "
            "Return findings as a table: location, severity, suggestion."
        ),
    )


def signal_example_done_state(stats: TelemetryStats, profile: DataProfile) -> Insight | None:
    total = len(stats.sessions_seen)
    if total == 0:
        return None
    completed = sum(1 for v in stats.session_goal_completed.values() if v)
    rate = 100 * _safe_ratio(completed, total)
    if rate >= 70:
        return None
    return Insight(
        kind="example",
        title="End tasks with a clear done-state",
        body="Define explicit completion criteria so the AI knows when to stop.",
        category="workflow", severity=Severity.MEDIUM, confidence=0.8,
        before="Deploy the fix",
        after=(
            "Deploy the hotfix to staging.\n"
            "Done when: staging pipeline is green, smoke test passes, "
            "and monitoring shows no new errors for 5 minutes."
        ),
    )


SIGNALS = [
    signal_example_pin_context,
    signal_example_prevent_failures,
    signal_example_structured_review,
    signal_example_model_routing,
    signal_example_context_reset,
    signal_example_scope_mcp,
    signal_example_subagent_deliverables,
    signal_example_done_state,
]
