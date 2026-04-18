from __future__ import annotations

from math import ceil

from reflect.models import TelemetryStats
from reflect.utils import _safe_ratio

# ---------------------------------------------------------------------------
# Derived insights
# ---------------------------------------------------------------------------

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


def compute_session_quality(
    session_id: str,
    spans: list[dict],
    tokens: dict[str, int],
) -> float:
    """Heuristic quality score (0-100) based on signals in spans."""
    # 1. Goal Completion (40%)
    # Increased weight for completion signals
    has_completion_signal = any(s["event"] in ("Stop", "SubagentStop", "SessionEnd") for s in spans)
    completion_score = 40 if has_completion_signal else 0

    # 2. Efficiency (30%)
    # Penalize extreme token usage or excessive tool count
    tool_uses = len([s for s in spans if s.get("tool")])
    total_tokens = tokens.get("input", 0) + tokens.get("output", 0)

    efficiency_score = 30 # Start full
    if tool_uses > 0:
        tokens_per_tool = total_tokens / tool_uses
        if tokens_per_tool > 30000:
            efficiency_score -= 20
        elif tokens_per_tool > 10000:
            efficiency_score -= 10

        if tool_uses > 20: # Long session penalty
             efficiency_score -= 10
    else:
        # Chat only: penalty only for extreme length
        if total_tokens > 50000:
            efficiency_score -= 15
        elif total_tokens > 20000:
            efficiency_score -= 5

    efficiency_score = max(0, efficiency_score)

    # 3. Tool Reliability (20%)
    # ok: False means we explicitly saw a failure event for that span
    failures = len([s for s in spans if not s.get("ok", True)])
    reliability_score = max(0, 20 - (failures * 10))

    # 4. Loop Detection (10%)
    # Basic detection: identical tool calls back-to-back
    tool_seq = [s["tool"] for s in spans if s.get("tool")]
    loops = 0
    for i in range(len(tool_seq) - 1):
        if tool_seq[i] == tool_seq[i+1]:
            loops += 1
    loop_score = max(0, 10 - (loops * 5))

    return float(completion_score + efficiency_score + reliability_score + loop_score)


def compute_token_economy(stats: TelemetryStats) -> dict:
    """Derive token-economy signals from local telemetry."""
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    file_reads = stats.events_by_type.get("BeforeReadFile", 0)
    mcp_calls = stats.events_by_type.get("BeforeMCPExecution", 0)

    total_tokens = (
        stats.total_input_tokens
        + stats.total_output_tokens
        + stats.total_cache_creation_tokens
        + stats.total_cache_read_tokens
    )
    avg_input_per_prompt = _safe_ratio(stats.total_input_tokens, prompts)
    avg_output_per_prompt = _safe_ratio(stats.total_output_tokens, prompts)
    reads_per_prompt = _safe_ratio(file_reads, prompts)
    mcp_per_prompt = _safe_ratio(mcp_calls, prompts)
    cache_reuse_ratio = _safe_ratio(stats.total_cache_read_tokens, stats.total_input_tokens)
    cache_hit_pct = 100 * min(cache_reuse_ratio, 1.0)

    session_rows: list[dict] = []
    for sid in stats.sessions_seen:
        tok = stats.session_tokens.get(sid, {})
        total_session_tokens = (
            tok.get("input", 0)
            + tok.get("output", 0)
            + tok.get("cache_creation", 0)
            + tok.get("cache_read", 0)
        )
        prompt_count = sum(
            1 for span in stats.session_span_details.get(sid, [])
            if span.get("event") == "UserPromptSubmit"
        )
        if prompt_count == 0:
            prompt_count = stats.session_events.get(sid, 0) // 20
        session_rows.append({
            "sid": sid,
            "tokens": total_session_tokens,
            "prompts": prompt_count,
            "events": stats.session_events.get(sid, 0),
        })
    session_rows.sort(key=lambda row: row["tokens"], reverse=True)
    top_session_tokens = session_rows[0]["tokens"] if session_rows else 0
    top_session_share = 100 * _safe_ratio(top_session_tokens, total_tokens)
    high_context_sessions = sum(
        1 for row in session_rows
        if row["tokens"] >= 500_000 or row["prompts"] >= 25 or row["events"] >= 1500
    )

    total_model_events = sum(stats.models_by_count.values())
    heavy_model_events = 0
    for model, count in stats.models_by_count.items():
        m = model.lower()
        if "opus" in m or "pro" in m or "thinking" in m:
            heavy_model_events += count
    heavy_model_share = 100 * _safe_ratio(heavy_model_events, total_model_events)

    return {
        "total_tokens": total_tokens,
        "avg_input_per_prompt": avg_input_per_prompt,
        "avg_output_per_prompt": avg_output_per_prompt,
        "reads_per_prompt": reads_per_prompt,
        "mcp_per_prompt": mcp_per_prompt,
        "cache_hit_pct": cache_hit_pct,
        "cache_reuse_ratio": cache_reuse_ratio,
        "top_session_tokens": top_session_tokens,
        "top_session_share": top_session_share,
        "high_context_sessions": high_context_sessions,
        "heavy_model_share": heavy_model_share,
    }


def build_strengths(stats: TelemetryStats) -> list[str]:
    strengths: list[str] = []

    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    failures = stats.events_by_type.get("PostToolUseFailure", 0)
    before_read = stats.events_by_type.get("BeforeReadFile", 0)
    subagent_starts = stats.events_by_type.get("SubagentStart", 0)
    mcp_before = stats.events_by_type.get("BeforeMCPExecution", 0)
    mcp_after = stats.events_by_type.get("AfterMCPExecution", 0)
    file_edits = stats.events_by_type.get("AfterFileEdit", 0)
    shell_before = stats.events_by_type.get("BeforeShellExecution", 0)

    tool_to_prompt_ratio = _safe_ratio(pre_tool, prompts)
    failure_rate = _safe_ratio(failures, pre_tool) if pre_tool > 0 else 0.0

    if tool_to_prompt_ratio >= 10.0:
        strengths.append(
            f"**Exceptional leverage per prompt** — Each prompt drives {tool_to_prompt_ratio:.1f} tool actions, "
            "showing extremely well-scoped, actionable requests with minimal back-and-forth."
        )
    elif tool_to_prompt_ratio >= 5.0:
        strengths.append(
            "**High leverage per prompt** — Each prompt triggers many tool actions "
            f"(ratio: {tool_to_prompt_ratio:.1f}:1), meaning prompts are well-scoped "
            "and actionable rather than vague."
        )
    elif tool_to_prompt_ratio >= 3.0:
        strengths.append(
            "**Good prompt-to-action ratio** — Prompts consistently drive meaningful "
            f"execution ({tool_to_prompt_ratio:.1f} tool calls per prompt), showing "
            "effective task delegation."
        )

    if failure_rate < 0.05 and pre_tool > 10:
        strengths.append(
            f"**Low tool failure rate** ({failures} failures out of {pre_tool} tool calls = "
            f"{failure_rate:.1%}) — Indicates well-formed requests with clear context."
        )

    if subagent_starts > 0:
        sub_count = len(stats.subagent_types)
        strengths.append(
            f"**Effective subagent delegation** — Used {subagent_starts} subagent launches "
            f"across {sub_count} agent type(s), keeping the main conversation focused "
            "and enabling parallel investigation."
        )

    if mcp_before > 0 and abs(mcp_before - mcp_after) <= 2:
        strengths.append(
            f"**Clean MCP integration** — All {mcp_before} MCP executions completed "
            "successfully (before/after counts match), showing proper schema usage."
        )

    if file_edits > 0 and prompts > 0:
        edits_per_prompt = _safe_ratio(file_edits, prompts)
        if edits_per_prompt >= 0.5:
            strengths.append(
                f"**Highly productive editing sessions** — {edits_per_prompt:.1f} file edits "
                "per prompt. Prompts translate directly into code changes, showing clear intent."
            )
        elif edits_per_prompt >= 0.2:
            strengths.append(
                "**Productive editing sessions** — Prompts lead to actual code changes, "
                "not just exploration. This shows clear intent in requests."
            )

    if before_read > 0 and before_read < prompts * 3:
        strengths.append(
            "**Efficient context gathering** — File reads are proportionate to prompt "
            "volume, avoiding unnecessary exploration overhead."
        )

    if shell_before > 0:
        strengths.append(
            f"**Active shell usage** — {shell_before} shell executions for validation, "
            "builds, and scripting alongside AI assistance."
        )

    if len(stats.sessions_seen) > 5:
        strengths.append(
            f"**Sustained usage** — {len(stats.sessions_seen)} unique sessions over {stats.days_active} "
            "active days, showing consistent and productive AI collaboration."
        )
    elif stats.session_files > 1:
        strengths.append(
            f"**Multiple focused sessions** — {stats.session_files} sessions suggest "
            "breaking work into discrete, manageable chunks rather than one monolithic chat."
        )

    if not strengths:
        strengths.append(
            "**Active usage** — Generating telemetry data across multiple sessions is a "
            "good foundation for continuous improvement."
        )

    return strengths


def build_observations(stats: TelemetryStats) -> list[str]:
    observations: list[str] = []
    economy = compute_token_economy(stats)

    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    post_tool = stats.events_by_type.get("PostToolUse", 0)
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    failures = stats.events_by_type.get("PostToolUseFailure", 0)
    before_read = stats.events_by_type.get("BeforeReadFile", 0)
    subagent_starts = stats.events_by_type.get("SubagentStart", 0)

    tool_to_prompt_ratio = _safe_ratio(pre_tool, prompts)

    if tool_to_prompt_ratio >= 3.0:
        observations.append(
            "Tool activity is significantly higher than prompt volume, indicating execution-heavy usage. "
            "Ensure tasks have clear done-criteria so the AI knows when to stop."
        )
    elif prompts >= 10 and tool_to_prompt_ratio <= 1.5:
        observations.append(
            f"Prompt volume is high relative to execution ({tool_to_prompt_ratio:.1f} tool calls per prompt). "
            "That usually signals clarification churn, vague asks, or too much back-and-forth before action."
        )
    else:
        observations.append(
            "Prompt and tool activity are relatively balanced, indicating mixed planning and execution usage."
        )

    if before_read >= max(8, prompts * 4):
        observations.append(
            f"Heavy context gathering — {before_read} file reads across {prompts} prompts "
            f"({_safe_ratio(before_read, prompts):.1f} reads/prompt). Early context pinning could reduce overhead."
        )
    else:
        observations.append(
            "Context gathering looks controlled relative to prompt volume."
        )

    if failures > 0:
        fail_rate = _safe_ratio(failures, pre_tool)
        observations.append(
            f"{failures} tool failures detected ({fail_rate:.1%} of tool calls). "
            "Path and schema validation up front can reduce iteration cost."
        )
    else:
        observations.append("No tool failure events were observed in the analyzed span files.")

    if subagent_starts > 0:
        observations.append(
            f"Subagents are used for deeper workflows ({subagent_starts} launches). "
            "Provide explicit deliverable formats in subagent prompts for cleaner outputs."
        )

    if economy["top_session_share"] >= 25:
        observations.append(
            f"One session accounts for {economy['top_session_share']:.1f}% of all observed tokens. "
            "That usually means context accumulation is dominating cost rather than isolated prompts."
        )

    if economy["high_context_sessions"] > 0:
        observations.append(
            f"{economy['high_context_sessions']} session(s) look context-heavy (many prompts, many events, or large token volume). "
            "Long-lived sessions are where token accumulation quietly compounds."
        )

    if economy["reads_per_prompt"] >= 3.0 or economy["mcp_per_prompt"] >= 0.5:
        observations.append(
            f"Context hygiene pressure is elevated — {economy['reads_per_prompt']:.1f} file reads/prompt "
            f"and {economy['mcp_per_prompt']:.1f} MCP calls/prompt. Tool and MCP metadata can bloat context even before real work starts."
        )

    if prompts > 0 and economy["cache_reuse_ratio"] < 0.05 and stats.total_input_tokens > 1_000_000:
        observations.append(
            f"Prompt caching leverage is currently weak ({economy['cache_hit_pct']:.1f}% effective hit rate). "
            "Frequent context resets, long pauses, or highly variable prefixes may be forcing expensive re-sends."
        )
    elif prompts > 0 and economy["cache_reuse_ratio"] >= 0.15:
        if economy["cache_reuse_ratio"] > 1.0:
            observations.append(
                f"Prompt cache reuse is materially helping ({economy['cache_reuse_ratio']:.1f}x cached read volume vs fresh input). "
                "Stable prefixes and repeated working patterns are reducing resend cost."
            )
        else:
            observations.append(
                f"Prompt cache reuse is materially helping ({economy['cache_hit_pct']:.1f}% effective hit rate). "
                "Stable prefixes and repeated working patterns are reducing resend cost."
            )

    if economy["avg_input_per_prompt"] >= 20_000:
        observations.append(
            f"Average prompt cost is very high ({economy['avg_input_per_prompt']:.0f} input tokens per prompt). "
            "Large pasted context, repeated session history, or oversized MCP/tool descriptions are likely driving spend."
        )

    output_to_input_ratio = _safe_ratio(stats.total_output_tokens, stats.total_input_tokens)
    if stats.total_output_tokens > 0 and output_to_input_ratio >= 1.2:
        observations.append(
            f"Output-heavy sessions are amplifying cost ({output_to_input_ratio:.2f} output/input ratio). "
            "Long reasoning traces and verbose drafts are expensive because output tokens cost more."
        )

    if economy["heavy_model_share"] >= 60:
        observations.append(
            f"Premium model concentration is high ({economy['heavy_model_share']:.1f}% of model events). "
            "That is often correct for planning or deep analysis, but expensive for repetitive implementation loops."
        )
    elif economy["heavy_model_share"] <= 20 and stats.total_events > 200:
        observations.append(
            f"Heavy-model usage is restrained ({economy['heavy_model_share']:.1f}% of model events), "
            "which usually helps contain cost on routine implementation work."
        )

    if len(stats.agents) >= 3:
        observations.append(
            f"Work is spread across {len(stats.agents)} agents. Cross-agent workflows can be powerful, "
            "but they also make model/tool discipline and context hygiene more important."
        )

    if stats.events_by_type.get("BeforeShellExecution", 0) >= max(10, pre_tool * 0.2):
        observations.append(
            "Validation activity is strong relative to execution. That usually means the workflow is grounded in real shell checks, "
            "not just generated edits."
        )

    if pre_tool > 0 and post_tool > 0:
        gap = pre_tool - post_tool
        if gap > 5:
            observations.append(
                f"{gap} tool calls have no matching PostToolUse — some may be interrupted. "
                "Consider explicit validation/checkpoint steps."
            )

    return observations


def build_achievement_badges(stats: TelemetryStats) -> list[dict[str, str]]:
    economy = compute_token_economy(stats)
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    failures = stats.events_by_type.get("PostToolUseFailure", 0)
    completion_rate = 100 * _safe_ratio(
        sum(1 for done in stats.session_goal_completed.values() if done),
        len(stats.sessions_seen),
    )

    badges: list[dict[str, str]] = []

    if stats.days_active >= 5:
        badges.append({"icon": "&#128293;", "name": "On a Roll", "sub": f"{stats.days_active} active days"})
    if len(stats.sessions_seen) >= 10:
        badges.append({"icon": "&#9889;", "name": "Power User", "sub": f"{len(stats.sessions_seen)} sessions"})
    if _safe_ratio(pre_tool, prompts) >= 10:
        badges.append({"icon": "&#128640;", "name": "High Leverage", "sub": f"{_safe_ratio(pre_tool, prompts):.1f}:1 tool ratio"})
    if stats.events_by_type.get("SubagentStart", 0) >= 10:
        badges.append({"icon": "&#129302;", "name": "Delegator", "sub": f"{stats.events_by_type.get('SubagentStart', 0)} subagents"})
    if stats.events_by_type.get("BeforeMCPExecution", 0) >= 100:
        badges.append({"icon": "&#128268;", "name": "MCP Heavy", "sub": f"{stats.events_by_type.get('BeforeMCPExecution', 0):,} MCP calls"})
    if failures == 0 and pre_tool > 10:
        badges.append({"icon": "&#9989;", "name": "Zero Failures", "sub": "clean tool execution"})
    if stats.events_by_type.get("BeforeShellExecution", 0) >= 50:
        badges.append({"icon": "&#128187;", "name": "Shell Ninja", "sub": f"{stats.events_by_type.get('BeforeShellExecution', 0):,} shell runs"})
    if len(stats.subagent_types) >= 5:
        badges.append({"icon": "&#128450;", "name": "Multi-Agent", "sub": f"{len(stats.subagent_types)} agent types"})
    if economy["cache_reuse_ratio"] >= 0.15:
        if economy["cache_reuse_ratio"] > 1.0:
            badges.append({"icon": "&#129534;", "name": "Cache Saver", "sub": f"{economy['cache_reuse_ratio']:.1f}x cached reuse"})
        else:
            badges.append({"icon": "&#129534;", "name": "Cache Saver", "sub": f"{economy['cache_hit_pct']:.0f}% hit rate"})
    if economy["top_session_share"] <= 15 and len(stats.sessions_seen) >= 5:
        badges.append({"icon": "&#127919;", "name": "Context Tamer", "sub": "token spend well distributed"})
    if 5 <= economy["heavy_model_share"] <= 40 and stats.total_events >= 100:
        badges.append({"icon": "&#9878;", "name": "Model Mixer", "sub": "premium usage looks selective"})
    if completion_rate >= 70 and len(stats.sessions_seen) >= 3:
        badges.append({"icon": "&#127942;", "name": "Closer", "sub": f"{completion_rate:.0f}% completion"})
    if sum(stats.session_recovered_failures.values()) >= 3:
        badges.append({"icon": "&#128295;", "name": "Recovery Loop", "sub": f"{sum(stats.session_recovered_failures.values())} recovered failures"})

    if not badges:
        badges.append({"icon": "&#127793;", "name": "Getting Started", "sub": "keep going"})

    return badges


def build_practical_examples(stats: TelemetryStats) -> list[tuple[str, str, str]]:
    examples: list[tuple[str, str, str]] = []
    economy = compute_token_economy(stats)

    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    failures = stats.events_by_type.get("PostToolUseFailure", 0)
    before_read = stats.events_by_type.get("BeforeReadFile", 0)
    mcp_before = stats.events_by_type.get("BeforeMCPExecution", 0)
    subagent_starts = stats.events_by_type.get("SubagentStart", 0)

    tool_to_prompt_ratio = _safe_ratio(pre_tool, prompts)

    if before_read >= max(8, prompts * 4):
        examples.append((
            "Reduce file exploration by pinning context upfront",
            "Fix the bug in the payment service",
            (
                "@src/services/payment.py @src/models/transaction.py "
                "Fix the timeout bug in process_payment — the DB query on line 84 "
                "is missing a connection pool timeout"
            ),
        ))

    if failures > 0:
        examples.append((
            "Prevent tool failures with explicit paths and schemas",
            "Run the GitLab MCP to check the pipeline",
            (
                "Use the GitLab MCP to check pipeline status for project "
                "isr/cloud-low-level-infra/via-network-infrastructure, "
                "pipeline ID 12345. Read the MCP schema first."
            ),
        ))

    if tool_to_prompt_ratio >= 5.0:
        examples.append((
            "Add structure to high-leverage prompts for even better results",
            "Review this MR and check everything",
            (
                "Goal: Review MR !532 for security and correctness\n"
                "Context: @.cursor/rules/platform/network-standards-agent.mdc\n"
                "Constraints: Focus on IAM trust policies and VPC endpoint config\n"
                "Output: Structured review with severity ratings\n"
                "Done when: All findings documented in reviews/ folder"
            ),
        ))

    if economy["heavy_model_share"] >= 50:
        examples.append((
            "Use a heavy model for planning, then switch to a balanced model for implementation",
            "Use Opus to read the repo, design the fix, implement it, run tests, and write the PR summary.",
            (
                "Phase 1 (heavy model): inspect the repo and produce a concrete implementation plan.\n"
                "Done when: the plan lists touched files, edge cases, and validation steps.\n"
                "Then start a fresh session with a balanced model and execute only that plan."
            ),
        ))

    if economy["top_session_share"] >= 25 or economy["high_context_sessions"] > 0:
        examples.append((
            "Reset or compact context between milestones",
            "Keep working in the same session until the whole feature is done.",
            (
                "Finish one user story at a time.\n"
                "At the end of each milestone: summarize what changed, list remaining tasks, then start a fresh session for the next step."
            ),
        ))

    if mcp_before > 0:
        examples.append((
            "Scope MCP calls with specific parameters",
            "Check what's happening in Coralogix",
            (
                "Query Coralogix for ERROR-level logs from payment-service "
                "in the last 2 hours, filtering by region=us-east-1. "
                "Summarize the top 5 error patterns."
            ),
        ))

    if subagent_starts > 0:
        examples.append((
            "Guide subagents with clear deliverables",
            "Use the code reviewer to look at my changes",
            (
                "Use the code-reviewer subagent to review changes in "
                "src/services/auth.py — focus on input validation, "
                "error handling, and missing test coverage. "
                "Return findings as a table: location, severity, suggestion."
            ),
        ))

    examples.append((
        "End tasks with a clear done-state",
        "Deploy the fix",
        (
            "Deploy the hotfix for ISR-15646 to staging.\n"
            "Done when: staging pipeline is green, smoke test passes, "
            "and Coralogix shows no new errors for 5 minutes."
        ),
    ))

    return examples


def build_recommendations(stats: TelemetryStats) -> list[str]:
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    failures = stats.events_by_type.get("PostToolUseFailure", 0)
    stops = stats.events_by_type.get("Stop", 0)
    economy = compute_token_economy(stats)

    recommendations: list[str] = []
    recommendations.append(
        "Use a fixed prompt contract for non-trivial requests: Goal, Context, Constraints, Output, Done-when."
    )

    if _safe_ratio(pre_tool, prompts) >= 3.0:
        recommendations.append(
            "Pin relevant files/folders in the first prompt to reduce exploratory tool churn."
        )

    if failures > 0:
        recommendations.append(
            "For MCP and path-sensitive tasks, require schema/path check as step one before execution."
        )

    if prompts > 0 and stops >= prompts:
        recommendations.append(
            "Close each major task with a structured handoff: changes, validations, residual risk, and next command."
        )

    recommendations.append(
        "For medium/large tasks, request two phases explicitly: plan first, execute second."
    )

    if economy["heavy_model_share"] >= 50:
        recommendations.append(
            "Use heavy models for planning, architecture, and hard multi-repo analysis — then clear context and switch to a balanced model for implementation."
        )

    if economy["top_session_share"] >= 25 or economy["high_context_sessions"] > 0:
        recommendations.append(
            "Split large tasks into smaller user stories and start a fresh session after each completed milestone to control context accumulation."
        )

    if prompts > 0 and economy["cache_reuse_ratio"] < 0.05 and stats.total_input_tokens > 1_000_000:
        recommendations.append(
            "Compact or summarize after task completion instead of carrying a swollen context forward; this also improves prompt-cache reuse."
        )

    if economy["reads_per_prompt"] >= 3.0:
        recommendations.append(
            "Reduce file-read churn by pinning the exact files, functions, and examples in the first prompt."
        )

    if economy["mcp_per_prompt"] >= 0.5:
        recommendations.append(
            "Turn off unnecessary MCPs and prefer skills/scripts for deterministic tasks so tool descriptions and large responses do not bloat context."
        )

    if len(stats.subagent_types) > 0:
        recommendations.append(
            "When delegating to subagents, specify output format (table, markdown, JSON) to avoid manual reformatting."
        )

    return recommendations
