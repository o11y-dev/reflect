from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from reflect.dashboard_query_common import (
    dict_rows,
    empty_sql_lazy_tabs,
    quality_rules_payload,
    session_card_from_row,
    sql_insight_payload,
    sql_session_first_prompts,
)
from reflect.graph import _compute_weekly_trends
from reflect.utils import _safe_ratio


def _comparison_delta(primary: float | int, baseline: float | int) -> dict:
    primary_value = float(primary or 0)
    baseline_value = float(baseline or 0)
    absolute = primary_value - baseline_value
    pct = round(100 * _safe_ratio(absolute, baseline_value), 1) if baseline_value else None
    return {
        "primary": primary_value,
        "baseline": baseline_value,
        "absolute": round(absolute, 1),
        "pct": pct,
    }


def _sql_report_payload(
    db_path: Path,
    *,
    limit: int = 50,
    offset: int = 0,
    include_tabs: bool = True,
) -> dict[str, object]:
    from reflect.store.sqlite import connect_sqlite_read_only
    from reflect.views.overview import build_overview
    from reflect.views.report_tabs import build_report_tabs
    from reflect.views.sessions import list_sessions

    conn = connect_sqlite_read_only(db_path)
    try:
        if include_tabs:
            overview = build_overview(conn).model_dump()
        else:
            totals = conn.execute(
                """
                SELECT
                  COUNT(*),
                  COUNT(DISTINCT NULLIF(agent, '')),
                  COALESCE(SUM(tool_call_count), 0),
                  COALESCE(SUM(input_tokens), 0),
                  COALESCE(SUM(output_tokens), 0),
                  COALESCE(SUM(total_cost), 0),
                  COALESCE(SUM(error_count), 0)
                FROM session_rollups
                """
            ).fetchone()
            overview = {
                "session_count": totals[0],
                "agent_count": totals[1],
                "model_count": 0,
                "tool_call_count": totals[2],
                "input_tokens": totals[3],
                "output_tokens": totals[4],
                "estimated_cost_usd": totals[5],
                "failure_count": totals[6],
                "recovered_failure_count": 0,
                "source_provenance": [],
                "agent_cost_over_time": [],
                "top_sessions": [],
                "top_models": [],
                "top_tools": [],
            }
        tabs = build_report_tabs(conn).model_dump() if include_tabs else empty_sql_lazy_tabs()
        tabs["usage"] = {}
        return {
            "db_path": str(db_path),
            "overview": overview,
            "sessions": list_sessions(conn, limit=limit, offset=offset).model_dump(),
            "tabs": tabs,
        }
    finally:
        conn.close()


def _filter_sql_session_rows(
    rows: list[dict[str, object]],
    *,
    q: str = "",
    session_id: str = "",
    agents: set[str] | None = None,
    model: str = "all",
    status: str = "all",
    range_name: str = "all",
) -> list[dict[str, object]]:
    search_text = q.lower().strip()
    agents = agents or set()
    range_days = {"24h": 1, "7d": 7, "30d": 30}.get(range_name)
    range_anchor = datetime.now(tz=UTC)
    if range_days is not None:
        parsed_dates: list[datetime] = []
        for row in rows:
            try:
                started_at = datetime.fromisoformat(
                    str(row.get("started_at") or row.get("created_at") or "")
                )
            except ValueError:
                continue
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            parsed_dates.append(started_at)
        if parsed_dates:
            range_anchor = max(parsed_dates)
    filtered: list[dict[str, object]] = []
    for row in rows:
        if session_id and str(row.get("id") or row.get("session_id") or "") != session_id:
            continue
        agent = str(row.get("agent") or "")
        if agents and agent not in agents:
            continue
        if model != "all" and str(row.get("primary_model") or "") != model:
            continue
        row_status = str(row.get("status") or "")
        failures = int(row.get("failure_count") or row.get("failures") or 0)
        if status == "completed" and row_status not in {"ok", "completed", "success"}:
            continue
        if status == "active" and row_status in {"ok", "completed", "success"}:
            continue
        if status == "failing" and failures <= 0:
            continue
        if status == "recovered":
            continue
        if range_days is not None:
            try:
                started_at = datetime.fromisoformat(
                    str(row.get("started_at") or row.get("created_at") or "")
                )
            except ValueError:
                continue
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            if (range_anchor - started_at).total_seconds() > range_days * 86400:
                continue
        if search_text:
            haystack = " ".join(
                str(row.get(key) or "")
                for key in ("session_id", "id", "title", "agent", "primary_model", "status")
            ).lower()
            if search_text not in haystack:
                continue
        filtered.append(row)
    return filtered


def _sql_session_primary_models(db_path: Path, session_ids: set[str]) -> dict[str, str]:
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
            SELECT
              session_id,
              COALESCE(NULLIF(response_model, ''), NULLIF(request_model, '')) AS model,
              COUNT(*) AS count
            FROM llm_calls
            WHERE session_id IN ({placeholders})
              AND COALESCE(NULLIF(response_model, ''), NULLIF(request_model, '')) IS NOT NULL
            GROUP BY session_id, model
            ORDER BY session_id, count DESC, model ASC
            """,
                ids,
            )
        )
    finally:
        conn.close()
    models: dict[str, str] = {}
    for row in rows:
        session_id = str(row["session_id"])
        if session_id not in models:
            models[session_id] = str(row["model"] or "")
    return models










def _sql_dashboard_metrics(
    db_path: Path,
    *,
    session_ids: set[str] | None = None,
    include_heavy: bool = True,
    include_base: bool = True,
    base_tab_names: set[str] | None = None,
) -> dict[str, object]:
    from reflect.store.sqlite import connect_sqlite_read_only
    from reflect.views.overview import list_source_provenance
    from reflect.views.report_tabs import build_report_tab, build_report_tabs

    scoped = session_ids is not None
    scoped_ids = sorted(session_ids or [])

    conn = connect_sqlite_read_only(db_path)
    try:
        selected_session_ids = set(scoped_ids) if scoped else None
        if not include_base:
            tab_views = empty_sql_lazy_tabs()
            source_provenance = []
        elif include_heavy:
            tab_views = build_report_tabs(conn, session_ids=selected_session_ids).model_dump()
            source_provenance = list_source_provenance(
                conn,
                session_ids=selected_session_ids,
            )
        else:
            tab_views = empty_sql_lazy_tabs()
            selected_base_tabs = (
                base_tab_names
                if base_tab_names is not None
                else {"activity", "models", "costs", "tools", "mcp", "agents"}
            )
            for tab_name in selected_base_tabs:
                tab_payload = build_report_tab(
                    conn,
                    tab_name,
                    session_ids=selected_session_ids,
                )
                if tab_name == "usage_tools":
                    tab_views["agents"] = {
                        "agent_comparison": tab_payload.pop("agent_comparison"),
                        "agents": tab_payload.pop("agents"),
                    }
                    tab_views["tools"].update(tab_payload)
                else:
                    tab_views[tab_name] = tab_payload
            source_provenance = (
                list_source_provenance(conn, session_ids=selected_session_ids)
                if "activity" in selected_base_tabs
                else []
            )
    finally:
        conn.close()

    activity_view = tab_views["activity"]
    models_view = tab_views["models"]
    costs_view = tab_views["costs"]
    tools_view = tab_views["tools"]
    mcp_view = tab_views["mcp"]
    agents_view = tab_views["agents"]
    graph_view = tab_views["graph"]
    specs_view = tab_views["specs"]
    memory_view = tab_views["memory"]
    privacy_view = tab_views["privacy"]
    exports_view = tab_views["exports"]
    return {
        "events_by_type": activity_view["events_by_type"],
        "activity_by_day": activity_view["activity_by_day"],
        "activity_by_hour": activity_view["activity_by_hour"],
        "peak_hour": activity_view["peak_hour"],
        "peak_hour_count": activity_view["peak_hour_count"],
        "models_by_count": models_view["models_by_count"],
        "unique_models": models_view["unique_models"],
        "model_costs": costs_view["model_costs"],
        "model_costs_usd": costs_view["model_costs_usd"],
        "cost_breakdown": costs_view["cost_breakdown"],
        "total_cache_creation_tokens": costs_view["total_cache_creation_tokens"],
        "total_cache_read_tokens": costs_view["total_cache_read_tokens"],
        "agent_cost_over_time": costs_view["agent_cost_over_time"],
        "tools_by_count": tools_view["tools_by_count"],
        "tool_percentiles": tools_view["tool_percentiles"],
        "agent_comparison": agents_view["agent_comparison"],
        "mcp_calls": mcp_view["mcp_calls"],
        "mcp_servers_by_count": mcp_view["mcp_servers_by_count"],
        "mcp_server_before": mcp_view["mcp_server_before"],
        "mcp_server_after": mcp_view["mcp_server_after"],
        "skills_by_count": tools_view["skills_by_count"],
        "subagent_types_by_count": tools_view["subagent_types_by_count"],
        "subagent_stops_by_type": tools_view["subagent_stops_by_type"],
        "subagent_launches": tools_view["subagent_launches"],
        "subagent_total_starts": tools_view["subagent_total_starts"],
        "subagent_total_stops": tools_view["subagent_total_stops"],
        "top_commands": tools_view["top_commands"],
        "unique_commands": tools_view["unique_commands"],
        "signature_command": tools_view["signature_command"],
        "signature_command_count": tools_view["signature_command_count"],
        "shell_executions": tools_view["shell_executions"],
        "file_edits": tools_view["file_edits"],
        "file_reads": tools_view["file_reads"],
        "graph_tool_transitions": graph_view["graph_tool_transitions"],
        "graph_cooccurrence": graph_view["graph_cooccurrence"],
        "graph_dep": graph_view["graph_dep"],
        "graph_session_timeline": graph_view["graph_session_timeline"],
        "graph_semantic": graph_view["graph_semantic"],
        "source_provenance": source_provenance,
        "agents": agents_view["agents"],
        "specs": specs_view,
        "memory": memory_view,
        "privacy": privacy_view,
        "exports": exports_view,
    }




def _sql_cohort_summary(
    sessions: list[dict[str, object]],
    metrics: dict[str, object],
    *,
    label: str,
    agent_names: list[str] | None = None,
) -> dict[str, object]:
    input_tokens = sum(int(session.get("input_tokens") or 0) for session in sessions)
    output_tokens = sum(int(session.get("output_tokens") or 0) for session in sessions)
    prompt_count = sum(int(session.get("prompt_count") or 0) for session in sessions)
    tool_calls = sum(
        int(session.get("tool_calls") or session.get("tool_call_count") or 0)
        for session in sessions
    )
    failures = sum(
        int(session.get("failure_count") or session.get("failures") or 0) for session in sessions
    )
    quality_values = [
        float(session.get("quality_score") or 0)
        for session in sessions
        if float(session.get("quality_score") or 0) > 0
    ]
    tools = metrics.get("tools_by_count") or {}
    commands = metrics.get("top_commands") or []
    return {
        "label": label,
        "agents": list(
            agent_names or sorted({str(session.get("agent") or "unknown") for session in sessions})
        ),
        "sessions": len(sessions),
        "prompts": prompt_count,
        "tool_calls": tool_calls,
        "avg_quality": (sum(quality_values) / len(quality_values)) if quality_values else 0.0,
        "failure_rate_pct": round(100 * failures / tool_calls, 1) if tool_calls else 0.0,
        "tokens": input_tokens + output_tokens,
        "shell_runs": int(metrics.get("shell_executions") or 0),
        "mcp_calls": int(metrics.get("mcp_calls") or 0),
        "subagent_launches": int(
            metrics.get("subagent_launches") or metrics.get("subagent_total_starts") or 0
        ),
        "top_tools": [
            {"tool": str(tool), "count": int(count)} for tool, count in list(tools.items())[:5]
        ],
        "top_commands": [
            {"command": str(entry.get("command") or ""), "count": int(entry.get("count") or 0)}
            for entry in commands[:5]
            if entry.get("command")
        ],
    }


def _sql_comparison_payload(
    db_path: Path,
    all_sessions: list[dict[str, object]],
    primary_sessions: list[dict[str, object]],
    *,
    agents: set[str] | None,
    q: str = "",
    model: str = "all",
    status: str = "all",
    range_name: str = "all",
) -> dict[str, object] | None:
    primary_agent_names = sorted({agent.lower() for agent in (agents or set()) if agent})
    if not primary_agent_names:
        return None
    baseline_scope = _filter_sql_session_rows(
        all_sessions,
        q=q,
        model=model,
        status=status,
        range_name=range_name,
    )
    baseline_sessions = [
        session
        for session in baseline_scope
        if str(session.get("agent") or "").lower() not in set(primary_agent_names)
    ]
    if not primary_sessions or not baseline_sessions:
        return None
    primary_ids = {str(session["id"]) for session in primary_sessions}
    baseline_ids = {str(session["id"]) for session in baseline_sessions}
    primary_metrics = _sql_cohort_metrics(db_path, primary_ids)
    baseline_metrics = _sql_cohort_metrics(db_path, baseline_ids)
    primary_summary = _sql_cohort_summary(
        primary_sessions,
        primary_metrics,
        label=" + ".join(primary_agent_names),
        agent_names=primary_agent_names,
    )
    baseline_summary = _sql_cohort_summary(
        baseline_sessions,
        baseline_metrics,
        label="All other agents in scope",
    )
    baseline_agents = sorted(
        _cohort_agent_comparison(baseline_sessions),
        key=lambda item: (-int(item.get("sessions") or 0), str(item.get("name") or "")),
    )
    quality_by_agent: dict[str, list[float]] = {}
    for session in baseline_sessions:
        quality_by_agent.setdefault(str(session.get("agent") or "unknown"), []).append(
            float(session.get("quality_score") or 0)
        )
    for agent in baseline_agents:
        values = quality_by_agent.get(str(agent.get("name") or "unknown"), [])
        if values:
            agent["avg_quality"] = sum(values) / len(values)
    return {
        "mode": "cohort-vs-rest",
        "primary": primary_summary,
        "baseline": baseline_summary,
        "baseline_agents": baseline_agents,
        "deltas": {
            "sessions": _comparison_delta(
                primary_summary["sessions"], baseline_summary["sessions"]
            ),
            "prompts": _comparison_delta(primary_summary["prompts"], baseline_summary["prompts"]),
            "tool_calls": _comparison_delta(
                primary_summary["tool_calls"], baseline_summary["tool_calls"]
            ),
            "avg_quality": _comparison_delta(
                primary_summary["avg_quality"], baseline_summary["avg_quality"]
            ),
            "failure_rate_pct": _comparison_delta(
                primary_summary["failure_rate_pct"], baseline_summary["failure_rate_pct"]
            ),
            "tokens": _comparison_delta(primary_summary["tokens"], baseline_summary["tokens"]),
            "shell_runs": _comparison_delta(
                primary_summary["shell_runs"], baseline_summary["shell_runs"]
            ),
            "mcp_calls": _comparison_delta(
                primary_summary["mcp_calls"], baseline_summary["mcp_calls"]
            ),
            "subagent_launches": _comparison_delta(
                primary_summary["subagent_launches"], baseline_summary["subagent_launches"]
            ),
        },
    }


def _sql_cohort_metrics(
    db_path: Path,
    session_ids: set[str],
) -> dict[str, object]:
    """Load only the aggregates rendered by cohort comparison cards."""
    from reflect.store.sqlite import connect_sqlite_read_only

    if not session_ids:
        return {
            "tools_by_count": {},
            "top_commands": [],
            "shell_executions": 0,
            "mcp_calls": 0,
            "subagent_launches": 0,
        }
    ordered_ids = sorted(session_ids)
    placeholders = ", ".join("?" for _ in ordered_ids)
    conn = connect_sqlite_read_only(db_path)
    try:
        tool_rows = dict_rows(
            conn.execute(
                f"""
            SELECT tool_name, COUNT(*) AS call_count
            FROM tool_calls
            WHERE session_id IN ({placeholders})
            GROUP BY tool_name
            ORDER BY call_count DESC, tool_name ASC
            LIMIT 5
            """,
                ordered_ids,
            )
        )
        shell_runs = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM tool_calls
            WHERE session_id IN ({placeholders})
              AND LOWER(tool_name) IN ('shell', 'bash', 'exec_command')
            """,
            ordered_ids,
        ).fetchone()[0]
        mcp_calls = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM mcp_calls AS mc
            JOIN tool_calls AS tc ON tc.id = mc.tool_call_id
            WHERE tc.session_id IN ({placeholders})
            """,
            ordered_ids,
        ).fetchone()[0]
        subagent_launches = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM steps st
            LEFT JOIN agent_events ae ON ae.step_id = st.id
            WHERE st.session_id IN ({placeholders})
              AND (
                ae.event_name = 'SubagentStart'
                OR st.type = 'subagent_start'
                OR st.summary = 'SubagentStart'
                OR st.summary LIKE '%.SubagentStart'
              )
            """,
            ordered_ids,
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "tools_by_count": {
            str(row["tool_name"]): int(row["call_count"] or 0)
            for row in tool_rows
            if row.get("tool_name")
        },
        "top_commands": [],
        "shell_executions": int(shell_runs or 0),
        "mcp_calls": int(mcp_calls or 0),
        "subagent_launches": int(subagent_launches or 0),
    }


def _cohort_agent_comparison(
    sessions: list[dict[str, object]],
) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for session in sessions:
        grouped.setdefault(str(session.get("agent") or "unknown"), []).append(session)
    rows = []
    for name, agent_sessions in grouped.items():
        quality = [float(item.get("quality_score") or 0) for item in agent_sessions]
        rows.append(
            {
                "name": name,
                "sessions": len(agent_sessions),
                "prompts": sum(int(item.get("prompt_count") or 0) for item in agent_sessions),
                "tools": sum(int(item.get("tool_calls") or 0) for item in agent_sessions),
                "failures": sum(int(item.get("failure_count") or 0) for item in agent_sessions),
                "tokens": sum(
                    int(item.get("input_tokens") or 0) + int(item.get("output_tokens") or 0)
                    for item in agent_sessions
                ),
                "total_cost": sum(
                    float(item.get("total_cost_usd") or 0) for item in agent_sessions
                ),
                "total_cost_usd": sum(
                    float(item.get("total_cost_usd") or 0) for item in agent_sessions
                ),
                "avg_quality": sum(quality) / len(quality) if quality else 0.0,
            }
        )
    return rows




def build_dashboard_payload(
    db_path: Path,
    *,
    limit: int = 50,
    offset: int = 0,
    q: str = "",
    session_id: str = "",
    agents: set[str] | None = None,
    model: str = "all",
    status: str = "all",
    range_name: str = "all",
    lazy_heavy_tabs: bool = False,
    lazy_all_tabs: bool = False,
    include_comparison: bool = True,
    base_tab_names: set[str] | None = None,
) -> dict[str, object]:
    has_scope_filter = bool(
        q or session_id or agents or model != "all" or status != "all" or range_name != "all"
    )
    sqlite_payload = _sql_report_payload(
        db_path,
        limit=500,
        offset=0,
        include_tabs=not (lazy_heavy_tabs or lazy_all_tabs),
    )
    overview = sqlite_payload["overview"]
    sessions_page = sqlite_payload["sessions"]
    session_rows = sessions_page["rows"]
    primary_models = (
        {}
        if (lazy_heavy_tabs or lazy_all_tabs) and model == "all"
        else _sql_session_primary_models(
            db_path,
            {str(row["session_id"]) for row in session_rows},
        )
    )
    first_prompts = (
        {}
        if lazy_heavy_tabs or lazy_all_tabs
        else sql_session_first_prompts(
            db_path,
            {str(row["session_id"]) for row in session_rows},
        )
    )
    sessions = [
        session_card_from_row(
            dict(row),
            first_prompt=first_prompts.get(str(row["session_id"]), ""),
            primary_model=primary_models.get(str(row["session_id"]), ""),
        )
        for row in session_rows
    ]
    all_sessions = sessions[:]
    nav_sessions = _filter_sql_session_rows(
        sessions,
        q=q,
        agents=agents,
        model=model,
        status=status,
        range_name=range_name,
    )
    scoped_sessions = (
        _filter_sql_session_rows(nav_sessions, session_id=session_id)
        if session_id
        else nav_sessions
    )
    nav_session_rows = [
        {
            "session_id": session["id"],
            "agent": session["agent"],
            "status": session["status"],
            "title": session["title"],
            "first_prompt": session["first_prompt"],
            "started_at": session["started_at"],
            "ended_at": session["ended_at"],
            "duration_ms": session["duration_ms"],
            "prompt_count": session["prompt_count"],
            "tool_call_count": session["tool_calls"],
            "failure_count": session["failure_count"],
            "input_tokens": session["input_tokens"],
            "output_tokens": session["output_tokens"],
            "cache_creation_tokens": session["cache_creation_tokens"],
            "cache_read_tokens": session["cache_read_tokens"],
            "estimated_cost_usd": session["total_cost_usd"],
            "total_tokens": session["total_tokens"],
            "quality_score": session["quality_score"],
        }
        for session in nav_sessions
    ]
    sessions_page = {
        **sessions_page,
        "rows": nav_session_rows[offset : offset + limit],
        "total": len(nav_sessions),
        "limit": limit,
        "offset": offset,
    }
    sessions = nav_sessions[offset : offset + limit]
    scoped_overview = {
        **overview,
        "session_count": len(scoped_sessions),
        "prompt_count": sum(int(row["prompt_count"] or 0) for row in scoped_sessions),
        "tool_call_count": sum(int(row["tool_calls"] or 0) for row in scoped_sessions),
        "failure_count": sum(int(row["failure_count"] or 0) for row in scoped_sessions),
        "input_tokens": sum(int(row["input_tokens"] or 0) for row in scoped_sessions),
        "output_tokens": sum(int(row["output_tokens"] or 0) for row in scoped_sessions),
        "estimated_cost_usd": sum(
            float(row["total_cost_usd"] or 0) for row in scoped_sessions
        ),
    }
    sqlite_payload["overview"] = scoped_overview
    sqlite_payload["sessions"] = sessions_page
    first_event_ts = ""
    if scoped_sessions:
        first_event_ts = min(row["started_at"] for row in scoped_sessions if row.get("started_at"))
    prompt_count = sum(row["prompt_count"] for row in scoped_sessions)
    scoped_session_ids = {str(row["id"]) for row in scoped_sessions}
    metrics = _sql_dashboard_metrics(
        db_path,
        session_ids=scoped_session_ids if has_scope_filter else None,
        include_heavy=not (lazy_heavy_tabs or lazy_all_tabs),
        include_base=not lazy_all_tabs,
        base_tab_names=base_tab_names,
    )
    scoped_overview["source_provenance"] = metrics["source_provenance"]
    cost_breakdown = metrics["cost_breakdown"]
    total_cost_usd = float(
        scoped_overview["estimated_cost_usd"] or cost_breakdown["total_cost_usd"] or 0
    )
    failure_rate_pct = (
        round(
            100 * scoped_overview["failure_count"] / scoped_overview["tool_call_count"],
            1,
        )
        if scoped_overview["tool_call_count"]
        else 0.0
    )
    weekly_trends = _compute_weekly_trends(Counter(metrics["activity_by_day"]))
    sqlite_payload["tabs"] = {
        **dict(sqlite_payload.get("tabs") or {}),
        "usage": {
            "avg_quality_score": (
                sum(float(row.get("quality_score") or 0) for row in scoped_sessions)
                / len(scoped_sessions)
                if scoped_sessions
                else 0
            ),
            "unique_sessions": scoped_overview["session_count"],
            "first_event_ts": first_event_ts,
            "prompt_submits": prompt_count,
            "tool_calls": scoped_overview["tool_call_count"],
            "tool_to_prompt_ratio": (
                f"{scoped_overview['tool_call_count'] / prompt_count:.1f}"
                if prompt_count
                else "0.0"
            ),
            "failure_rate_pct": failure_rate_pct,
            "tool_failures": int(scoped_overview["failure_count"]),
            "mcp_calls": metrics["mcp_calls"],
            "mcp_servers_by_count": metrics["mcp_servers_by_count"],
            "subagent_launches": metrics["subagent_launches"],
            "subagent_types_by_count": metrics["subagent_types_by_count"],
            "file_edits": metrics["file_edits"],
            "shell_executions": metrics["shell_executions"],
            "unique_commands": metrics["unique_commands"],
            "signature_command": metrics["signature_command"],
            "signature_command_count": metrics["signature_command_count"],
            "peak_hour": metrics["peak_hour"],
            "peak_hour_count": metrics["peak_hour_count"],
            "unique_models": metrics["unique_models"],
            "models_by_count": metrics["models_by_count"],
            "events_by_type": metrics["events_by_type"],
            "source_provenance": metrics["source_provenance"],
            "total_input_tokens": scoped_overview["input_tokens"],
            "total_output_tokens": scoped_overview["output_tokens"],
            "total_cache_creation_tokens": metrics["total_cache_creation_tokens"],
            "total_cache_read_tokens": metrics["total_cache_read_tokens"],
            "total_cost_usd": total_cost_usd,
            "input_cost_usd": cost_breakdown["input_cost_usd"],
            "output_cost_usd": cost_breakdown["output_cost_usd"],
            "cache_creation_cost_usd": cost_breakdown["cache_creation_cost_usd"],
            "cache_read_cost_usd": cost_breakdown["cache_read_cost_usd"],
            "pricing_unit": "usd",
            "pricing_source": "local",
            "model_costs": metrics["model_costs"],
            "agent_cost_over_time": metrics["agent_cost_over_time"],
        },
        "activity": {
            "events_by_type": metrics["events_by_type"],
            "activity_by_day": metrics["activity_by_day"],
            "activity_by_hour": metrics["activity_by_hour"],
            "peak_hour": metrics["peak_hour"],
            "peak_hour_count": metrics["peak_hour_count"],
            "weekly_trends": weekly_trends,
        },
        "models": {
            "models_by_count": metrics["models_by_count"],
            "unique_models": metrics["unique_models"],
        },
        "costs": {
            "model_costs": metrics["model_costs"],
            "model_costs_usd": metrics["model_costs_usd"],
            "cost_breakdown": metrics["cost_breakdown"],
            "total_cache_creation_tokens": metrics["total_cache_creation_tokens"],
            "total_cache_read_tokens": metrics["total_cache_read_tokens"],
            "agent_cost_over_time": metrics["agent_cost_over_time"],
        },
        "tools": {
            "tools_by_count": metrics["tools_by_count"],
            "tool_percentiles": metrics["tool_percentiles"],
            "skills_by_count": metrics["skills_by_count"],
            "subagent_types_by_count": metrics["subagent_types_by_count"],
            "subagent_stops_by_type": metrics["subagent_stops_by_type"],
            "subagent_launches": metrics["subagent_launches"],
            "subagent_total_starts": metrics["subagent_total_starts"],
            "subagent_total_stops": metrics["subagent_total_stops"],
            "top_commands": metrics["top_commands"],
            "unique_commands": metrics["unique_commands"],
            "signature_command": metrics["signature_command"],
            "signature_command_count": metrics["signature_command_count"],
            "shell_executions": metrics["shell_executions"],
            "file_edits": metrics["file_edits"],
            "file_reads": metrics["file_reads"],
        },
        "mcp": {
            "mcp_calls": metrics["mcp_calls"],
            "mcp_servers_by_count": metrics["mcp_servers_by_count"],
            "mcp_server_before": metrics["mcp_server_before"],
            "mcp_server_after": metrics["mcp_server_after"],
        },
        "agents": {
            "agent_comparison": metrics["agent_comparison"],
            "agents": metrics["agents"],
        },
        "graph": {
            "graph_tool_transitions": metrics["graph_tool_transitions"],
            "graph_cooccurrence": metrics["graph_cooccurrence"],
            "graph_dep": metrics["graph_dep"],
            "graph_session_timeline": metrics["graph_session_timeline"],
            "graph_semantic": metrics["graph_semantic"],
            "graph_latency_histograms": {},
        },
        "specs": metrics["specs"],
        "memory": metrics["memory"],
        "privacy": metrics["privacy"],
        "exports": metrics["exports"],
    }
    insight_payload = sql_insight_payload(scoped_overview, scoped_sessions, metrics)
    sqlite_payload["tabs"]["observations"] = {
        "strengths": insight_payload["strengths"],
        "observations": insight_payload["observations"],
        "recommendations": insight_payload["recommendations"],
        "practical_examples": insight_payload["practical_examples"],
        "achievements": insight_payload["achievements"],
        "token_economy": insight_payload["token_economy"],
    }
    comparison_payload = None
    if include_comparison and agents and not session_id:
        comparison_payload = _sql_comparison_payload(
            db_path,
            all_sessions,
            scoped_sessions,
            agents=agents,
            q=q,
            model=model,
            status=status,
            range_name=range_name,
        )
    sqlite_payload["tabs"]["cohort_comparison"] = {
        "comparison": comparison_payload,
        "agent_comparison": metrics["agent_comparison"],
    }
    payload = {
        "sql_backed": True,
        "sqlite": sqlite_payload,
        "sessions": sessions,
        "quality_rules": quality_rules_payload(),
        "session_list_total": len(nav_sessions),
        "focused_session_id": session_id,
        "first_event_ts": first_event_ts,
        "last_event_ts": max(
            (row["started_at"] for row in scoped_sessions if row.get("started_at")), default=""
        ),
    }
    return payload
