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
    measurement: dict[str, object] | None = None,
    cost_scope: str = "session_lifetime",
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
    pricing_coverage = None
    if measurement is not None:
        cost = float(measurement.get("known_cost_usd") or 0)
        pricing_coverage = {key: measurement.get(key) for key in (
            "cost_status", "observed_usage", "priced_usage", "complete_priced_usage",
            "partially_priced_usage", "legacy_cost_usage",
        )}
        cost_status = str(measurement["cost_status"])
        cost_unavailable_reason = {
            "complete": "",
            "partial": "Known cost subtotal; some usage or applicable prices are unavailable.",
            "unavailable": "The captured usage and applicable prices do not establish a cost.",
        }[cost_status]
    elif cost > 0:
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
        "pricing_coverage": pricing_coverage,
        "cost_scope": cost_scope,
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
    # Session cards are navigation/presentation rows and can be paginated.
    # Only canonical scoped aggregates may support facts here; R5 owns advice.
    session_count = int(overview.get("session_count") or 0)
    mcp_calls = int(metrics.get("mcp_calls") or 0)
    tool_calls = int(overview.get("tool_call_count") or 0)
    failures = int(overview.get("failure_count") or 0)
    subagents = int(metrics.get("subagent_total_starts") or 0)
    observed_input_tokens = input_tokens + cache_creation_tokens + cache_read_tokens
    economy = {
        "total_tokens": total_tokens,
        "avg_input_per_prompt": None,
        "avg_output_per_prompt": None,
        "top_session_share": None,
        "high_context_sessions": None,
        "reads_per_prompt": None,
        "mcp_per_prompt": None,
        "cache_reuse_ratio": None,
        "cache_read_share_pct": (
            100 * cache_read_tokens / observed_input_tokens if observed_input_tokens else None
        ),
        "heavy_model_share": None,
    }
    strengths = [f"**Report data** - {session_count:,} sessions in the filtered report scope."]
    observations = []
    if tool_calls:
        observations.append(f"**Tool activity** - {tool_calls:,} captured tool calls; {failures:,} recorded failures. Missing results remain unknown.")
    if cache_read_tokens:
        observations.append(f"**Cache tokens** - {cache_read_tokens:,} recorded cache-read tokens. Token volume alone does not establish savings or a complete cache-hit rate.")
    achievements = [{"icon": "&#128190;", "name": "Local Report", "sub": f"{session_count:,} captured sessions"}]
    if total_tokens:
        achievements.append({"icon": "&#129534;", "name": "Recorded Tokens", "sub": f"{total_tokens:,} tokens"})
    if cache_read_tokens:
        achievements.append({"icon": "&#129534;", "name": "Cache Tokens", "sub": f"{cache_read_tokens:,} recorded cache-read tokens"})
    if metrics.get("unique_models"):
        achievements.append({"icon": "&#9878;", "name": "Recorded Models", "sub": f"{metrics['unique_models']:,} models"})
    if metrics.get("unique_commands"):
        achievements.append({"icon": "&#128187;", "name": "Command Patterns", "sub": f"{metrics['unique_commands']:,} patterns"})
    if tool_calls:
        achievements.append({"icon": "&#128295;", "name": "Recorded Failures", "sub": f"{failures:,} failures; result coverage may be incomplete"})
    if overview.get("estimated_cost_usd"):
        achievements.append({"icon": "&#128176;", "name": "Recorded Cost", "sub": f"${float(overview['estimated_cost_usd']):.2f} recorded estimate"})
    if subagents:
        achievements.append({"icon": "&#129302;", "name": "Subagent Activity", "sub": f"{subagents:,} starts"})
    if mcp_calls:
        achievements.append({"icon": "&#128268;", "name": "MCP Activity", "sub": f"{mcp_calls:,} calls"})
    recommendations: list[str] = []
    practical_examples: list[tuple[str, str, str]] = []
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
