from __future__ import annotations

import json
from pathlib import Path

from reflect.session_rules import DEFAULT_SESSION_RULE_SCORER, context_from_summary

MIN_QUALITY_COVERAGE_PCT = 50.0


def quality_rules_payload() -> list[dict[str, object]]:
    """Dashboard copy for the session quality scoring rubric."""
    return DEFAULT_SESSION_RULE_SCORER.rules_payload()


def session_card_from_row(
    row: dict[str, object],
    *,
    first_prompt: str = "",
    primary_model: str = "",
    tools: dict[str, int] | None = None,
) -> dict[str, object]:
    """Shape one canonical session summary for dashboard navigation."""
    session_id = str(row.get("session_id") or row.get("id") or "")
    status = str(row.get("status") or "")
    quality_context = context_from_summary(row)
    quality_breakdown = DEFAULT_SESSION_RULE_SCORER.breakdown(quality_context)
    quality_score = DEFAULT_SESSION_RULE_SCORER.score_from_breakdown(quality_breakdown)
    quality_coverage = DEFAULT_SESSION_RULE_SCORER.coverage_from_breakdown(quality_breakdown)
    quality_available = quality_coverage >= MIN_QUALITY_COVERAGE_PCT
    quality_missing_reason = (
        ""
        if quality_coverage >= 100
        else (
            f"Score normalized across {quality_coverage:.0f}% evidence coverage; "
            "unavailable dimensions were excluded."
            if quality_available
            else (
                f"Only {quality_coverage:.0f}% of quality evidence was available; "
                f"at least {MIN_QUALITY_COVERAGE_PCT:.0f}% is required to display a score."
            )
        )
    )
    input_tokens = int(row.get("input_tokens") or 0)
    output_tokens = int(row.get("output_tokens") or 0)
    cache_creation_tokens = int(row.get("cache_creation_tokens") or 0)
    cache_read_tokens = int(row.get("cache_read_tokens") or 0)
    failure_count = int(row.get("failure_count") or row.get("failures") or 0)
    model = primary_model or str(row.get("primary_model") or "")
    cost = float(row.get("estimated_cost_usd") or row.get("total_cost_usd") or 0)
    total_tokens = input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens
    token_provenance = quality_context.token_provenance
    if cost > 0:
        cost_status = "estimated"
        cost_unavailable_reason = ""
    elif token_provenance == "unavailable":
        cost_status = "tokens_unavailable"
        cost_unavailable_reason = "Token usage was not captured for this session."
    elif total_tokens <= 0:
        cost_status = "zero_usage"
        cost_unavailable_reason = "Token telemetry was captured and reported no usage."
    elif not model:
        cost_status = "model_unavailable"
        cost_unavailable_reason = "Tokens were captured, but no model was available to resolve pricing."
    else:
        cost_status = "pricing_unavailable"
        cost_unavailable_reason = f'Model "{model}" did not resolve to a local price.'
    return {
        "id": session_id,
        "full_id": session_id,
        "agent": row.get("agent") or "unknown",
        "status": status,
        "title": row.get("title"),
        "first_prompt": first_prompt or row.get("first_prompt") or row.get("title") or "",
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "created_at": row.get("started_at"),
        "duration_ms": int(row.get("duration_ms") or 0),
        "event_count": int(row.get("event_count") or 0),
        "prompt_count": int(row.get("prompt_count") or 0),
        "tool_calls": int(row.get("tool_call_count") or row.get("tool_calls") or 0),
        "failures": failure_count,
        "failure_count": failure_count,
        "quality_score": quality_score,
        "quality_available": quality_available,
        "quality_coverage_pct": quality_coverage,
        "quality_missing_reason": quality_missing_reason,
        "quality_breakdown": quality_breakdown,
        "is_completed": status in {"ok", "completed", "success"},
        "recovered_failures": 0,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "token_provenance": token_provenance,
        "total_tokens": total_tokens,
        "total_cost": cost,
        "total_cost_usd": cost,
        "cost_status": cost_status,
        "cost_unavailable_reason": cost_unavailable_reason,
        "pricing_unit": "usd",
        "primary_model": model,
        "models": {model: 1} if model else {},
        "tools": tools or {},
        "skills": {},
        "conversation": [],
        "telemetry": [],
    }


def sql_session_first_prompts(db_path: Path, session_ids: set[str]) -> dict[str, str]:
    if not session_ids:
        return {}
    from reflect.store.sqlite import connect_sqlite_read_only

    ids = sorted(session_ids)
    placeholders = ", ".join("?" for _ in ids)
    conn = connect_sqlite_read_only(db_path)
    try:
        rows = dict_rows(
            conn.execute(
                f"""
            SELECT session_id, raw_attrs_json
            FROM steps
            WHERE session_id IN ({placeholders})
              AND raw_attrs_json LIKE '%gen_ai.client.prompt%'
            ORDER BY session_id, seq
            """,
                ids,
            )
        )
    finally:
        conn.close()
    prompts: dict[str, str] = {}
    for row in rows:
        session_id = str(row["session_id"])
        if session_id in prompts:
            continue
        attrs = load_json_dict(row["raw_attrs_json"])
        prompt = str(
            sql_attr(
                attrs,
                "gen_ai.client.prompt",
                "gen_ai.client.prompt.text",
                "prompt",
                "input",
            )
            or ""
        ).strip()
        if prompt:
            prompts[session_id] = prompt
    return prompts


def dict_rows(cursor) -> list[dict[str, object]]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def load_json_dict(value: object) -> dict[str, object]:
    if not value:
        return {}
    try:
        payload = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def sql_attr(attrs: dict[str, object], *keys: str) -> object:
    for key in keys:
        value = attrs.get(key)
        if value not in (None, ""):
            return value
    return None


def sql_insight_payload(
    overview: dict[str, object],
    sessions: list[dict[str, object]],
    metrics: dict[str, object],
) -> dict[str, object]:
    input_tokens = int(overview["input_tokens"] or 0)
    output_tokens = int(overview["output_tokens"] or 0)
    cache_creation_tokens = int(metrics["total_cache_creation_tokens"] or 0)
    cache_read_tokens = int(metrics["total_cache_read_tokens"] or 0)
    total_tokens = input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens
    prompt_count = sum(int(session["prompt_count"] or 0) for session in sessions)
    session_tokens = [int(session["total_tokens"] or 0) for session in sessions]
    top_session_share = (max(session_tokens) / total_tokens * 100) if total_tokens else 0.0
    high_context_sessions = sum(1 for tokens in session_tokens if tokens >= 100_000)
    mcp_calls = int(metrics["mcp_calls"] or 0)
    tool_calls = int(overview["tool_call_count"] or 0)
    failures = int(overview["failure_count"] or 0)
    subagents = int(metrics["subagent_total_starts"] or 0)
    file_reads = int(metrics.get("file_reads") or 0)
    estimated_cost = float(overview["estimated_cost_usd"] or 0)
    tool_to_prompt_ratio = (tool_calls / prompt_count) if prompt_count else 0.0
    reads_per_prompt = (file_reads / prompt_count) if prompt_count else 0.0
    mcp_per_prompt = (mcp_calls / prompt_count) if prompt_count else 0.0
    cache_reuse_ratio = (cache_read_tokens / input_tokens) if input_tokens else 0.0
    economy = {
        "total_tokens": total_tokens,
        "avg_input_per_prompt": (input_tokens / prompt_count) if prompt_count else 0,
        "avg_output_per_prompt": (output_tokens / prompt_count) if prompt_count else 0,
        "top_session_share": top_session_share,
        "high_context_sessions": high_context_sessions,
        "reads_per_prompt": reads_per_prompt,
        "mcp_per_prompt": mcp_per_prompt,
        "cache_reuse_ratio": cache_reuse_ratio,
        "cache_hit_pct": 100 * min(cache_reuse_ratio, 1.0),
        "heavy_model_share": 0,
    }
    strengths = [
        f"**Report data** - Loaded {len(sessions):,} session rows from the local report store.",
    ]
    if tool_calls:
        strengths.append(
            f"**Execution telemetry** - Captured {tool_calls:,} tool calls across the filtered report scope."
        )
    observations = [
        f"**Token concentration** - The largest session accounts for {top_session_share:.1f}% of observed token volume.",
    ]
    if mcp_calls:
        observations.append(
            f"**MCP activity** - This scope contains {mcp_calls:,} MCP calls across {len(metrics['mcp_servers_by_count']):,} server(s)."
        )
    recommendations: list[str] = []
    if prompt_count == 0 and (tool_calls or total_tokens):
        recommendations.append(
            "**Enable prompt capture for richer session analysis** - This scope has execution metadata, but no prompt submits. Re-run `reflect setup` and choose metadata, masked, or full text capture based on your local privacy preference."
        )
    if failures:
        recommendations.append(
            f"**Require schema/path checks before execution** - {failures:,} failed tool call(s) were observed. Make path, MCP schema, and required env checks the first step before mutating state."
        )
    if tool_to_prompt_ratio >= 3 or reads_per_prompt >= 3:
        recommendations.append(
            f"**Pin relevant files in the first prompt** - This scope averages {tool_to_prompt_ratio:.1f} tools/prompt and {reads_per_prompt:.1f} reads/prompt. Naming exact files, functions, and examples up front should reduce exploration churn."
        )
    if mcp_per_prompt >= 0.5:
        recommendations.append(
            f"**Reduce MCP context bloat** - This scope averages {mcp_per_prompt:.1f} MCP calls/prompt. Keep only the MCP servers needed for the task and prefer deterministic scripts for repeatable lookups."
        )
    if top_session_share >= 25 or high_context_sessions:
        recommendations.append(
            f"**Split large tasks into smaller sessions** - The largest session accounts for {top_session_share:.1f}% of observed token volume. Start a fresh session after each milestone to keep context pressure down."
        )
    if input_tokens >= 1_000_000 and cache_reuse_ratio < 0.05:
        recommendations.append(
            "**Compact context to improve cache reuse** - Input volume is high but cache reuse is low. Summarize at completed milestones instead of carrying a swollen context forward."
        )
    if estimated_cost >= 25:
        recommendations.append(
            f"**Review high model spend** - Estimated cost is ${estimated_cost:.2f}. Reserve expensive models for planning or hard analysis and route routine implementation to lower-cost models."
        )
    if subagents:
        recommendations.append(
            "**Specify subagent output format** - Subagents are active in this scope. Ask for a table, JSON, or concise markdown handoff so delegated work returns in a reusable shape."
        )
    if not recommendations:
        recommendations.extend(
            [
                "**Use a fixed prompt contract for non-trivial requests** - Goal, Context, Constraints, Output, Done-when keeps observed sessions easier to compare and review.",
                "**Close tasks with a structured handoff** - End each major task with changes, validations, residual risk, and the next command so future reports can distinguish completed work from drift.",
            ]
        )
    practical_examples = [
        (
            "Make the next action measurable",
            "Fix the flaky report.",
            "Fix the report session filter. Done when `/api/data?agents=claude` returns non-empty sessions and the dashboard shows the same count.",
        )
    ]
    achievements = [
        {"icon": "&#128190;", "name": "Local Report", "sub": f"{len(sessions):,} sessions loaded"},
    ]
    if total_tokens:
        achievements.append(
            {"icon": "&#129534;", "name": "Token Ledger", "sub": f"{total_tokens:,} tokens"}
        )
    if cache_read_tokens:
        achievements.append(
            {
                "icon": "&#129534;",
                "name": "Cache Saver",
                "sub": f"{economy['cache_reuse_ratio']:.1f}x cached reuse",
            }
        )
    if metrics["unique_models"]:
        achievements.append(
            {
                "icon": "&#9878;",
                "name": "Model Mixer",
                "sub": f"{metrics['unique_models']:,} models",
            }
        )
    if metrics["unique_commands"]:
        achievements.append(
            {
                "icon": "&#128187;",
                "name": "Command Runner",
                "sub": f"{metrics['unique_commands']:,} patterns",
            }
        )
    if tool_calls and failures == 0:
        achievements.append(
            {"icon": "&#9989;", "name": "Zero Failures", "sub": "clean tool execution"}
        )
    elif tool_calls:
        achievements.append(
            {"icon": "&#128295;", "name": "Tool Operator", "sub": f"{tool_calls:,} tool calls"}
        )
    if overview["estimated_cost_usd"]:
        achievements.append(
            {
                "icon": "&#128176;",
                "name": "Cost Visibility",
                "sub": f"${float(overview['estimated_cost_usd']):.2f} estimated",
            }
        )
    if subagents:
        achievements.append(
            {"icon": "&#129302;", "name": "Delegator", "sub": f"{subagents:,} subagents"}
        )
    if mcp_calls:
        achievements.append(
            {"icon": "&#128268;", "name": "MCP Active", "sub": f"{mcp_calls:,} MCP calls"}
        )
    return {
        "token_economy": economy,
        "strengths": strengths,
        "observations": observations,
        "recommendations": recommendations,
        "practical_examples": practical_examples,
        "achievements": achievements,
    }


def empty_sql_lazy_tabs() -> dict[str, object]:
    return {
        "usage": {
            **dict.fromkeys(
                (
                    "avg_quality_score",
                    "unique_sessions",
                    "prompt_submits",
                    "tool_calls",
                    "failure_rate_pct",
                    "tool_failures",
                    "mcp_calls",
                    "subagent_launches",
                    "file_edits",
                    "shell_executions",
                    "unique_commands",
                    "signature_command_count",
                    "peak_hour_count",
                    "unique_models",
                    "total_input_tokens",
                    "total_output_tokens",
                    "total_cache_creation_tokens",
                    "total_cache_read_tokens",
                    "total_cost_usd",
                    "input_cost_usd",
                    "output_cost_usd",
                    "cache_creation_cost_usd",
                    "cache_read_cost_usd",
                ),
                0,
            ),
            "first_event_ts": "",
            "tool_to_prompt_ratio": "0.0",
            "signature_command": "",
            "peak_hour": -1,
            "pricing_source": "local",
            "mcp_servers_by_count": {},
            "pricing_unit": "usd",
            "subagent_types_by_count": {},
            "models_by_count": {},
            "events_by_type": {},
            "model_costs": {},
            "source_provenance": [],
            "agent_cost_over_time": [],
        },
        "activity": {
            "events_by_type": {},
            "activity_by_day": {},
            "activity_by_hour": {str(hour): 0 for hour in range(24)},
            "peak_hour": -1,
            "peak_hour_count": 0,
            "weekly_trends": [],
        },
        "models": {"models_by_count": {}, "unique_models": 0},
        "costs": {
            "model_costs": {},
            "model_costs_usd": {},
            "cost_breakdown": dict.fromkeys(
                (
                    "total_cost_usd",
                    "input_cost_usd",
                    "output_cost_usd",
                    "cache_creation_cost_usd",
                    "cache_read_cost_usd",
                ),
                0.0,
            ),
            "total_cache_creation_tokens": 0,
            "total_cache_read_tokens": 0,
            "agent_cost_over_time": [],
        },
        "tools": {
            "tools_by_count": {},
            "tool_percentiles": [],
            "skills_by_count": {},
            "subagent_types_by_count": {},
            "subagent_stops_by_type": {},
            "subagent_launches": 0,
            "subagent_total_starts": 0,
            "subagent_total_stops": 0,
            "top_commands": [],
            "unique_commands": 0,
            "signature_command": "",
            "signature_command_count": 0,
            "shell_executions": 0,
            "file_edits": 0,
            "file_reads": 0,
        },
        "mcp": {
            "mcp_calls": 0,
            "mcp_servers_by_count": {},
            "mcp_server_before": {},
            "mcp_server_after": {},
            "mcp_server_status_known": {},
        },
        "agents": {"agent_comparison": [], "agents": {}},
        "graph": {
            "graph_tool_transitions": [],
            "graph_cooccurrence": {"tools": [], "matrix": []},
            "graph_dep": {"nodes": [], "edges": [], "top_mcp_servers": []},
            "graph_session_timeline": [],
            "graph_semantic": {"nodes": [], "edges": [], "sessions": [], "legend": []},
            "graph_latency_histograms": {},
        },
        "specs": {
            "total_specs": 0,
            "specs_by_status": {},
            "requirements_by_status": {},
            "evidence_by_kind": {},
            "specs": [],
        },
        "memory": {
            "total_memories": 0,
            "memories_by_scope": {},
            "memories_by_type": {},
            "memories_by_sensitivity": {},
            "memories_by_source": {},
            "recent_memories": [],
        },
        "privacy": {
            "total_findings": 0,
            "findings_by_type": {},
            "findings_by_severity": {},
            "findings_by_action": {},
            "recent_findings": [],
        },
        "exports": {
            "row_counts": dict.fromkeys(
                (
                    "sessions",
                    "steps",
                    "llm_calls",
                    "tool_calls",
                    "mcp_calls",
                    "memories",
                    "privacy_findings",
                    "evidence",
                ),
                0,
            ),
            "scoped": True,
        },
    }
