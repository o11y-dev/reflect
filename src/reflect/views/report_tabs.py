from __future__ import annotations

import json
import re
import shlex
import sqlite3
from collections import Counter
from typing import Any
from urllib.parse import urlparse

from pydantic import Field

from reflect.schema.base import ReflectModel
from reflect.views.measurements import MeasurementView


class ActivityViewModel(ReflectModel):
    events_by_type: dict[str, int]
    activity_by_day: dict[str, int]
    activity_by_hour: dict[str, int]
    peak_hour: int
    peak_hour_count: int


class ModelsViewModel(ReflectModel):
    models_by_count: dict[str, int]
    unique_models: int


class CostsViewModel(ReflectModel):
    model_costs: dict[str, float]
    model_costs_usd: dict[str, float]
    cost_breakdown: dict[str, float]
    total_cache_creation_tokens: int
    total_cache_read_tokens: int
    agent_cost_over_time: list[dict[str, Any]]


class ToolsViewModel(ReflectModel):
    events_by_type: dict[str, int]
    tools_by_count: dict[str, int]
    tool_percentiles: list[dict[str, Any]]
    skills_by_count: dict[str, int]
    skill_reports_by_count: dict[str, int] = Field(default_factory=dict)
    skill_mentions_by_count: dict[str, int] = Field(default_factory=dict)
    skill_attempts_by_count: dict[str, int] = Field(default_factory=dict)
    subagent_mentions_by_count: dict[str, int] = Field(default_factory=dict)
    subagent_types_by_count: dict[str, int]
    subagent_stops_by_type: dict[str, int]
    subagent_launches: int
    subagent_total_starts: int
    subagent_total_stops: int
    top_commands: list[dict[str, Any]]
    unique_commands: int
    commands_with_template: int = 0
    signature_command: str
    signature_command_count: int
    shell_executions: int
    file_edits: int
    file_reads: int


class McpViewModel(ReflectModel):
    mcp_calls: int
    mcp_servers_by_count: dict[str, int]
    mcp_server_before: dict[str, int]
    mcp_server_after: dict[str, int]
    mcp_server_status_known: dict[str, int]


class AgentsViewModel(ReflectModel):
    agent_comparison: list[dict[str, Any]]
    agents: dict[str, dict[str, Any]]


class UsageToolSummaryViewModel(ReflectModel):
    subagent_mentions_by_count: dict[str, int] = Field(default_factory=dict)
    subagent_types_by_count: dict[str, int]
    subagent_stops_by_type: dict[str, int]
    subagent_launches: int
    subagent_total_starts: int
    subagent_total_stops: int
    top_commands: list[dict[str, Any]]
    unique_commands: int
    commands_with_template: int = 0
    signature_command: str
    signature_command_count: int
    shell_executions: int
    file_edits: int
    file_reads: int
    agent_comparison: list[dict[str, Any]]
    agents: dict[str, dict[str, Any]]


class GraphViewModel(ReflectModel):
    graph_tool_transitions: list[dict[str, Any]]
    graph_cooccurrence: dict[str, Any]
    graph_dep: dict[str, Any]
    graph_session_timeline: list[dict[str, Any]]
    graph_semantic: dict[str, Any]


class SpecsViewModel(ReflectModel):
    total_specs: int
    specs_by_status: dict[str, int]
    requirements_by_status: dict[str, int]
    evidence_by_kind: dict[str, int]
    specs: list[dict[str, Any]]


class MemoryViewModel(ReflectModel):
    total_memories: int
    memories_by_scope: dict[str, int]
    memories_by_type: dict[str, int]
    memories_by_sensitivity: dict[str, int]
    memories_by_source: dict[str, int]
    recent_memories: list[dict[str, Any]]


class PrivacyViewModel(ReflectModel):
    total_findings: int
    findings_by_type: dict[str, int]
    findings_by_severity: dict[str, int]
    findings_by_action: dict[str, int]
    recent_findings: list[dict[str, Any]]


class ExportsViewModel(ReflectModel):
    export_ready: bool
    available_formats: list[str]
    row_counts: dict[str, int]
    scoped: bool


class ReportTabsViewModel(ReflectModel):
    activity: ActivityViewModel
    models: ModelsViewModel
    costs: CostsViewModel
    tools: ToolsViewModel
    mcp: McpViewModel
    agents: AgentsViewModel
    graph: GraphViewModel
    specs: SpecsViewModel
    memory: MemoryViewModel
    privacy: PrivacyViewModel
    exports: ExportsViewModel


def build_report_tabs(
    conn: sqlite3.Connection, *, session_ids: set[str] | None = None,
    measurement: MeasurementView | None = None,
) -> ReportTabsViewModel:
    """Build SQL-backed view models for browser report tabs beyond Overview/Sessions."""
    scoped_ids = sorted(session_ids or [])
    scoped = scoped_ids if session_ids is not None else None
    skill_subagent = _skill_subagent_counts(conn, scoped)
    activity = _build_activity(conn, scoped)
    models, costs = _build_models_and_costs(conn, scoped, measurement=measurement)
    tools = _build_tools(conn, scoped, skill_subagent)
    mcp = _build_mcp(conn, scoped)
    agents = _build_agents(conn, scoped, skill_subagent)
    graph = _build_graph(conn, scoped, tools.tools_by_count, mcp.mcp_servers_by_count)
    specs = _build_specs(conn, scoped)
    memory = _build_memory(conn, scoped)
    privacy = _build_privacy(conn, scoped)
    exports = _build_exports(conn, scoped)
    return ReportTabsViewModel(
        activity=activity,
        models=models,
        costs=costs,
        tools=tools,
        mcp=mcp,
        agents=agents,
        graph=graph,
        specs=specs,
        memory=memory,
        privacy=privacy,
        exports=exports,
    )


def build_report_tab(
    conn: sqlite3.Connection,
    tab_name: str,
    *,
    session_ids: set[str] | None = None,
    measurement: MeasurementView | None = None,
) -> dict[str, Any]:
    """Build one SQL-backed report tab for lazy dashboard loading."""
    scoped_ids = sorted(session_ids or [])
    scoped = scoped_ids if session_ids is not None else None
    normalized = tab_name.strip().lower().replace("-", "_")
    if normalized == "activity":
        return _build_activity(conn, scoped).model_dump()
    if normalized in {"models", "costs"}:
        models, costs = _build_models_and_costs(conn, scoped, measurement=measurement)
        return (models if normalized == "models" else costs).model_dump()
    if normalized == "tools":
        skill_subagent = _skill_subagent_counts(conn, scoped)
        return _build_tools(conn, scoped, skill_subagent).model_dump()
    if normalized == "mcp":
        return _build_mcp(conn, scoped).model_dump()
    if normalized == "agents":
        skill_subagent = _skill_subagent_counts(conn, scoped)
        return _build_agents(conn, scoped, skill_subagent).model_dump()
    if normalized == "usage_tools":
        return _build_usage_tool_summary(conn, scoped).model_dump()
    if normalized == "graph":
        return _build_graph(
            conn,
            scoped,
            _top_tool_counts(conn, scoped),
            _graph_mcp_server_counts(conn, scoped),
        ).model_dump()
    if normalized == "specs":
        return _build_specs(conn, scoped).model_dump()
    if normalized == "memory":
        return _build_memory(conn, scoped).model_dump()
    if normalized == "privacy":
        return _build_privacy(conn, scoped).model_dump()
    if normalized == "exports":
        return _build_exports(conn, scoped).model_dump()
    raise ValueError(f"Unknown report tab: {tab_name}")


def _scope_clause(column: str, scoped_ids: list[str] | None, *, prefix: str = "WHERE") -> tuple[str, list[str]]:
    if scoped_ids is None:
        return "", []
    if not scoped_ids:
        return f"{prefix} 1 = 0", []
    return f"{prefix} {column} IN ({', '.join('?' for _ in scoped_ids)})", scoped_ids


def _artifact_memory_scope_clause(
    scoped_ids: list[str] | None,
    *,
    type_filter: str,
    prefix: str = "WHERE",
) -> tuple[str, list[str]]:
    if scoped_ids is None:
        return f"{prefix} {type_filter}", []
    if not scoped_ids:
        return f"{prefix} 1 = 0 AND {type_filter}", []
    placeholders = ", ".join("?" for _ in scoped_ids)
    return (
        f"""
        {prefix} {type_filter}
          AND (
            memories.session_id IN ({placeholders})
            OR EXISTS (
              SELECT 1 FROM memory_exposures me
              WHERE me.memory_id = memories.id
                AND me.session_id IN ({placeholders})
            )
          )
        """,
        [*scoped_ids, *scoped_ids],
    )


def _cursor_plan_scope_clause(scoped_ids: list[str] | None, *, prefix: str = "WHERE") -> tuple[str, list[str]]:
    return _artifact_memory_scope_clause(
        scoped_ids,
        type_filter="type = 'cursor_plan'",
        prefix=prefix,
    )


def _memory_scope_clause(scoped_ids: list[str] | None, *, prefix: str = "WHERE") -> tuple[str, list[str]]:
    return _artifact_memory_scope_clause(
        scoped_ids,
        type_filter="type <> 'cursor_plan'",
        prefix=prefix,
    )


def _dict_rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _counter(rows: list[dict[str, Any]], key: str, value: str) -> dict[str, int]:
    return {str(row[key]): int(row[value] or 0) for row in rows if row.get(key) not in (None, "")}


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return float(ordered[index])


def _event_type_counts(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> dict[str, int]:
    scope, params = _scope_clause("session_id", scoped_ids)
    return dict(conn.execute(
        f"SELECT type, COUNT(*) FROM steps {scope} GROUP BY type ORDER BY COUNT(*) DESC, type ASC",
        params,
    ))


def _build_activity(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> ActivityViewModel:
    rollup_scope, rollup_params = _scope_clause("session_id", scoped_ids, prefix="AND")
    steps_scope, steps_params = _scope_clause("session_id", scoped_ids)
    daily_rows = _dict_rows(conn.execute(
        f"""
        SELECT
          substr(started_at, 1, 10) AS day,
          COALESCE(SUM(prompt_count + tool_call_count + error_count), 0) AS event_count
        FROM session_rollups
        WHERE started_at IS NOT NULL AND started_at <> ''
        {rollup_scope}
        GROUP BY substr(started_at, 1, 10)
        ORDER BY day
        """,
        rollup_params,
    ))
    hour_rows = _dict_rows(conn.execute(
        f"""
        SELECT CAST(strftime('%H', started_at) AS INTEGER) AS hour, COUNT(*) AS event_count
        FROM steps
        {steps_scope}
        GROUP BY hour
        ORDER BY hour
        """,
        steps_params,
    ))
    by_hour = {str(hour): 0 for hour in range(24)}
    by_hour.update({str(row["hour"]): int(row["event_count"] or 0) for row in hour_rows if row.get("hour") is not None})
    peak_hour = max(range(24), key=lambda hour: by_hour.get(str(hour), 0)) if hour_rows else -1
    return ActivityViewModel(
        events_by_type=_event_type_counts(conn, scoped_ids),
        activity_by_day=_counter(daily_rows, "day", "event_count"),
        activity_by_hour=by_hour,
        peak_hour=peak_hour,
        peak_hour_count=by_hour.get(str(peak_hour), 0) if peak_hour >= 0 else 0,
    )


def _build_models_and_costs(
    conn: sqlite3.Connection,
    scoped_ids: list[str] | None,
    *,
    measurement: MeasurementView | None = None,
) -> tuple[ModelsViewModel, CostsViewModel]:
    from reflect.views.measurements import build_measurements

    measurement = measurement if measurement is not None else build_measurements(
        conn, session_ids=set(scoped_ids) if scoped_ids is not None else None, usage_only=True,
    )
    model_counts = {str(row["model"] or "unknown"): row["calls"] for row in measurement.model_mix}
    model_costs = {str(row["model"] or "unknown"): row["cost_usd"] for row in measurement.model_mix}
    # Bound presentation history only after exact event-scoped aggregation.
    recent_days = set(sorted(measurement.cost_by_day)[-180:])
    return (
        ModelsViewModel(models_by_count=model_counts, unique_models=sum(row["model"] is not None for row in measurement.model_mix)),
        CostsViewModel(
            model_costs=model_costs,
            model_costs_usd=model_costs,
            cost_breakdown=measurement.cost_breakdown,
            total_cache_creation_tokens=int(measurement.totals["cache_creation_tokens"]),
            total_cache_read_tokens=int(measurement.totals["cache_read_tokens"]),
            agent_cost_over_time=[row for row in measurement.agent_cost_over_time if row["day"] in recent_days],
        ),
    )


def _build_tools(
    conn: sqlite3.Connection,
    scoped_ids: list[str] | None,
    skill_subagent: dict[str, Any],
) -> ToolsViewModel:
    tools_by_count = _top_tool_counts(conn, scoped_ids)
    duration_rows = _dict_rows(conn.execute(
        f"""
        SELECT tool_name, duration_ms
        FROM tool_calls tc
        WHERE duration_ms IS NOT NULL
        {_and_scope('tc.session_id', scoped_ids)}
        """,
        scoped_ids or [],
    ))
    tool_durations: dict[str, list[float]] = {}
    for row in duration_rows:
        tool_durations.setdefault(str(row["tool_name"]), []).append(float(row["duration_ms"] or 0))
    percentiles = [
        {
            "tool": tool,
            "count": len(values),
            "p50": _percentile(values, 0.50),
            "p90": _percentile(values, 0.90),
            "p95": _percentile(values, 0.95),
            "p99": _percentile(values, 0.99),
        }
        for tool, values in sorted(tool_durations.items(), key=lambda item: len(item[1]), reverse=True)
    ][:10]
    commands, shell_executions = _command_summary(conn, scoped_ids)
    top_commands = [{"command": command, "count": count} for command, count in commands.most_common(25)]
    signature = top_commands[0] if top_commands else {"command": "", "count": 0}
    file_counts = skill_subagent["file_counts"]
    return ToolsViewModel(
        events_by_type=_event_type_counts(conn, scoped_ids),
        tools_by_count=tools_by_count,
        tool_percentiles=percentiles,
        skills_by_count=dict(skill_subagent["skills"].most_common()),
        skill_reports_by_count=dict(skill_subagent["skill_reports"].most_common()),
        skill_mentions_by_count=dict(skill_subagent["skill_mentions"].most_common()),
        skill_attempts_by_count=dict(skill_subagent["skill_attempts"].most_common()),
        subagent_mentions_by_count=dict(skill_subagent["subagent_mentions"].most_common()),
        subagent_types_by_count=dict(skill_subagent["subagent_starts"].most_common()),
        subagent_stops_by_type=dict(skill_subagent["subagent_stops"].most_common()),
        subagent_launches=sum(skill_subagent["subagent_starts"].values()),
        subagent_total_starts=sum(skill_subagent["subagent_starts"].values()),
        subagent_total_stops=sum(skill_subagent["subagent_stops"].values()),
        top_commands=top_commands,
        unique_commands=len(commands),
        commands_with_template=sum(commands.values()),
        signature_command=str(signature["command"]),
        signature_command_count=int(signature["count"]),
        shell_executions=shell_executions,
        file_edits=file_counts["edits"],
        file_reads=file_counts["reads"],
    )


def period_tool_activity(
    conn: sqlite3.Connection, *, session_ids: set[str], date_from: str, date_to: str,
) -> tuple[int, int]:
    """Count only displayed period activity, without materializing unrelated steps."""
    if not session_ids:
        return 0, 0
    scope, params = _scope_clause("st.session_id", sorted(session_ids))
    observed_window = """julianday(st.started_at) >= julianday(?)
        AND julianday(st.started_at) < julianday(?)
        AND COALESCE(json_extract(st.raw_attrs_json, '$."reflect.time.provenance"'), 'observed') != 'inferred'"""
    mcp = conn.execute(f"""SELECT COUNT(*) FROM mcp_calls mc
        JOIN tool_calls tc ON tc.id=mc.tool_call_id JOIN steps st ON st.id=tc.step_id
        {scope} AND {observed_window}""", [*params, date_from, date_to]).fetchone()[0]
    subagents = conn.execute(f"""SELECT COUNT(*) FROM (
        SELECT ae.session_id,COALESCE(NULLIF(ae.agent_id,''),ae.event_id,ae.step_id)
        FROM agent_events ae JOIN steps st ON st.id=ae.step_id
        {scope} AND {observed_window} AND ae.event_name='SubagentStart'
        GROUP BY 1,2)""", [*params, date_from, date_to]).fetchone()[0]
    return int(mcp), int(subagents)


def _build_usage_tool_summary(
    conn: sqlite3.Connection,
    scoped_ids: list[str] | None,
) -> UsageToolSummaryViewModel:
    skill_subagent = _usage_subagent_counts(conn, scoped_ids)
    commands, shell_executions = _command_summary(conn, scoped_ids)
    top_commands = [{"command": command, "count": count} for command, count in commands.most_common(25)]
    signature = top_commands[0] if top_commands else {"command": "", "count": 0}
    file_counts = _usage_file_counts(conn, scoped_ids)
    agents = _build_agents(conn, scoped_ids, skill_subagent)
    starts = skill_subagent["subagent_starts"]
    stops = skill_subagent["subagent_stops"]
    return UsageToolSummaryViewModel(
        subagent_mentions_by_count=dict(skill_subagent["subagent_mentions"].most_common()),
        subagent_types_by_count=dict(starts.most_common()),
        subagent_stops_by_type=dict(stops.most_common()),
        subagent_launches=sum(starts.values()),
        subagent_total_starts=sum(starts.values()),
        subagent_total_stops=sum(stops.values()),
        top_commands=top_commands,
        unique_commands=len(commands),
        commands_with_template=sum(commands.values()),
        signature_command=str(signature["command"]),
        signature_command_count=int(signature["count"]),
        shell_executions=shell_executions,
        file_edits=file_counts["edits"],
        file_reads=file_counts["reads"],
        agent_comparison=agents.agent_comparison,
        agents=agents.agents,
    )


def _top_tool_counts(
    conn: sqlite3.Connection,
    scoped_ids: list[str] | None,
) -> dict[str, int]:
    if scoped_ids is None:
        rows = _dict_rows(conn.execute(
            """
            SELECT tool_name, COALESCE(SUM(call_count), 0) AS call_count
            FROM tool_rollups
            GROUP BY tool_name
            ORDER BY call_count DESC, tool_name ASC
            LIMIT 25
            """
        ))
    else:
        scope, params = _scope_clause("tc.session_id", scoped_ids)
        rows = _dict_rows(conn.execute(
            f"""
            SELECT tc.tool_name, COUNT(*) AS call_count
            FROM tool_calls tc
            {scope}
            GROUP BY tool_name
            ORDER BY call_count DESC, tool_name ASC
            LIMIT 25
            """,
            params,
        ))
    return _counter(rows, "tool_name", "call_count")


def _graph_mcp_server_counts(
    conn: sqlite3.Connection,
    scoped_ids: list[str] | None,
) -> dict[str, int]:
    scope, params = _scope_clause("tc.session_id", scoped_ids, prefix="AND")
    rows = _dict_rows(conn.execute(
        f"""
        SELECT mc.server_name, COUNT(*) AS call_count
        FROM mcp_calls AS mc
        JOIN tool_calls AS tc ON tc.id = mc.tool_call_id
        WHERE mc.server_name IS NOT NULL AND mc.server_name <> ''
        {scope}
        GROUP BY mc.server_name
        ORDER BY call_count DESC, mc.server_name ASC
        LIMIT 25
        """,
        params,
    ))
    counts: Counter[str] = Counter()
    for row in rows:
        server = display_mcp_server_name(row["server_name"])
        if server:
            counts[server] += int(row["call_count"] or 0)
    return dict(counts)


def _skill_subagent_counts(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> dict[str, Any]:
    """Count source-attributed usage; reports and mentions remain separate."""
    result = _usage_subagent_counts(conn, scoped_ids)
    scope, params = _scope_clause("u.session_id", scoped_ids)
    for slug, agent, state, relationship, count in conn.execute(f"""
        SELECT sk.slug,COALESCE(a.name,'unknown'),u.state,
          json_extract(u.evidence_json,'$.relationship'),COUNT(*)
        FROM skill_usage u JOIN skills sk ON sk.id=u.skill_id
        JOIN sessions s ON s.id=u.session_id LEFT JOIN agents a ON a.id=s.agent_id
        {scope} GROUP BY 1,2,3,4
    """, params):
        if state == "observed" and relationship in {"invoked", "loaded"}:
            result["skills"][slug] += count
            result["skills_by_agent"].setdefault(agent, Counter())[slug] += count
        elif state == "reported":
            result["skill_reports"][slug] += count
        elif relationship == "load_attempted":
            result["skill_attempts"][slug] += count
        else:
            result["skill_mentions"][slug] += count
    scope, params = _scope_clause("e.session_id", scoped_ids, prefix="AND")
    for label, count in conn.execute(f"""
        SELECT n.label,COUNT(*) FROM graph_edges e
        JOIN graph_edge_sources source ON source.edge_id=e.id
        JOIN graph_nodes n ON n.id=e.target_node_id
        WHERE e.kind='used_skill'
          AND NOT EXISTS (
            SELECT 1 FROM skill_usage u JOIN skills sk ON sk.id=u.skill_id
            WHERE u.session_id=e.session_id AND sk.slug=n.label
              AND json_extract(u.evidence_json,'$.step_id')=source.source_id
          ) {scope} GROUP BY n.label
    """, params):
        result["skill_mentions"][label] += count
    result["file_counts"] = _usage_file_counts(conn, scoped_ids)
    return result


def _usage_subagent_counts(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> dict[str, Any]:
    """Use normalized lifecycle facts, never a prompt mentioning an agent.

    Compact graph sources retain historical inferred mentions for explanation;
    they cannot increase observed starts/stops or skill usage.
    """
    result = {"skills": Counter(), "skills_by_agent": {}, "skill_reports": Counter(),
              "skill_mentions": Counter(), "skill_attempts": Counter(),
              "subagent_starts": Counter(), "subagent_stops": Counter(),
              "subagents_by_agent": {}, "subagent_mentions": Counter()}
    scope, params = _scope_clause("ae.session_id", scoped_ids)
    for agent, event, kind, count in conn.execute(f"""
        SELECT COALESCE(a.name,'unknown'),ae.event_name,
          COALESCE(NULLIF(ae.agent_type,''),'Role not recorded'),
          COUNT(DISTINCT ae.session_id || char(0) || COALESCE(NULLIF(ae.agent_id,''),ae.event_id,ae.step_id))
        FROM agent_events ae JOIN sessions s ON s.id=ae.session_id
        LEFT JOIN agents a ON a.id=s.agent_id
        {scope} GROUP BY 1,2,3
    """, params):
        if event == "SubagentStart":
            result["subagent_starts"][kind] += count
            result["subagents_by_agent"].setdefault(agent, Counter())[kind] += count
        elif event == "SubagentStop":
            result["subagent_stops"][kind] += count
    scope, params = _scope_clause("e.session_id", scoped_ids, prefix="AND")
    for label, count in conn.execute(f"""
        SELECT n.label,COUNT(*) FROM graph_edges e
        JOIN graph_edge_sources source ON source.edge_id=e.id
        JOIN graph_nodes n ON n.id=e.target_node_id
        WHERE e.kind='spawned_subagent'
          AND json_extract(source.attrs_json,'$.evidence_class')='inferred'
        {scope} GROUP BY n.label
    """, params):
        result["subagent_mentions"][label] += count
    return result


def _load_json_dict(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _and_scope(column: str, scoped_ids: list[str] | None) -> str:
    if scoped_ids is None:
        return ""
    if not scoped_ids:
        return "AND 1 = 0"
    return f"AND {column} IN ({', '.join('?' for _ in scoped_ids)})"


def _command_summary(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> tuple[Counter[str], int]:
    rows = conn.execute(
        f"""SELECT command_template, COUNT(*) FROM tool_calls tc
        WHERE is_shell_call = 1 {_and_scope('tc.session_id', scoped_ids)}
        GROUP BY command_template""", scoped_ids or [],
    )
    commands: Counter[str] = Counter()
    shell_calls = 0
    for template, count in rows:
        if template:
            commands[str(template)] += int(count)
        shell_calls += int(count)
    return commands, shell_calls


def _usage_file_counts(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> dict[str, int]:
    scope, params = _scope_clause("session_id", scoped_ids, prefix="AND")
    rows = conn.execute(
        f"""
        SELECT LOWER(COALESCE(tool_name, '')) AS tool_name, COUNT(*) AS call_count
        FROM tool_calls
        WHERE 1 = 1
        {scope}
        GROUP BY LOWER(COALESCE(tool_name, ''))
        """,
        params,
    ).fetchall()
    read_tools = {"read", "read_file", "readfile", "view", "view_file"}
    edit_tools = {
        "apply_patch", "edit", "edit_file", "editfile", "multi_edit", "patch",
        "replace", "write", "write_file", "writefile",
    }
    reads = sum(int(count or 0) for tool_name, count in rows if str(tool_name) in read_tools)
    edits = sum(int(count or 0) for tool_name, count in rows if str(tool_name) in edit_tools)
    # Some hooks record files without a tool row. Their canonical event name is
    # sufficient; command text merely mentioning "read" or "edit" is not.
    scope, params = _scope_clause("st.session_id", scoped_ids, prefix="AND")
    for kind, summary, count in conn.execute(f"""
        SELECT lower(st.type),lower(st.summary),COUNT(*)
        FROM steps st INDEXED BY idx_steps_measurement_times
        WHERE NOT EXISTS (SELECT 1 FROM tool_calls tc WHERE tc.step_id=st.id)
          AND (st.type IN ('file_read','file_edit')
            OR lower(st.summary) LIKE '%beforereadfile'
            OR lower(st.summary) LIKE '%afterfileedit')
        {scope} GROUP BY 1,2
    """, params):
        reads += count if kind == "file_read" or str(summary).endswith("beforereadfile") else 0
        edits += count if kind == "file_edit" or str(summary).endswith("afterfileedit") else 0
    return {"reads": reads, "edits": edits}


def _build_mcp(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> McpViewModel:
    mcp_scope, mcp_params = _scope_clause("tc.session_id", scoped_ids, prefix="AND")
    rows = _dict_rows(conn.execute(
        f"""
        SELECT
          mc.server_name,
          COUNT(*) AS call_count,
          SUM(
            CASE
              WHEN LOWER(COALESCE(tc.status, '')) IN ('ok', 'success', 'completed') THEN 1
              ELSE 0
            END
          ) AS completion_count,
          SUM(
            CASE
              WHEN LOWER(COALESCE(tc.status, '')) IN (
                'ok', 'success', 'completed', 'error', 'failed', 'failure'
              ) THEN 1
              ELSE 0
            END
          ) AS status_known_count
        FROM mcp_calls AS mc
        JOIN tool_calls AS tc ON tc.id = mc.tool_call_id
        WHERE mc.server_name IS NOT NULL AND mc.server_name <> ''
        {mcp_scope}
        GROUP BY mc.server_name
        ORDER BY call_count DESC, mc.server_name ASC
        """,
        mcp_params,
    ))
    counts: Counter[str] = Counter()
    completions: Counter[str] = Counter()
    known_statuses: Counter[str] = Counter()
    for row in rows:
        server = display_mcp_server_name(row["server_name"])
        if server:
            counts[server] += int(row["call_count"] or 0)
            completions[server] += int(row["completion_count"] or 0)
            known_statuses[server] += int(row["status_known_count"] or 0)
    return McpViewModel(
        mcp_calls=sum(counts.values()),
        mcp_servers_by_count=dict(counts),
        mcp_server_before=dict(counts),
        mcp_server_after=dict(completions),
        mcp_server_status_known=dict(known_statuses),
    )


def display_mcp_server_name(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "\n" in text:
        text = text.splitlines()[0].strip()
    lowered = text.lower()
    if len(text) > 32 * 1024 and lowered.startswith(("npx ", "docker run ")):
        return "npx" if lowered.startswith("npx ") else "docker"
    if lowered.startswith("npx "):
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()
        for part in parts[1:]:
            parsed = urlparse(part)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                text = parsed.netloc
                break
        else:
            text = next(
                (
                    part.rsplit("/", 1)[-1]
                    for part in parts[1:]
                    if not part.startswith("-")
                ),
                "npx",
            ) or "npx"
    elif lowered.startswith("docker run "):
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()
        skip_next_for = {"-e", "--env", "--env-file", "-v", "--volume", "-p", "--publish", "--name", "--network"}
        index = 2
        while index < len(parts):
            part = parts[index]
            if part in skip_next_for:
                index += 2
                continue
            if part.startswith("-"):
                index += 1
                continue
            text = part.rsplit("/", 1)[-1].split(":", 1)[0] or "docker"
            break
        else:
            text = "docker"
    text = re.sub(r"^user[-_]", "", text, flags=re.IGNORECASE)
    return re.sub(r"-{2,}", "-", text.replace("_", "-")).strip("-")


def _build_agents(
    conn: sqlite3.Connection,
    scoped_ids: list[str] | None,
    skill_subagent: dict[str, Any],
) -> AgentsViewModel:
    scope, params = _scope_clause("sr.session_id", scoped_ids)
    rows = _dict_rows(conn.execute(
        f"""
        SELECT
          COALESCE(NULLIF(sr.agent, ''), 'unknown') AS name,
          COUNT(*) AS sessions,
          COALESCE(SUM(sr.prompt_count), 0) AS prompts,
          COALESCE(SUM(sr.tool_call_count), 0) AS tools,
          COALESCE(SUM(sr.error_count), 0) AS failures,
          COALESCE(SUM(sr.input_tokens), 0) AS input_tokens,
          COALESCE(SUM(sr.output_tokens), 0) AS output_tokens,
          COALESCE(SUM(sr.cache_write_tokens), 0) AS cache_creation_tokens,
          COALESCE(SUM(sr.cache_read_tokens), 0) AS cache_read_tokens,
          COALESCE(SUM(sr.input_tokens), 0)
            + COALESCE(SUM(sr.output_tokens), 0)
            + COALESCE(SUM(sr.cache_write_tokens), 0)
            + COALESCE(SUM(sr.cache_read_tokens), 0) AS tokens,
          COALESCE(SUM(sr.total_cost), 0) AS total_cost,
          COALESCE(AVG(
            MAX(0, MIN(100,
              CASE
                WHEN COALESCE(NULLIF(s.status, ''), 'unknown') IN ('ok', 'completed', 'success') THEN 90
                WHEN COALESCE(NULLIF(s.status, ''), 'unknown') = 'unknown' THEN 80
                ELSE 65
              END - COALESCE(sr.error_count, 0) * 12
            ))
          ), 0) AS avg_quality
        FROM session_rollups sr
        LEFT JOIN sessions s ON s.id = sr.session_id
        {scope}
        GROUP BY COALESCE(NULLIF(sr.agent, ''), 'unknown')
        ORDER BY sessions DESC, tools DESC, name ASC
        """,
        params,
    ))
    tool_rows = _dict_rows(conn.execute(
        f"""
        SELECT COALESCE(NULLIF(sr.agent, ''), 'unknown') AS agent, tc.tool_name, COUNT(*) AS count
        FROM tool_calls tc
        LEFT JOIN session_rollups sr ON sr.session_id = tc.session_id
        {_scope_clause('tc.session_id', scoped_ids)[0]}
        GROUP BY COALESCE(NULLIF(sr.agent, ''), 'unknown'), tc.tool_name
        ORDER BY count DESC
        """,
        scoped_ids or [],
    ))
    top_tools: dict[str, Counter[str]] = {}
    for row in tool_rows:
        if row.get("tool_name"):
            top_tools.setdefault(str(row["agent"]), Counter())[str(row["tool_name"])] += int(row["count"] or 0)
    mcp_rows = _dict_rows(conn.execute(
        f"""
        SELECT COALESCE(NULLIF(sr.agent, ''), 'unknown') AS agent, COUNT(*) AS count
        FROM mcp_calls AS mc
        JOIN tool_calls AS tc ON tc.id = mc.tool_call_id
        LEFT JOIN session_rollups sr ON sr.session_id = tc.session_id
        {_scope_clause('tc.session_id', scoped_ids)[0]}
        GROUP BY COALESCE(NULLIF(sr.agent, ''), 'unknown')
        """,
        scoped_ids or [],
    ))
    mcp_by_agent = {str(row["agent"]): int(row["count"] or 0) for row in mcp_rows}
    comparison = []
    agents: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row["name"])
        prompts = int(row["prompts"] or 0)
        tools = int(row["tools"] or 0)
        failures = int(row["failures"] or 0)
        total_cost = float(row["total_cost"] or 0)
        comparison.append({
            "name": name,
            "events": prompts + tools,
            "sessions": int(row["sessions"] or 0),
            "avg_quality": float(row["avg_quality"] or 0),
            "completed": 0,
            "recovered": 0,
            "prompts": prompts,
            "tools": tools,
            "failures": failures,
            "tokens": int(row["tokens"] or 0),
            "total_cost": total_cost,
            "total_cost_usd": total_cost,
        })
        agents[name] = {
            "total_events": prompts + tools + failures,
            "sessions": int(row["sessions"] or 0),
            "prompts": prompts,
            "tool_calls": tools,
            "tool_ratio": round((tools / prompts) if prompts else 0, 1),
            "failures": failures,
            "failure_rate": round(100 * failures / tools, 1) if tools else 0,
            "mcp_calls": mcp_by_agent.get(name, 0),
            "subagents": sum(skill_subagent["subagents_by_agent"].get(name, Counter()).values()),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "cache_creation_tokens": int(row["cache_creation_tokens"] or 0),
            "cache_read_tokens": int(row["cache_read_tokens"] or 0),
            "total_cost_usd": total_cost,
            "top_model": "",
            "top_tools": dict(top_tools.get(name, Counter()).most_common(10)),
            "top_skills": dict(skill_subagent["skills_by_agent"].get(name, Counter()).most_common(10)),
            "percentiles": [],
        }
    extra_agent_names = (
        set(skill_subagent["subagents_by_agent"]) | set(skill_subagent["skills_by_agent"])
    ) - set(agents)
    for name in sorted(extra_agent_names):
        agents[name] = {
            "total_events": 0,
            "sessions": 0,
            "prompts": 0,
            "tool_calls": 0,
            "tool_ratio": 0,
            "failures": 0,
            "failure_rate": 0,
            "mcp_calls": mcp_by_agent.get(name, 0),
            "subagents": sum(skill_subagent["subagents_by_agent"].get(name, Counter()).values()),
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "total_cost_usd": 0,
            "top_model": "",
            "top_tools": dict(top_tools.get(name, Counter()).most_common(10)),
            "top_skills": dict(skill_subagent["skills_by_agent"].get(name, Counter()).most_common(10)),
            "percentiles": [],
        }
    return AgentsViewModel(agent_comparison=comparison, agents=agents)


def _build_graph(
    conn: sqlite3.Connection,
    scoped_ids: list[str] | None,
    tools_by_count: dict[str, int],
    mcp_servers_by_count: dict[str, int],
) -> GraphViewModel:
    tool_scope, tool_params = _scope_clause("tc.session_id", scoped_ids)
    transitions = _dict_rows(conn.execute(
        f"""
        WITH ordered AS (
          SELECT
            tc.session_id,
            tc.tool_name,
            LEAD(tc.tool_name) OVER (PARTITION BY tc.session_id ORDER BY st.seq, tc.id) AS next_tool
          FROM tool_calls tc
          JOIN steps st ON st.id = tc.step_id
          {tool_scope}
        )
        SELECT tool_name AS source, next_tool AS target, COUNT(*) AS count
        FROM ordered
        WHERE next_tool IS NOT NULL AND next_tool <> tool_name
        GROUP BY tool_name, next_tool
        ORDER BY count DESC, source ASC, target ASC
        LIMIT 30
        """,
        tool_params,
    ))
    co_tools = list(tools_by_count.keys())[:12]
    co_matrix = _cooccurrence_matrix(conn, scoped_ids, co_tools)
    timeline = _timeline(conn, scoped_ids)
    graph_dep = _dependency_graph(conn, scoped_ids, tools_by_count, mcp_servers_by_count)
    graph_semantic = _semantic_graph(conn, scoped_ids)
    return GraphViewModel(
        graph_tool_transitions=[
            {"from": row["source"], "to": row["target"], "count": int(row["count"] or 0)}
            for row in transitions
        ],
        graph_cooccurrence={"tools": co_tools, "matrix": co_matrix},
        graph_dep=graph_dep,
        graph_session_timeline=timeline,
        graph_semantic=graph_semantic,
    )


def _semantic_graph(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> dict[str, Any]:
    graph_scoped_ids = _workspace_peer_session_ids(conn, scoped_ids)
    # Rank compact metadata before loading attributes; calls are bounded below.
    node_filters: list[str] = []
    node_params: list[str] = []
    if graph_scoped_ids is not None:
        if not graph_scoped_ids:
            return {"nodes": [], "edges": [], "sessions": [], "legend": []}
        placeholders = ", ".join("?" for _ in graph_scoped_ids)
        node_filters.append(f"(session_id IS NULL OR session_id IN ({placeholders}))")
        node_params.extend(graph_scoped_ids)
    where = f"WHERE {' AND '.join(node_filters)}" if node_filters else ""
    node_rows = _dict_rows(conn.execute(
        f"""
        WITH filtered AS (
          SELECT id, kind, label, first_seen_at, last_seen_at, session_id, rowid AS rank_rowid
          FROM graph_nodes INDEXED BY idx_graph_nodes_rank
          {where}
        ),
        ranked AS (
          SELECT
            *,
            ROW_NUMBER() OVER (
              PARTITION BY kind
              ORDER BY COALESCE(last_seen_at, first_seen_at, '') DESC, label ASC, session_id ASC, rank_rowid ASC
            ) AS kind_rank
          FROM filtered
        )
        SELECT ranked.id, ranked.kind, ranked.label, node.session_id, node.attrs_json,
               ranked.first_seen_at, ranked.last_seen_at
        FROM ranked JOIN graph_nodes node ON node.id = ranked.id
        WHERE kind_rank <= CASE ranked.kind
          WHEN 'Agent' THEN 10
          WHEN 'Session' THEN 40
          WHEN 'Execution' THEN 40
          WHEN 'Workspace' THEN 30
          WHEN 'Repo' THEN 20
          WHEN 'Spec' THEN 35
          WHEN 'Folder' THEN 75
          WHEN 'Path' THEN 60
          WHEN 'Tool' THEN 50
          WHEN 'ToolCall' THEN 40
          WHEN 'MCPServer' THEN 20
          WHEN 'Memory' THEN 45
          WHEN 'Skill' THEN 45
          WHEN 'Subagent' THEN 45
          WHEN 'Outcome' THEN 45
          ELSE 20
        END
        ORDER BY
          CASE ranked.kind
            WHEN 'Agent' THEN 0
            WHEN 'Session' THEN 1
            WHEN 'Workspace' THEN 2
            WHEN 'Skill' THEN 3
            WHEN 'Subagent' THEN 4
            WHEN 'Outcome' THEN 5
            WHEN 'Repo' THEN 6
            WHEN 'Spec' THEN 7
            WHEN 'Folder' THEN 8
            WHEN 'Tool' THEN 9
            WHEN 'MCPServer' THEN 10
            WHEN 'Path' THEN 11
            WHEN 'ToolCall' THEN 12
            WHEN 'Memory' THEN 13
            ELSE 99
          END,
          COALESCE(ranked.last_seen_at, ranked.first_seen_at, '') DESC,
          ranked.label ASC
        """,
        node_params,
    ))
    selected_ids = [str(row["id"]) for row in node_rows]
    selected = set(selected_ids)
    if not selected_ids:
        return {"nodes": [], "edges": [], "sessions": [], "legend": []}

    edge_filters = [
        f"source_node_id IN ({', '.join('?' for _ in selected_ids)})",
        f"target_node_id IN ({', '.join('?' for _ in selected_ids)})",
    ]
    edge_params = [*selected_ids, *selected_ids]
    if graph_scoped_ids is not None:
        placeholders = ", ".join("?" for _ in graph_scoped_ids)
        edge_filters.append(f"(session_id IS NULL OR session_id IN ({placeholders}))")
        edge_params.extend(graph_scoped_ids)
    # Probe displayed sources rather than every edge in workspace peer sessions.
    # Explicit ties retain the scoped session-index traversal order even when
    # the planner uses the much smaller displayed-node population.
    edge_index = "INDEXED BY idx_graph_edges_source" if graph_scoped_ids is not None else ""
    edge_ties = ", session_id ASC, rowid ASC" if graph_scoped_ids is not None else ""
    edge_rows = _dict_rows(conn.execute(
        f"""
        SELECT source_node_id, target_node_id, kind, session_id, weight, attrs_json, first_seen_at, last_seen_at
        FROM graph_edges {edge_index}
        WHERE {' AND '.join(edge_filters)}
        ORDER BY weight DESC, kind ASC{edge_ties}
        LIMIT 900
        """,
        edge_params,
    ))
    if graph_scoped_ids is not None:
        scope_placeholders = ", ".join("?" for _ in graph_scoped_ids)
        scoped_context_rows = _dict_rows(conn.execute(
            f"""
            SELECT source_node_id, target_node_id, kind, session_id, weight,
                   attrs_json, first_seen_at, last_seen_at
            FROM graph_edges
            WHERE session_id IN ({scope_placeholders})
              AND kind IN (
                'ran_in_workspace', 'ran_session', 'spawned_session',
                'worked_in_repo'
              )
            ORDER BY
              CASE kind
                WHEN 'ran_in_workspace' THEN 0
                WHEN 'ran_session' THEN 1
                WHEN 'spawned_session' THEN 2
                ELSE 3
              END,
              COALESCE(last_seen_at, first_seen_at, '') DESC
            """,
            graph_scoped_ids,
        ))
        scoped_bridge_rows = _dict_rows(conn.execute(
            f"""
            WITH ranked AS MATERIALIZED (
              SELECT id, kind, weight, first_seen_at, last_seen_at, session_id, rowid AS rank_rowid
              FROM graph_edges INDEXED BY idx_graph_edges_rank
              WHERE session_id IN ({scope_placeholders})
                AND kind IN (
                  'touched_folder', 'touched_path', 'used_skill',
                  'suggests_outcome', 'recorded_memory', 'has_execution', 'checked_by'
                )
              ORDER BY CASE kind WHEN 'touched_folder' THEN 0 ELSE 1 END,
                weight DESC, COALESCE(last_seen_at, first_seen_at, '') DESC,
                session_id ASC, kind ASC, rank_rowid ASC
              LIMIT 600
            )
            SELECT edge.source_node_id, edge.target_node_id, edge.kind, edge.session_id,
                   edge.weight, edge.attrs_json, edge.first_seen_at, edge.last_seen_at
            FROM ranked JOIN graph_edges edge ON edge.id=ranked.id
            ORDER BY CASE ranked.kind WHEN 'touched_folder' THEN 0 ELSE 1 END,
              ranked.weight DESC, COALESCE(ranked.last_seen_at, ranked.first_seen_at, '') DESC,
              ranked.session_id ASC, ranked.kind ASC, ranked.rank_rowid ASC
            """,
            graph_scoped_ids,
        ))
        scoped_bridge_rows = [*scoped_context_rows, *scoped_bridge_rows]
        if scoped_bridge_rows:
            bridge_node_ids = {
                str(node_id)
                for row in scoped_bridge_rows
                for node_id in (row["source_node_id"], row["target_node_id"])
            }
            missing_ids = [node_id for node_id in bridge_node_ids if node_id not in selected]
            if missing_ids:
                placeholders = ", ".join("?" for _ in missing_ids)
                node_rows.extend(
                    _dict_rows(conn.execute(
                        f"""
                        SELECT id, kind, label, session_id, attrs_json, first_seen_at, last_seen_at
                        FROM graph_nodes
                        WHERE id IN ({placeholders})
                        """,
                        missing_ids,
                    ))
                )
                selected_ids.extend(missing_ids)
                selected.update(missing_ids)
            edge_rows.extend(scoped_bridge_rows)
    session_node_ids = [
        str(row["id"])
        for row in node_rows
        if str(row.get("kind") or "") == "Session"
    ]
    if session_node_ids:
        session_placeholders = ", ".join("?" for _ in session_node_ids)
        lineage_rows = _dict_rows(conn.execute(
            f"""
            SELECT source_node_id, target_node_id, kind, session_id, weight,
                   attrs_json, first_seen_at, last_seen_at
            FROM graph_edges
            WHERE kind = 'spawned_session'
              AND (
                source_node_id IN ({session_placeholders})
                OR target_node_id IN ({session_placeholders})
              )
            ORDER BY COALESCE(last_seen_at, first_seen_at, '') DESC
            LIMIT 120
            """,
            [*session_node_ids, *session_node_ids],
        ))
        if lineage_rows:
            lineage_node_ids = {
                str(node_id)
                for row in lineage_rows
                for node_id in (row["source_node_id"], row["target_node_id"])
            }
            missing_ids = [node_id for node_id in lineage_node_ids if node_id not in selected]
            if missing_ids:
                placeholders = ", ".join("?" for _ in missing_ids)
                node_rows.extend(
                    _dict_rows(conn.execute(
                        f"""
                        SELECT id, kind, label, session_id, attrs_json, first_seen_at, last_seen_at
                        FROM graph_nodes
                        WHERE id IN ({placeholders})
                        """,
                        missing_ids,
                    ))
                )
                selected_ids.extend(missing_ids)
                selected.update(missing_ids)
            edge_rows.extend(lineage_rows)
    if scoped_ids is None:
        bridge_ids = [
            str(row["id"])
            for row in node_rows
            if str(row.get("kind") or "") in {"Memory", "Spec"}
        ]
        if bridge_ids:
            bridge_placeholders = ", ".join("?" for _ in bridge_ids)
            bridge_rows = _dict_rows(conn.execute(
                f"""
                SELECT source_node_id, target_node_id, kind, session_id, weight, attrs_json, first_seen_at, last_seen_at
                FROM graph_edges
                WHERE kind IN ('recorded_memory', 'described_by_path', 'contains_path', 'defines_spec')
                  AND (
                    source_node_id IN ({bridge_placeholders})
                    OR target_node_id IN ({bridge_placeholders})
                  )
                ORDER BY COALESCE(last_seen_at, first_seen_at, '') DESC, weight DESC
                LIMIT 240
                """,
                [*bridge_ids, *bridge_ids],
            ))
            if bridge_rows:
                bridge_ids = {
                    str(node_id)
                    for row in bridge_rows
                    for node_id in (row["source_node_id"], row["target_node_id"])
                }
                missing_ids = [node_id for node_id in bridge_ids if node_id not in selected]
                if missing_ids:
                    placeholders = ", ".join("?" for _ in missing_ids)
                    node_rows.extend(
                        _dict_rows(conn.execute(
                            f"""
                            SELECT id, kind, label, session_id, attrs_json, first_seen_at, last_seen_at
                            FROM graph_nodes
                            WHERE id IN ({placeholders})
                            """,
                            missing_ids,
                        ))
                    )
                    selected_ids.extend(missing_ids)
                    selected.update(missing_ids)
                edge_rows.extend(bridge_rows)
                deduped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
                for row in edge_rows:
                    key = (
                        str(row["source_node_id"]),
                        str(row["target_node_id"]),
                        str(row["kind"]),
                        str(row.get("session_id") or ""),
                    )
                    existing = deduped.get(key)
                    if existing is None or float(row.get("weight") or 0) > float(existing.get("weight") or 0):
                        deduped[key] = row
                edge_rows = list(deduped.values())
        context_ids = [
            str(row["id"])
            for row in node_rows
            if str(row.get("kind") or "") in {"Memory", "Spec", "Repo", "Workspace"}
        ]
        if context_ids:
            context_placeholders = ", ".join("?" for _ in context_ids)
            session_bridge_rows = _dict_rows(conn.execute(
                f"""
                SELECT ge.source_node_id, ge.target_node_id, ge.kind, ge.session_id, ge.weight, ge.attrs_json, ge.first_seen_at, ge.last_seen_at
                FROM graph_edges ge
                JOIN graph_nodes gs ON gs.id = ge.source_node_id
                JOIN graph_nodes gt ON gt.id = ge.target_node_id
                WHERE ge.kind IN (
                  'recorded_memory', 'planned_spec', 'addressed_spec',
                  'worked_in_repo', 'ran_in_workspace'
                )
                  AND (
                    (ge.source_node_id IN ({context_placeholders}) AND gt.kind = 'Session')
                    OR (ge.target_node_id IN ({context_placeholders}) AND gs.kind = 'Session')
                  )
                ORDER BY COALESCE(ge.last_seen_at, ge.first_seen_at, '') DESC, ge.weight DESC
                LIMIT 320
                """,
                [*context_ids, *context_ids],
            ))
            if session_bridge_rows:
                session_ids = {
                    str(node_id)
                    for row in session_bridge_rows
                    for node_id in (row["source_node_id"], row["target_node_id"])
                }
                missing_ids = [node_id for node_id in session_ids if node_id not in selected]
                if missing_ids:
                    placeholders = ", ".join("?" for _ in missing_ids)
                    node_rows.extend(
                        _dict_rows(conn.execute(
                            f"""
                            SELECT id, kind, label, session_id, attrs_json, first_seen_at, last_seen_at
                            FROM graph_nodes
                            WHERE id IN ({placeholders})
                            """,
                            missing_ids,
                        ))
                    )
                    selected_ids.extend(missing_ids)
                    selected.update(missing_ids)
                edge_rows.extend(session_bridge_rows)
                deduped = {}
                for row in edge_rows:
                    key = (
                        str(row["source_node_id"]),
                        str(row["target_node_id"]),
                        str(row["kind"]),
                        str(row.get("session_id") or ""),
                    )
                    existing = deduped.get(key)
                    if existing is None or float(row.get("weight") or 0) > float(existing.get("weight") or 0):
                        deduped[key] = row
                edge_rows = list(deduped.values())

    from reflect.store.graph_ontology import virtual_tool_graph
    virtual_nodes, virtual_edges = virtual_tool_graph(conn, graph_scoped_ids)
    for node in virtual_nodes:
        if node["id"] not in selected:
            node_rows.append(node)
            selected.add(node["id"])
    edge_rows.extend(virtual_edges)

    deduped_edges: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in edge_rows:
        key = (
            str(row["source_node_id"]),
            str(row["target_node_id"]),
            str(row["kind"]),
            str(row.get("session_id") or ""),
        )
        existing = deduped_edges.get(key)
        if existing is None or float(row.get("weight") or 0) > float(existing.get("weight") or 0):
            deduped_edges[key] = row
    edge_rows = list(deduped_edges.values())

    colors = {
        "Agent": "#ffb156",
        "Session": "#f5f2ea",
        "Workspace": "#f28a1a",
        "Execution": "#F28A1A",
        "Tool": "#48d597",
        "ToolCall": "#2fa66f",
        "MCPServer": "#76b7ff",
        "Memory": "#ff8d7d",
        "Skill": "#c79cff",
        "Subagent": "#ffd166",
        "Repo": "#b7e4c7",
        "Spec": "#f97316",
        "Folder": "#64c7a1",
        "Path": "#95d5b2",
        "Outcome": "#ff6b6b",
    }
    edge_rows = [
        row for row in edge_rows
        if str(row["source_node_id"]) in selected and str(row["target_node_id"]) in selected
    ]
    if edge_rows:
        connected_ids = {
            node_id
            for row in edge_rows
            for node_id in (str(row["source_node_id"]), str(row["target_node_id"]))
        }
        node_rows = [row for row in node_rows if str(row["id"]) in connected_ids]
        edge_rows = [
            row for row in edge_rows
            if str(row["source_node_id"]) in connected_ids and str(row["target_node_id"]) in connected_ids
        ]
    if graph_scoped_ids is not None:
        session_roots = {
            str(row["id"])
            for row in node_rows
            if str(row["kind"] or "") == "Session"
            and str(row["session_id"] or "") in set(graph_scoped_ids)
        }
        adjacency: dict[str, set[str]] = {}
        for row in edge_rows:
            source = str(row["source_node_id"])
            target = str(row["target_node_id"])
            adjacency.setdefault(source, set()).add(target)
            adjacency.setdefault(target, set()).add(source)
        reachable = set(session_roots)
        frontier = list(session_roots)
        while frontier:
            current = frontier.pop()
            for neighbor in adjacency.get(current, set()):
                if neighbor in reachable:
                    continue
                reachable.add(neighbor)
                frontier.append(neighbor)
        node_rows = [row for row in node_rows if str(row["id"]) in reachable]
        edge_rows = [
            row for row in edge_rows
            if str(row["source_node_id"]) in reachable and str(row["target_node_id"]) in reachable
        ]
    nodes = []
    for row in node_rows:
        attrs = _load_json_dict(str(row.get("attrs_json") or ""))
        kind = str(row["kind"] or "")
        label = str(row["label"] or "")
        nodes.append({
            "id": str(row["id"]),
            "kind": kind,
            "label": label,
            "session_id": str(row["session_id"] or ""),
            "color": colors.get(kind, "#d7d1c6"),
            "size": min(18, 5 + len(label) / 12),
            "attrs": attrs,
            "first_seen_at": str(row["first_seen_at"] or ""),
            "last_seen_at": str(row["last_seen_at"] or ""),
        })
    kinds = {node["kind"] for node in nodes}
    return {
        "nodes": nodes,
        "edges": [
            {
                "source": str(row["source_node_id"]),
                "target": str(row["target_node_id"]),
                "kind": str(row["kind"] or ""),
                "session_id": str(row["session_id"] or ""),
                "weight": float(row["weight"] or 1.0),
                "attrs": _load_json_dict(str(row.get("attrs_json") or "")),
                "first_seen_at": str(row["first_seen_at"] or ""),
                "last_seen_at": str(row["last_seen_at"] or ""),
            }
            for row in edge_rows
        ],
        "sessions": sorted({str(node["session_id"]) for node in nodes if node["session_id"]}),
        "legend": [{"kind": kind, "color": colors.get(kind, "#d7d1c6")} for kind in sorted(kinds)],
    }


def _workspace_peer_session_ids(
    conn: sqlite3.Connection,
    scoped_ids: list[str] | None,
) -> list[str] | None:
    """Expand a semantic-graph session scope to every canonical workspace peer."""
    if scoped_ids is None or not scoped_ids:
        return scoped_ids
    placeholders = ", ".join("?" for _ in scoped_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT peer.id
        FROM sessions selected
        JOIN sessions peer
          ON peer.workspace_id = selected.workspace_id
        WHERE selected.id IN ({placeholders})
          AND COALESCE(selected.workspace_id, '') <> ''
        ORDER BY peer.id
        """,
        scoped_ids,
    )
    return sorted({*scoped_ids, *(str(row[0]) for row in rows)})


def _cooccurrence_matrix(conn: sqlite3.Connection, scoped_ids: list[str] | None, co_tools: list[str]) -> list[list[int]]:
    matrix = [[0 for _ in co_tools] for _ in co_tools]
    if not co_tools:
        return matrix
    filters: list[str] = []
    params: list[str] = []
    if scoped_ids is not None:
        if not scoped_ids:
            return matrix
        filters.append(f"session_id IN ({', '.join('?' for _ in scoped_ids)})")
        params.extend(scoped_ids)
    filters.append(f"tool_name IN ({', '.join('?' for _ in co_tools)})")
    params.extend(co_tools)
    rows = _dict_rows(conn.execute(
        f"""
        WITH session_tools AS (
          SELECT DISTINCT session_id, tool_name
          FROM tool_calls
          WHERE {' AND '.join(filters)}
        )
        SELECT a.tool_name AS tool_a, b.tool_name AS tool_b, COUNT(*) AS sessions
        FROM session_tools a
        JOIN session_tools b ON b.session_id = a.session_id AND b.tool_name <> a.tool_name
        GROUP BY a.tool_name, b.tool_name
        """,
        params,
    ))
    index = {tool: pos for pos, tool in enumerate(co_tools)}
    for row in rows:
        if row["tool_a"] in index and row["tool_b"] in index:
            matrix[index[row["tool_a"]]][index[row["tool_b"]]] = int(row["sessions"] or 0)
    return matrix


def _timeline(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> list[dict[str, Any]]:
    scope, params = _scope_clause("session_id", scoped_ids)
    sessions = _dict_rows(conn.execute(
        f"""
        SELECT session_id
        FROM session_rollups
        {scope}
        ORDER BY tool_call_count DESC, started_at DESC
        LIMIT 6
        """,
        params,
    ))
    timeline = []
    for session in sessions:
        spans = _dict_rows(conn.execute(
            """
            SELECT tc.tool_name, COALESCE(tc.duration_ms, st.duration_ms, 1) AS duration_ms, tc.status
            FROM tool_calls tc
            JOIN steps st ON st.id = tc.step_id
            WHERE tc.session_id = ?
            ORDER BY st.seq, tc.id
            LIMIT 500
            """,
            (session["session_id"],),
        ))
        timeline.append({
            "session": session["session_id"],
            "spans": [
                {
                    "tool": span["tool_name"],
                    "t": index * 1000,
                    "dur": int(span["duration_ms"] or 1),
                    "ok": span["status"] != "error",
                }
                for index, span in enumerate(spans)
            ],
        })
    return timeline


def _dependency_graph(
    conn: sqlite3.Connection,
    scoped_ids: list[str] | None,
    tools_by_count: dict[str, int],
    mcp_servers_by_count: dict[str, int],
) -> dict[str, Any]:
    scope, params = _scope_clause("tc.session_id", scoped_ids)
    rows = _dict_rows(conn.execute(
        f"""
        SELECT COALESCE(NULLIF(sr.agent, ''), 'unknown') AS agent, tc.tool_name, COUNT(*) AS count
        FROM tool_calls tc
        LEFT JOIN session_rollups sr ON sr.session_id = tc.session_id
        {scope}
        GROUP BY COALESCE(NULLIF(sr.agent, ''), 'unknown'), tc.tool_name
        ORDER BY count DESC
        LIMIT 80
        """,
        params,
    ))
    mcp_filters = ["mc.server_name IS NOT NULL", "mc.server_name <> ''"]
    mcp_params: list[str] = []
    if scoped_ids is not None:
        if scoped_ids:
            mcp_filters.append(f"tc.session_id IN ({', '.join('?' for _ in scoped_ids)})")
            mcp_params.extend(scoped_ids)
        else:
            mcp_filters.append("1 = 0")
    mcp_rows = _dict_rows(conn.execute(
        f"""
        SELECT
          COALESCE(NULLIF(sr.agent, ''), 'unknown') AS agent,
          mc.server_name,
          COUNT(*) AS count
        FROM mcp_calls AS mc
        JOIN tool_calls AS tc ON tc.id = mc.tool_call_id
        LEFT JOIN session_rollups sr ON sr.session_id = tc.session_id
        WHERE {' AND '.join(mcp_filters)}
        GROUP BY COALESCE(NULLIF(sr.agent, ''), 'unknown'), mc.server_name
        ORDER BY count DESC
        """,
        mcp_params,
    ))
    nodes: dict[str, dict[str, Any]] = {}
    links: list[dict[str, Any]] = []
    for row in rows:
        agent = str(row["agent"] or "unknown")
        tool = str(row["tool_name"] or "")
        if not tool:
            continue
        agent_id = f"agent:{agent}"
        tool_id = f"tool:{tool}"
        nodes.setdefault(agent_id, {"id": agent_id, "label": agent, "type": "agent", "value": 0})
        nodes.setdefault(tool_id, {"id": tool_id, "label": tool, "type": "tool", "value": int(tools_by_count.get(tool, 0))})
        nodes[agent_id]["value"] += int(row["count"] or 0)
        links.append({"source": agent_id, "target": tool_id, "value": int(row["count"] or 0)})
    for server, count in mcp_servers_by_count.items():
        server_id = f"mcp_server:{server}"
        nodes.setdefault(server_id, {"id": server_id, "label": server, "type": "mcp_server", "value": count})
        tool_id = f"mcp_tool:{server}"
        nodes.setdefault(tool_id, {"id": tool_id, "label": server, "type": "mcp_tool", "value": count})
        links.append({"source": tool_id, "target": server_id, "value": count})
    for row in mcp_rows:
        agent = str(row["agent"] or "unknown")
        server = display_mcp_server_name(row["server_name"])
        count = int(row["count"] or 0)
        if not server or count <= 0:
            continue
        agent_id = f"agent:{agent}"
        tool_id = f"mcp_tool:{server}"
        nodes.setdefault(agent_id, {"id": agent_id, "label": agent, "type": "agent", "value": 0})
        nodes.setdefault(tool_id, {"id": tool_id, "label": server, "type": "mcp_tool", "value": count})
        nodes[agent_id]["value"] += count
        links.append({"source": agent_id, "target": tool_id, "value": count})
    return {
        "nodes": list(nodes.values()),
        "links": links,
        "isolated_agents": [],
        "top_mcp_servers": [{"server": server, "count": count} for server, count in mcp_servers_by_count.items()],
    }


def _scoped_spec_filter(scoped_ids: list[str] | None) -> tuple[str, list[str]]:
    if scoped_ids is None:
        return "", []
    if not scoped_ids:
        return "WHERE 1 = 0", []
    placeholders = ", ".join("?" for _ in scoped_ids)
    return (
        f"""
        WHERE s.id IN (
          SELECT r.spec_id
          FROM requirements r
          JOIN evidence e ON e.requirement_id = r.id
          WHERE e.session_id IN ({placeholders})
          UNION
          SELECT m.spec_id
          FROM memories m
          WHERE m.spec_id IS NOT NULL
            AND (
              m.session_id IN ({placeholders})
              OR EXISTS (
                SELECT 1 FROM memory_exposures me
                WHERE me.memory_id = m.id
                  AND me.session_id IN ({placeholders})
              )
            )
          UNION
          SELECT tr.task_contract_id
          FROM mcp_task_runs tr
          WHERE tr.runtime_session_id IN ({placeholders})
            AND tr.task_contract_id IS NOT NULL
        )
        """,
        [*scoped_ids, *scoped_ids, *scoped_ids, *scoped_ids],
    )


def _build_specs(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> SpecsViewModel:
    spec_where, spec_params = _scoped_spec_filter(scoped_ids)
    specs = _dict_rows(conn.execute(
        f"""
        SELECT
          s.id,
          s.title,
          s.status,
          s.owner,
          s.source_path,
          s.updated_at,
          COUNT(DISTINCT r.id) AS requirement_count,
          COUNT(DISTINCT e.id) AS evidence_count,
          COALESCE(AVG(r.confidence), 0) AS avg_confidence
        FROM specs s
        LEFT JOIN requirements r ON r.spec_id = s.id
        LEFT JOIN evidence e ON e.requirement_id = r.id
        {spec_where}
        GROUP BY s.id
        ORDER BY s.updated_at DESC, s.title ASC
        LIMIT 100
        """,
        spec_params,
    ))
    cursor_plan_where, cursor_plan_params = _cursor_plan_scope_clause(scoped_ids)
    cursor_plan_rows = _dict_rows(conn.execute(
        f"""
        SELECT
          id,
          content_preview_redacted,
          confidence,
          source,
          last_seen_at,
          raw_attrs_json
        FROM memories
        {cursor_plan_where}
        ORDER BY COALESCE(last_seen_at, updated_at, created_at) DESC, id ASC
        LIMIT 100
        """,
        cursor_plan_params,
    ))
    status_rows = _dict_rows(conn.execute(
        f"""
        SELECT s.status, COUNT(*) AS count
        FROM specs s
        {spec_where}
        GROUP BY s.status
        ORDER BY count DESC, s.status ASC
        """,
        spec_params,
    ))
    plan_status_rows = _dict_rows(conn.execute(
        f"""
        SELECT 'plan' AS status, COUNT(*) AS count
        FROM memories
        {cursor_plan_where}
        HAVING COUNT(*) > 0
        """,
        cursor_plan_params,
    ))
    req_rows = _dict_rows(conn.execute(
        f"""
        SELECT r.status, COUNT(*) AS count
        FROM requirements r
        JOIN specs s ON s.id = r.spec_id
        {spec_where}
        GROUP BY r.status
        ORDER BY count DESC, r.status ASC
        """,
        spec_params,
    ))
    evidence_rows = _dict_rows(conn.execute(
        f"""
        SELECT e.kind, COUNT(*) AS count
        FROM evidence e
        JOIN requirements r ON r.id = e.requirement_id
        JOIN specs s ON s.id = r.spec_id
        {spec_where}
        GROUP BY e.kind
        ORDER BY count DESC, e.kind ASC
        """,
        spec_params,
    ))
    specs_by_status = _counter([*status_rows, *plan_status_rows], "status", "count")
    spec_items = [
        {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "owner": row["owner"] or "",
            "source_path": row["source_path"] or "",
            "updated_at": row["updated_at"],
            "requirements": int(row["requirement_count"] or 0),
            "evidence": int(row["evidence_count"] or 0),
            "avg_confidence": float(row["avg_confidence"] or 0),
        }
        for row in specs
    ]
    for row in cursor_plan_rows:
        try:
            attrs = json.loads(str(row["raw_attrs_json"] or "{}"))
        except json.JSONDecodeError:
            attrs = {}
        source_path = ""
        title = row["content_preview_redacted"] or row["id"]
        if isinstance(attrs, dict):
            source_path = str(attrs.get("path") or "")
            name = str(attrs.get("name") or "")
            if name:
                title = name.removesuffix(".plan.md") or name
        spec_items.append(
            {
                "id": row["id"],
                "title": title,
                "status": "plan",
                "owner": "cursor",
                "source_path": source_path,
                "updated_at": row["last_seen_at"] or "",
                "requirements": 0,
                "evidence": 0,
                "avg_confidence": float(row["confidence"] or 0),
            }
        )
    spec_items.sort(key=lambda item: (str(item["updated_at"] or ""), str(item["title"] or "")), reverse=True)
    return SpecsViewModel(
        total_specs=sum(specs_by_status.values()),
        specs_by_status=specs_by_status,
        requirements_by_status=_counter(req_rows, "status", "count"),
        evidence_by_kind=_counter(evidence_rows, "kind", "count"),
        specs=spec_items[:100],
    )


def _build_memory(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> MemoryViewModel:
    scope, params = _memory_scope_clause(scoped_ids)
    rows = _dict_rows(conn.execute(
        f"""
        SELECT id, scope, type, sensitivity, source, confidence,
               content_preview_redacted, last_seen_at, session_id,
               (
                 SELECT COUNT(*)
                 FROM memory_exposures me
                 WHERE me.memory_id = memories.id
               ) AS exposure_count
        FROM memories
        {scope}
        ORDER BY COALESCE(last_seen_at, updated_at, created_at) DESC, id ASC
        LIMIT 100
        """,
        params,
    ))
    return MemoryViewModel(
        total_memories=int(
            conn.execute(
                f"SELECT COUNT(*) FROM memories {scope}",
                params,
            ).fetchone()[0]
            or 0
        ),
        memories_by_scope=_counter(
            _dict_rows(
                conn.execute(
                    f"SELECT scope, COUNT(*) AS count FROM memories {scope} GROUP BY scope ORDER BY count DESC, scope ASC",
                    params,
                )
            ),
            "scope",
            "count",
        ),
        memories_by_type=_counter(
            _dict_rows(
                conn.execute(
                    f"SELECT type, COUNT(*) AS count FROM memories {scope} GROUP BY type ORDER BY count DESC, type ASC",
                    params,
                )
            ),
            "type",
            "count",
        ),
        memories_by_sensitivity=_counter(
            _dict_rows(
                conn.execute(
                    f"SELECT sensitivity, COUNT(*) AS count FROM memories {scope} GROUP BY sensitivity ORDER BY count DESC, sensitivity ASC",
                    params,
                )
            ),
            "sensitivity",
            "count",
        ),
        memories_by_source=_counter(
            _dict_rows(
                conn.execute(
                    f"SELECT source, COUNT(*) AS count FROM memories {scope} GROUP BY source ORDER BY count DESC, source ASC",
                    params,
                )
            ),
            "source",
            "count",
        ),
        recent_memories=[
            {
                "id": row["id"],
                "scope": row["scope"],
                "type": row["type"],
                "sensitivity": row["sensitivity"],
                "source": row["source"],
                "confidence": float(row["confidence"] or 0),
                "preview": row["content_preview_redacted"] or "",
                "exposure_count": int(row["exposure_count"] or 0),
                "last_seen_at": row["last_seen_at"] or "",
                "session_id": row["session_id"] or "",
            }
            for row in rows
        ],
    )


def _build_privacy(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> PrivacyViewModel:
    scope, params = _scope_clause("session_id", scoped_ids)
    rows = _dict_rows(conn.execute(
        f"""
        SELECT id, session_id, finding_type, severity, field_name, action_taken, detail_redacted, created_at
        FROM privacy_findings
        {scope}
        ORDER BY created_at DESC, id ASC
        LIMIT 100
        """,
        params,
    ))
    return PrivacyViewModel(
        total_findings=_count_rows(conn, "privacy_findings", "session_id", scoped_ids),
        findings_by_type=_count_by(conn, "privacy_findings", "finding_type", "session_id", scoped_ids),
        findings_by_severity=_count_by(conn, "privacy_findings", "severity", "session_id", scoped_ids),
        findings_by_action=_count_by(conn, "privacy_findings", "action_taken", "session_id", scoped_ids),
        recent_findings=[
            {
                "id": row["id"],
                "session_id": row["session_id"] or "",
                "type": row["finding_type"],
                "severity": row["severity"],
                "field_name": row["field_name"] or "",
                "action_taken": row["action_taken"],
                "detail": row["detail_redacted"] or "",
                "created_at": row["created_at"],
            }
            for row in rows
        ],
    )


def _build_exports(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> ExportsViewModel:
    scoped = scoped_ids is not None
    row_counts = {
        "sessions": _count_rows(conn, "sessions", "id", scoped_ids),
        "steps": _count_rows(conn, "steps", "session_id", scoped_ids),
        "llm_calls": _count_rows(conn, "llm_calls", "session_id", scoped_ids),
        "tool_calls": _count_rows(conn, "tool_calls", "session_id", scoped_ids),
        "mcp_calls": _count_mcp_calls(conn, scoped_ids),
        "memories": _count_memories(conn, scoped_ids),
        "privacy_findings": _count_rows(conn, "privacy_findings", "session_id", scoped_ids),
        "evidence": _count_evidence(conn, scoped_ids),
        "specs": _count_specs(conn, scoped_ids),
    }
    return ExportsViewModel(
        export_ready=any(count > 0 for count in row_counts.values()),
        available_formats=["json"],
        row_counts=row_counts,
        scoped=scoped,
    )


def _count_rows(conn: sqlite3.Connection, table: str, scope_column: str, scoped_ids: list[str] | None) -> int:
    scope, params = _scope_clause(scope_column, scoped_ids)
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} {scope}", params).fetchone()[0] or 0)


def _count_mcp_calls(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> int:
    scope, params = _scope_clause("tc.session_id", scoped_ids)
    return int(conn.execute(
        f"""
        SELECT COUNT(*)
        FROM mcp_calls AS mc
        JOIN tool_calls AS tc ON tc.id = mc.tool_call_id
        {scope}
        """,
        params,
    ).fetchone()[0] or 0)


def _count_by(
    conn: sqlite3.Connection,
    table: str,
    group_column: str,
    scope_column: str,
    scoped_ids: list[str] | None,
) -> dict[str, int]:
    scope, params = _scope_clause(scope_column, scoped_ids)
    rows = _dict_rows(conn.execute(
        f"""
        SELECT {group_column} AS name, COUNT(*) AS count
        FROM {table}
        {scope}
        GROUP BY {group_column}
        ORDER BY count DESC, name ASC
        """,
        params,
    ))
    return _counter(rows, "name", "count")


def _count_specs(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> int:
    spec_where, spec_params = _scoped_spec_filter(scoped_ids)
    return int(conn.execute(f"SELECT COUNT(*) FROM specs s {spec_where}", spec_params).fetchone()[0] or 0)


def _count_memories(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> int:
    scope, params = _artifact_memory_scope_clause(scoped_ids, type_filter="1 = 1")
    return int(conn.execute(f"SELECT COUNT(*) FROM memories {scope}", params).fetchone()[0] or 0)


def _count_evidence(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> int:
    if scoped_ids is None:
        return int(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] or 0)
    if not scoped_ids:
        return 0
    placeholders = ", ".join("?" for _ in scoped_ids)
    return int(conn.execute(
        f"SELECT COUNT(*) FROM evidence WHERE session_id IN ({placeholders})",
        scoped_ids,
    ).fetchone()[0] or 0)
