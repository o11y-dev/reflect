from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
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
from reflect.store.pending_refresh import analysis_coverage, derived_analysis_state
from reflect.store.sqlite import consistent_snapshot
from reflect.utils import _json_loads, _safe_ratio
from reflect.views.measurements import MeasurementView


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
    from reflect.views import build_overview, build_report_tabs, list_sessions

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


def _sql_dashboard_metrics(
    db_path: Path,
    *,
    session_ids: set[str] | None = None,
    include_heavy: bool = True,
    include_base: bool = True,
    base_tab_names: set[str] | None = None,
    measurement: MeasurementView | None = None,
) -> dict[str, object]:
    from reflect.store.sqlite import connect_sqlite_read_only
    from reflect.views import (
        build_report_tab,
        build_report_tabs,
        list_source_provenance,
    )

    scoped = session_ids is not None
    scoped_ids = sorted(session_ids or [])

    conn = connect_sqlite_read_only(db_path, profile="usage")
    try:
        selected_session_ids = set(scoped_ids) if scoped else None
        if not include_base:
            tab_views = empty_sql_lazy_tabs()
            source_provenance = []
        elif include_heavy:
            tab_views = build_report_tabs(
                conn, session_ids=selected_session_ids, measurement=measurement,
            ).model_dump()
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
                    measurement=measurement,
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
        "mcp_server_status_known": mcp_view["mcp_server_status_known"],
        "skills_by_count": tools_view["skills_by_count"],
        "skill_reports_by_count": tools_view.get("skill_reports_by_count", {}),
        "skill_mentions_by_count": tools_view.get("skill_mentions_by_count", {}),
        "skill_attempts_by_count": tools_view.get("skill_attempts_by_count", {}),
        "subagent_mentions_by_count": tools_view.get("subagent_mentions_by_count", {}),
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




def _session_token_total(session: dict[str, object]) -> int:
    component_total = sum(
        int(session.get(field) or 0)
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
        )
    )
    return component_total or int(session.get("total_tokens") or 0)


def _sql_cohort_summary(
    sessions: list[dict[str, object]],
    metrics: dict[str, object],
    *,
    label: str,
    agent_names: list[str] | None = None,
) -> dict[str, object]:
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
        if session.get("quality_available") is True
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
        "tokens": sum(_session_token_total(session) for session in sessions),
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
        if session.get("quality_available") is not True:
            continue
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
        quality = [
            float(item.get("quality_score") or 0)
            for item in agent_sessions
            if item.get("quality_available") is True
        ]
        rows.append(
            {
                "name": name,
                "sessions": len(agent_sessions),
                "prompts": sum(int(item.get("prompt_count") or 0) for item in agent_sessions),
                "tools": sum(int(item.get("tool_calls") or 0) for item in agent_sessions),
                "failures": sum(int(item.get("failure_count") or 0) for item in agent_sessions),
                "tokens": sum(_session_token_total(item) for item in agent_sessions),
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




@consistent_snapshot
def finalize_dashboard_analysis(
    db_path: Path, payload: dict[str, object], *,
    usage_completed_at: str, publication_revision: tuple[tuple[str, ...], ...],
) -> dict[str, object]:
    """Attach completed analysis to this run's immutable default usage scope."""
    from reflect.improvements.session_insights import read_session_insights
    from reflect.store.sqlite import SnapshotNotReadyError, connect_sqlite_read_only

    conn = connect_sqlite_read_only(db_path)
    try:
        revision = tuple(tuple(row) for row in conn.execute(
            "SELECT key,value,updated_at FROM store_metadata "
            "WHERE key IN ('prepared_usage','prepared_snapshot') ORDER BY key"
        ))
        metadata = dict(conn.execute(
            "SELECT key,value FROM store_metadata WHERE key IN "
            "('last_successful_usage_refresh','last_successful_refresh','analysis_window_v1')"
        ))
        if (revision != publication_revision or not usage_completed_at
                or len(revision) != 2 or any(row[1] != "ready" for row in revision)
                or metadata.get("last_successful_usage_refresh") != usage_completed_at
                or payload.get("snapshot_completed_at") != usage_completed_at):
            raise SnapshotNotReadyError("The usage publication changed before analysis completed.")
        # This canonical projection contains the full scope, including zero-usage
        # sessions; navigation cards are paginated and cannot supply these IDs.
        measurements = payload.get("measurements")
        sessions = measurements.get("sessions") if isinstance(measurements, dict) else None
        if not isinstance(sessions, dict):
            raise SnapshotNotReadyError("The usage publication has no canonical session scope.")
        session_ids = set(sessions)
        coverage = analysis_coverage(conn, session_ids)
        state = derived_analysis_state(coverage, snapshot_ready=True)
        try:
            window = _json_loads(metadata["analysis_window_v1"]) if metadata.get("analysis_window_v1") else None
        except (TypeError, ValueError):
            window = None
        return {
            **payload,
            "analysis_state": state,
            "analysis_completed_at": metadata.get("last_successful_refresh"),
            "analysis_window": window,
            "analysis_coverage": coverage,
            "insights": read_session_insights(conn, session_ids=session_ids) if state == "ready" else {},
        }
    finally:
        conn.close()


@consistent_snapshot(profile="usage")
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
    from reflect.improvements.session_insights import read_session_insights
    from reflect.session_rules import DEFAULT_SESSION_RULE_SCORER, context_from_summary
    from reflect.store.git_activity import git_activity
    from reflect.store.sqlite import connect_sqlite_read_only, snapshot_incomplete
    from reflect.views.measurements import build_measurements
    from reflect.views.sessions import aggregate_session_scope, list_sessions, select_session_scope

    days = 1 if range_name == "24h" else (
        int(range_name[:-1]) if range_name.endswith("d") and range_name[:-1].isdigit() else None
    )
    now = datetime.now(tz=UTC)
    date_from = (now - timedelta(days=days)).isoformat() if days else None
    date_to = now.isoformat() if days else None
    conn = connect_sqlite_read_only(db_path, profile="usage")
    try:
        all_sessions = select_session_scope(conn, q=q, model=model, status=status,
            date_from=date_from, date_to=date_to)
        for row in all_sessions:
            breakdown = DEFAULT_SESSION_RULE_SCORER.breakdown(context_from_summary(row))
            row["quality_score"] = DEFAULT_SESSION_RULE_SCORER.score_from_breakdown(breakdown)
            row["quality_available"] = DEFAULT_SESSION_RULE_SCORER.coverage_from_breakdown(breakdown) >= 50
        nav_sessions = [row for row in all_sessions if not agents or row["agent"] in agents]
        scoped_sessions = [row for row in nav_sessions if not session_id or row["id"] == session_id]
        scoped_session_ids = {row["id"] for row in scoped_sessions}
        coverage = analysis_coverage(conn, scoped_session_ids)
        analysis_state = derived_analysis_state(coverage, snapshot_ready=not snapshot_incomplete(conn))
        analysis_ready = analysis_state == "ready"
        if not analysis_ready:
            lazy_heavy_tabs = True
            include_comparison = False
            base_tab_names = (base_tab_names if base_tab_names is not None else {"activity", "models", "costs", "usage_tools", "mcp"}) & {
                "activity", "models", "costs", "usage_tools", "mcp",
            }
        page = list_sessions(conn, session_ids={row["id"] for row in nav_sessions}, limit=limit, offset=offset)
        sessions_page = page.model_dump()
        scoped_overview = aggregate_session_scope(conn, scoped_session_ids)
        period_activity = None
        if date_from and date_to:
            from reflect.usage import UsageService
            period_activity = UsageService(conn).activity_totals(
                session_ids=scoped_session_ids, date_from=date_from, date_to=date_to)
            scoped_overview.update(prompt_count=period_activity.prompts,
                tool_call_count=period_activity.tool_calls, failure_count=period_activity.failures)
        measurement_view = build_measurements(conn, session_ids=scoped_session_ids,
            date_from=date_from, date_to=date_to)
        measurement = measurement_view.model_dump()
        git_operations = git_activity(conn, session_ids=scoped_session_ids, date_from=date_from, date_to=date_to)
        insights = read_session_insights(conn, session_ids=scoped_session_ids) if analysis_ready else {}
        publication = conn.execute(
            "SELECT value FROM store_metadata WHERE key IN ('last_successful_usage_refresh', 'last_successful_refresh') "
            "ORDER BY value DESC LIMIT 1"
        ).fetchone()
        snapshot_completed_at = str(publication[0]) if publication else None
        analysis_publication = conn.execute(
            "SELECT value FROM store_metadata WHERE key = 'last_successful_refresh'"
        ).fetchone()
        analysis_completed_at = str(analysis_publication[0]) if analysis_publication else None
        window = conn.execute(
            "SELECT value FROM store_metadata WHERE key = 'analysis_window_v1'"
        ).fetchone()
        try:
            analysis_window = _json_loads(window[0]) if window else None
        except (TypeError, ValueError):
            analysis_window = None
    finally:
        conn.close()
    session_rows = sessions_page["rows"]
    first_prompts = {} if lazy_heavy_tabs or lazy_all_tabs else sql_session_first_prompts(
        db_path, {str(row["session_id"]) for row in session_rows})
    sessions = [session_card_from_row(dict(row), first_prompt=first_prompts.get(str(row["session_id"]), ""),
        measurement=measurement["sessions"].get(str(row["session_id"])),
        cost_scope=measurement["scope"]["kind"] if str(row["session_id"]) in measurement["sessions"] else "session_lifetime") for row in session_rows]
    # The overview is computed before navigation pagination. Usage is event-scoped.
    scoped_overview.update(input_tokens=measurement["totals"]["input_tokens"],
        output_tokens=measurement["totals"]["output_tokens"],
        estimated_cost_usd=measurement["totals"]["estimated_cost_usd"])
    sqlite_payload = {"db_path": str(db_path), "overview": scoped_overview,
        "sessions": sessions_page, "tabs": empty_sql_lazy_tabs()}
    first_event_ts = min((row["started_at"] for row in scoped_sessions if row.get("started_at")), default="")
    prompt_count = scoped_overview["prompt_count"]
    metrics = _sql_dashboard_metrics(
        db_path,
        session_ids=scoped_session_ids,
        include_heavy=not (lazy_heavy_tabs or lazy_all_tabs),
        include_base=not lazy_all_tabs,
        base_tab_names=base_tab_names,
        measurement=measurement_view,
    )
    if period_activity is not None:
        metrics["mcp_calls"] = period_activity.mcp_calls
        metrics["subagent_launches"] = period_activity.subagent_launches
        metrics["subagent_total_starts"] = period_activity.subagent_launches
    scoped_overview["source_provenance"] = metrics["source_provenance"]
    cost_breakdown = measurement["cost_breakdown"]
    metrics["cost_breakdown"] = cost_breakdown
    metrics["model_costs"] = {row["model"] or "unknown": row["cost_usd"] for row in measurement["model_mix"]}
    metrics["model_costs_usd"] = metrics["model_costs"]
    metrics["agent_cost_over_time"] = measurement["agent_cost_over_time"]
    metrics["total_cache_creation_tokens"] = measurement["totals"]["cache_creation_tokens"]
    metrics["total_cache_read_tokens"] = measurement["totals"]["cache_read_tokens"]
    total_cost_usd = float(measurement["totals"]["estimated_cost_usd"])
    failure_rate_pct = (
        round(
            100 * scoped_overview["failure_count"] / scoped_overview["tool_call_count"],
            1,
        )
        if scoped_overview["tool_call_count"]
        else 0.0
    )
    weekly_trends = _compute_weekly_trends(Counter(metrics["activity_by_day"]))
    quality_values = [
        float(row.get("quality_score") or 0)
        for row in scoped_sessions
        if row.get("quality_available") is True
    ]
    sqlite_payload["tabs"] = {
        **dict(sqlite_payload.get("tabs") or {}),
        "usage": {
            "avg_quality_score": sum(quality_values) / len(quality_values) if quality_values else 0,
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
            "subagent_mentions_by_count": metrics["subagent_mentions_by_count"],
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
            "skill_reports_by_count": metrics["skill_reports_by_count"],
            "skill_mentions_by_count": metrics["skill_mentions_by_count"],
            "skill_attempts_by_count": metrics["skill_attempts_by_count"],
            "subagent_types_by_count": metrics["subagent_types_by_count"],
            "subagent_mentions_by_count": metrics["subagent_mentions_by_count"],
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
            "mcp_server_status_known": metrics["mcp_server_status_known"],
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
            range_name="all",
        )
    sqlite_payload["tabs"]["cohort_comparison"] = {
        "comparison": comparison_payload,
        "agent_comparison": metrics["agent_comparison"],
    }
    payload = {
        "sql_backed": True,
        "snapshot_completed_at": snapshot_completed_at,
        "analysis_state": analysis_state,
        "analysis_completed_at": analysis_completed_at,
        "analysis_window": analysis_window,
        "analysis_coverage": coverage,
        "inventory_scope": "matching_session_lifetimes",
        "inventory_scope_label": (
            "Costs, tokens, and KPI totals use the selected period. Activity charts and tool inventories cover matching sessions in full."
            if days else ""
        ),
        "range": range_name,
        "measurements": measurement,
        "git_activity": git_operations,
        "insights": insights,
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
