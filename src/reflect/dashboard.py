from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from reflect.graph import (
    _compute_dep_graph,
    _compute_latency_histograms,
    _compute_session_timeline,
    _compute_tool_cooccurrence,
    _compute_tool_transitions,
    _compute_weekly_trends,
)
from reflect.insights import (
    build_achievement_badges,
    build_observations,
    build_practical_examples,
    build_recommendations,
    build_strengths,
    compute_token_economy,
    compute_tool_percentiles,
)
from reflect.models import AgentStats, TelemetryStats
from reflect.utils import (
    _json_dumps,
    _safe_ratio,
    _sanitize_command_counter,
    _sanitize_command_display,
    logger,
)


def _rough_token_count(text: str) -> int:
    normalized = text.strip() if isinstance(text, str) else ""
    if not normalized:
        return 0
    return max(1, round(len(normalized) / 4))


def _estimate_cursor_tokens_from_native(file_path: Path) -> tuple[int, int]:
    import json as _json

    input_tokens = 0
    output_tokens = 0
    try:
        import orjson
        _loads = orjson.loads
    except ImportError:
        _loads = _json.loads

    try:
        with file_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = _loads(line)
                except (ValueError, _json.JSONDecodeError):
                    continue
                role = entry.get("role")
                content = entry.get("message", {}).get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        item.get("text", "") for item in content if isinstance(item, dict)
                    )
                text = str(content)
                if role == "user":
                    input_tokens += _rough_token_count(text)
                elif role == "assistant":
                    output_tokens += _rough_token_count(text)
    except OSError:
        return (0, 0)

    return (input_tokens, output_tokens)


def _cursor_estimate_note(has_full_transcript: bool = True) -> str:
    scope = "local Cursor transcript" if has_full_transcript else "available Cursor transcript preview"
    return (
        f"Token counts are estimated from the {scope} with a rough len(text)/4 heuristic "
        "because exact per-session usage is not present in local telemetry."
    )


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


def _session_row_id(session: dict) -> str:
    return str(session.get("full_id") or session.get("id") or "")


def _parse_session_created_at(session: dict) -> float:
    created = session.get("created_at")
    if not isinstance(created, str) or not created:
        return 0.0
    try:
        return datetime.strptime(created, "%Y-%m-%d %H:%M UTC").replace(tzinfo=UTC).timestamp() * 1000
    except ValueError:
        return 0.0


def _filter_dashboard_sessions(
    sessions: list[dict],
    *,
    q: str = "",
    agents: set[str] | None = None,
    model: str = "all",
    status: str = "all",
    range_name: str = "all",
) -> list[dict]:
    search_text = q.strip().lower()
    selected_agents = {agent for agent in (agents or set()) if agent}
    now_ms = datetime.now(UTC).timestamp() * 1000
    range_ms = (
        24 * 60 * 60 * 1000 if range_name == "24h"
        else 7 * 24 * 60 * 60 * 1000 if range_name == "7d"
        else 30 * 24 * 60 * 60 * 1000 if range_name == "30d"
        else 0
    )

    filtered: list[dict] = []
    for session in sessions:
        if selected_agents and (session.get("agent") or "") not in selected_agents:
            continue
        if model != "all" and (session.get("primary_model") or "") != model:
            continue
        if status == "completed" and not session.get("is_completed"):
            continue
        if status == "active" and session.get("is_completed"):
            continue
        if status == "recovered" and not (int(session.get("recovered_failures") or 0) > 0):
            continue
        if status == "failing" and not (int(session.get("failure_count") or 0) > 0):
            continue
        if range_ms > 0:
            created_ms = _parse_session_created_at(session)
            if not created_ms or (now_ms - created_ms) > range_ms:
                continue
        if search_text:
            haystack = " ".join([
                str(session.get("first_prompt") or ""),
                str(session.get("id") or ""),
                str(session.get("full_id") or ""),
                str(session.get("primary_model") or ""),
                str(session.get("agent") or ""),
            ]).lower()
            if search_text not in haystack:
                continue
        filtered.append(session)
    return filtered


def _build_filtered_stats(stats: TelemetryStats, sessions: list[dict]) -> TelemetryStats:
    selected_ids = {_session_row_id(session) for session in sessions if _session_row_id(session)}
    if selected_ids == set(stats.sessions_seen):
        return stats

    session_rows = {
        sid: session
        for session in sessions
        for sid in [_session_row_id(session)]
        if sid
    }
    session_events = {
        sid: int(stats.session_events.get(sid, session_rows[sid].get("event_count") or 0))
        for sid in selected_ids
    }
    session_models = {
        sid: Counter(stats.session_models.get(sid, {}))
        for sid in selected_ids
    }
    session_shell_commands = {
        sid: Counter(stats.session_shell_commands.get(sid, {}))
        for sid in selected_ids
    }
    session_conversation = {
        sid: list(stats.session_conversation.get(sid) or session_rows.get(sid, {}).get("conversation") or [])
        for sid in selected_ids
    }
    session_span_details = {
        sid: list(stats.session_span_details.get(sid, []))
        for sid in selected_ids
    }
    session_tool_seq = {
        sid: list(stats.session_tool_seq.get(sid, []))
        for sid in selected_ids
    }
    session_tokens = {
        sid: dict(stats.session_tokens.get(sid, {}))
        for sid in selected_ids
    }
    session_first_ts = {
        sid: stats.session_first_ts[sid]
        for sid in selected_ids
        if sid in stats.session_first_ts
    }
    session_source = {
        sid: stats.session_source[sid]
        for sid in selected_ids
        if sid in stats.session_source
    }
    session_quality_scores = {
        sid: float(stats.session_quality_scores.get(sid, session_rows.get(sid, {}).get("quality_score") or 0.0))
        for sid in selected_ids
    }
    session_goal_completed = {
        sid: bool(stats.session_goal_completed.get(sid, session_rows.get(sid, {}).get("is_completed")))
        for sid in selected_ids
    }
    session_recovered_failures = {
        sid: int(stats.session_recovered_failures.get(sid, session_rows.get(sid, {}).get("recovered_failures") or 0))
        for sid in selected_ids
    }
    session_tags = {
        sid: set(stats.session_tags.get(sid, set()))
        for sid in selected_ids
        if sid in stats.session_tags
    }

    models_by_count: Counter[str] = Counter()
    tools_by_count: Counter[str] = Counter()
    subagent_types: Counter[str] = Counter()
    mcp_servers: Counter[str] = Counter()
    shell_commands: Counter[str] = Counter()
    tool_durations_ms: dict[str, list[float]] = {}
    activity_by_day: Counter[str] = Counter()
    activity_by_hour: Counter[int] = Counter()
    model_by_day: dict[str, Counter[str]] = {}
    events_by_type: Counter[str] = Counter()
    mcp_server_before: Counter[str] = Counter()
    mcp_server_after: Counter[str] = Counter()
    subagent_stops_by_type: Counter[str] = Counter()
    agents: dict[str, AgentStats] = {}

    def ensure_agent(name: str) -> AgentStats:
        if name not in agents:
            agents[name] = AgentStats(name=name)
        return agents[name]

    for sid in selected_ids:
        session = session_rows.get(sid, {})
        agent_name = str(session.get("agent") or (session_source.get(sid) or ("unknown", ""))[0] or "unknown").lower()
        if agent_name == "unknown":
            for existing_agent, agent_stats in stats.agents.items():
                if sid in agent_stats.sessions_seen:
                    agent_name = existing_agent
                    break
        agent_stats = ensure_agent(agent_name)
        agent_stats.sessions_seen.add(sid)
        agent_stats.total_events += session_events.get(sid, 0)
        token_row = session_tokens.get(sid, {})
        agent_stats.total_input_tokens += int(token_row.get("input", session.get("input_tokens") or 0))
        agent_stats.total_output_tokens += int(token_row.get("output", session.get("output_tokens") or 0))
        agent_stats.total_cache_creation_tokens += int(token_row.get("cache_creation", session.get("cache_creation_tokens") or 0))
        agent_stats.total_cache_read_tokens += int(token_row.get("cache_read", session.get("cache_read_tokens") or 0))
        agent_stats.total_quality_score += session_quality_scores.get(sid, 0.0)
        if session_goal_completed.get(sid):
            agent_stats.completed_sessions += 1
        agent_stats.recovered_failures += session_recovered_failures.get(sid, 0)

        model_counter = session_models.get(sid, Counter())
        models_by_count.update(model_counter)
        agent_stats.models_by_count.update(model_counter)

        tool_counter: Counter[str] = Counter()
        for span in session_span_details.get(sid, []):
            tool_name = str(span.get("tool") or "").strip()
            if tool_name:
                tool_counter[tool_name] += 1
        tools_by_count.update(tool_counter)
        agent_stats.tools_by_count.update(tool_counter)
        read_count = sum(count for tool, count in tool_counter.items() if tool.lower() in {"read", "view"})
        edit_count = sum(count for tool, count in tool_counter.items() if tool.lower() in {"edit", "write", "apply_patch"})
        shell_count = sum(count for tool, count in tool_counter.items() if tool.lower() in {"bash", "shell"})
        if read_count:
            events_by_type["BeforeReadFile"] += read_count
            agent_stats.events_by_type["BeforeReadFile"] += read_count
        if edit_count:
            events_by_type["AfterFileEdit"] += edit_count
            agent_stats.events_by_type["AfterFileEdit"] += edit_count
        if shell_count:
            events_by_type["BeforeShellExecution"] += shell_count
            agent_stats.events_by_type["BeforeShellExecution"] += shell_count

        command_counter = session_shell_commands.get(sid, Counter())
        shell_commands.update(command_counter)

        for span in session_span_details.get(sid, []):
            event_name = str(span.get("event") or "")
            if event_name in {"PreToolUse", "BeforeShellExecution", "BeforeMCPExecution", "Stop", "SubagentStop", "SessionEnd"}:
                events_by_type[event_name] += 1
                agent_stats.events_by_type[event_name] += 1
            tool_name = str(span.get("tool") or "")
            duration = float(span.get("dur") or 0)
            if tool_name and duration > 0:
                tool_durations_ms.setdefault(tool_name, []).append(duration)
                agent_stats.tool_durations_ms.setdefault(tool_name, []).append(duration)

        model_day = ""
        for event in session_conversation.get(sid, []):
            event_type = event.get("type")
            ts_ms = int(event.get("ts") or 0)
            if ts_ms > 0:
                dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
                day_key = dt.strftime("%Y-%m-%d")
                activity_by_day[day_key] += 1
                activity_by_hour[dt.hour] += 1
                model_day = model_day or day_key
            if event_type == "prompt":
                events_by_type["UserPromptSubmit"] += 1
                agent_stats.events_by_type["UserPromptSubmit"] += 1
            elif event_type == "tool_result":
                result_event = "PostToolUse" if event.get("success") else "PostToolUseFailure"
                events_by_type[result_event] += 1
                agent_stats.events_by_type[result_event] += 1
            elif event_type == "subagent_start":
                subtype = str(event.get("subagent_type") or "unknown")
                events_by_type["SubagentStart"] += 1
                agent_stats.events_by_type["SubagentStart"] += 1
                subagent_types[subtype] += 1
                agent_stats.subagent_types[subtype] += 1
            elif event_type == "subagent_stop":
                subtype = str(event.get("subagent_type") or "unknown")
                subagent_stops_by_type[subtype] += 1
                agent_stats.events_by_type["SubagentStop"] += 1
            elif event_type == "mcp_call":
                server = str(event.get("server") or "")
                if server:
                    mcp_servers[server] += 1
                    mcp_server_before[server] += 1
                    agent_stats.mcp_servers[server] += 1
            elif event_type == "mcp_result":
                server = str(event.get("server") or "")
                events_by_type["AfterMCPExecution"] += 1
                agent_stats.events_by_type["AfterMCPExecution"] += 1
                if server:
                    mcp_server_after[server] += 1
        if not model_day:
            created_ms = _parse_session_created_at(session)
            if created_ms:
                model_day = datetime.fromtimestamp(created_ms / 1000, tz=UTC).strftime("%Y-%m-%d")
        if model_day:
            for model_name, count in model_counter.items():
                model_by_day.setdefault(model_day, Counter())[model_name] += int(count)

    total_input_tokens = sum(int((session_tokens.get(sid) or {}).get("input", 0)) for sid in selected_ids)
    total_output_tokens = sum(int((session_tokens.get(sid) or {}).get("output", 0)) for sid in selected_ids)
    total_cache_creation_tokens = sum(int((session_tokens.get(sid) or {}).get("cache_creation", 0)) for sid in selected_ids)
    total_cache_read_tokens = sum(int((session_tokens.get(sid) or {}).get("cache_read", 0)) for sid in selected_ids)
    if not any([total_input_tokens, total_output_tokens, total_cache_creation_tokens, total_cache_read_tokens]):
        total_input_tokens = sum(int(session_rows.get(sid, {}).get("input_tokens") or 0) for sid in selected_ids)
        total_output_tokens = sum(int(session_rows.get(sid, {}).get("output_tokens") or 0) for sid in selected_ids)
        total_cache_creation_tokens = sum(int(session_rows.get(sid, {}).get("cache_creation_tokens") or 0) for sid in selected_ids)
        total_cache_read_tokens = sum(int(session_rows.get(sid, {}).get("cache_read_tokens") or 0) for sid in selected_ids)

    first_ns = min((value for value in session_first_ts.values() if value), default=0)
    last_event_candidates: list[int] = []
    for sid in selected_ids:
        conv = session_conversation.get(sid, [])
        if conv:
            max_conv_ms = max(int(event.get("ts") or 0) for event in conv)
            if max_conv_ms:
                last_event_candidates.append(max_conv_ms * 1_000_000)
        spans = session_span_details.get(sid, [])
        if spans:
            last_event_candidates.append(max(int(span.get("t") or 0) for span in spans))
    last_ns = max(last_event_candidates, default=first_ns)

    first_event_ts = datetime.fromtimestamp(first_ns / 1e9, tz=UTC).strftime("%Y-%m-%d %H:%M UTC") if first_ns else ""
    last_event_ts = datetime.fromtimestamp(last_ns / 1e9, tz=UTC).strftime("%Y-%m-%d %H:%M UTC") if last_ns else ""

    return TelemetryStats(
        session_files=len(selected_ids),
        span_files=stats.span_files,
        total_events=sum(session_events.values()),
        events_by_type=events_by_type,
        events_by_file={},
        models_by_count=models_by_count,
        tools_by_count=tools_by_count,
        subagent_types=subagent_types,
        mcp_servers=mcp_servers,
        sessions_seen=selected_ids,
        session_events=session_events,
        session_models=session_models,
        session_first_ts=session_first_ts,
        tool_durations_ms=tool_durations_ms,
        activity_by_day=activity_by_day,
        activity_by_hour=activity_by_hour,
        model_by_day=model_by_day,
        shell_commands=shell_commands,
        session_shell_commands=session_shell_commands,
        agents=agents,
        session_tool_seq=session_tool_seq,
        session_span_details=session_span_details,
        first_event_ts=first_event_ts,
        last_event_ts=last_event_ts,
        days_active=len([day for day, count in activity_by_day.items() if count > 0]),
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_cache_creation_tokens=total_cache_creation_tokens,
        total_cache_read_tokens=total_cache_read_tokens,
        session_tokens=session_tokens,
        mcp_server_before=mcp_server_before,
        mcp_server_after=mcp_server_after,
        subagent_stops_by_type=subagent_stops_by_type,
        session_quality_scores=session_quality_scores,
        session_goal_completed=session_goal_completed,
        session_recovered_failures=session_recovered_failures,
        session_tags=session_tags,
        session_conversation=session_conversation,
        session_source=session_source,
    )


def _cohort_summary_from_dashboard_data(
    data: dict,
    *,
    label: str,
    agent_names: list[str] | None = None,
) -> dict:
    tools = data.get("tools_by_count") or {}
    commands = data.get("top_commands") or []
    total_tokens = int(data.get("total_input_tokens") or 0) + int(data.get("total_output_tokens") or 0)
    return {
        "label": label,
        "agents": list(agent_names or sorted((data.get("agents") or {}).keys())),
        "sessions": int(data.get("unique_sessions") or 0),
        "prompts": int(data.get("prompt_submits") or 0),
        "tool_calls": int(data.get("tool_calls") or 0),
        "avg_quality": float(data.get("avg_quality_score") or 0.0),
        "failure_rate_pct": float(data.get("failure_rate_pct") or 0.0),
        "tokens": total_tokens,
        "shell_runs": int(data.get("shell_executions") or 0),
        "mcp_calls": int(data.get("mcp_calls") or 0),
        "subagent_launches": int(data.get("subagent_launches") or 0),
        "top_tools": [{"tool": str(tool), "count": int(count)} for tool, count in list(tools.items())[:5]],
        "top_commands": [
            {"command": str(entry.get("command") or ""), "count": int(entry.get("count") or 0)}
            for entry in commands[:5]
            if entry.get("command")
        ],
    }


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


def _build_filtered_comparison_payload(
    stats: TelemetryStats,
    all_sessions: list[dict],
    primary_sessions: list[dict],
    *,
    q: str = "",
    model: str = "all",
    status: str = "all",
    range_name: str = "all",
    primary_stats: TelemetryStats | None = None,
    primary_data: dict | None = None,
) -> dict | None:
    primary_agent_names = sorted({
        str(session.get("agent") or "").lower()
        for session in primary_sessions
        if str(session.get("agent") or "").strip()
    })
    if not primary_agent_names:
        return None

    scoped_sessions = _filter_dashboard_sessions(
        all_sessions,
        q=q,
        model=model,
        status=status,
        range_name=range_name,
    )
    baseline_sessions = [
        session for session in scoped_sessions
        if str(session.get("agent") or "").lower() not in set(primary_agent_names)
    ]
    if not baseline_sessions:
        return None

    primary_stats = primary_stats or _build_filtered_stats(stats, primary_sessions)
    primary_data = primary_data or json.loads(_build_dashboard_json(primary_stats))
    baseline_stats = _build_filtered_stats(stats, baseline_sessions)
    baseline_data = json.loads(_build_dashboard_json(baseline_stats))

    primary_label = " + ".join(primary_agent_names)
    baseline_label = "All other agents in scope"
    baseline_agents = sorted(
        baseline_data.get("agent_comparison") or [],
        key=lambda item: (-int(item.get("sessions") or 0), str(item.get("name") or "")),
    )

    primary_summary = _cohort_summary_from_dashboard_data(
        primary_data,
        label=primary_label,
        agent_names=primary_agent_names,
    )
    baseline_summary = _cohort_summary_from_dashboard_data(
        baseline_data,
        label=baseline_label,
    )
    return {
        "mode": "cohort-vs-rest",
        "primary": primary_summary,
        "baseline": baseline_summary,
        "baseline_agents": baseline_agents,
        "deltas": {
            "sessions": _comparison_delta(primary_summary["sessions"], baseline_summary["sessions"]),
            "prompts": _comparison_delta(primary_summary["prompts"], baseline_summary["prompts"]),
            "tool_calls": _comparison_delta(primary_summary["tool_calls"], baseline_summary["tool_calls"]),
            "avg_quality": _comparison_delta(primary_summary["avg_quality"], baseline_summary["avg_quality"]),
            "failure_rate_pct": _comparison_delta(primary_summary["failure_rate_pct"], baseline_summary["failure_rate_pct"]),
            "tokens": _comparison_delta(primary_summary["tokens"], baseline_summary["tokens"]),
            "shell_runs": _comparison_delta(primary_summary["shell_runs"], baseline_summary["shell_runs"]),
            "mcp_calls": _comparison_delta(primary_summary["mcp_calls"], baseline_summary["mcp_calls"]),
            "subagent_launches": _comparison_delta(primary_summary["subagent_launches"], baseline_summary["subagent_launches"]),
        },
    }


def _default_otlp_trace_path() -> Path | None:
    from reflect.parsing import REFLECT_HOME, _canonical_otlp_traces_path

    canonical = _canonical_otlp_traces_path()
    if canonical.exists():
        return canonical
    fallback = REFLECT_HOME / "state" / "otel-traces.json"
    return fallback if fallback.exists() else None


def _default_otlp_log_path(otlp_traces_file: Path | None) -> Path | None:
    from reflect.parsing import REFLECT_HOME, _infer_otlp_logs_file

    candidate = _infer_otlp_logs_file(otlp_traces_file)
    if candidate and candidate.exists():
        return candidate
    fallback = REFLECT_HOME / "state" / "otel-logs.json"
    return fallback if fallback.exists() else None


def _telemetry_severity(
    severity_text: str | None,
    severity_number: int | None,
    body_text: str | None = None,
) -> str:
    if severity_text:
        return str(severity_text).upper()
    value = int(severity_number or 0)
    if value >= 21:
        return "FATAL"
    if value >= 17:
        return "ERROR"
    if value >= 13:
        return "WARN"
    if value >= 9:
        return "INFO"
    if value >= 5:
        return "DEBUG"
    body = str(body_text or "").lower()
    if "error" in body or "fail" in body:
        return "ERROR"
    if "warn" in body:
        return "WARN"
    if "info" in body:
        return "INFO"
    return "TRACE"


def _sanitize_telemetry_attrs(attrs: dict) -> dict:
    allowed_keys = {
        "service.name",
        "service.version",
        "gen_ai.client.name",
        "gen_ai.client.hook.event",
        "gen_ai.client.tool_name",
        "gen_ai.client.mcp_tool",
        "gen_ai.client.mcp_server",
        "gen_ai.request.model",
        "error.type",
        "error.message",
        "exception.type",
        "exception.message",
        "code.function",
        "code.filepath",
        "code.lineno",
    }
    safe: dict[str, object] = {}
    for key, value in attrs.items():
        lowered = key.lower()
        if key not in allowed_keys:
            continue
        if any(token in lowered for token in ("authorization", "token", "secret", "password", "cookie")):
            continue
        if isinstance(value, (bool, int, float)):
            safe[key] = value
            continue
        text = str(value).strip()
        if not text:
            continue
        safe[key] = text[:280] + ("…" if len(text) > 280 else "")
    return safe


def _telemetry_span_phase(attrs: dict) -> str:
    event = str(attrs.get("gen_ai.client.hook.event") or "").lower()
    tool_name = str(attrs.get("gen_ai.client.tool_name") or "").lower()
    mcp_tool = str(attrs.get("gen_ai.client.mcp_tool") or "").lower()
    if mcp_tool or "mcp" in event:
        return "mcp"
    if "subagent" in event:
        return "subagent"
    if tool_name in {"bash", "shell"} or "shell" in event:
        return "shell"
    if tool_name:
        return "tool"
    if event in {"userpromptsubmit", "stop"}:
        return "conversation"
    return "session"


def _load_session_telemetry(
    session_id: str,
    session_start_ns: int | None = None,
    otlp_traces_file: Path | None = None,
    otlp_logs_file: Path | None = None,
) -> dict:
    from reflect.parsing import _extract_session_id, _load_otlp_logs, _load_otlp_traces

    trace_path = otlp_traces_file or _default_otlp_trace_path()
    log_path = otlp_logs_file or _default_otlp_log_path(trace_path)

    raw_spans: list[dict] = []
    raw_logs: list[dict] = []
    anchor_candidates: list[int] = []

    if trace_path and trace_path.exists():
        for span in _load_otlp_traces(trace_path):
            attrs = span.get("attributes") or {}
            if _extract_session_id(attrs) != session_id:
                continue
            raw_spans.append(span)
            start_ns = int(span.get("start_time_ns", 0) or 0)
            end_ns = int(span.get("end_time_ns", 0) or 0)
            if start_ns:
                anchor_candidates.append(start_ns)
            if end_ns:
                anchor_candidates.append(end_ns)

    if log_path and log_path.exists():
        for record in _load_otlp_logs(log_path):
            attrs = record.get("attributes") or {}
            if _extract_session_id(attrs) != session_id:
                continue
            raw_logs.append(record)
            ts_ns = int(record.get("time_ns", 0) or 0)
            if ts_ns:
                anchor_candidates.append(ts_ns)

    raw_spans.sort(key=lambda span: int(span.get("start_time_ns", 0) or 0))
    raw_logs.sort(key=lambda record: int(record.get("time_ns", 0) or 0))

    earliest_candidate = min(anchor_candidates) if anchor_candidates else 0
    anchor_ns = min(
        [value for value in [session_start_ns or 0, earliest_candidate] if value > 0],
        default=0,
    )
    if not raw_spans and not raw_logs:
        return {
            "summary": {
                "spans": 0,
                "logs": 0,
                "errors": 0,
                "warnings": 0,
                "services": 0,
                "duration_ms": 0,
                "truncated_spans": 0,
                "truncated_logs": 0,
            },
            "spans": [],
            "logs": [],
            "warnings": [],
        }

    truncated_spans = max(0, len(raw_spans) - 400)
    truncated_logs = max(0, len(raw_logs) - 500)
    spans: list[dict] = []
    for span in raw_spans[:400]:
        attrs = span.get("attributes") or {}
        start_ns = int(span.get("start_time_ns", 0) or 0)
        end_ns = int(span.get("end_time_ns", 0) or 0)
        duration_ms = max(0.0, (end_ns - start_ns) / 1e6) if start_ns and end_ns else 0.0
        spans.append({
            "id": span.get("spanId", ""),
            "trace_id": span.get("traceId", ""),
            "parent_id": span.get("parentSpanId", ""),
            "name": span.get("name", ""),
            "event": attrs.get("gen_ai.client.hook.event", ""),
            "service": attrs.get("service.name", ""),
            "agent": attrs.get("gen_ai.client.name", ""),
            "tool_name": attrs.get("gen_ai.client.tool_name", ""),
            "mcp_tool": attrs.get("gen_ai.client.mcp_tool", ""),
            "mcp_server": attrs.get("gen_ai.client.mcp_server", ""),
            "model": attrs.get("gen_ai.request.model", ""),
            "phase": _telemetry_span_phase(attrs),
            "start_ns": start_ns,
            "end_ns": end_ns,
            "rel_ms": round((start_ns - anchor_ns) / 1e6, 1) if anchor_ns and start_ns else 0.0,
            "duration_ms": round(duration_ms, 1),
            "attrs": _sanitize_telemetry_attrs(attrs),
        })

    logs: list[dict] = []
    for record in raw_logs[:500]:
        attrs = record.get("attributes") or {}
        ts_ns = int(record.get("time_ns", 0) or 0)
        body = record.get("body")
        if isinstance(body, (dict, list)):
            body_text = json.dumps(body, default=str)
        else:
            body_text = str(body or "")
        logs.append({
            "trace_id": record.get("trace_id", ""),
            "span_id": record.get("span_id", ""),
            "service": attrs.get("service.name", ""),
            "agent": attrs.get("gen_ai.client.name", ""),
            "event": attrs.get("gen_ai.client.hook.event", ""),
            "tool_name": attrs.get("gen_ai.client.tool_name", ""),
            "mcp_tool": attrs.get("gen_ai.client.mcp_tool", ""),
            "mcp_server": attrs.get("gen_ai.client.mcp_server", ""),
            "severity": _telemetry_severity(
                str(record.get("severity_text") or ""),
                int(record.get("severity_number", 0) or 0),
                body_text,
            ),
            "time_ns": ts_ns,
            "rel_ms": round((ts_ns - anchor_ns) / 1e6, 1) if anchor_ns and ts_ns else 0.0,
            "body": body_text[:2000],
            "attrs": _sanitize_telemetry_attrs(attrs),
        })

    services = {
        service
        for service in [*(span.get("service", "") for span in spans), *(log.get("service", "") for log in logs)]
        if service
    }
    error_count = sum(1 for log in logs if log.get("severity") in {"ERROR", "FATAL"})
    warning_count = sum(1 for log in logs if log.get("severity") == "WARN")
    session_end_ns = max(anchor_candidates) if anchor_candidates else anchor_ns
    duration_ms = round((session_end_ns - anchor_ns) / 1e6, 1) if anchor_ns and session_end_ns else 0.0
    telemetry_warnings: list[str] = []
    if truncated_spans:
        telemetry_warnings.append(f"Showing first 400 of {len(raw_spans)} telemetry spans.")
    if truncated_logs:
        telemetry_warnings.append(f"Showing first 500 of {len(raw_logs)} telemetry logs.")

    return {
        "summary": {
            "spans": len(raw_spans),
            "logs": len(raw_logs),
            "errors": error_count,
            "warnings": warning_count,
            "services": len(services),
            "duration_ms": duration_ms,
            "anchor_ns": anchor_ns,
            "truncated_spans": truncated_spans,
            "truncated_logs": truncated_logs,
        },
        "spans": spans,
        "logs": logs,
        "warnings": telemetry_warnings,
    }


def _build_dashboard_json(stats: TelemetryStats) -> str:
    """Build the JSON data object that powers the HTML dashboard."""
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    failures = stats.events_by_type.get("PostToolUseFailure", 0)
    skills_by_count: Counter[str] = Counter()
    skills_by_agent: dict[str, Counter[str]] = {}

    # Activity by day — fill gaps for heatmap (last year)
    if stats.activity_by_day:
        all_days = sorted(stats.activity_by_day.keys())
        last_day = datetime.strptime(all_days[-1], "%Y-%m-%d")
        first_day = last_day - __import__("datetime").timedelta(days=364)
        activity_by_day = {}
        d = first_day
        while d <= last_day:
            key = d.strftime("%Y-%m-%d")
            activity_by_day[key] = stats.activity_by_day.get(key, 0)
            d += __import__("datetime").timedelta(days=1)
    else:
        activity_by_day = {}

    # Activity by hour (fill 0-23)
    activity_by_hour = {str(h): stats.activity_by_hour.get(h, 0) for h in range(24)}

    # Peak hour
    peak_hour = max(range(24), key=lambda h: stats.activity_by_hour.get(h, 0)) if stats.activity_by_hour else -1
    peak_hour_count = stats.activity_by_hour.get(peak_hour, 0) if peak_hour >= 0 else 0

    # Sessions with event counts and primary model
    sessions_list = []
    session_agents: dict[str, str] = {}
    for sid in sorted(stats.sessions_seen, key=lambda s: stats.session_events.get(s, 0), reverse=True):
        first_ts_ns = stats.session_first_ts.get(sid)
        created = ""
        if first_ts_ns:
            dt = datetime.fromtimestamp(first_ts_ns / 1e9, tz=UTC)
            created = dt.strftime("%Y-%m-%d %H:%M UTC")
        model_counter = stats.session_models.get(sid, Counter())
        primary_model = model_counter.most_common(1)[0][0] if model_counter else ""
        # Per-session tool breakdown and stats for comparison
        spans = stats.session_span_details.get(sid, [])
        tool_counter: Counter = Counter()
        failure_count = 0
        duration_ms = 0
        tool_dur_lists: dict[str, list[float]] = {}
        if spans:
            for sp in spans:
                tool_counter[sp["tool"]] += 1
                if not sp["ok"]:
                    failure_count += 1
                if sp["dur"] > 0:
                    tool_dur_lists.setdefault(sp["tool"], []).append(sp["dur"])
            ts_sorted = sorted(sp["t"] for sp in spans)
            duration_ms = round((ts_sorted[-1] - ts_sorted[0]) / 1e6)
        # Per-session p50 latency per tool (ms)
        tool_p50 = {
            tool: round(sorted(durs)[len(durs) // 2], 1)
            for tool, durs in tool_dur_lists.items()
            if durs
        }
        cmd_counter = _sanitize_command_counter(stats.session_shell_commands.get(sid, Counter()))
        tok = stats.session_tokens.get(sid, {})

        # New quality metrics
        quality_score = stats.session_quality_scores.get(sid, 0.0)
        is_completed = stats.session_goal_completed.get(sid, False)
        recovered = stats.session_recovered_failures.get(sid, 0)

        # Conversation events for session browser (capped at 500 per session)
        conv_events = stats.session_conversation.get(sid, [])[:500]
        # Extract first prompt preview for session card
        first_prompt = ""
        for ce in conv_events:
            if ce.get("type") == "prompt" and ce.get("preview"):
                first_prompt = ce["preview"]
                break
        # Agent name for this session
        source_info = stats.session_source.get(sid)
        agent_name = source_info[0] if source_info else ""
        if not agent_name:
            # Fall back to most common agent from spans
            for aname, ag in stats.agents.items():
                if sid in ag.sessions_seen:
                    agent_name = aname
                    break
        if agent_name:
            session_agents[sid] = agent_name
        skill_counter: Counter[str] = Counter()
        for event in stats.session_conversation.get(sid, []):
            if event.get("type") != "tool_call" or event.get("tool_name") != "skill":
                continue
            skill_name = _extract_skill_name_from_preview(str(event.get("preview", "")))
            if not skill_name:
                continue
            skill_counter[skill_name] += 1
            skills_by_count[skill_name] += 1
            if agent_name:
                skills_by_agent.setdefault(agent_name, Counter())[skill_name] += 1
        token_source = tok.get("source", "")
        token_note = tok.get("note", "")
        has_exact_tokens = any(
            tok.get(key, 0) for key in ("input", "output", "cache_creation", "cache_read")
        )
        if not token_source and has_exact_tokens:
            token_source = "local_telemetry"
        if agent_name == "cursor" and not has_exact_tokens:
            source_path = Path(source_info[1]) if source_info and len(source_info) > 1 else None
            estimated_in = estimated_out = 0
            if source_path and source_path.exists():
                estimated_in, estimated_out = _estimate_cursor_tokens_from_native(source_path)
            if estimated_in or estimated_out:
                tok = {
                    **tok,
                    "input": estimated_in,
                    "output": estimated_out,
                }
                token_source = "estimated_cursor_transcript"
                token_note = _cursor_estimate_note(has_full_transcript=True)
            else:
                token_source = "cursor_local_unavailable"
                token_note = (
                    "Exact per-session Cursor token usage is not present in local OTLP spans or "
                    "Cursor transcripts. Add provider-side usage context when available."
                )

        sessions_list.append({
            "id": sid[:10] + "...",
            "full_id": sid,
            "created_at": created,
            "event_count": stats.session_events.get(sid, 0),
            "primary_model": primary_model,
            "models": dict(model_counter.most_common()),
            "tools": dict(tool_counter.most_common(10)),
            "skills": dict(skill_counter.most_common(10)),
            "tool_p50": tool_p50,
            "commands": dict(cmd_counter.most_common(10)),
            "failure_count": failure_count,
            "duration_ms": duration_ms,
            "input_tokens": tok.get("input", 0),
            "output_tokens": tok.get("output", 0),
            "cache_creation_tokens": tok.get("cache_creation", 0),
            "cache_read_tokens": tok.get("cache_read", 0),
            "token_source": token_source,
            "token_note": token_note,
            "quality_score": quality_score,
            "is_completed": is_completed,
            "recovered_failures": recovered,
            "agent": agent_name,
            "first_prompt": first_prompt,
            "conversation": conv_events,
        })

    # Top commands
    display_shell_commands = _sanitize_command_counter(stats.shell_commands)
    display_tools_by_count = _sanitize_command_counter(stats.tools_by_count)
    display_tool_durations_ms: dict[str, list[float]] = {}
    for tool, durations in stats.tool_durations_ms.items():
        label = _sanitize_command_display(str(tool or ""))
        if not label:
            continue
        display_tool_durations_ms.setdefault(label, []).extend(durations)
    top_cmds = [{"command": cmd, "count": cnt} for cmd, cnt in display_shell_commands.most_common(25)]
    sig_cmd = display_shell_commands.most_common(1)

    pctl_data = compute_tool_percentiles(display_tool_durations_ms)
    token_economy = compute_token_economy(stats)
    strengths = build_strengths(stats)
    observations = build_observations(stats)
    practical_examples = build_practical_examples(stats)
    recommendations = build_recommendations(stats)
    achievements = build_achievement_badges(stats)

    avg_quality = sum(stats.session_quality_scores.values()) / len(stats.sessions_seen) if stats.sessions_seen else 0

    # Agent comparison for HTML
    agent_comparison = []
    for name, ag in stats.agents.items():
        sess_count = len(ag.sessions_seen)
        agent_comparison.append({
            "name": name,
            "events": ag.total_events,
            "sessions": sess_count,
            "avg_quality": ag.total_quality_score / sess_count if sess_count else 0,
            "completed": ag.completed_sessions,
            "recovered": ag.recovered_failures,
            "prompts": ag.events_by_type.get("UserPromptSubmit", 0),
            "tools": sum(ag.tools_by_count.values()),
            "failures": ag.events_by_type.get("PostToolUseFailure", 0),
            "tokens": ag.total_input_tokens + ag.total_output_tokens,
        })

    data = {
        "total_spans": stats.total_events,
        "unique_sessions": len(stats.sessions_seen),
        "unique_models": len(stats.models_by_count),
        "avg_quality_score": avg_quality,
        "agent_comparison": agent_comparison,
        "prompt_submits": prompts,
        "tool_calls": pre_tool,
        "mcp_calls": stats.events_by_type.get("BeforeMCPExecution", 0),
        "subagent_launches": stats.events_by_type.get("SubagentStart", 0),
        "tool_failures": failures,
        "file_reads": stats.events_by_type.get("BeforeReadFile", 0),
        "file_edits": stats.events_by_type.get("AfterFileEdit", 0),
        "shell_executions": stats.events_by_type.get("BeforeShellExecution", 0),
        "first_event_ts": stats.first_event_ts,
        "last_event_ts": stats.last_event_ts,
        "days_active": stats.days_active,
        "events_by_type": dict(stats.events_by_type.most_common()),
        "models_by_count": dict(stats.models_by_count.most_common()),
        "tools_by_count": dict(display_tools_by_count.most_common(15)),
        "skills_by_count": dict(skills_by_count.most_common()),
        "mcp_servers_by_count": dict(stats.mcp_servers.most_common()),
        "subagent_types_by_count": dict(stats.subagent_types.most_common()),
        "activity_by_day": activity_by_day,
        "sessions": sessions_list,
        "top_commands": top_cmds,
        "unique_commands": len(display_shell_commands),
        "activity_by_hour": activity_by_hour,
        "peak_hour": peak_hour,
        "peak_hour_count": peak_hour_count,
        "signature_command": sig_cmd[0][0] if sig_cmd else "",
        "signature_command_count": sig_cmd[0][1] if sig_cmd else 0,
        "total_input_tokens": stats.total_input_tokens,
        "total_output_tokens": stats.total_output_tokens,
        "total_cache_creation_tokens": stats.total_cache_creation_tokens,
        "total_cache_read_tokens": stats.total_cache_read_tokens,
        "tool_to_prompt_ratio": round(_safe_ratio(pre_tool, prompts), 1),
        "failure_rate_pct": round(100 * _safe_ratio(failures, pre_tool), 1),
        "reads_per_prompt": round(
            _safe_ratio(stats.events_by_type.get("BeforeReadFile", 0), prompts), 1
        ),
        "tool_percentiles": pctl_data,
        "token_economy": token_economy,
        "strengths": strengths,
        "observations": observations,
        "recommendations": recommendations,
        "practical_examples": practical_examples,
        "achievements": achievements,
        # Weekly trends
        "weekly_trends": _compute_weekly_trends(stats.activity_by_day),
        # MCP server availability
        "mcp_server_before": dict(stats.mcp_server_before),
        "mcp_server_after": dict(stats.mcp_server_after),
        # Subagent effectiveness
        "subagent_stops_by_type": dict(stats.subagent_stops_by_type),
        "subagent_total_starts": stats.events_by_type.get("SubagentStart", 0),
        "subagent_total_stops": stats.events_by_type.get("SubagentStop", 0),
        # Graph analysis
        "graph_tool_transitions": _compute_tool_transitions(stats.session_tool_seq),
        "graph_cooccurrence": _compute_tool_cooccurrence(
            stats.session_tool_seq,
            [t for t, _ in display_tools_by_count.most_common(12)],
        ),
        "graph_latency_histograms": _compute_latency_histograms(
            display_tool_durations_ms,
            [t for t, _ in display_tools_by_count.most_common(10)],
        ),
        "graph_dep": _compute_dep_graph(
            stats.agents,
            display_tools_by_count,
            stats.mcp_servers,
            stats.session_conversation,
            session_agents,
        ),
        "graph_session_timeline": _compute_session_timeline(
            stats.session_span_details, stats.session_events,
        ),
        "agents": {
            name: {
                "total_events": ag.total_events,
                "sessions": len(ag.sessions_seen),
                "prompts": ag.events_by_type.get("UserPromptSubmit", 0),
                "tool_calls": ag.events_by_type.get("PreToolUse", 0),
                "tool_ratio": round(_safe_ratio(
                    ag.events_by_type.get("PreToolUse", 0),
                    ag.events_by_type.get("UserPromptSubmit", 0),
                ), 1),
                "failures": ag.events_by_type.get("PostToolUseFailure", 0),
                "failure_rate": round(100 * _safe_ratio(
                    ag.events_by_type.get("PostToolUseFailure", 0),
                    ag.events_by_type.get("PreToolUse", 0),
                ), 1),
                "mcp_calls": ag.events_by_type.get("BeforeMCPExecution", 0),
                "subagents": ag.events_by_type.get("SubagentStart", 0),
                "input_tokens": ag.total_input_tokens,
                "output_tokens": ag.total_output_tokens,
                "cache_creation_tokens": ag.total_cache_creation_tokens,
                "cache_read_tokens": ag.total_cache_read_tokens,
                "top_model": ag.models_by_count.most_common(1)[0][0] if ag.models_by_count else "",
                "top_tools": dict(ag.tools_by_count.most_common(10)),
                "top_skills": dict(skills_by_agent.get(name, Counter()).most_common(10)),
                "percentiles": compute_tool_percentiles(ag.tool_durations_ms)[:8],
            }
            for name, ag in sorted(stats.agents.items())
        },
    }
    return _json_dumps(data)




def _dashboard_docs_dir() -> Path:
    # Prefer repo-level docs/ (development), fall back to packaged data/ (pip install)
    repo_docs = Path(__file__).resolve().parents[2] / "docs"
    if (repo_docs / "index.html").exists():
        return repo_docs
    pkg_data = Path(__file__).resolve().parent / "data"
    if (pkg_data / "index.html").exists():
        return pkg_data
    return repo_docs  # caller handles missing file


def _write_dashboard_artifact(stats: TelemetryStats, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_build_dashboard_json(stats), encoding="utf-8")


def _artifact_report_ref(path: Path) -> str | None:
    override = os.environ.get("REFLECT_PUBLISH_REPORT_REF", "").strip()
    if override:
        return override
    try:
        return path.resolve().relative_to(_dashboard_docs_dir().resolve()).as_posix()
    except ValueError:
        for parent in path.resolve().parents:
            if parent.name == "docs":
                try:
                    return path.resolve().relative_to(parent.resolve()).as_posix()
                except ValueError:
                    continue
        return None



def _load_detail_from_native(session_id: str, agent: str, file_path: Path) -> dict:
    """Read a native session file and return full conversation events."""
    import json as _json
    try:
        import orjson
        _loads = orjson.loads
    except ImportError:
        _loads = _json.loads

    if agent not in {"claude", "copilot", "gemini", "cursor"}:
        return {
            "session_id": session_id,
            "agent": agent,
            "events": [],
            "source": "native_unavailable",
            "warnings": [f"Session detail loading is not implemented for agent '{agent}' yet."],
        }

    events: list[dict] = []
    try:
        if agent == "claude":
            for line in file_path.open("r", encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = _loads(line)
                except (ValueError, _json.JSONDecodeError):
                    continue
                etype = entry.get("type")
                ts = entry.get("timestamp", "")
                if etype == "user":
                    content = entry.get("message", {}).get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            item.get("text", "") for item in content
                            if isinstance(item, dict) and item.get("type") == "text"
                        )
                    events.append({"type": "prompt", "content": str(content), "timestamp": ts})
                elif etype == "assistant":
                    msg = entry.get("message", {}) or {}
                    usage = msg.get("usage", {}) or {}
                    model = msg.get("model", "")
                    content_items = msg.get("content") or []
                    text_parts: list[str] = []
                    tool_uses: list[dict] = []
                    for item in content_items:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif item.get("type") == "tool_use":
                            tool_uses.append({
                                "type": "tool_call",
                                "tool_name": item.get("name", ""),
                                "input": _json.dumps(item.get("input", {}), default=str)[:2000],
                                "timestamp": ts,
                            })
                    events.append({
                        "type": "response",
                        "content": "\n".join(text_parts)[:5000],
                        "model": model,
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                        "timestamp": ts,
                    })
                    events.extend(tool_uses)

        elif agent == "copilot":
            for line in file_path.open("r", encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = _loads(line)
                except (ValueError, _json.JSONDecodeError):
                    continue
                etype = entry.get("type")
                data = entry.get("data", {})
                ts = entry.get("timestamp", "")
                if etype == "user.message":
                    events.append({"type": "prompt", "content": data.get("content", ""), "timestamp": ts})
                elif etype == "assistant.message":
                    events.append({
                        "type": "response",
                        "content": data.get("content", "")[:5000],
                        "model": data.get("model", ""),
                        "output_tokens": data.get("outputTokens", 0),
                        "timestamp": ts,
                    })
                elif etype == "tool.execution_start":
                    events.append({
                        "type": "tool_call",
                        "tool_name": data.get("toolName", ""),
                        "input": _json.dumps(data.get("arguments", {}), default=str)[:2000],
                        "timestamp": ts,
                    })
                elif etype == "tool.execution_complete":
                    events.append({
                        "type": "tool_result",
                        "tool_name": data.get("toolName", ""),
                        "success": bool(data.get("success", False)),
                        "timestamp": ts,
                    })
                elif etype == "session.shutdown":
                    metrics = data.get("modelMetrics") or {}
                    total_in = sum(int((m.get("usage") or {}).get("inputTokens") or 0) for m in metrics.values())
                    total_out = sum(int((m.get("usage") or {}).get("outputTokens") or 0) for m in metrics.values())
                    total_cr = sum(int((m.get("usage") or {}).get("cacheReadTokens") or 0) for m in metrics.values())
                    if total_in or total_out or total_cr:
                        events.append({
                            "type": "session_end",
                            "input_tokens": total_in,
                            "output_tokens": total_out,
                            "cache_read_tokens": total_cr,
                            "timestamp": ts,
                        })

        elif agent == "gemini":
            payload = _loads(file_path.read_text())
            for msg in payload.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                ts = msg.get("timestamp", "")
                if msg.get("type") == "user":
                    events.append({"type": "prompt", "content": msg.get("content", ""), "timestamp": ts})
                elif msg.get("type") == "gemini":
                    events.append({
                        "type": "response",
                        "content": msg.get("content", "")[:5000],
                        "model": msg.get("model", ""),
                        "input_tokens": (msg.get("tokens") or {}).get("input", 0),
                        "output_tokens": (msg.get("tokens") or {}).get("output", 0),
                        "timestamp": ts,
                    })
                    for call in msg.get("toolCalls") or []:
                        if not isinstance(call, dict):
                            continue
                        events.append({
                            "type": "tool_call",
                            "tool_name": call.get("displayName") or call.get("name", ""),
                            "input": _json.dumps(call.get("args", {}), default=str)[:2000],
                            "timestamp": call.get("timestamp", ts),
                        })

        elif agent == "cursor":
            for line in file_path.open("r", encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = _loads(line)
                except (ValueError, _json.JSONDecodeError):
                    continue
                role = entry.get("role")
                ts = entry.get("timestamp", "")
                content = entry.get("message", {}).get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        item.get("text", "") for item in content if isinstance(item, dict)
                    )
                if role == "user":
                    events.append({"type": "prompt", "content": str(content)[:5000], "timestamp": ts})
                elif role == "assistant":
                    events.append({"type": "response", "content": str(content)[:5000], "timestamp": ts})
    except (OSError, ValueError, _json.JSONDecodeError) as exc:
        logger.warning("Failed to load %s session detail from %s: %s", agent, file_path, exc)
        return {
            "session_id": session_id,
            "agent": agent,
            "events": [],
            "source": "native_error",
            "warnings": [f"Failed to load native session detail from {file_path}."],
        }

    return {"session_id": session_id, "agent": agent, "events": events, "source": "native", "warnings": []}


def _load_session_detail(session_id: str, stats: TelemetryStats) -> dict | None:
    """Load full conversation detail for a session from its source file."""
    detail: dict | None = None
    source_info = stats.session_source.get(session_id)
    agent_name = source_info[0] if source_info else ""
    if source_info:
        _, file_path = source_info
        fp = Path(file_path)
        if fp.exists():
            detail = _load_detail_from_native(session_id, agent_name, fp)

    if detail is None:
        conv = stats.session_conversation.get(session_id)
        if conv:
            detail = {"session_id": session_id, "agent": "", "events": conv, "source": "spans"}

    telemetry = _load_session_telemetry(session_id, stats.session_first_ts.get(session_id))
    if detail is None:
        warnings = []
        if agent_name:
            warnings.append(f"Session detail loading is not implemented for agent '{agent_name}' yet.")
        elif (telemetry.get("summary") or {}).get("spans", 0) == 0 and (telemetry.get("summary") or {}).get("logs", 0) == 0:
            warnings.append("No stored conversation or OTLP telemetry was found for this session.")
        detail = {
            "session_id": session_id,
            "agent": agent_name,
            "events": [],
            "source": "native_unavailable" if agent_name else ("unavailable" if warnings else "telemetry"),
            "warnings": warnings,
        }
    detail.setdefault("warnings", [])
    detail["telemetry"] = telemetry
    return detail


def _start_publish_server(stats: TelemetryStats) -> None:
    """Start a local FastAPI server and open the dashboard in a browser.

    Blocks until Ctrl-C. Uses ``?report=api/data`` so the dashboard
    fetches JSON from the API — no URL encoding at all.
    """
    port = int(os.environ.get("REFLECT_PORT", "8765"))
    docs_dir = _dashboard_docs_dir()
    _start_publish_server_inline(stats, port, docs_dir)


def _start_publish_server_inline(stats: TelemetryStats, port: int, docs_dir: Path) -> None:
    """Inline FastAPI server for reflect --publish."""
    import threading
    import webbrowser

    try:
        import uvicorn
        from fastapi import FastAPI, Request
        from fastapi.responses import FileResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError:
        logger.warning("FastAPI/uvicorn not installed. Install with: pip install fastapi uvicorn")
        logger.warning("Falling back to writing artifact file...")
        artifact = docs_dir / "_reflect_data.json"
        artifact.write_text(_build_dashboard_json(stats), encoding="utf-8")
        print(f"Wrote: {artifact}")
        return

    globals()["Request"] = Request

    import json as _json
    app = FastAPI(title="reflect dashboard", docs_url=None, redoc_url=None)
    _cached = _json.loads(_build_dashboard_json(stats))
    _cached["comparison"] = None

    @app.get("/api/data")
    def api_data(request: Request):
        params = request.query_params
        q = (params.get("q") or "").strip()
        agents = {agent for agent in (params.get("agents") or "").split(",") if agent}
        legacy_agent = (params.get("agent") or "").strip()
        if legacy_agent and legacy_agent != "all":
            agents.add(legacy_agent)
        model = params.get("model") or "all"
        status = params.get("status") or "all"
        range_name = params.get("range") or "all"
        if not any([q, agents, model != "all", status != "all", range_name != "all"]):
            return JSONResponse(_cached)
        filtered_sessions = _filter_dashboard_sessions(
            _cached.get("sessions") or [],
            q=q,
            agents=agents,
            model=model,
            status=status,
            range_name=range_name,
        )
        filtered_stats = _build_filtered_stats(stats, filtered_sessions)
        payload = json.loads(_build_dashboard_json(filtered_stats))
        payload["comparison"] = _build_filtered_comparison_payload(
            stats,
            _cached.get("sessions") or [],
            filtered_sessions,
            q=q,
            model=model,
            status=status,
            range_name=range_name,
            primary_stats=filtered_stats,
            primary_data=payload,
        )
        return JSONResponse(payload)

    @app.get("/api/session/{session_id:path}")
    def api_session(session_id: str):
        detail = _load_session_detail(session_id, stats)
        if detail is None:
            return JSONResponse({"error": f"Session {session_id} not found"}, status_code=404)
        return JSONResponse(detail, headers={"Access-Control-Allow-Origin": "*"})

    @app.get("/")
    def index():
        return FileResponse(docs_dir / "index.html", media_type="text/html")

    if docs_dir.exists():
        app.mount("/", StaticFiles(directory=str(docs_dir)), name="static")

    url = f"http://127.0.0.1:{port}/?report=api/data"
    threading.Timer(0.5, webbrowser.open, args=[url]).start()
    print(f"\n  Serving at: {url}")
    print("  Press Ctrl-C to stop\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def _update_dashboard_data(stats: TelemetryStats, html_path: Path) -> None:
    """Inject fresh data into the HTML dashboard file."""
    if not html_path.exists():
        logger.warning("Dashboard template not found: %s", html_path)
        return

    html = html_path.read_text(encoding="utf-8")
    new_data = _build_dashboard_json(stats)

    # Replace the const D = {...}; line.
    # Use a lambda so re.sub doesn't interpret \n/\1 etc in the replacement string.
    replacement = f"const D = {new_data};"
    updated = re.sub(
        r"const D = \{.*?\};",
        lambda _: replacement,
        html,
        count=1,
        flags=re.DOTALL,
    )
    html_path.write_text(updated, encoding="utf-8")
