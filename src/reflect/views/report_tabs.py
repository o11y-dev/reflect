from __future__ import annotations

import json
import re
import shlex
import sqlite3
from collections import Counter
from typing import Any
from urllib.parse import urlparse

from reflect.schema.base import ReflectModel


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


class ToolsViewModel(ReflectModel):
    tools_by_count: dict[str, int]
    tool_percentiles: list[dict[str, Any]]
    skills_by_count: dict[str, int]
    subagent_types_by_count: dict[str, int]
    subagent_stops_by_type: dict[str, int]
    subagent_launches: int
    subagent_total_starts: int
    subagent_total_stops: int
    top_commands: list[dict[str, Any]]
    unique_commands: int
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


class AgentsViewModel(ReflectModel):
    agent_comparison: list[dict[str, Any]]
    agents: dict[str, dict[str, Any]]


class GraphsViewModel(ReflectModel):
    graph_tool_transitions: list[dict[str, Any]]
    graph_cooccurrence: dict[str, Any]
    graph_dep: dict[str, Any]
    graph_session_timeline: list[dict[str, Any]]


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
    graphs: GraphsViewModel
    specs: SpecsViewModel
    memory: MemoryViewModel
    privacy: PrivacyViewModel
    exports: ExportsViewModel


def build_report_tabs(conn: sqlite3.Connection, *, session_ids: set[str] | None = None) -> ReportTabsViewModel:
    """Build SQL-backed view models for browser report tabs beyond Overview/Sessions."""
    scoped_ids = sorted(session_ids or [])
    scoped = scoped_ids if session_ids is not None else None
    skill_subagent = _skill_subagent_counts(conn, scoped)
    activity = _build_activity(conn, scoped)
    models, costs = _build_models_and_costs(conn, scoped)
    tools = _build_tools(conn, scoped, skill_subagent)
    mcp = _build_mcp(conn, scoped)
    agents = _build_agents(conn, scoped, skill_subagent)
    graphs = _build_graphs(conn, scoped, tools.tools_by_count, mcp.mcp_servers_by_count)
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
        graphs=graphs,
        specs=specs,
        memory=memory,
        privacy=privacy,
        exports=exports,
    )


def _scope_clause(column: str, scoped_ids: list[str] | None, *, prefix: str = "WHERE") -> tuple[str, list[str]]:
    if scoped_ids is None:
        return "", []
    if not scoped_ids:
        return f"{prefix} 1 = 0", []
    return f"{prefix} {column} IN ({', '.join('?' for _ in scoped_ids)})", scoped_ids


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
    event_rows = _dict_rows(conn.execute(
        f"""
        SELECT type, COUNT(*) AS event_count
        FROM steps
        {steps_scope}
        GROUP BY type
        ORDER BY event_count DESC, type ASC
        """,
        steps_params,
    ))
    by_hour = {str(hour): 0 for hour in range(24)}
    by_hour.update({str(row["hour"]): int(row["event_count"] or 0) for row in hour_rows if row.get("hour") is not None})
    peak_hour = max(range(24), key=lambda hour: by_hour.get(str(hour), 0)) if hour_rows else -1
    return ActivityViewModel(
        events_by_type=_counter(event_rows, "type", "event_count"),
        activity_by_day=_counter(daily_rows, "day", "event_count"),
        activity_by_hour=by_hour,
        peak_hour=peak_hour,
        peak_hour_count=by_hour.get(str(peak_hour), 0) if peak_hour >= 0 else 0,
    )


def _build_models_and_costs(
    conn: sqlite3.Connection,
    scoped_ids: list[str] | None,
) -> tuple[ModelsViewModel, CostsViewModel]:
    llm_scope, llm_params = _scope_clause("session_id", scoped_ids, prefix="AND")
    model_rows = _dict_rows(conn.execute(
        f"""
        SELECT
          COALESCE(NULLIF(response_model, ''), NULLIF(request_model, '')) AS model,
          COUNT(*) AS call_count,
          COALESCE(SUM(estimated_cost_usd), 0) AS total_cost,
          COALESCE(SUM(input_tokens), 0) AS input_tokens,
          COALESCE(SUM(output_tokens), 0) AS output_tokens,
          COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_tokens,
          COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_tokens
        FROM llm_calls
        WHERE COALESCE(NULLIF(response_model, ''), NULLIF(request_model, '')) IS NOT NULL
        {llm_scope}
        GROUP BY model
        ORDER BY call_count DESC, model ASC
        """,
        llm_params,
    ))
    rollup_scope, rollup_params = _scope_clause("session_id", scoped_ids)
    cache_totals = conn.execute(
        f"""
        SELECT
          COALESCE(SUM(cache_write_tokens), 0) AS cache_creation_tokens,
          COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens
        FROM session_rollups
        {rollup_scope}
        """,
        rollup_params,
    ).fetchone()
    model_counts = _counter(model_rows, "model", "call_count")
    model_costs = {str(row["model"]): float(row["total_cost"] or 0) for row in model_rows}
    cost_breakdown = _token_weighted_cost_breakdown(model_rows)
    return (
        ModelsViewModel(models_by_count=model_counts, unique_models=len(model_counts)),
        CostsViewModel(
            model_costs=model_costs,
            model_costs_usd=model_costs,
            cost_breakdown=cost_breakdown,
            total_cache_creation_tokens=int(cache_totals[0] or 0),
            total_cache_read_tokens=int(cache_totals[1] or 0),
        ),
    )


def _token_weighted_cost_breakdown(rows: list[dict[str, Any]]) -> dict[str, float]:
    totals = {
        "total_cost_usd": 0.0,
        "input_cost_usd": 0.0,
        "output_cost_usd": 0.0,
        "cache_creation_cost_usd": 0.0,
        "cache_read_cost_usd": 0.0,
    }
    for row in rows:
        estimated_cost = float(row["total_cost"] or 0)
        weights = {
            "input_cost_usd": int(row["input_tokens"] or 0),
            "output_cost_usd": int(row["output_tokens"] or 0),
            "cache_creation_cost_usd": int(row["cache_creation_tokens"] or 0),
            "cache_read_cost_usd": int(row["cache_read_tokens"] or 0),
        }
        total_weight = sum(weights.values()) or 1
        totals["total_cost_usd"] += estimated_cost
        for key, weight in weights.items():
            totals[key] += estimated_cost * weight / total_weight
    return totals


def _build_tools(
    conn: sqlite3.Connection,
    scoped_ids: list[str] | None,
    skill_subagent: dict[str, Any],
) -> ToolsViewModel:
    if scoped_ids is None:
        tool_rows = _dict_rows(conn.execute(
            """
            SELECT tool_name, COALESCE(SUM(call_count), 0) AS call_count
            FROM tool_rollups
            GROUP BY tool_name
            ORDER BY call_count DESC, tool_name ASC
            LIMIT 25
            """
        ))
    else:
        tool_scope, tool_params = _scope_clause("tc.session_id", scoped_ids)
        tool_rows = _dict_rows(conn.execute(
            f"""
            SELECT tc.tool_name, COUNT(*) AS call_count
            FROM tool_calls tc
            {tool_scope}
            GROUP BY tool_name
            ORDER BY call_count DESC, tool_name ASC
            LIMIT 25
            """,
            tool_params,
        ))
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
    commands = _command_patterns(conn, scoped_ids)
    top_commands = [{"command": command, "count": count} for command, count in commands.most_common(25)]
    signature = top_commands[0] if top_commands else {"command": "", "count": 0}
    file_counts = _file_counts(conn, scoped_ids)
    return ToolsViewModel(
        tools_by_count=_counter(tool_rows, "tool_name", "call_count"),
        tool_percentiles=percentiles,
        skills_by_count=dict(skill_subagent["skills"].most_common()),
        subagent_types_by_count=dict(skill_subagent["subagent_starts"].most_common()),
        subagent_stops_by_type=dict(skill_subagent["subagent_stops"].most_common()),
        subagent_launches=sum(skill_subagent["subagent_starts"].values()),
        subagent_total_starts=sum(skill_subagent["subagent_starts"].values()),
        subagent_total_stops=sum(skill_subagent["subagent_stops"].values()),
        top_commands=top_commands,
        unique_commands=len(commands),
        signature_command=str(signature["command"]),
        signature_command_count=int(signature["count"]),
        shell_executions=sum(commands.values()),
        file_edits=file_counts["edits"],
        file_reads=file_counts["reads"],
    )


def _skill_subagent_counts(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> dict[str, Any]:
    scope, params = _scope_clause("st.session_id", scoped_ids)
    rows = _dict_rows(conn.execute(
        f"""
        SELECT
          st.session_id,
          COALESCE(NULLIF(sr.agent, ''), 'unknown') AS agent,
          st.summary,
          st.raw_attrs_json
        FROM steps st
        LEFT JOIN session_rollups sr ON sr.session_id = st.session_id
        {scope}
        """,
        params,
    ))
    skills: Counter[str] = Counter()
    skills_by_agent: dict[str, Counter[str]] = {}
    subagent_starts: Counter[str] = Counter()
    subagent_stops: Counter[str] = Counter()
    subagents_by_agent: dict[str, Counter[str]] = {}
    for row in rows:
        try:
            attrs = json.loads(str(row["raw_attrs_json"] or "{}"))
        except json.JSONDecodeError:
            attrs = {}
        if not isinstance(attrs, dict):
            continue
        agent = str(row["agent"] or "unknown")
        event = str(_attr(attrs, "gen_ai.client.hook.event") or row["summary"] or "")
        event_lc = event.lower()
        subagent_type = str(_attr(attrs, "gen_ai.client.subagent_type", "subagent.type") or "")
        if subagent_type:
            if event == "SubagentStop" or "stop" in event_lc:
                subagent_stops[subagent_type] += 1
            else:
                subagent_starts[subagent_type] += 1
                subagents_by_agent.setdefault(agent, Counter())[subagent_type] += 1
        tool_name = str(_attr(attrs, "gen_ai.client.tool_name") or "")
        preview = str(_attr(attrs, "gen_ai.client.tool.input", "tool.input") or "")
        prompt_text = str(_attr(attrs, "gen_ai.client.prompt", "gen_ai.client.prompt.text", "prompt") or "")
        skill_names = set(_extract_skill_names_from_text(prompt_text))
        if tool_name == "skill":
            skill_name = _extract_skill_name_from_preview(preview)
            if skill_name:
                skill_names.add(skill_name)
        skill_names.update(_extract_skill_names_from_text(preview))
        for skill_name in sorted(skill_names):
            skills[skill_name] += 1
            skills_by_agent.setdefault(agent, Counter())[skill_name] += 1
        for subagent_name in sorted(_extract_subagent_names_from_text(prompt_text)):
            subagent_starts[subagent_name] += 1
            subagents_by_agent.setdefault(agent, Counter())[subagent_name] += 1
    return {
        "skills": skills,
        "skills_by_agent": skills_by_agent,
        "subagent_starts": subagent_starts,
        "subagent_stops": subagent_stops,
        "subagents_by_agent": subagents_by_agent,
    }


def _attr(attrs: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = attrs.get(key)
        if value not in (None, ""):
            return value
    return None


def _extract_skill_name_from_preview(preview: str) -> str:
    if not isinstance(preview, str) or not preview.strip():
        return ""
    try:
        payload = json.loads(preview)
    except json.JSONDecodeError:
        match = re.search(r'"skill"\s*:\s*"([^"]+)"', preview)
        return match.group(1).strip() if match else ""
    if isinstance(payload, dict):
        skill = payload.get("skill")
        if isinstance(skill, str):
            return skill.strip()
    return ""


def _extract_skill_names_from_text(text: str) -> set[str]:
    if not isinstance(text, str) or not text.strip():
        return set()
    names: set[str] = set()
    for match in re.finditer(r"(?<![:\w.-])/([A-Za-z0-9][A-Za-z0-9_-]{1,60})", text):
        name = match.group(1).strip().strip(".,;:)")
        lowered = name.lower()
        if "-" not in lowered and not lowered.endswith("skill") and lowered not in {"review", "investigate"}:
            continue
        names.add(name)
    for match in re.finditer(r"`([^`/\n]{2,80})`\s+skill\b", text, flags=re.IGNORECASE):
        names.add(match.group(1).strip())
    return {name for name in names if name}


def _extract_subagent_names_from_text(text: str) -> set[str]:
    if not isinstance(text, str) or not text.strip():
        return set()
    names: set[str] = set()
    for match in re.finditer(r"`([^`/\n]{2,80})`\s+subagent\b", text, flags=re.IGNORECASE):
        names.add(match.group(1).strip())
    for match in re.finditer(
        r"\b(?:use|run|invoke|launch|call)\s+(?:the\s+)?([A-Za-z0-9][A-Za-z0-9_-]{2,80})\s+subagent\b",
        text,
        flags=re.IGNORECASE,
    ):
        names.add(match.group(1).strip())
    return {name for name in names if name}


def _and_scope(column: str, scoped_ids: list[str] | None) -> str:
    if scoped_ids is None:
        return ""
    if not scoped_ids:
        return "AND 1 = 0"
    return f"AND {column} IN ({', '.join('?' for _ in scoped_ids)})"


def _command_patterns(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> Counter[str]:
    step_rows = _dict_rows(conn.execute(
        f"""
        SELECT summary, raw_attrs_json
        FROM steps
        WHERE (raw_attrs_json LIKE '%command%' OR type = 'shell_command')
        {_and_scope('session_id', scoped_ids)}
        """,
        scoped_ids or [],
    ))
    tool_rows = _dict_rows(conn.execute(
        f"""
        SELECT input_preview_redacted, raw_attrs_json
        FROM tool_calls tc
        WHERE (LOWER(tool_name) IN ('shell', 'bash', 'exec_command')
           OR raw_attrs_json LIKE '%command%'
           OR input_preview_redacted LIKE '%"cmd"%')
        {_and_scope('tc.session_id', scoped_ids)}
        """,
        scoped_ids or [],
    ))
    commands: Counter[str] = Counter()
    for row in step_rows:
        command = _extract_command(row["raw_attrs_json"], row["summary"])
        if command:
            commands[_sanitize_command(command)] += 1
    for row in tool_rows:
        command = _extract_command(row["raw_attrs_json"], row["input_preview_redacted"])
        if command:
            commands[_sanitize_command(command)] += 1
    return Counter({command: count for command, count in commands.items() if command})


def _extract_command(attrs_json: object, preview: object = "") -> str:
    import json

    attrs: dict[str, Any] = {}
    try:
        payload = json.loads(str(attrs_json or "{}"))
        if isinstance(payload, dict):
            attrs = payload
    except json.JSONDecodeError:
        attrs = {}
    for key in ("gen_ai.client.command", "ide.command", "command", "shell.command"):
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    text = str(preview or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text.splitlines()[0].strip()
    if isinstance(payload, dict):
        value = payload.get("cmd") or payload.get("command")
        if isinstance(value, str):
            return value.strip()
    return ""


def _sanitize_command(value: str) -> str:
    text = " ".join(value.strip().split())
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith("poetry run pytest") or " pytest" in lowered:
        return text[:120]
    if lowered.startswith("python"):
        return "python command"
    return text[:120]


def _file_counts(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> dict[str, int]:
    rows = _dict_rows(conn.execute(
        f"""
        SELECT type, summary, raw_attrs_json
        FROM steps
        WHERE raw_attrs_json IS NOT NULL
        {_and_scope('session_id', scoped_ids)}
        """,
        scoped_ids or [],
    ))
    reads = 0
    edits = 0
    for row in rows:
        text = f"{row.get('type') or ''} {row.get('summary') or ''} {row.get('raw_attrs_json') or ''}".lower()
        if "beforereadfile" in text or '"read"' in text or '"view"' in text:
            reads += 1
        if "afterfileedit" in text or '"edit"' in text or '"write"' in text or "apply_patch" in text:
            edits += 1
    return {"reads": reads, "edits": edits}


def _build_mcp(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> McpViewModel:
    mcp_scope, mcp_params = _scope_clause("session_id", scoped_ids, prefix="AND")
    rows = _dict_rows(conn.execute(
        f"""
        SELECT server_name, COUNT(*) AS call_count
        FROM mcp_calls
        WHERE server_name IS NOT NULL AND server_name <> ''
        {mcp_scope}
        GROUP BY server_name
        ORDER BY call_count DESC, server_name ASC
        """,
        mcp_params,
    ))
    counts: Counter[str] = Counter()
    for row in rows:
        server = _display_mcp_server_name(row["server_name"])
        if server:
            counts[server] += int(row["call_count"] or 0)
    raw_counts, raw_after_counts = _raw_mcp_counts(conn, scoped_ids)
    counts.update(raw_counts)
    after = raw_after_counts or raw_counts
    return McpViewModel(
        mcp_calls=sum(counts.values()),
        mcp_servers_by_count=dict(counts),
        mcp_server_before=dict(counts),
        mcp_server_after=dict(after),
    )


def _display_mcp_server_name(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "\n" in text:
        text = text.splitlines()[0].strip()
    lowered = text.lower()
    if lowered.startswith("npx "):
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()
        for part in parts[1:]:
            parsed = urlparse(part)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                return parsed.netloc
        for part in parts[1:]:
            if part.startswith("-"):
                continue
            return part.rsplit("/", 1)[-1] or "npx"
        return "npx"
    if lowered.startswith("docker run "):
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
            return part.rsplit("/", 1)[-1].split(":", 1)[0] or "docker"
        return "docker"
    return text


def _raw_mcp_counts(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> tuple[Counter[str], Counter[str]]:
    import json

    rows = _dict_rows(conn.execute(
        f"""
        SELECT summary, raw_attrs_json
        FROM steps
        WHERE raw_attrs_json LIKE '%mcp%'
        {_and_scope('session_id', scoped_ids)}
        """,
        scoped_ids or [],
    ))
    counts: Counter[str] = Counter()
    after_counts: Counter[str] = Counter()
    for row in rows:
        try:
            attrs = json.loads(str(row["raw_attrs_json"] or "{}"))
        except json.JSONDecodeError:
            attrs = {}
        if not isinstance(attrs, dict):
            continue
        server = _display_mcp_server_name(
            attrs.get("gen_ai.client.mcp_server")
            or attrs.get("gen_ai.mcp.server")
            or attrs.get("mcp.server")
            or attrs.get("mcp.server.name")
            or attrs.get("server.name")
        )
        if not server:
            continue
        counts[server] += 1
        event = str(attrs.get("gen_ai.client.hook.event") or row["summary"] or "").lower()
        if "after" in event:
            after_counts[server] += 1
    return counts, after_counts


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
          COALESCE(SUM(sr.input_tokens + sr.output_tokens), 0) AS tokens,
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
        FROM mcp_calls mc
        LEFT JOIN session_rollups sr ON sr.session_id = mc.session_id
        {_scope_clause('mc.session_id', scoped_ids)[0]}
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
            "input_tokens": int(row["tokens"] or 0),
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "total_cost_usd": total_cost,
            "top_model": "",
            "top_tools": dict(top_tools.get(name, Counter()).most_common(10)),
            "top_skills": dict(skill_subagent["skills_by_agent"].get(name, Counter()).most_common(10)),
            "percentiles": [],
        }
    return AgentsViewModel(agent_comparison=comparison, agents=agents)


def _build_graphs(
    conn: sqlite3.Connection,
    scoped_ids: list[str] | None,
    tools_by_count: dict[str, int],
    mcp_servers_by_count: dict[str, int],
) -> GraphsViewModel:
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
    return GraphsViewModel(
        graph_tool_transitions=[
            {"from": row["source"], "to": row["target"], "count": int(row["count"] or 0)}
            for row in transitions
        ],
        graph_cooccurrence={"tools": co_tools, "matrix": co_matrix},
        graph_dep=graph_dep,
        graph_session_timeline=timeline,
    )


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
            mcp_filters.append(f"mc.session_id IN ({', '.join('?' for _ in scoped_ids)})")
            mcp_params.extend(scoped_ids)
        else:
            mcp_filters.append("1 = 0")
    mcp_rows = _dict_rows(conn.execute(
        f"""
        SELECT
          COALESCE(NULLIF(sr.agent, ''), 'unknown') AS agent,
          mc.server_name,
          COUNT(*) AS count
        FROM mcp_calls mc
        LEFT JOIN session_rollups sr ON sr.session_id = mc.session_id
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
        server = _display_mcp_server_name(row["server_name"])
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
          WHERE m.session_id IN ({placeholders}) AND m.spec_id IS NOT NULL
        )
        """,
        [*scoped_ids, *scoped_ids],
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
    return SpecsViewModel(
        total_specs=_count_specs(conn, scoped_ids),
        specs_by_status=_counter(status_rows, "status", "count"),
        requirements_by_status=_counter(req_rows, "status", "count"),
        evidence_by_kind=_counter(evidence_rows, "kind", "count"),
        specs=[
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
        ],
    )


def _build_memory(conn: sqlite3.Connection, scoped_ids: list[str] | None) -> MemoryViewModel:
    scope, params = _scope_clause("session_id", scoped_ids)
    rows = _dict_rows(conn.execute(
        f"""
        SELECT id, scope, type, sensitivity, source, confidence, content_preview_redacted, last_seen_at, session_id
        FROM memories
        {scope}
        ORDER BY COALESCE(last_seen_at, updated_at, created_at) DESC, id ASC
        LIMIT 100
        """,
        params,
    ))
    return MemoryViewModel(
        total_memories=_count_rows(conn, "memories", "session_id", scoped_ids),
        memories_by_scope=_count_by(conn, "memories", "scope", "session_id", scoped_ids),
        memories_by_type=_count_by(conn, "memories", "type", "session_id", scoped_ids),
        memories_by_sensitivity=_count_by(conn, "memories", "sensitivity", "session_id", scoped_ids),
        memories_by_source=_count_by(conn, "memories", "source", "session_id", scoped_ids),
        recent_memories=[
            {
                "id": row["id"],
                "scope": row["scope"],
                "type": row["type"],
                "sensitivity": row["sensitivity"],
                "source": row["source"],
                "confidence": float(row["confidence"] or 0),
                "preview": row["content_preview_redacted"] or "",
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
        "mcp_calls": _count_rows(conn, "mcp_calls", "session_id", scoped_ids),
        "memories": _count_rows(conn, "memories", "session_id", scoped_ids),
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
