from __future__ import annotations

from datetime import datetime
from pathlib import Path

from reflect.graph import (
    _compute_tool_transitions,
    _compute_weekly_trends,
)
from reflect.insights import (
    build_observations,
    build_practical_examples,
    build_recommendations,
    build_strengths,
    compute_token_economy,
    compute_tool_percentiles,
)
from reflect.models import TelemetryStats
from reflect.utils import _safe_ratio

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render_examples_section(examples: list[tuple[str, str, str]]) -> list[str]:
    lines: list[str] = []
    for idx, (title, before, after) in enumerate(examples, 1):
        lines.append(f"### {idx}. {title}")
        lines.append("")
        lines.append("**Before (vague):**")
        lines.append(f"> {before}")
        lines.append("")
        lines.append("**After (actionable):**")
        for after_line in after.split("\n"):
            lines.append(f"> {after_line}")
        lines.append("")
    return lines


def render_report(
    stats: TelemetryStats,
    sessions_dir: Path,
    spans_dir: Path,
    output_path: Path,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d")
    strengths = build_strengths(stats)
    observations = build_observations(stats)
    practical_examples = build_practical_examples(stats)
    recommendations = build_recommendations(stats)
    economy = compute_token_economy(stats)

    top_events = stats.events_by_type.most_common()
    top_tools = stats.tools_by_count.most_common(10)
    top_models = stats.models_by_count.most_common()
    top_subagents = stats.subagent_types.most_common()

    # Compute derived metrics
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    failures = stats.events_by_type.get("PostToolUseFailure", 0)
    tool_ratio = _safe_ratio(pre_tool, prompts)
    fail_rate = _safe_ratio(failures, pre_tool) if pre_tool else 0.0

    # Build markdown sections
    date_range = (
        f"{stats.first_event_ts} — {stats.last_event_ts}"
        if stats.first_event_ts else "N/A"
    )

    model_lines: list[str] = []
    if top_models:
        total_model_spans = sum(stats.models_by_count.values())
        model_lines.append("| Model | Events | Share |")
        model_lines.append("|-------|--------|-------|")
        for model, count in top_models:
            pct = 100 * count / total_model_spans if total_model_spans else 0
            model_lines.append(f"| `{model}` | {count:,} | {pct:.1f}% |")
    else:
        model_lines.append("*No model attribution found in spans.*")

    tool_lines: list[str] = []
    if top_tools:
        tool_lines.append("| Tool | Calls |")
        tool_lines.append("|------|-------|")
        for tool, count in top_tools:
            tool_lines.append(f"| `{tool}` | {count:,} |")
    else:
        tool_lines.append("*No tool name data found.*")

    # Tool latency percentiles
    pctl_data = compute_tool_percentiles(stats.tool_durations_ms)
    pctl_lines: list[str] = []
    if pctl_data:
        pctl_lines.append("| Tool | Calls | p50 (ms) | p90 (ms) | p95 (ms) | p99 (ms) |")
        pctl_lines.append("|------|-------|----------|----------|----------|----------|")
        for row in pctl_data[:15]:
            pctl_lines.append(
                f"| `{row['tool']}` | {row['count']:,} "
                f"| {row['p50']:,.1f} | {row['p90']:,.1f} "
                f"| {row['p95']:,.1f} | {row['p99']:,.1f} |"
            )
    else:
        pctl_lines.append("*No duration data available.*")

    event_lines = [f"| `{name}` | {count:,} |" for name, count in top_events]
    subagent_lines = (
        [f"| `{t}` | {c} |" for t, c in top_subagents]
        if top_subagents else ["*No subagent usage detected.*"]
    )

    # Agent comparison
    agent_lines: list[str] = []
    if len(stats.agents) > 1:
        agent_names = sorted(stats.agents.keys())
        # Summary comparison table
        header = "| Metric | " + " | ".join(f"**{n}**" for n in agent_names) + " |"
        sep = "|--------|" + "|".join("-------:" for _ in agent_names) + "|"
        agent_lines.extend([header, sep])

        def _agent_val(name: str, key: str) -> str:
            ag = stats.agents[name]
            if key == "events":
                return f"{ag.total_events:,}"
            if key == "sessions":
                return str(len(ag.sessions_seen))
            if key == "avg_quality":
                avg = ag.total_quality_score / len(ag.sessions_seen) if ag.sessions_seen else 0
                return f"{avg:.1f}%"
            if key == "completed":
                done = ag.completed_sessions
                total = len(ag.sessions_seen)
                rate = 100 * done / total if total > 0 else 0
                return f"{done}/{total} ({rate:.0f}%)"
            if key == "recovered":
                return str(ag.recovered_failures)
            if key == "prompts":
                return f"{ag.events_by_type.get('UserPromptSubmit', 0):,}"
            if key == "tool_calls":
                return f"{ag.events_by_type.get('PreToolUse', 0):,}"
            if key == "tool_ratio":
                p = ag.events_by_type.get("UserPromptSubmit", 0)
                t = ag.events_by_type.get("PreToolUse", 0)
                return f"{_safe_ratio(t, p):.1f}:1"
            if key == "failures":
                f = ag.events_by_type.get("PostToolUseFailure", 0)
                t = ag.events_by_type.get("PreToolUse", 0)
                rate = _safe_ratio(f, t) if t else 0
                return f"{f} ({rate:.1%})"
            if key == "mcp":
                return f"{ag.events_by_type.get('BeforeMCPExecution', 0):,}"
            if key == "subagents":
                return str(ag.events_by_type.get("SubagentStart", 0))
            if key == "top_model":
                mc = ag.models_by_count.most_common(1)
                return f"`{mc[0][0]}`" if mc else "—"
            if key == "top_tool":
                tc = ag.tools_by_count.most_common(1)
                return f"`{tc[0][0]}`" if tc else "—"
            if key == "input_tokens":
                return f"{ag.total_input_tokens:,}"
            if key == "output_tokens":
                return f"{ag.total_output_tokens:,}"
            if key == "cache_read_tokens":
                return f"{ag.total_cache_read_tokens:,}"
            return "—"

        for label, key in [
            ("Avg Quality", "avg_quality"),
            ("Completed", "completed"),
            ("Recovered", "recovered"),
            ("Total events", "events"),
            ("Sessions", "sessions"),
            ("Prompts", "prompts"),
            ("Tool calls", "tool_calls"),
            ("Tool-to-prompt", "tool_ratio"),
            ("Failures", "failures"),
            ("MCP calls", "mcp"),
            ("Subagents", "subagents"),
            ("Input tokens", "input_tokens"),
            ("Output tokens", "output_tokens"),
            ("Cache read tokens", "cache_read_tokens"),
            ("Top model", "top_model"),
            ("Top tool", "top_tool"),
        ]:
            row = f"| {label} | " + " | ".join(_agent_val(n, key) for n in agent_names) + " |"
            agent_lines.append(row)

        # Per-agent latency percentiles (top 5 tools each)
        for ag_name in agent_names:
            ag = stats.agents[ag_name]
            ag_pctl = compute_tool_percentiles(ag.tool_durations_ms)
            if ag_pctl:
                agent_lines.append("")
                agent_lines.append(f"#### {ag_name} — Tool Latency (ms)")
                agent_lines.append("")
                agent_lines.append("| Tool | Calls | p50 | p90 | p95 | p99 |")
                agent_lines.append("|------|-------|-----|-----|-----|-----|")
                for row in ag_pctl[:8]:
                    agent_lines.append(
                        f"| `{row['tool']}` | {row['count']:,} "
                        f"| {row['p50']:,.1f} | {row['p90']:,.1f} "
                        f"| {row['p95']:,.1f} | {row['p99']:,.1f} |"
                    )
    elif stats.agents:
        only = next(iter(stats.agents.values()))
        agent_lines.append(f"*Single agent detected: **{only.name}** ({only.total_events:,} events)*")
    else:
        agent_lines.append("*No agent identity data found.*")

    strength_lines = [f"- {line}" for line in strengths]
    observation_lines = [f"- {line}" for line in observations]
    recommendation_lines = [f"- {line}" for line in recommendations]
    example_lines = _render_examples_section(practical_examples)

    # Weekly trends
    weekly_trends = _compute_weekly_trends(stats.activity_by_day)
    weekly_lines: list[str] = []
    if weekly_trends:
        weekly_lines += ["| Week | Events | Δ | Δ% | Active Days |", "|------|--------|---|-----|-------------|"]
        for w in weekly_trends[-12:]:  # last 12 weeks
            delta_str = f"+{w['delta']:,}" if w["delta"] > 0 else (f"{w['delta']:,}" if w["delta"] < 0 else "—")
            pct_str = f"{w['delta_pct']:+.1f}%" if w["delta_pct"] is not None else "—"
            weekly_lines.append(f"| {w['week']} | {w['events']:,} | {delta_str} | {pct_str} | {w['days_active']} |")
    else:
        weekly_lines.append("*Not enough data for weekly trends.*")

    # MCP server availability
    mcp_avail_lines: list[str] = []
    all_mcp_servers = sorted(
        set(stats.mcp_server_before.keys()) | set(stats.mcp_servers.keys()),
        key=lambda s: stats.mcp_server_before.get(s, stats.mcp_servers.get(s, 0)),
        reverse=True,
    )
    if all_mcp_servers:
        mcp_avail_lines += ["| Server | Calls | Completions | Completion rate |", "|--------|-------|-------------|-----------------|"]
        for srv in all_mcp_servers[:15]:
            before = stats.mcp_server_before.get(srv, 0)
            after = stats.mcp_server_after.get(srv, 0)
            avail = f"{100 * after / before:.1f}%" if before > 0 else "—"
            mcp_avail_lines.append(f"| `{srv}` | {before} | {after} | {avail} |")
    else:
        mcp_avail_lines.append("*No MCP server usage detected.*")

    # Subagent effectiveness
    subagent_eff_lines: list[str] = []
    total_starts = stats.events_by_type.get("SubagentStart", 0)
    total_stops = stats.events_by_type.get("SubagentStop", 0)
    if top_subagents:
        subagent_eff_lines += [
            f"Overall completion rate: **{total_stops}/{total_starts}** "
            f"({100 * _safe_ratio(total_stops, total_starts):.1f}%)" if total_starts else
            "*No subagent usage detected.*",
            "",
            "| Type | Launches | Completions | Rate |",
            "|------|----------|-------------|------|",
        ]
        for subagent_type, launches in top_subagents:
            stops = stats.subagent_stops_by_type.get(subagent_type, 0)
            effective = min(stops, launches)
            rate = f"{100 * effective / launches:.1f}%" if effective > 0 else "—"
            subagent_eff_lines.append(f"| `{subagent_type}` | {launches} | {effective} | {rate} |")
    else:
        subagent_eff_lines.append("*No subagent usage detected.*")

    # Tool transitions
    transitions = _compute_tool_transitions(stats.session_tool_seq)
    transition_lines: list[str] = []
    if transitions:
        transition_lines += ["| From | → To | Count |", "|------|------|-------|"]
        for t in transitions[:20]:
            transition_lines.append(f"| `{t['from']}` | `{t['to']}` | {t['count']} |")
    else:
        transition_lines.append("*No tool sequence data available.*")

    markdown = "\n".join([
        "# AI Usage Telemetry Report",
        "",
        f"Date: {now}",
        f"Scope: `{sessions_dir}` and `{spans_dir}`",
        "Analyst: ai-usage-telemetry-reporter",
        "",
        "## Executive Summary",
        "",
        "This report analyzes AI session/span telemetry usage patterns, "
        "highlights what's working well, identifies areas for improvement, "
        "and provides practical examples to get even more out of AI assistance.",
        "",
        "## Data Snapshot",
        "",
        f"- Session metadata files: {stats.session_files}",
        f"- Unique sessions (from spans): {len(stats.sessions_seen)}",
        f"- Local span files: {stats.span_files}",
        f"- Total span events: {stats.total_events:,}",
        f"- Date range: {date_range}",
        f"- Active days: {stats.days_active}",
        "",
        "### Key Metrics",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Prompt submits | {prompts:,} |",
        f"| Tool calls (PreToolUse) | {pre_tool:,} |",
        f"| Tool-to-prompt ratio | {tool_ratio:.1f}:1 |",
        f"| Tool failures | {failures} ({fail_rate:.1%}) |",
        f"| MCP calls | {stats.events_by_type.get('BeforeMCPExecution', 0):,} |",
        f"| Subagent launches | {stats.events_by_type.get('SubagentStart', 0)} |",
        f"| File reads | {stats.events_by_type.get('BeforeReadFile', 0):,} |",
        f"| File edits | {stats.events_by_type.get('AfterFileEdit', 0):,} |",
        f"| Shell executions | {stats.events_by_type.get('BeforeShellExecution', 0):,} |",
        "",
        "### Token Usage",
        "",
        "| Metric | Tokens |",
        "|--------|--------|",
        f"| Input tokens | {stats.total_input_tokens:,} |",
        f"| Output tokens | {stats.total_output_tokens:,} |",
        f"| Cache creation tokens | {stats.total_cache_creation_tokens:,} |",
        f"| Cache read tokens | {stats.total_cache_read_tokens:,} |",
        f"| **Total tokens** | **{stats.total_input_tokens + stats.total_output_tokens:,}** |",
        f"| Output / Input ratio | {_safe_ratio(stats.total_output_tokens, stats.total_input_tokens):.2f} |",
        f"| Cache read hit rate | {100 * _safe_ratio(stats.total_cache_read_tokens, stats.total_input_tokens):.1f}% |",
        "",
        "### Token Economy Signals",
        "",
        "| Signal | Value | Why it matters |",
        "|--------|-------|----------------|",
        f"| Avg input / prompt | {economy['avg_input_per_prompt']:,.0f} | Re-sent context compounds input cost every turn. |",
        f"| Avg output / prompt | {economy['avg_output_per_prompt']:,.0f} | Output and extended thinking are usually the expensive side of generation. |",
        f"| Largest session share | {economy['top_session_share']:.1f}% | A single bloated session often dominates cost. |",
        f"| Context-heavy sessions | {economy['high_context_sessions']} | Long-lived sessions benefit from compaction or reset. |",
        f"| File reads / prompt | {economy['reads_per_prompt']:.1f} | Exploratory reads can quietly bloat context. |",
        f"| MCP calls / prompt | {economy['mcp_per_prompt']:.1f} | Tool metadata and large responses can add hidden context cost. |",
        f"| Heavy-model event share | {economy['heavy_model_share']:.1f}% | Heavy models are best saved for planning and hard analysis. |",
        "",
        "## Agent Comparison",
        "",
        *agent_lines,
        "",
        "## Model Usage Breakdown",
        "",
        *model_lines,
        "",
        "## Top Tools",
        "",
        *tool_lines,
        "",
        "## Tool Latency Percentiles",
        "",
        *pctl_lines,
        "",
        "## Week-over-Week Trends",
        "",
        *weekly_lines,
        "",
        "## Observed MCP Completion",
        "",
        *mcp_avail_lines,
        "",
        "## Subagent Effectiveness",
        "",
        *subagent_eff_lines,
        "",
        "## Tool Transition Patterns",
        "",
        "Top tool call sequences across sessions:",
        "",
        *transition_lines,
        "",
        "## Event Distribution",
        "",
        "| Event type | Count |",
        "|------------|-------|",
        *event_lines,
        "",
        "## Subagent Types Used",
        "",
        *(["| Type | Launches |", "|------|----------|"] + subagent_lines
          if top_subagents else subagent_lines),
        "",
        "## Event Volume by File",
        "",
        *[f"- `{fname}`: {count:,}" for fname, count in sorted(stats.events_by_file.items())],
        "",
        "## What's Working Well",
        "",
        *strength_lines,
        "",
        "## Areas for Improvement",
        "",
        *observation_lines,
        "",
        "## Practical Examples",
        "",
        "Concrete before/after prompt improvements based on your usage patterns:",
        "",
        *example_lines,
        "## Recommendations (Prioritized)",
        "",
        *recommendation_lines,
        "",
        "## Suggested Prompt Template",
        "",
        "```text",
        "Goal: <what to achieve>",
        "Context: @<files/folders> <links/tickets>",
        "Constraints: <must/must-not>",
        "Output: <exact sections and format>",
        "Done when: <objective success checks>",
        "```",
        "",
        "## Next Step",
        "",
        "Re-run this report weekly and compare event ratios (tool calls, failures, and prompt turns) "
        "to track improvement. Use `reflect report` to open the hosted dashboard view for the same dataset.",
        "",
    ])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return markdown
