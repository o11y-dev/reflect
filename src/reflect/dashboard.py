from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable
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
    build_all_insights,
    build_session_insights,
    compute_session_quality_breakdown,
    compute_tool_percentiles,
)
from reflect.insights.renderers import insights_to_example_tuples, insights_to_strings
from reflect.models import AgentStats, TelemetryStats
from reflect.preparation import BackgroundPreparationWorker, PreparationSnapshot, PreparationState
from reflect.utils import (
    _json_dumps,
    _safe_ratio,
    _sanitize_command_counter,
    _sanitize_command_display,
    logger,
)


def _perf_start() -> float:
    return time.perf_counter() if os.environ.get("REFLECT_DEBUG_PERF") else 0.0


def _perf_finish(name: str, start: float, **fields: object) -> None:
    if not start:
        return
    duration_ms = (time.perf_counter() - start) * 1000
    field_text = " ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, ""))
    suffix = f" {field_text}" if field_text else ""
    logger.info("reflect.dashboard.perf %s duration_ms=%.1f%s", name, duration_ms, suffix)


class DashboardDataCache:
    """Thread-safe dashboard snapshot cache refreshed after background preparation."""

    def __init__(
        self,
        loader: Callable[[], dict[str, object]],
        *,
        refresh_loader: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self._loader = loader
        self._refresh_loader = refresh_loader or loader
        self._lock = threading.Lock()
        self._payload = loader()

    def get(self) -> dict[str, object]:
        with self._lock:
            return self._payload

    def refresh(self) -> dict[str, object]:
        payload = self._refresh_loader()
        with self._lock:
            self._payload = payload
        return payload


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


def _quality_rules_payload() -> list[dict[str, object]]:
    """Dashboard copy for the session quality scoring rubric."""
    return [
        {
            "name": "Completion",
            "points": 25,
            "signals": ["Stop", "SessionEnd", "SubagentStop"],
            "description": "Full credit when the session emits a normal completion event; partial credit for subagent-only completion.",
        },
        {
            "name": "Efficiency",
            "points": 20,
            "signals": ["tokens per tool", "tool count", "session token volume"],
            "description": "Penalizes high token-per-tool usage and unusually large tool counts, using local distribution thresholds when available.",
        },
        {
            "name": "Tool reliability",
            "points": 15,
            "signals": ["PostToolUseFailure", "tool failure rate"],
            "description": "Rewards clean tool execution and scales down as failed tool calls exceed the expected local failure rate.",
        },
        {
            "name": "Loop detection",
            "points": 10,
            "signals": ["repeated consecutive tool calls"],
            "description": "Penalizes repeated use of the same tool in adjacent steps, which usually indicates stalled exploration or retry loops.",
        },
        {
            "name": "Duration health",
            "points": 10,
            "signals": ["session span timestamps"],
            "description": "Gives partial credit when timing data is sparse, penalizes very short sessions and long outliers.",
        },
        {
            "name": "Error recovery",
            "points": 10,
            "signals": ["failure followed by successful PostToolUse"],
            "description": "Rewards sessions that recover after failed tool calls; sessions without failures receive baseline credit.",
        },
        {
            "name": "Tool diversity",
            "points": 5,
            "signals": ["distinct tools"],
            "description": "Rewards sessions that use a reasonable mix of tools instead of a single repeated action.",
        },
        {
            "name": "Edit productivity",
            "points": 5,
            "signals": ["AfterFileEdit", "BeforeReadFile"],
            "description": "Rewards sessions that convert exploration into edits, using the edit-to-read ratio when available.",
        },
    ]


def _summary_breakdown_item(
    name: str,
    earned: float,
    max_points: float,
    summary: str,
    metrics: dict[str, object],
) -> dict[str, object]:
    return {
        "name": name,
        "earned": round(max(0.0, min(max_points, earned)), 2),
        "max": max_points,
        "summary": summary,
        "metrics": metrics,
        "inputs": [
            {"name": key.replace("_", " "), "value": value}
            for key, value in metrics.items()
        ],
    }


def _sql_quality_breakdown(row: dict[str, object], recovered: int = 0) -> list[dict[str, object]]:
    status = str(row.get("status") or "unknown")
    failures = int(row.get("failure_count") or row.get("failures") or 0)
    tool_uses = int(row.get("tool_call_count") or row.get("tool_calls") or 0)
    duration_ms = int(row.get("duration_ms") or 0)
    input_tokens = int(row.get("input_tokens") or 0)
    output_tokens = int(row.get("output_tokens") or 0)
    total_tokens = input_tokens + output_tokens
    completed = status in {"ok", "completed", "success"}

    efficiency = 20.0
    if tool_uses > 0:
        tokens_per_tool = total_tokens / tool_uses
        if tokens_per_tool > 25_000:
            efficiency -= 15.0
            efficiency_summary = "Tokens per tool exceeded the severe threshold."
        elif tokens_per_tool > 10_000:
            efficiency -= 7.0
            efficiency_summary = "Tokens per tool exceeded the mild threshold."
        else:
            efficiency_summary = "Tokens per tool stayed within the expected range."
        if tool_uses > 30:
            efficiency -= 5.0
            efficiency_summary += " Tool count exceeded the cold-start threshold."
    else:
        tokens_per_tool = 0.0
        efficiency_summary = "No tool calls were present; scoring used total token volume."
        if total_tokens > 50_000:
            efficiency -= 10.0
            efficiency_summary = "No tool calls were present and total tokens exceeded 50k."
        elif total_tokens > 20_000:
            efficiency -= 5.0
            efficiency_summary = "No tool calls were present and total tokens exceeded 20k."

    if tool_uses > 0:
        failure_rate = failures / tool_uses
        if failure_rate > 0.15:
            reliability = max(0.0, 15.0 - failure_rate * 100)
            reliability_summary = "Failure rate exceeded the threshold."
        elif failures == 0:
            reliability = 15.0
            reliability_summary = "No failed tool calls were observed."
        else:
            reliability = 15.0 * (1.0 - failure_rate / 0.15)
            reliability_summary = "Failures were present but stayed under the threshold."
    else:
        failure_rate = 0.0
        reliability = 15.0
        reliability_summary = "No tool calls were present, so no tool failures were observed."

    duration_score = 10.0
    if duration_ms <= 0:
        duration_score = 5.0
        duration_summary = "Only partial timing data was available."
    elif duration_ms < 30_000:
        duration_score -= 3.0
        duration_summary = "Session was very short, so duration health was reduced."
    elif duration_ms > 1_800_000:
        duration_score -= 5.0
        duration_summary = "Session exceeded the 30 minute cold-start duration threshold."
    else:
        duration_summary = "Duration stayed within the expected range."

    if failures == 0:
        recovery = 7.0
        recovery_summary = "No failures were observed, so recovery gets baseline credit."
    elif recovered > 0:
        recovery = 10.0 * min(1.0, recovered / failures)
        recovery_summary = "Failures were followed by successful tool results."
    else:
        recovery = 0.0
        recovery_summary = "Failures were observed without a matching successful recovery."

    return [
        _summary_breakdown_item(
            "Completion",
            25.0 if completed else 0.0,
            25.0,
            "Session status indicates completion." if completed else "Session status does not indicate completion.",
            {"status": status, "completed": completed},
        ),
        _summary_breakdown_item(
            "Efficiency",
            efficiency,
            20.0,
            efficiency_summary,
            {
                "tool_uses": tool_uses,
                "total_tokens": total_tokens,
                "tokens_per_tool": round(tokens_per_tool, 2),
                "mild_threshold": 10_000,
                "severe_threshold": 25_000,
            },
        ),
        _summary_breakdown_item(
            "Tool reliability",
            reliability,
            15.0,
            reliability_summary,
            {"failures": failures, "tool_uses": tool_uses, "failure_rate": round(failure_rate, 4), "threshold": 0.15},
        ),
        _summary_breakdown_item(
            "Loop detection",
            10.0,
            10.0,
            "No repeated-tool loop signal was present in the session summary.",
            {"tool_sequence_available": False},
        ),
        _summary_breakdown_item(
            "Duration health",
            duration_score,
            10.0,
            duration_summary,
            {"duration_ms": duration_ms},
        ),
        _summary_breakdown_item(
            "Error recovery",
            recovery,
            10.0,
            recovery_summary,
            {"failures": failures, "recovered": recovered},
        ),
        _summary_breakdown_item(
            "Tool diversity",
            0.0,
            5.0,
            "Distinct per-session tool count was not present in the session summary.",
            {"distinct_tools_available": False},
        ),
        _summary_breakdown_item(
            "Edit productivity",
            0.0,
            5.0,
            "Read/edit productivity events were not present in the session summary.",
            {"edit_events_available": False},
        ),
    ]


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


def _extract_skill_name_from_path(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        return ""
    match = re.search(r"(?:^|/)skills/(?:.*/)?([^/]+)/SKILL\.md$", path)
    return match.group(1).strip() if match else ""


def _extract_file_path_from_attrs(attrs: dict[str, object]) -> str:
    return str(
        _sql_attr(
            attrs,
            "gen_ai.client.file_path",
            "gen_ai.client.tool.input.file_path",
            "gen_ai.client.tool.input.path",
            "tool.input.file_path",
            "tool.input.path",
            "file.path",
            "path",
        )
        or ""
    ).strip()


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


def _build_tool_inventory(
    tool_events: list[dict[str, object]],
    mcp_events: list[dict[str, object]] | None = None,
    subagent_events: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    tools: Counter[str] = Counter()
    tool_failures: Counter[str] = Counter()
    tool_durations: defaultdict[str, list[int]] = defaultdict(list)
    tool_examples: defaultdict[str, list[str]] = defaultdict(list)
    skills: Counter[str] = Counter()
    skill_tools: defaultdict[str, Counter[str]] = defaultdict(Counter)
    subagents: Counter[str] = Counter()
    subagent_stops: Counter[str] = Counter()
    subagent_sources: defaultdict[str, Counter[str]] = defaultdict(Counter)
    mcp_tools: Counter[str] = Counter()
    mcp_servers: Counter[str] = Counter()

    for event in tool_events:
        tool_name = str(event.get("tool_name") or "unknown").strip() or "unknown"
        # Support explicit count (default 1) so result-only rows can use count=0 without inflating totals
        count = int(event.get("count", 1))
        tools[tool_name] += count
        status = str(event.get("status") or "").lower()
        success = event.get("success")
        if status == "error" or success is False:
            tool_failures[tool_name] += count
        duration = event.get("duration_ms")
        if isinstance(duration, (int, float)) and duration > 0:
            tool_durations[tool_name].append(int(duration))
        preview = str(event.get("input_preview") or event.get("preview") or event.get("input") or "").strip()
        if preview and len(tool_examples[tool_name]) < 3:
            tool_examples[tool_name].append(preview[:500])
        file_path = str(event.get("file_path") or "").strip()
        path_skill = _extract_skill_name_from_path(file_path)
        if path_skill:
            skills[path_skill] += count
            skill_tools[path_skill][tool_name] += count
        if tool_name == "skill":
            skill_name = _extract_skill_name_from_preview(preview)
            if skill_name:
                skills[skill_name] += count
                skill_tools[skill_name][tool_name] += count
        attrs = event.get("attrs")
        if not isinstance(attrs, dict):
            attrs = {}
        subagent_name = _extract_subagent_name_from_tool(tool_name, attrs, preview)
        if subagent_name:
            subagents[subagent_name] += count
            subagent_sources[subagent_name][tool_name] += count

    for event in mcp_events or []:
        tool_name = str(event.get("tool_name") or "").strip()
        server_name = str(event.get("server") or event.get("server_name") or "").strip()
        label = f"{server_name}/{tool_name}" if server_name and tool_name else tool_name or server_name
        if label:
            mcp_tools[label] += 1
        if server_name:
            mcp_servers[server_name] += 1

    for event in subagent_events or []:
        name = _clean_subagent_name(event.get("name")) or "unknown"
        status = str(event.get("status") or "start").lower()
        if status == "stop":
            subagent_stops[name] += 1
        else:
            subagents[name] += 1
            source = str(event.get("source") or "lifecycle")
            subagent_sources[name][source] += 1

    def tool_row(item: tuple[str, int]) -> dict[str, object]:
        name, count = item
        durations = tool_durations.get(name, [])
        return {
            "name": name,
            "count": count,
            "failures": tool_failures.get(name, 0),
            "avg_duration_ms": round(sum(durations) / len(durations), 1) if durations else 0,
            "examples": tool_examples.get(name, []),
        }

    return {
        "tools": [tool_row(item) for item in tools.most_common()],
        "skills": [
            {
                "name": name,
                "count": count,
                "tools": dict(skill_tools.get(name, Counter()).most_common()),
            }
            for name, count in skills.most_common()
        ],
        "mcp_tools": [
            {"name": name, "count": count}
            for name, count in mcp_tools.most_common()
        ],
        "subagents": [
            {
                "name": name,
                "count": count,
                "stops": subagent_stops.get(name, 0),
                "sources": dict(subagent_sources.get(name, Counter()).most_common()),
            }
            for name, count in subagents.most_common()
        ],
        "mcp_servers": [
            {"name": name, "count": count}
            for name, count in mcp_servers.most_common()
        ],
        "total_tool_calls": sum(tools.values()),
        "total_skill_calls": sum(skills.values()),
        "total_mcp_calls": sum(mcp_tools.values()),
        "total_subagent_starts": sum(subagents.values()),
        "total_subagent_stops": sum(subagent_stops.values()),
    }


def _add_skill_hints_to_inventory(inventory: dict[str, object], skill_names: set[str]) -> dict[str, object]:
    if not skill_names:
        return inventory
    existing = {
        str(item.get("name") or "")
        for item in inventory.get("skills", [])
        if isinstance(item, dict)
    }
    hinted = [
        {"name": name, "count": 1, "tools": {}, "source": "conversation"}
        for name in sorted(skill_names)
        if name not in existing
    ]
    if hinted:
        inventory["skills"] = [*inventory.get("skills", []), *hinted]
        inventory["total_skill_calls"] = int(inventory.get("total_skill_calls") or 0) + len(hinted)
    return inventory


def _add_subagent_hints_to_inventory(inventory: dict[str, object], subagent_names: set[str]) -> dict[str, object]:
    if not subagent_names:
        return inventory
    existing = {
        str(item.get("name") or "")
        for item in inventory.get("subagents", [])
        if isinstance(item, dict)
    }
    hinted = [
        {"name": name, "count": 1, "stops": 0, "sources": {}, "source": "conversation"}
        for name in sorted(subagent_names)
        if name not in existing
    ]
    if hinted:
        inventory["subagents"] = [*inventory.get("subagents", []), *hinted]
        inventory["total_subagent_starts"] = int(inventory.get("total_subagent_starts") or 0) + len(hinted)
    return inventory


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


def _extract_subagent_name_from_tool(tool_name: str, attrs: dict | None = None, preview: str = "") -> str:
    normalized_tool = str(tool_name or "").strip().lower()
    attrs = attrs or {}
    payload = _load_json_dict(preview)

    def first_value(*keys: str) -> str:
        for key in keys:
            value = _sql_attr(attrs, f"gen_ai.client.tool.input.{key}", f"tool.input.{key}")
            if value in (None, ""):
                value = payload.get(key)
            cleaned = _clean_subagent_name(value)
            if cleaned:
                return cleaned
        return ""

    if normalized_tool in {"subagent", "agent"}:
        return first_value("subagent_type", "agent_type", "name", "agent_id", "description")
    if normalized_tool in {"task", "read_agent"}:
        return first_value("agent_id", "name", "agent_type")
    return ""


def _clean_subagent_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if not name or "REDACTED" in name.upper() or name.startswith("["):
        return ""
    return name[:80]


def _sql_step_id_for_raw_event(raw_event_id: object) -> str:
    digest = hashlib.sha1(str(raw_event_id or "").encode("utf-8")).hexdigest()
    return f"step_{digest}"


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
    session_id: str = "",
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
        if session_id and _session_row_id(session) != session_id:
            continue
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
    sessions_with_telemetry = {
        sid for sid in selected_ids if sid in stats.sessions_with_telemetry
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
        cost_row = stats.session_costs.get(sid, {})
        agent_stats.total_input_tokens += int(token_row.get("input", session.get("input_tokens") or 0))
        agent_stats.total_output_tokens += int(token_row.get("output", session.get("output_tokens") or 0))
        agent_stats.total_cache_creation_tokens += int(token_row.get("cache_creation", session.get("cache_creation_tokens") or 0))
        agent_stats.total_cache_read_tokens += int(token_row.get("cache_read", session.get("cache_read_tokens") or 0))
        agent_stats.total_cost += float(cost_row.get("total_cost_usd") or 0.0)
        agent_stats.total_cost_usd += float(cost_row.get("total_cost_usd") or 0.0)
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
            elif event_type == "tool_call":
                subagent_name = _extract_subagent_name_from_tool(
                    str(event.get("tool_name") or ""),
                    {},
                    str(event.get("preview") or ""),
                )
                if subagent_name:
                    events_by_type["SubagentStart"] += 1
                    agent_stats.events_by_type["SubagentStart"] += 1
                    subagent_types[subagent_name] += 1
                    agent_stats.subagent_types[subagent_name] += 1
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
    session_costs = {
        sid: dict(stats.session_costs.get(sid, {}))
        for sid in selected_ids
        if sid in stats.session_costs
    }
    model_costs: Counter[str] = Counter()
    model_costs_usd: Counter[str] = Counter()
    for _sid, row in session_costs.items():
        model_name = str(row.get("model") or "")
        if model_name:
            model_costs[model_name] += float(row.get("total_cost_usd") or 0.0)
            model_costs_usd[model_name] += float(row.get("total_cost_usd") or 0.0)
    total_cost = sum(float((session_costs.get(sid) or {}).get("total_cost_usd") or 0.0) for sid in selected_ids)
    input_cost = sum(float((session_costs.get(sid) or {}).get("input_cost_usd") or 0.0) for sid in selected_ids)
    output_cost = sum(float((session_costs.get(sid) or {}).get("output_cost_usd") or 0.0) for sid in selected_ids)
    cache_creation_cost = sum(float((session_costs.get(sid) or {}).get("cache_creation_cost_usd") or 0.0) for sid in selected_ids)
    cache_read_cost = sum(float((session_costs.get(sid) or {}).get("cache_read_cost_usd") or 0.0) for sid in selected_ids)
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
        sessions_with_telemetry=sessions_with_telemetry,
        pricing_unit=stats.pricing_unit,
        total_cost=total_cost,
        input_cost=input_cost,
        output_cost=output_cost,
        cache_creation_cost=cache_creation_cost,
        cache_read_cost=cache_read_cost,
        total_cost_usd=total_cost,
        input_cost_usd=input_cost,
        output_cost_usd=output_cost,
        cache_creation_cost_usd=cache_creation_cost,
        cache_read_cost_usd=cache_read_cost,
        session_costs=session_costs,
        model_costs=model_costs,
        model_costs_usd=model_costs_usd,
        pricing_source=stats.pricing_source,
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
    session_id: str = "",
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
        session_id=session_id,
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

    # Compute insights once (profile needed for per-session insights inside the loop)
    all_insights = build_all_insights(stats)
    _profile = all_insights["profile"]

    # Sessions with event counts and primary model
    sessions_list = []
    session_agents: dict[str, str] = {}
    session_ids = sorted(
        set(stats.sessions_seen) | set(stats.session_source),
        key=lambda sid: (
            stats.session_events.get(sid, 0),
            stats.session_first_ts.get(sid, 0),
            sid,
        ),
        reverse=True,
    )
    discovered_session_count = len(session_ids)
    for sid in session_ids:
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

        # New quality metrics. Sessions discovered only from source maps may not
        # have enough scored spans yet, so keep availability separate from value.
        quality_available = sid in stats.session_quality_scores
        quality_score = (
            float(stats.session_quality_scores[sid])
            if quality_available
            else None
        )
        is_completed = stats.session_goal_completed.get(sid, False)
        recovered = stats.session_recovered_failures.get(sid, 0)
        has_scoring_spans = bool(stats.session_span_details.get(sid))
        quality_breakdown = (
            compute_session_quality_breakdown(sid, stats.session_span_details.get(sid, []), tok, _profile)
            if quality_available and has_scoring_spans
            else []
        )
        quality_missing_reason = ""
        if not quality_available:
            quality_missing_reason = (
                "No quality score is available because this session was discovered from a native session file "
                "or source map, but no scored telemetry spans were loaded for it."
            )
        elif not has_scoring_spans:
            quality_missing_reason = (
                "This score is based on sparse session metadata because detailed scored spans were not available."
            )

        # Conversation events for session browser (capped at 500 per session)
        conv_events = stats.session_conversation.get(sid, [])[:500]
        tool_inventory = _build_tool_inventory(
            [
                {
                    "tool_name": event.get("tool_name"),
                    "status": "error" if event.get("success") is False else "completed",
                    "success": event.get("success", True),
                    "duration_ms": event.get("duration_ms", 0),
                    "preview": event.get("preview", ""),
                    "file_path": event.get("file_path", ""),
                }
                for event in stats.session_conversation.get(sid, [])
                if event.get("type") == "tool_call"
            ],
            [
                {
                    "tool_name": event.get("tool_name"),
                    "server": event.get("server"),
                    "status": "error" if event.get("success") is False else "completed",
                }
                for event in stats.session_conversation.get(sid, [])
                if event.get("type") == "mcp_call"
            ],
            [
                {
                    "name": event.get("subagent_type"),
                    "status": "stop" if event.get("type") == "subagent_stop" else "start",
                    "source": "lifecycle",
                }
                for event in stats.session_conversation.get(sid, [])
                if event.get("type") in {"subagent_start", "subagent_stop"}
            ],
        )
        # Extract first prompt preview for session card
        first_prompt = ""
        skill_hints: set[str] = set()
        subagent_hints: set[str] = set()
        for ce in conv_events:
            if not first_prompt and ce.get("type") == "prompt" and ce.get("preview"):
                first_prompt = ce["preview"]
            if ce.get("type") in {"prompt", "response"}:
                text = str(ce.get("preview") or "")
                skill_hints.update(_extract_skill_names_from_text(text))
                subagent_hints.update(_extract_subagent_names_from_text(text))
        tool_inventory = _add_skill_hints_to_inventory(tool_inventory, skill_hints)
        tool_inventory = _add_subagent_hints_to_inventory(tool_inventory, subagent_hints)
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
            "tool_inventory": tool_inventory,
            "tool_p50": tool_p50,
            "commands": dict(cmd_counter.most_common(10)),
            "failure_count": failure_count,
            "duration_ms": duration_ms,
            "input_tokens": tok.get("input", 0),
            "output_tokens": tok.get("output", 0),
            "cache_creation_tokens": tok.get("cache_creation", 0),
            "cache_read_tokens": tok.get("cache_read", 0),
            "pricing_unit": str((stats.session_costs.get(sid) or {}).get("pricing_unit") or stats.pricing_unit or "usd"),
            "total_cost": float((stats.session_costs.get(sid) or {}).get("total_cost_usd") or 0.0),
            "input_cost": float((stats.session_costs.get(sid) or {}).get("input_cost_usd") or 0.0),
            "output_cost": float((stats.session_costs.get(sid) or {}).get("output_cost_usd") or 0.0),
            "total_cost_usd": float((stats.session_costs.get(sid) or {}).get("total_cost_usd") or 0.0),
            "input_cost_usd": float((stats.session_costs.get(sid) or {}).get("input_cost_usd") or 0.0),
            "output_cost_usd": float((stats.session_costs.get(sid) or {}).get("output_cost_usd") or 0.0),
            "cache_creation_cost": float((stats.session_costs.get(sid) or {}).get("cache_creation_cost_usd") or 0.0),
            "cache_read_cost": float((stats.session_costs.get(sid) or {}).get("cache_read_cost_usd") or 0.0),
            "cache_creation_cost_usd": float((stats.session_costs.get(sid) or {}).get("cache_creation_cost_usd") or 0.0),
            "cache_read_cost_usd": float((stats.session_costs.get(sid) or {}).get("cache_read_cost_usd") or 0.0),
            "pricing_source": str((stats.session_costs.get(sid) or {}).get("pricing_source") or stats.pricing_source or ""),
            "token_source": token_source,
            "token_note": token_note,
            "quality_score": quality_score,
            "quality_available": quality_available,
            "quality_missing_reason": quality_missing_reason,
            "quality_breakdown": quality_breakdown,
            "is_completed": is_completed,
            "recovered_failures": recovered,
            "agent": agent_name,
            "first_prompt": first_prompt,
            "conversation": conv_events,
            "has_telemetry": sid in stats.sessions_with_telemetry,
            "insights": [
                {
                    "kind": i.kind,
                    "title": i.title,
                    "body": i.body,
                    "severity": int(i.severity),
                    "confidence": i.confidence,
                    "category": i.category,
                }
                for i in build_session_insights(sid, stats, _profile)
            ],
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
    token_economy = _profile.token_economy
    strengths = insights_to_strings(all_insights["strengths"])
    if not strengths:
        strengths = ["**Active usage** — Generating telemetry data across multiple sessions "
                     "is a good foundation for continuous improvement."]
    observations = insights_to_strings(all_insights["observations"])
    practical_examples = insights_to_example_tuples(all_insights["examples"])
    recommendations = insights_to_strings(all_insights["recommendations"])
    achievements = all_insights["badges"]

    avg_quality = (
        sum(float(stats.session_quality_scores.get(sid, 0.0)) for sid in session_ids) / discovered_session_count
        if discovered_session_count
        else 0
    )

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
            "total_cost": ag.total_cost,
            "total_cost_usd": ag.total_cost_usd,
        })

    data = {
        "total_spans": stats.total_events,
        "unique_sessions": discovered_session_count,
        "unique_models": len(stats.models_by_count),
        "avg_quality_score": avg_quality,
        "quality_rules": _quality_rules_payload(),
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
        "pricing_unit": stats.pricing_unit,
        "total_cost": stats.total_cost,
        "input_cost": stats.input_cost,
        "output_cost": stats.output_cost,
        "cache_creation_cost": stats.cache_creation_cost,
        "cache_read_cost": stats.cache_read_cost,
        "total_cost_usd": stats.total_cost_usd,
        "input_cost_usd": stats.input_cost_usd,
        "output_cost_usd": stats.output_cost_usd,
        "cache_creation_cost_usd": stats.cache_creation_cost_usd,
        "cache_read_cost_usd": stats.cache_read_cost_usd,
        "model_costs": dict(stats.model_costs),
        "model_costs_usd": dict(stats.model_costs_usd),
        "pricing_source": stats.pricing_source,
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
        "insights_structured": [
            {
                "kind": i.kind, "title": i.title, "body": i.body,
                "category": i.category, "severity": int(i.severity),
                "confidence": i.confidence,
            }
            for category in ("strengths", "observations", "recommendations")
            for i in all_insights[category]
        ],
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
        "graph_semantic": {"nodes": [], "edges": [], "sessions": [], "legend": []},
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
                "total_cost_usd": ag.total_cost_usd,
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
        summary = telemetry.get("summary") or {}
        has_telemetry = summary.get("spans", 0) > 0 or summary.get("logs", 0) > 0
        session_known = session_id in stats.sessions_seen
        if not agent_name and not has_telemetry and not session_known:
            return None

        warnings = []
        if agent_name:
            warnings.append(f"Session detail loading is not implemented for agent '{agent_name}' yet.")
        elif not has_telemetry:
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
    detail["tool_inventory"] = _build_tool_inventory(
        [
            {
                "tool_name": event.get("tool_name"),
                "status": "error" if event.get("success") is False else "completed",
                "success": event.get("success", True),
                "duration_ms": event.get("duration_ms", 0),
                "preview": event.get("preview", event.get("input", "")),
                "file_path": event.get("file_path", ""),
            }
            for event in detail.get("events", [])
            if isinstance(event, dict) and event.get("type") in ("tool_call", "tool_result")
        ],
        [
            {
                "tool_name": event.get("tool_name"),
                "server": event.get("server"),
                "status": "error" if event.get("success") is False else "completed",
            }
            for event in detail.get("events", [])
            if isinstance(event, dict) and event.get("type") == "mcp_call"
        ],
        [
            {
                "name": event.get("subagent_type"),
                "status": "stop" if event.get("type") == "subagent_stop" else "start",
                "source": "lifecycle",
            }
            for event in detail.get("events", [])
            if isinstance(event, dict) and event.get("type") in {"subagent_start", "subagent_stop"}
        ],
    )
    detail["tool_inventory"] = _add_skill_hints_to_inventory(
        detail["tool_inventory"],
        {
            skill_name
            for event in detail.get("events", [])
            if isinstance(event, dict) and event.get("type") in {"prompt", "response"}
            for skill_name in _extract_skill_names_from_text(
                str(event.get("preview") or event.get("content") or "")
            )
        },
    )
    detail["tool_inventory"] = _add_subagent_hints_to_inventory(
        detail["tool_inventory"],
        {
            subagent_name
            for event in detail.get("events", [])
            if isinstance(event, dict) and event.get("type") in {"prompt", "response"}
            for subagent_name in _extract_subagent_names_from_text(
                str(event.get("preview") or event.get("content") or "")
            )
        },
    )
    detail["insights"] = [
        {
            "kind": i.kind, "title": i.title, "body": i.body,
            "severity": int(i.severity), "confidence": i.confidence,
            "category": i.category,
        }
        for i in build_session_insights(session_id, stats)
    ]
    return detail


def _sql_report_payload(
    db_path: Path,
    *,
    limit: int = 50,
    offset: int = 0,
    include_tabs: bool = True,
) -> dict[str, object]:
    from reflect.store.sqlite import connect_sqlite
    from reflect.views.overview import build_overview
    from reflect.views.report_tabs import build_report_tabs
    from reflect.views.sessions import list_sessions

    conn = connect_sqlite(db_path)
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
        return {
            "db_path": str(db_path),
            "overview": overview,
            "sessions": list_sessions(conn, limit=limit, offset=offset).model_dump(),
            "tabs": (
                build_report_tabs(conn).model_dump()
                if include_tabs
                else _empty_sql_lazy_tabs()
            ),
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
                started_at = datetime.fromisoformat(str(row.get("started_at") or row.get("created_at") or ""))
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
                started_at = datetime.fromisoformat(str(row.get("started_at") or row.get("created_at") or ""))
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
    from reflect.store.sqlite import connect_sqlite

    ids = sorted(session_ids)
    placeholders = ", ".join("?" for _ in ids)
    conn = connect_sqlite(db_path)
    try:
        rows = _dict_rows(conn.execute(
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
        ))
    finally:
        conn.close()
    models: dict[str, str] = {}
    for row in rows:
        session_id = str(row["session_id"])
        if session_id not in models:
            models[session_id] = str(row["model"] or "")
    return models


def _sql_session_first_prompts(db_path: Path, session_ids: set[str]) -> dict[str, str]:
    if not session_ids:
        return {}
    from reflect.store.sqlite import connect_sqlite

    ids = sorted(session_ids)
    placeholders = ", ".join("?" for _ in ids)
    conn = connect_sqlite(db_path)
    try:
        rows = _dict_rows(conn.execute(
            f"""
            SELECT session_id, raw_attrs_json
            FROM steps
            WHERE session_id IN ({placeholders})
              AND raw_attrs_json LIKE '%gen_ai.client.prompt%'
            ORDER BY session_id, seq
            """,
            ids,
        ))
    finally:
        conn.close()
    prompts: dict[str, str] = {}
    for row in rows:
        session_id = str(row["session_id"])
        if session_id in prompts:
            continue
        attrs = _load_json_dict(row["raw_attrs_json"])
        prompt = str(_sql_attr(
            attrs,
            "gen_ai.client.prompt",
            "gen_ai.client.prompt.text",
            "prompt",
            "input",
        ) or "").strip()
        if prompt:
            prompts[session_id] = prompt
    return prompts


def _dict_rows(cursor) -> list[dict[str, object]]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _load_json_dict(value: object) -> dict[str, object]:
    if not value:
        return {}
    try:
        payload = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _sql_attr(attrs: dict[str, object], *keys: str) -> object:
    for key in keys:
        value = attrs.get(key)
        if value not in (None, ""):
            return value
    return None


def _sql_attr_text(attrs: dict[str, object], *keys: str, limit: int = 500) -> str:
    value = _sql_attr(attrs, *keys)
    if value in (None, ""):
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, default=str)
    return str(value).strip()[:limit]


def _iso_to_epoch_ns(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1_000_000_000)


def _sql_log_body_text(body: dict[str, object], attrs: dict[str, object], event_type: object) -> str:
    for key in ("message", "body", "text", "content", "error.message", "exception.message"):
        value = body.get(key)
        if value not in (None, ""):
            return _sql_attr_text(body, key, limit=2000)
    for key in ("error.message", "exception.message"):
        value = attrs.get(key)
        if value not in (None, ""):
            return _sql_attr_text(attrs, key, limit=2000)
    event = str(
        attrs.get("gen_ai.client.hook.event")
        or event_type
        or ""
    ).strip()
    return event[:2000]


def _sql_response_preview(attrs: dict[str, object], call: dict[str, object]) -> str:
    captured = _sql_attr_text(
        attrs,
        "gen_ai.client.output",
        "gen_ai.response.text",
        "gen_ai.response.content",
        "response",
        "output",
        limit=2000,
    )
    if captured:
        return captured
    status = _sql_attr_text(attrs, "gen_ai.client.status", "status", limit=80)
    input_tokens = int(call.get("input_tokens") or 0)
    output_tokens = int(call.get("output_tokens") or 0)
    token_parts = []
    if input_tokens:
        token_parts.append(f"{input_tokens:,} input tokens")
    if output_tokens:
        token_parts.append(f"{output_tokens:,} output tokens")
    token_text = " and ".join(token_parts) if token_parts else "token usage metadata"
    status_text = f" with status {status}" if status else ""
    return f"Assistant turn completed{status_text}; captured {token_text}."


def _sql_dashboard_compat_payload(
    db_path: Path,
    *,
    session_ids: set[str] | None = None,
    include_heavy: bool = True,
    include_base: bool = True,
    base_tab_names: set[str] | None = None,
) -> dict[str, object]:
    from reflect.store.sqlite import connect_sqlite
    from reflect.views.overview import list_source_provenance
    from reflect.views.report_tabs import build_report_tab, build_report_tabs

    scoped = session_ids is not None
    scoped_ids = sorted(session_ids or [])

    conn = connect_sqlite(db_path)
    try:
        selected_session_ids = set(scoped_ids) if scoped else None
        if not include_base:
            tab_views = _empty_sql_lazy_tabs()
            source_provenance = []
        elif include_heavy:
            tab_views = build_report_tabs(conn, session_ids=selected_session_ids).model_dump()
            source_provenance = list_source_provenance(
                conn,
                session_ids=selected_session_ids,
            )
        else:
            tab_views = _empty_sql_lazy_tabs()
            selected_base_tabs = (
                base_tab_names
                if base_tab_names is not None
                else {"activity", "models", "costs", "tools", "mcp", "agents"}
            )
            for tab_name in selected_base_tabs:
                tab_views[tab_name] = build_report_tab(
                    conn,
                    tab_name,
                    session_ids=selected_session_ids,
                )
            source_provenance = []
    finally:
        conn.close()

    activity_view = tab_views["activity"]
    models_view = tab_views["models"]
    costs_view = tab_views["costs"]
    tools_view = tab_views["tools"]
    mcp_view = tab_views["mcp"]
    agents_view = tab_views["agents"]
    graphs_view = tab_views["graphs"]
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
        "graph_tool_transitions": graphs_view["graph_tool_transitions"],
        "graph_cooccurrence": graphs_view["graph_cooccurrence"],
        "graph_dep": graphs_view["graph_dep"],
        "graph_session_timeline": graphs_view["graph_session_timeline"],
        "graph_semantic": graphs_view["graph_semantic"],
        "source_provenance": source_provenance,
        "agents": agents_view["agents"],
        "specs": specs_view,
        "memory": memory_view,
        "privacy": privacy_view,
        "exports": exports_view,
    }


def _sql_insight_payload(
    overview: dict[str, object],
    sessions: list[dict[str, object]],
    compat: dict[str, object],
) -> dict[str, object]:
    input_tokens = int(overview["input_tokens"] or 0)
    output_tokens = int(overview["output_tokens"] or 0)
    cache_creation_tokens = int(compat["total_cache_creation_tokens"] or 0)
    cache_read_tokens = int(compat["total_cache_read_tokens"] or 0)
    total_tokens = input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens
    prompt_count = sum(int(session["prompt_count"] or 0) for session in sessions)
    session_tokens = [int(session["total_tokens"] or 0) for session in sessions]
    top_session_share = (max(session_tokens) / total_tokens * 100) if total_tokens else 0.0
    high_context_sessions = sum(1 for tokens in session_tokens if tokens >= 100_000)
    mcp_calls = int(compat["mcp_calls"] or 0)
    tool_calls = int(overview["tool_call_count"] or 0)
    failures = int(overview["failure_count"] or 0)
    subagents = int(compat["subagent_total_starts"] or 0)
    file_reads = int(compat.get("file_reads") or 0)
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
        strengths.append(f"**Execution telemetry** - Captured {tool_calls:,} tool calls across the filtered report scope.")
    observations = [
        f"**Token concentration** - The largest session accounts for {top_session_share:.1f}% of observed token volume.",
    ]
    if mcp_calls:
        observations.append(f"**MCP activity** - This scope contains {mcp_calls:,} MCP calls across {len(compat['mcp_servers_by_count']):,} server(s).")
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
        recommendations.extend([
            "**Use a fixed prompt contract for non-trivial requests** - Goal, Context, Constraints, Output, Done-when keeps observed sessions easier to compare and review.",
            "**Close tasks with a structured handoff** - End each major task with changes, validations, residual risk, and the next command so future reports can distinguish completed work from drift.",
        ])
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
        achievements.append({"icon": "&#129534;", "name": "Token Ledger", "sub": f"{total_tokens:,} tokens"})
    if cache_read_tokens:
        achievements.append({"icon": "&#129534;", "name": "Cache Saver", "sub": f"{economy['cache_reuse_ratio']:.1f}x cached reuse"})
    if compat["unique_models"]:
        achievements.append({"icon": "&#9878;", "name": "Model Mixer", "sub": f"{compat['unique_models']:,} models"})
    if compat["unique_commands"]:
        achievements.append({"icon": "&#128187;", "name": "Command Runner", "sub": f"{compat['unique_commands']:,} patterns"})
    if tool_calls and failures == 0:
        achievements.append({"icon": "&#9989;", "name": "Zero Failures", "sub": "clean tool execution"})
    elif tool_calls:
        achievements.append({"icon": "&#128295;", "name": "Tool Operator", "sub": f"{tool_calls:,} tool calls"})
    if overview["estimated_cost_usd"]:
        achievements.append({"icon": "&#128176;", "name": "Cost Visibility", "sub": f"${float(overview['estimated_cost_usd']):.2f} estimated"})
    if subagents:
        achievements.append({"icon": "&#129302;", "name": "Delegator", "sub": f"{subagents:,} subagents"})
    if mcp_calls:
        achievements.append({"icon": "&#128268;", "name": "MCP Active", "sub": f"{mcp_calls:,} MCP calls"})
    return {
        "token_economy": economy,
        "strengths": strengths,
        "observations": observations,
        "recommendations": recommendations,
        "practical_examples": practical_examples,
        "achievements": achievements,
    }


def _sql_cohort_summary(
    sessions: list[dict[str, object]],
    compat: dict[str, object],
    *,
    label: str,
    agent_names: list[str] | None = None,
) -> dict[str, object]:
    input_tokens = sum(int(session.get("input_tokens") or 0) for session in sessions)
    output_tokens = sum(int(session.get("output_tokens") or 0) for session in sessions)
    prompt_count = sum(int(session.get("prompt_count") or 0) for session in sessions)
    tool_calls = sum(int(session.get("tool_calls") or session.get("tool_call_count") or 0) for session in sessions)
    failures = sum(int(session.get("failure_count") or session.get("failures") or 0) for session in sessions)
    quality_values = [float(session.get("quality_score") or 0) for session in sessions if float(session.get("quality_score") or 0) > 0]
    tools = compat.get("tools_by_count") or {}
    commands = compat.get("top_commands") or []
    return {
        "label": label,
        "agents": list(agent_names or sorted({str(session.get("agent") or "unknown") for session in sessions})),
        "sessions": len(sessions),
        "prompts": prompt_count,
        "tool_calls": tool_calls,
        "avg_quality": (sum(quality_values) / len(quality_values)) if quality_values else 0.0,
        "failure_rate_pct": round(100 * failures / tool_calls, 1) if tool_calls else 0.0,
        "tokens": input_tokens + output_tokens,
        "shell_runs": int(compat.get("shell_executions") or 0),
        "mcp_calls": int(compat.get("mcp_calls") or 0),
        "subagent_launches": int(compat.get("subagent_launches") or compat.get("subagent_total_starts") or 0),
        "top_tools": [{"tool": str(tool), "count": int(count)} for tool, count in list(tools.items())[:5]],
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
        session for session in baseline_scope
        if str(session.get("agent") or "").lower() not in set(primary_agent_names)
    ]
    if not primary_sessions or not baseline_sessions:
        return None
    primary_ids = {str(session["id"]) for session in primary_sessions}
    baseline_ids = {str(session["id"]) for session in baseline_sessions}
    primary_compat = _sql_dashboard_compat_payload(db_path, session_ids=primary_ids)
    baseline_compat = _sql_dashboard_compat_payload(db_path, session_ids=baseline_ids)
    primary_summary = _sql_cohort_summary(
        primary_sessions,
        primary_compat,
        label=" + ".join(primary_agent_names),
        agent_names=primary_agent_names,
    )
    baseline_summary = _sql_cohort_summary(
        baseline_sessions,
        baseline_compat,
        label="All other agents in scope",
    )
    baseline_agents = sorted(
        baseline_compat.get("agent_comparison") or [],
        key=lambda item: (-int(item.get("sessions") or 0), str(item.get("name") or "")),
    )
    quality_by_agent: dict[str, list[float]] = {}
    for session in baseline_sessions:
        quality_by_agent.setdefault(str(session.get("agent") or "unknown"), []).append(float(session.get("quality_score") or 0))
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


def _empty_sql_lazy_tabs() -> dict[str, object]:
    return {
        "overview": {
            "avg_quality_score": 0.0,
            "unique_sessions": 0,
            "first_event_ts": "",
            "prompt_submits": 0,
            "tool_calls": 0,
            "tool_to_prompt_ratio": "0.0",
            "failure_rate_pct": 0,
            "tool_failures": 0,
            "mcp_calls": 0,
            "mcp_servers_by_count": {},
            "subagent_launches": 0,
            "subagent_types_by_count": {},
            "file_edits": 0,
            "shell_executions": 0,
            "unique_commands": 0,
            "signature_command": "",
            "signature_command_count": 0,
            "peak_hour": -1,
            "peak_hour_count": 0,
            "unique_models": 0,
            "models_by_count": {},
            "events_by_type": {},
            "source_provenance": [],
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cache_creation_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_cost_usd": 0.0,
            "input_cost_usd": 0.0,
            "output_cost_usd": 0.0,
            "cache_creation_cost_usd": 0.0,
            "cache_read_cost_usd": 0.0,
            "pricing_source": "local",
            "model_costs": {},
        },
        "activity": {
            "events_by_type": {},
            "activity_by_day": {},
            "activity_by_hour": {str(hour): 0 for hour in range(24)},
            "peak_hour": -1,
            "peak_hour_count": 0,
        },
        "models": {"models_by_count": {}, "unique_models": 0},
        "costs": {
            "model_costs": {},
            "model_costs_usd": {},
            "cost_breakdown": {
                "total_cost_usd": 0.0,
                "input_cost_usd": 0.0,
                "output_cost_usd": 0.0,
                "cache_creation_cost_usd": 0.0,
                "cache_read_cost_usd": 0.0,
            },
            "total_cache_creation_tokens": 0,
            "total_cache_read_tokens": 0,
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
        },
        "agents": {"agent_comparison": [], "agents": {}},
        "graphs": {
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
            "row_counts": {
                "sessions": 0,
                "steps": 0,
                "llm_calls": 0,
                "tool_calls": 0,
                "mcp_calls": 0,
                "memories": 0,
                "privacy_findings": 0,
                "evidence": 0,
            },
            "scoped": True,
        },
    }


def _sql_dashboard_session_payload(db_path: Path, session_id: str) -> dict[str, object]:
    from reflect.store.migrate import migrate
    from reflect.store.sqlite import connect_sqlite
    from reflect.views.report_tabs import _display_mcp_server_name, build_report_tab
    from reflect.views.sessions import list_sessions

    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        row = _dict_rows(conn.execute(
            """
            SELECT
              s.id AS session_id,
              COALESCE(a.name, sr.agent, 'unknown') AS agent,
              s.status,
              s.title,
              CASE
                WHEN (s.started_at IS NULL OR s.started_at = '' OR substr(s.started_at, 1, 4) < '2000')
                  AND s.ended_at IS NOT NULL AND s.ended_at <> '' AND substr(s.ended_at, 1, 4) >= '2000'
                THEN s.ended_at
                ELSE s.started_at
              END AS started_at,
              s.ended_at,
              COALESCE(
                sr.duration_ms,
                CASE
                  WHEN s.started_at IS NOT NULL AND s.ended_at IS NOT NULL
                  THEN CAST((julianday(s.ended_at) - julianday(s.started_at)) * 86400000 AS INTEGER)
                  ELSE 0
                END,
                0
              ) AS duration_ms,
              COALESCE(sr.prompt_count, 0) AS prompt_count,
              COALESCE(sr.tool_call_count, 0) AS tool_call_count,
              COALESCE(sr.error_count, s.failure_count, 0) AS failure_count,
              COALESCE(sr.input_tokens, s.input_tokens, 0) AS input_tokens,
              COALESCE(sr.output_tokens, s.output_tokens, 0) AS output_tokens,
              COALESCE(sr.cache_write_tokens, s.cache_creation_tokens, 0) AS cache_creation_tokens,
              COALESCE(sr.cache_read_tokens, s.cache_read_tokens, 0) AS cache_read_tokens,
              COALESCE(sr.total_cost, s.estimated_cost_usd, 0) AS estimated_cost_usd
            FROM sessions s
            LEFT JOIN agents a ON a.id = s.agent_id
            LEFT JOIN session_rollups sr ON sr.session_id = s.id
            WHERE s.id = ?
            """,
            (session_id,),
        ))
        if not row:
            return {
                "sql_backed": True,
                "focused_session_id": session_id,
                "unique_sessions": 0,
                "sessions": [],
                "sqlite": {
                    "db_path": str(db_path),
                    "overview": {"session_count": 0},
                    "sessions": {"rows": [], "total": 0, "limit": 1, "offset": 0},
                    "tabs": _empty_sql_lazy_tabs(),
                },
            }
        session_row = row[0]
        primary_model = ""
        model_row = conn.execute(
            """
            SELECT COALESCE(NULLIF(response_model, ''), NULLIF(request_model, '')) AS model
            FROM llm_calls
            WHERE session_id = ?
              AND COALESCE(NULLIF(response_model, ''), NULLIF(request_model, '')) IS NOT NULL
            GROUP BY model
            ORDER BY COUNT(*) DESC, model ASC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if model_row:
            primary_model = str(model_row[0] or "")
        first_prompt = ""
        for prompt_row in _dict_rows(conn.execute(
            """
            SELECT raw_attrs_json
            FROM steps
            WHERE session_id = ?
            ORDER BY seq
            LIMIT 50
            """,
            (session_id,),
        )):
            attrs = _load_json_dict(prompt_row["raw_attrs_json"])
            first_prompt = str(_sql_attr(
                attrs,
                "gen_ai.client.prompt",
                "gen_ai.client.prompt.text",
                "prompt",
                "input",
            ) or "").strip()
            if first_prompt:
                break
        tool_rows = _dict_rows(conn.execute(
            """
            SELECT tool_name, COUNT(*) AS count
            FROM tool_calls
            WHERE session_id = ?
            GROUP BY tool_name
            ORDER BY count DESC, tool_name ASC
            LIMIT 10
            """,
            (session_id,),
        ))
        mcp_rows = _dict_rows(conn.execute(
            """
            SELECT server_name, COUNT(*) AS count
            FROM mcp_calls
            WHERE session_id = ? AND server_name IS NOT NULL AND server_name <> ''
            GROUP BY server_name
            ORDER BY count DESC, server_name ASC
            LIMIT 10
            """,
            (session_id,),
        ))
        event_rows = _dict_rows(conn.execute(
            """
            SELECT type, COUNT(*) AS count
            FROM steps
            WHERE session_id = ?
            GROUP BY type
            ORDER BY count DESC, type ASC
            """,
            (session_id,),
        ))
        operational_tabs = {
            tab_name: build_report_tab(conn, tab_name, session_ids={session_id})
            for tab_name in ("activity", "models", "costs", "tools", "mcp", "agents")
        }
        navigation_page = list_sessions(conn, limit=100, offset=0).model_dump()
    finally:
        conn.close()

    quality_breakdown = _sql_quality_breakdown(session_row)
    quality_score = sum(float(item["earned"]) for item in quality_breakdown)
    total_tokens = (
        int(session_row["input_tokens"] or 0)
        + int(session_row["output_tokens"] or 0)
        + int(session_row["cache_creation_tokens"] or 0)
        + int(session_row["cache_read_tokens"] or 0)
    )
    tools_by_count = {str(row["tool_name"]): int(row["count"] or 0) for row in tool_rows}
    mcp_servers: Counter[str] = Counter()
    for row in mcp_rows:
        server = _display_mcp_server_name(row["server_name"])
        if server:
            mcp_servers[server] += int(row["count"] or 0)
    mcp_servers_by_count = dict(mcp_servers)
    events_by_type = {str(row["type"]): int(row["count"] or 0) for row in event_rows}
    cost = float(session_row["estimated_cost_usd"] or 0.0)
    session_card = {
        "id": session_id,
        "full_id": session_id,
        "agent": session_row.get("agent") or "unknown",
        "status": session_row["status"],
        "title": session_row.get("title"),
        "first_prompt": first_prompt or session_row.get("title") or "",
        "started_at": session_row["started_at"],
        "ended_at": session_row.get("ended_at"),
        "created_at": session_row["started_at"],
        "duration_ms": session_row.get("duration_ms") or 0,
        "event_count": int(session_row["prompt_count"] or 0) + int(session_row["tool_call_count"] or 0),
        "prompt_count": session_row["prompt_count"],
        "tool_calls": session_row["tool_call_count"],
        "failures": session_row["failure_count"],
        "failure_count": session_row["failure_count"],
        "quality_score": quality_score,
        "quality_available": True,
        "quality_missing_reason": "",
        "quality_breakdown": quality_breakdown,
        "is_completed": session_row["status"] in {"ok", "completed", "success"},
        "recovered_failures": 0,
        "input_tokens": session_row["input_tokens"],
        "output_tokens": session_row["output_tokens"],
        "cache_creation_tokens": session_row["cache_creation_tokens"],
        "cache_read_tokens": session_row["cache_read_tokens"],
        "total_tokens": total_tokens,
        "total_cost": cost,
        "total_cost_usd": cost,
        "pricing_unit": "usd",
        "primary_model": primary_model,
        "models": {primary_model: 1} if primary_model else {},
        "tools": tools_by_count,
        "skills": {},
        "conversation": [],
        "telemetry": [],
    }
    navigation_cards: list[dict[str, object]] = []
    for navigation_row in navigation_page["rows"]:
        navigation_id = str(navigation_row["session_id"])
        if navigation_id == session_id:
            navigation_cards.append(session_card)
            continue
        navigation_quality = _sql_quality_breakdown(dict(navigation_row))
        navigation_cards.append({
            "id": navigation_id,
            "full_id": navigation_id,
            "agent": navigation_row.get("agent") or "unknown",
            "status": navigation_row["status"],
            "title": navigation_row.get("title"),
            "first_prompt": navigation_row.get("title") or "",
            "started_at": navigation_row["started_at"],
            "ended_at": navigation_row.get("ended_at"),
            "created_at": navigation_row["started_at"],
            "duration_ms": navigation_row.get("duration_ms") or 0,
            "event_count": int(navigation_row["prompt_count"] or 0)
                + int(navigation_row["tool_call_count"] or 0),
            "prompt_count": navigation_row["prompt_count"],
            "tool_calls": navigation_row["tool_call_count"],
            "failures": navigation_row["failure_count"],
            "failure_count": navigation_row["failure_count"],
            "quality_score": sum(float(item["earned"]) for item in navigation_quality),
            "quality_available": True,
            "quality_missing_reason": "",
            "quality_breakdown": navigation_quality,
            "is_completed": navigation_row["status"] in {"ok", "completed", "success"},
            "recovered_failures": 0,
            "input_tokens": navigation_row["input_tokens"],
            "output_tokens": navigation_row["output_tokens"],
            "cache_creation_tokens": navigation_row["cache_creation_tokens"],
            "cache_read_tokens": navigation_row["cache_read_tokens"],
            "total_tokens": (
                int(navigation_row["input_tokens"] or 0)
                + int(navigation_row["output_tokens"] or 0)
                + int(navigation_row["cache_creation_tokens"] or 0)
                + int(navigation_row["cache_read_tokens"] or 0)
            ),
            "total_cost": navigation_row["estimated_cost_usd"],
            "total_cost_usd": navigation_row["estimated_cost_usd"],
            "pricing_unit": "usd",
            "primary_model": "",
            "models": {},
            "tools": {},
            "skills": {},
            "conversation": [],
            "telemetry": [],
        })
    if not any(str(card["id"]) == session_id for card in navigation_cards):
        navigation_cards.insert(0, session_card)
    scoped_overview = {
        "session_count": 1,
        "prompt_count": int(session_row["prompt_count"] or 0),
        "tool_call_count": int(session_row["tool_call_count"] or 0),
        "failure_count": int(session_row["failure_count"] or 0),
        "input_tokens": int(session_row["input_tokens"] or 0),
        "output_tokens": int(session_row["output_tokens"] or 0),
        "estimated_cost_usd": cost,
        "source_provenance": [],
    }
    tabs = _empty_sql_lazy_tabs()
    tabs["overview"].update({
        "avg_quality_score": quality_score,
        "unique_sessions": 1,
        "first_event_ts": session_row["started_at"] or "",
        "prompt_submits": scoped_overview["prompt_count"],
        "tool_calls": scoped_overview["tool_call_count"],
        "tool_to_prompt_ratio": (
            f"{scoped_overview['tool_call_count'] / scoped_overview['prompt_count']:.1f}"
            if scoped_overview["prompt_count"] else "0.0"
        ),
        "failure_rate_pct": 0,
        "tool_failures": scoped_overview["failure_count"],
        "mcp_calls": sum(mcp_servers_by_count.values()),
        "mcp_servers_by_count": mcp_servers_by_count,
        "subagent_launches": 0,
        "subagent_types_by_count": {},
        "file_edits": 0,
        "shell_executions": 0,
        "unique_commands": 0,
        "signature_command": "",
        "signature_command_count": 0,
        "peak_hour": -1,
        "peak_hour_count": 0,
        "unique_models": 1 if primary_model else 0,
        "models_by_count": {primary_model: 1} if primary_model else {},
        "events_by_type": events_by_type,
        "source_provenance": [],
        "total_input_tokens": scoped_overview["input_tokens"],
        "total_output_tokens": scoped_overview["output_tokens"],
        "total_cache_creation_tokens": int(session_row["cache_creation_tokens"] or 0),
        "total_cache_read_tokens": int(session_row["cache_read_tokens"] or 0),
        "total_cost_usd": cost,
        "input_cost_usd": 0.0,
        "output_cost_usd": 0.0,
        "cache_creation_cost_usd": 0.0,
        "cache_read_cost_usd": 0.0,
        "pricing_source": "local",
        "model_costs": {primary_model: cost} if primary_model else {},
    })
    tabs["activity"]["events_by_type"] = events_by_type
    tabs["models"] = {"models_by_count": {primary_model: 1} if primary_model else {}, "unique_models": 1 if primary_model else 0}
    tabs["costs"].update({
        "model_costs": {primary_model: cost} if primary_model else {},
        "model_costs_usd": {primary_model: cost} if primary_model else {},
        "total_cache_creation_tokens": int(session_row["cache_creation_tokens"] or 0),
        "total_cache_read_tokens": int(session_row["cache_read_tokens"] or 0),
    })
    tabs["tools"]["tools_by_count"] = tools_by_count
    tabs["mcp"].update({
        "mcp_calls": sum(mcp_servers_by_count.values()),
        "mcp_servers_by_count": mcp_servers_by_count,
        "mcp_server_before": mcp_servers_by_count,
        "mcp_server_after": mcp_servers_by_count,
    })
    agent = str(session_card["agent"] or "unknown")
    agent_payload = {
        "name": agent,
        "sessions": 1,
        "events": int(session_card["event_count"] or 0),
        "prompts": scoped_overview["prompt_count"],
        "tools": scoped_overview["tool_call_count"],
        "failures": scoped_overview["failure_count"],
        "tokens": total_tokens,
        "total_cost": cost,
        "total_cost_usd": cost,
        "avg_quality": quality_score,
        "completed": 1 if session_card["is_completed"] else 0,
        "recovered": 0,
    }
    tabs["agents"] = {
        "agent_comparison": [agent_payload],
        "agents": {
            agent: {
                "total_events": int(session_card["event_count"] or 0),
                "sessions": 1,
                "prompts": scoped_overview["prompt_count"],
                "tool_calls": scoped_overview["tool_call_count"],
                "failures": scoped_overview["failure_count"],
                "input_tokens": scoped_overview["input_tokens"],
                "output_tokens": scoped_overview["output_tokens"],
                "total_cost_usd": cost,
                "top_model": primary_model,
                "top_tools": tools_by_count,
                "top_skills": {},
                "percentiles": [],
            }
        },
    }
    tabs.update(operational_tabs)
    tools_view = tabs["tools"]
    mcp_view = tabs["mcp"]
    activity_view = tabs["activity"]
    models_view = tabs["models"]
    costs_view = tabs["costs"]
    agents_view = tabs["agents"]
    session_card["skills"] = tools_view["skills_by_count"]
    tabs["overview"].update({
        "mcp_calls": mcp_view["mcp_calls"],
        "mcp_servers_by_count": mcp_view["mcp_servers_by_count"],
        "subagent_launches": tools_view["subagent_launches"],
        "subagent_types_by_count": tools_view["subagent_types_by_count"],
        "file_edits": tools_view["file_edits"],
        "shell_executions": tools_view["shell_executions"],
        "unique_commands": tools_view["unique_commands"],
        "signature_command": tools_view["signature_command"],
        "signature_command_count": tools_view["signature_command_count"],
        "peak_hour": activity_view["peak_hour"],
        "peak_hour_count": activity_view["peak_hour_count"],
        "unique_models": models_view["unique_models"],
        "models_by_count": models_view["models_by_count"],
        "events_by_type": activity_view["events_by_type"],
        "total_cache_creation_tokens": costs_view["total_cache_creation_tokens"],
        "total_cache_read_tokens": costs_view["total_cache_read_tokens"],
        "input_cost_usd": costs_view["cost_breakdown"]["input_cost_usd"],
        "output_cost_usd": costs_view["cost_breakdown"]["output_cost_usd"],
        "cache_creation_cost_usd": costs_view["cost_breakdown"]["cache_creation_cost_usd"],
        "cache_read_cost_usd": costs_view["cost_breakdown"]["cache_read_cost_usd"],
        "model_costs": costs_view["model_costs"],
    })
    insight_payload = _sql_insight_payload(scoped_overview, [session_card], {
        "total_cache_creation_tokens": int(session_row["cache_creation_tokens"] or 0),
        "total_cache_read_tokens": int(session_row["cache_read_tokens"] or 0),
        "mcp_calls": mcp_view["mcp_calls"],
        "mcp_servers_by_count": mcp_view["mcp_servers_by_count"],
        "subagent_total_starts": tools_view["subagent_total_starts"],
        "file_reads": tools_view["file_reads"],
        "unique_models": models_view["unique_models"],
        "unique_commands": tools_view["unique_commands"],
    })
    tabs["observations"] = {
        "strengths": insight_payload["strengths"],
        "observations": insight_payload["observations"],
        "recommendations": insight_payload["recommendations"],
        "practical_examples": insight_payload["practical_examples"],
        "achievements": insight_payload["achievements"],
        "token_economy": insight_payload["token_economy"],
    }
    tabs["compare"] = {"comparison": None, "agent_comparison": [agent_payload]}
    return {
        "sql_backed": True,
        "sqlite": {
            "db_path": str(db_path),
            "overview": scoped_overview,
            "sessions": navigation_page,
            "tabs": tabs,
        },
        "comparison": None,
        "sessions": navigation_cards,
        "quality_rules": _quality_rules_payload(),
        "session_list_total": navigation_page["total"],
        "focused_session_id": session_id,
        "unique_sessions": 1,
        "first_event_ts": session_row["started_at"] or "",
        "last_event_ts": session_row["started_at"] or "",
        "avg_quality_score": quality_score,
        "prompt_submits": scoped_overview["prompt_count"],
        "tool_calls": scoped_overview["tool_call_count"],
        "tool_to_prompt_ratio": tabs["overview"]["tool_to_prompt_ratio"],
        "events_by_type": activity_view["events_by_type"],
        "source_provenance": [],
        "failure_rate_pct": 0,
        "file_edits": tools_view["file_edits"],
        "file_reads": tools_view["file_reads"],
        "total_input_tokens": scoped_overview["input_tokens"],
        "total_output_tokens": scoped_overview["output_tokens"],
        "total_cache_creation_tokens": int(session_row["cache_creation_tokens"] or 0),
        "total_cache_read_tokens": int(session_row["cache_read_tokens"] or 0),
        "total_tokens": total_tokens,
        "total_cost": cost,
        "total_cost_usd": cost,
        "input_cost": costs_view["cost_breakdown"]["input_cost_usd"],
        "input_cost_usd": costs_view["cost_breakdown"]["input_cost_usd"],
        "output_cost": costs_view["cost_breakdown"]["output_cost_usd"],
        "output_cost_usd": costs_view["cost_breakdown"]["output_cost_usd"],
        "cache_creation_cost": costs_view["cost_breakdown"]["cache_creation_cost_usd"],
        "cache_creation_cost_usd": costs_view["cost_breakdown"]["cache_creation_cost_usd"],
        "cache_read_cost": costs_view["cost_breakdown"]["cache_read_cost_usd"],
        "cache_read_cost_usd": costs_view["cost_breakdown"]["cache_read_cost_usd"],
        "pricing_unit": "usd",
        "pricing_source": "local",
        "tools_by_count": tools_view["tools_by_count"],
        "models_by_count": models_view["models_by_count"],
        "unique_models": models_view["unique_models"],
        "skills_by_count": tools_view["skills_by_count"],
        "activity_by_day": activity_view["activity_by_day"],
        "activity_by_hour": activity_view["activity_by_hour"],
        "peak_hour": activity_view["peak_hour"],
        "peak_hour_count": activity_view["peak_hour_count"],
        "weekly_trends": [],
        "graph_tool_transitions": [],
        "graph_cooccurrence": {"tools": [], "matrix": []},
        "graph_latency_histograms": {},
        "graph_dep": {"nodes": [], "edges": [], "top_mcp_servers": []},
        "graph_session_timeline": [],
        "graph_semantic": {"nodes": [], "edges": [], "sessions": [], "legend": []},
        "agents": agents_view["agents"],
        "agent_comparison": agents_view["agent_comparison"],
        "mcp_calls": mcp_view["mcp_calls"],
        "mcp_servers_by_count": mcp_view["mcp_servers_by_count"],
        "mcp_server_before": mcp_view["mcp_server_before"],
        "mcp_server_after": mcp_view["mcp_server_after"],
        "subagent_types_by_count": tools_view["subagent_types_by_count"],
        "subagent_stops_by_type": tools_view["subagent_stops_by_type"],
        "subagent_launches": tools_view["subagent_launches"],
        "subagent_total_starts": tools_view["subagent_total_starts"],
        "subagent_total_stops": tools_view["subagent_total_stops"],
        "top_commands": tools_view["top_commands"],
        "unique_commands": tools_view["unique_commands"],
        "signature_command": tools_view["signature_command"],
        "signature_command_count": tools_view["signature_command_count"],
        "tool_percentiles": tools_view["tool_percentiles"],
        "model_costs": costs_view["model_costs"],
        "model_costs_usd": costs_view["model_costs_usd"],
        "strengths": insight_payload["strengths"],
        "observations": insight_payload["observations"],
        "recommendations": insight_payload["recommendations"],
        "practical_examples": insight_payload["practical_examples"],
        "achievements": insight_payload["achievements"],
        "token_economy": insight_payload["token_economy"],
        "tool_failures": scoped_overview["failure_count"],
        "shell_executions": tools_view["shell_executions"],
    }


def _sql_dashboard_tab_payload(
    db_path: Path,
    tab_name: str,
    *,
    session_id: str = "",
) -> dict[str, object]:
    from reflect.store.migrate import migrate
    from reflect.store.sqlite import connect_sqlite
    from reflect.views.report_tabs import build_report_tab

    scoped_ids = {session_id} if session_id else None
    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        payload = build_report_tab(conn, tab_name, session_ids=scoped_ids)
    finally:
        conn.close()
    return {
        "sql_backed": True,
        "tab": tab_name.strip().lower().replace("-", "_"),
        "scoped": scoped_ids is not None,
        "session_id": session_id,
        **payload,
    }


def _sql_dashboard_payload(
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
    has_scope_filter = bool(q or session_id or agents or model != "all" or status != "all" or range_name != "all")
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
        else _sql_session_first_prompts(
            db_path,
            {str(row["session_id"]) for row in session_rows},
        )
    )
    sessions = []
    for row in session_rows:
        quality_breakdown = _sql_quality_breakdown(dict(row))
        quality_score = sum(float(item["earned"]) for item in quality_breakdown)
        sessions.append({
            "id": row["session_id"],
            "full_id": row["session_id"],
            "agent": row.get("agent") or "unknown",
            "status": row["status"],
            "title": row.get("title"),
            "first_prompt": first_prompts.get(str(row["session_id"]), "") or row.get("title") or "",
            "started_at": row["started_at"],
            "ended_at": row.get("ended_at"),
            "created_at": row["started_at"],
            "duration_ms": row.get("duration_ms") or 0,
            "event_count": row["prompt_count"] + row["tool_call_count"],
            "prompt_count": row["prompt_count"],
            "tool_calls": row["tool_call_count"],
            "failures": row["failure_count"],
            "failure_count": row["failure_count"],
            "quality_score": quality_score,
            "quality_available": True,
            "quality_missing_reason": "",
            "quality_breakdown": quality_breakdown,
            "is_completed": row["status"] in {"ok", "completed", "success"},
            "recovered_failures": 0,
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "cache_creation_tokens": row["cache_creation_tokens"],
            "cache_read_tokens": row["cache_read_tokens"],
            "total_tokens": (
                row["input_tokens"]
                + row["output_tokens"]
                + row["cache_creation_tokens"]
                + row["cache_read_tokens"]
            ),
            "total_cost": row["estimated_cost_usd"],
            "total_cost_usd": row["estimated_cost_usd"],
            "pricing_unit": "usd",
            "primary_model": primary_models.get(str(row["session_id"]), ""),
            "models": (
                {primary_models[str(row["session_id"])]: 1}
                if primary_models.get(str(row["session_id"]))
                else {}
            ),
            "tools": {},
            "skills": {},
            "conversation": [],
            "telemetry": [],
        })
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
        if session_id else nav_sessions
    )
    scoped_session_rows = [
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
        for session in scoped_sessions
    ]
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
        "rows": nav_session_rows[offset:offset + limit],
        "total": len(nav_sessions),
        "limit": limit,
        "offset": offset,
    }
    sessions = nav_sessions[offset:offset + limit]
    scoped_overview = {
        **overview,
        "session_count": len(scoped_session_rows),
        "prompt_count": sum(int(row["prompt_count"] or 0) for row in scoped_session_rows),
        "tool_call_count": sum(int(row["tool_call_count"] or 0) for row in scoped_session_rows),
        "failure_count": sum(int(row["failure_count"] or 0) for row in scoped_session_rows),
        "input_tokens": sum(int(row["input_tokens"] or 0) for row in scoped_session_rows),
        "output_tokens": sum(int(row["output_tokens"] or 0) for row in scoped_session_rows),
        "estimated_cost_usd": sum(float(row["estimated_cost_usd"] or 0) for row in scoped_session_rows),
    }
    sqlite_payload["overview"] = scoped_overview
    sqlite_payload["sessions"] = sessions_page
    first_event_ts = ""
    if scoped_session_rows:
        first_event_ts = min(row["started_at"] for row in scoped_session_rows if row.get("started_at"))
    prompt_count = sum(row["prompt_count"] for row in scoped_session_rows)
    scoped_session_ids = {str(row["session_id"]) for row in scoped_session_rows}
    compat = _sql_dashboard_compat_payload(
        db_path,
        session_ids=scoped_session_ids if has_scope_filter else None,
        include_heavy=not (lazy_heavy_tabs or lazy_all_tabs),
        include_base=not lazy_all_tabs,
        base_tab_names=base_tab_names,
    )
    scoped_overview["source_provenance"] = compat["source_provenance"]
    cost_breakdown = compat["cost_breakdown"]
    total_cost_usd = float(scoped_overview["estimated_cost_usd"] or cost_breakdown["total_cost_usd"] or 0)
    sqlite_payload["tabs"] = {
        **dict(sqlite_payload.get("tabs") or {}),
        "overview": {
            "avg_quality_score": (
                sum(float(row.get("quality_score") or 0) for row in scoped_session_rows) / len(scoped_session_rows)
                if scoped_session_rows else 0
            ),
            "unique_sessions": scoped_overview["session_count"],
            "first_event_ts": first_event_ts,
            "prompt_submits": prompt_count,
            "tool_calls": scoped_overview["tool_call_count"],
            "tool_to_prompt_ratio": (
                f"{scoped_overview['tool_call_count'] / prompt_count:.1f}"
                if prompt_count else "0.0"
            ),
            "failure_rate_pct": 0,
            "tool_failures": int(scoped_overview["failure_count"]),
            "mcp_calls": compat["mcp_calls"],
            "mcp_servers_by_count": compat["mcp_servers_by_count"],
            "subagent_launches": compat["subagent_launches"],
            "subagent_types_by_count": compat["subagent_types_by_count"],
            "file_edits": compat["file_edits"],
            "shell_executions": compat["shell_executions"],
            "unique_commands": compat["unique_commands"],
            "signature_command": compat["signature_command"],
            "signature_command_count": compat["signature_command_count"],
            "peak_hour": compat["peak_hour"],
            "peak_hour_count": compat["peak_hour_count"],
            "unique_models": compat["unique_models"],
            "models_by_count": compat["models_by_count"],
            "events_by_type": compat["events_by_type"],
            "source_provenance": compat["source_provenance"],
            "total_input_tokens": scoped_overview["input_tokens"],
            "total_output_tokens": scoped_overview["output_tokens"],
            "total_cache_creation_tokens": compat["total_cache_creation_tokens"],
            "total_cache_read_tokens": compat["total_cache_read_tokens"],
            "total_cost_usd": total_cost_usd,
            "input_cost_usd": cost_breakdown["input_cost_usd"],
            "output_cost_usd": cost_breakdown["output_cost_usd"],
            "cache_creation_cost_usd": cost_breakdown["cache_creation_cost_usd"],
            "cache_read_cost_usd": cost_breakdown["cache_read_cost_usd"],
            "pricing_source": "local",
            "model_costs": compat["model_costs"],
        },
        "activity": {
            "events_by_type": compat["events_by_type"],
            "activity_by_day": compat["activity_by_day"],
            "activity_by_hour": compat["activity_by_hour"],
            "peak_hour": compat["peak_hour"],
            "peak_hour_count": compat["peak_hour_count"],
        },
        "models": {
            "models_by_count": compat["models_by_count"],
            "unique_models": compat["unique_models"],
        },
        "costs": {
            "model_costs": compat["model_costs"],
            "model_costs_usd": compat["model_costs_usd"],
            "cost_breakdown": compat["cost_breakdown"],
            "total_cache_creation_tokens": compat["total_cache_creation_tokens"],
            "total_cache_read_tokens": compat["total_cache_read_tokens"],
        },
        "tools": {
            "tools_by_count": compat["tools_by_count"],
            "tool_percentiles": compat["tool_percentiles"],
            "skills_by_count": compat["skills_by_count"],
            "subagent_types_by_count": compat["subagent_types_by_count"],
            "subagent_stops_by_type": compat["subagent_stops_by_type"],
            "subagent_launches": compat["subagent_launches"],
            "subagent_total_starts": compat["subagent_total_starts"],
            "subagent_total_stops": compat["subagent_total_stops"],
            "top_commands": compat["top_commands"],
            "unique_commands": compat["unique_commands"],
            "signature_command": compat["signature_command"],
            "signature_command_count": compat["signature_command_count"],
            "shell_executions": compat["shell_executions"],
            "file_edits": compat["file_edits"],
            "file_reads": compat["file_reads"],
        },
        "mcp": {
            "mcp_calls": compat["mcp_calls"],
            "mcp_servers_by_count": compat["mcp_servers_by_count"],
            "mcp_server_before": compat["mcp_server_before"],
            "mcp_server_after": compat["mcp_server_after"],
        },
        "agents": {
            "agent_comparison": compat["agent_comparison"],
            "agents": compat["agents"],
        },
        "graphs": {
            "graph_tool_transitions": compat["graph_tool_transitions"],
            "graph_cooccurrence": compat["graph_cooccurrence"],
            "graph_dep": compat["graph_dep"],
            "graph_session_timeline": compat["graph_session_timeline"],
            "graph_semantic": compat["graph_semantic"],
        },
        "specs": compat["specs"],
        "memory": compat["memory"],
        "privacy": compat["privacy"],
        "exports": compat["exports"],
    }
    insight_payload = _sql_insight_payload(scoped_overview, scoped_sessions, compat)
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
    sqlite_payload["tabs"]["compare"] = {
        "comparison": comparison_payload,
        "agent_comparison": compat["agent_comparison"],
    }
    payload = {
        "sql_backed": True,
        "sqlite": sqlite_payload,
        "comparison": comparison_payload,
        "sessions": sessions,
        "quality_rules": _quality_rules_payload(),
        "session_list_total": len(nav_sessions),
        "focused_session_id": session_id,
        "unique_sessions": scoped_overview["session_count"],
        "first_event_ts": first_event_ts,
        "last_event_ts": max((row["started_at"] for row in scoped_session_rows if row.get("started_at")), default=""),
        "avg_quality_score": (
            sum(float(row.get("quality_score") or 0) for row in scoped_session_rows) / len(scoped_session_rows)
            if scoped_session_rows else 0
        ),
        "prompt_submits": prompt_count,
        "tool_calls": scoped_overview["tool_call_count"],
        "tool_to_prompt_ratio": f"{scoped_overview['tool_call_count'] / prompt_count:.1f}" if prompt_count else "0.0",
        "events_by_type": compat["events_by_type"],
        "source_provenance": compat["source_provenance"],
        "failure_rate_pct": 0,
        "file_edits": compat["file_edits"],
        "file_reads": compat["file_reads"],
        "total_input_tokens": scoped_overview["input_tokens"],
        "total_output_tokens": scoped_overview["output_tokens"],
        "total_cache_creation_tokens": compat["total_cache_creation_tokens"],
        "total_cache_read_tokens": compat["total_cache_read_tokens"],
        "total_tokens": (
            scoped_overview["input_tokens"]
            + scoped_overview["output_tokens"]
            + compat["total_cache_creation_tokens"]
            + compat["total_cache_read_tokens"]
        ),
        "total_cost": total_cost_usd,
        "total_cost_usd": total_cost_usd,
        "input_cost": cost_breakdown["input_cost_usd"],
        "input_cost_usd": cost_breakdown["input_cost_usd"],
        "output_cost": cost_breakdown["output_cost_usd"],
        "output_cost_usd": cost_breakdown["output_cost_usd"],
        "cache_creation_cost": cost_breakdown["cache_creation_cost_usd"],
        "cache_creation_cost_usd": cost_breakdown["cache_creation_cost_usd"],
        "cache_read_cost": cost_breakdown["cache_read_cost_usd"],
        "cache_read_cost_usd": cost_breakdown["cache_read_cost_usd"],
        "pricing_unit": "usd",
        "pricing_source": "local",
        "tools_by_count": compat["tools_by_count"],
        "models_by_count": compat["models_by_count"],
        "unique_models": compat["unique_models"],
        "skills_by_count": compat["skills_by_count"],
        "activity_by_day": compat["activity_by_day"],
        "activity_by_hour": compat["activity_by_hour"],
        "peak_hour": compat["peak_hour"],
        "peak_hour_count": compat["peak_hour_count"],
        "weekly_trends": [],
        "graph_tool_transitions": compat["graph_tool_transitions"],
        "graph_cooccurrence": compat["graph_cooccurrence"],
        "graph_latency_histograms": {},
        "graph_dep": compat["graph_dep"],
        "graph_session_timeline": compat["graph_session_timeline"],
        "graph_semantic": compat["graph_semantic"],
        "agents": compat["agents"],
        "agent_comparison": compat["agent_comparison"],
        "mcp_calls": compat["mcp_calls"],
        "mcp_servers_by_count": compat["mcp_servers_by_count"],
        "mcp_server_before": compat["mcp_server_before"],
        "mcp_server_after": compat["mcp_server_after"],
        "subagent_types_by_count": compat["subagent_types_by_count"],
        "subagent_stops_by_type": compat["subagent_stops_by_type"],
        "subagent_launches": compat["subagent_launches"],
        "subagent_total_starts": compat["subagent_total_starts"],
        "subagent_total_stops": compat["subagent_total_stops"],
        "top_commands": compat["top_commands"],
        "unique_commands": compat["unique_commands"],
        "signature_command": compat["signature_command"],
        "signature_command_count": compat["signature_command_count"],
        "tool_percentiles": compat["tool_percentiles"],
        "model_costs": compat["model_costs"],
        "model_costs_usd": compat["model_costs_usd"],
        "strengths": insight_payload["strengths"],
        "observations": insight_payload["observations"],
        "recommendations": insight_payload["recommendations"],
        "practical_examples": insight_payload["practical_examples"],
        "achievements": insight_payload["achievements"],
        "token_economy": insight_payload["token_economy"],
    }
    payload["tool_failures"] = int(scoped_overview["failure_count"])
    payload["shell_executions"] = compat["shell_executions"]
    return payload


def _load_sql_session_detail(db_path: Path, session_id: str) -> dict[str, object] | None:
    from reflect.store.migrate import migrate
    from reflect.store.sqlite import connect_sqlite

    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        session = conn.execute(
            """
            SELECT s.*, COALESCE(a.name, '') AS agent
            FROM sessions s
            LEFT JOIN agents a ON a.id = s.agent_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if session is None:
            return None
        columns = [column[0] for column in conn.execute(
            """
            SELECT s.*, COALESCE(a.name, '') AS agent
            FROM sessions s
            LEFT JOIN agents a ON a.id = s.agent_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).description]
        session_row = dict(zip(columns, session, strict=True))
        steps = _dict_rows(conn.execute(
            """
            SELECT *
            FROM steps
            WHERE session_id = ?
            ORDER BY seq
            """,
            (session_id,),
        ))
        llm_by_step = {
            row["step_id"]: row
            for row in _dict_rows(conn.execute("SELECT * FROM llm_calls WHERE session_id = ?", (session_id,)))
        }
        tool_rows = _dict_rows(conn.execute("SELECT * FROM tool_calls WHERE session_id = ?", (session_id,)))
        tools_by_step = {row["step_id"]: row for row in tool_rows}
        mcp_rows = _dict_rows(conn.execute("SELECT * FROM mcp_calls WHERE session_id = ?", (session_id,)))
        mcp_by_step = {row["step_id"]: row for row in mcp_rows}
        raw_span_rows = _dict_rows(conn.execute(
            """
            SELECT id, event_type, trace_id, span_id, parent_span_id, observed_at
            FROM raw_events
            WHERE session_id = ?
              AND (
                COALESCE(trace_id, '') <> ''
                OR COALESCE(span_id, '') <> ''
                OR COALESCE(parent_span_id, '') <> ''
              )
            ORDER BY observed_at, id
            """,
            (session_id,),
        ))
        raw_log_rows = _dict_rows(conn.execute(
            """
            SELECT *
            FROM raw_events
            WHERE session_id = ? AND source_type LIKE '%log%'
            ORDER BY observed_at, id
            LIMIT 500
            """,
            (session_id,),
        ))
        raw_log_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM raw_events
            WHERE session_id = ? AND source_type LIKE '%log%'
            """,
            (session_id,),
        ).fetchone()[0]
        tool_inventory = _build_tool_inventory(
            [
                {
                    "tool_name": row.get("tool_name"),
                    "status": row.get("status"),
                    "duration_ms": row.get("duration_ms") or 0,
                    "input_preview": row.get("input_preview_redacted") or "",
                    "file_path": _extract_file_path_from_attrs(
                        attrs := _load_json_dict(row.get("raw_attrs_json"))
                    ),
                    "attrs": attrs,
                }
                for row in tool_rows
            ],
            [
                {
                    "tool_name": row.get("tool_name"),
                    "server": row.get("server_name"),
                    "status": row.get("status"),
                    "duration_ms": row.get("duration_ms") or 0,
                }
                for row in mcp_rows
            ],
            [
                {
                    "name": _sql_attr(
                        attrs := _load_json_dict(step.get("raw_attrs_json")),
                        "gen_ai.client.subagent_type",
                        "ide.subagent_type",
                        "subagent.type",
                    ) or "unknown",
                    "status": "stop" if "stop" in str(
                        _sql_attr(attrs, "gen_ai.client.hook.event", "ide.hook.event") or step.get("summary") or ""
                    ).lower() else "start",
                    "source": "lifecycle",
                }
                for step in steps
                if (
                    "subagent" in str(step.get("summary") or "").lower()
                    or "subagent" in str(step.get("raw_attrs_json") or "").lower()
                )
            ],
        )
    finally:
        conn.close()

    conversation: list[dict[str, object]] = []
    telemetry_spans: list[dict[str, object]] = []
    raw_by_step_id: dict[str, dict[str, object]] = {}
    raw_by_time_event: dict[tuple[str, str], dict[str, object]] = {}
    step_id_by_span_id: dict[str, str] = {}
    for row in raw_span_rows:
        step_id = _sql_step_id_for_raw_event(row["id"])
        raw_by_step_id[step_id] = row
        observed_at = str(row.get("observed_at") or "")
        event_type = str(row.get("event_type") or "")
        if observed_at and event_type:
            raw_by_time_event.setdefault((observed_at, event_type), row)
        span_id = str(row.get("span_id") or "")
        if span_id:
            step_id_by_span_id[span_id] = step_id
    for step in steps:
        attrs = _load_json_dict(step["raw_attrs_json"])
        event_type = str(_sql_attr(attrs, "gen_ai.client.hook.event") or step["summary"] or step["type"])
        raw_span = raw_by_step_id.get(step["id"]) or raw_by_time_event.get((str(step["started_at"] or ""), event_type)) or {}
        span_id = str(raw_span.get("span_id") or "")
        if span_id:
            step_id_by_span_id[span_id] = step["id"]
    seen_prompts: set[tuple[str, str]] = set()
    seen_prompt_generations: set[str] = set()
    seen_responses: set[tuple[str, str, int, int]] = set()
    seen_tools: set[tuple[str, str, str]] = set()
    for step in steps:
        attrs = _load_json_dict(step["raw_attrs_json"])
        event_type = str(_sql_attr(attrs, "gen_ai.client.hook.event") or step["summary"] or step["type"])
        event_lc = event_type.lower()
        is_prompt_event = "userpromptsubmit" in event_lc or event_lc.endswith(".prompt")
        is_response_event = event_lc == "stop" or event_lc.endswith(".stop")
        base_ts = step["started_at"] or ""
        generation_id = str(_sql_attr(attrs, "gen_ai.client.generation_id", "gen_ai.generation.id") or "")
        prompt = _sql_attr_text(
            attrs,
            "gen_ai.client.prompt",
            "gen_ai.client.prompt.text",
            "prompt",
            "input",
            limit=5000,
        )
        prompt_added = False
        if is_prompt_event:
            prompt_key = (generation_id or str(_sql_attr(attrs, "gen_ai.client.prompt.sha256") or ""), prompt)
            if prompt_key not in seen_prompts:
                seen_prompts.add(prompt_key)
                if generation_id:
                    seen_prompt_generations.add(generation_id)
                conversation.append({
                    "type": "prompt",
                    "ts": base_ts,
                    "preview": prompt or "Prompt text was not captured for this turn; metadata is available.",
                })
                prompt_added = True
        if step["id"] in llm_by_step:
            call = llm_by_step[step["id"]]
            has_prompt_for_call = prompt_added or (bool(generation_id) and generation_id in seen_prompt_generations)
            if call["input_tokens"] and not has_prompt_for_call:
                if generation_id:
                    seen_prompt_generations.add(generation_id)
                conversation.append({
                    "type": "prompt",
                    "ts": base_ts,
                    "input_tokens": call["input_tokens"],
                    "preview": "Prompt text was not captured for this turn; token metadata is available.",
                })
            if is_response_event or call["output_tokens"]:
                model = call["response_model"] or call["request_model"] or ""
                response_key = (
                    generation_id,
                    str(model or ""),
                    int(call["input_tokens"] or 0),
                    int(call["output_tokens"] or 0),
                )
                if response_key not in seen_responses:
                    seen_responses.add(response_key)
                    conversation.append({
                        "type": "response",
                        "ts": base_ts,
                        "model": model,
                        "input_tokens": call["input_tokens"],
                        "output_tokens": call["output_tokens"],
                        "preview": _sql_response_preview(attrs, call)
                        if call["output_tokens"]
                        else "Assistant turn completed, but response text was not captured.",
                    })
        if step["id"] in tools_by_step:
            tool = tools_by_step[step["id"]]
            tool_use_id = str(_sql_attr(attrs, "gen_ai.client.tool_use_id", "tool.id") or step["id"])
            tool_key = (tool_use_id, str(tool["tool_name"] or ""), str(tool["status"] or ""))
            if tool_key not in seen_tools:
                seen_tools.add(tool_key)
                conversation.append({
                    "type": "tool_call",
                    "ts": base_ts,
                    "tool_name": tool["tool_name"],
                    "preview": tool["input_preview_redacted"] or _sql_attr_text(
                        attrs,
                        "gen_ai.client.tool.input",
                        "tool.input",
                        "input",
                        limit=2000,
                    ),
                })
                conversation.append({
                    "type": "tool_result",
                    "ts": step["ended_at"] or base_ts,
                    "tool_name": tool["tool_name"],
                    "success": tool["status"] != "error",
                    "duration_ms": tool["duration_ms"] or 0,
                    "preview": tool["output_preview_redacted"] or _sql_attr_text(
                        attrs,
                        "gen_ai.client.tool.output",
                        "tool.output",
                        "output",
                        "error.message",
                        limit=2000,
                    ),
                })
        if step["id"] in mcp_by_step:
            mcp = mcp_by_step[step["id"]]
            conversation.append({
                "type": "mcp_call",
                "ts": base_ts,
                "tool_name": mcp["tool_name"] or "",
                "server": mcp["server_name"] or "",
                "success": mcp["status"] != "error",
            })
        raw_span = raw_by_step_id.get(step["id"]) or raw_by_time_event.get((str(step["started_at"] or ""), event_type)) or {}
        trace_id = str(raw_span.get("trace_id") or "")
        span_id = str(raw_span.get("span_id") or "")
        parent_span_id = str(raw_span.get("parent_span_id") or "")
        parent_id = str(step.get("parent_step_id") or "")
        if not parent_id and parent_span_id:
            parent_id = step_id_by_span_id.get(parent_span_id, "")
        telemetry_spans.append({
            "id": step["id"],
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "parent_id": parent_id,
            "name": step["summary"] or step["type"],
            "event": event_type,
            "agent": session_row.get("agent") or "",
            "tool_name": (tools_by_step.get(step["id"]) or {}).get("tool_name", ""),
            "mcp_tool": (mcp_by_step.get(step["id"]) or {}).get("tool_name", ""),
            "mcp_server": (mcp_by_step.get(step["id"]) or {}).get("server_name", ""),
            "phase": step["type"],
            "rel_ms": 0,
            "duration_ms": step["duration_ms"] or 0,
            "attrs": attrs,
        })
    anchor_ns = min(
        (
            value
            for value in [
                *(_iso_to_epoch_ns(step["started_at"]) for step in steps),
                *(_iso_to_epoch_ns(row["observed_at"]) for row in raw_log_rows),
            ]
            if value > 0
        ),
        default=0,
    )
    telemetry_logs: list[dict[str, object]] = []
    for row in raw_log_rows:
        attrs = _load_json_dict(row["attrs_json"])
        body = _load_json_dict(row["body_json"])
        body_text = _sql_log_body_text(body, attrs, row.get("event_type"))
        observed_ns = _iso_to_epoch_ns(row["observed_at"])
        telemetry_logs.append({
            "trace_id": row.get("trace_id") or "",
            "span_id": row.get("span_id") or "",
            "service": attrs.get("service.name", ""),
            "agent": attrs.get("gen_ai.client.name", ""),
            "event": attrs.get("gen_ai.client.hook.event", row.get("event_type") or ""),
            "tool_name": attrs.get("gen_ai.client.tool_name", ""),
            "mcp_tool": attrs.get("gen_ai.client.mcp_tool", ""),
            "mcp_server": attrs.get("gen_ai.client.mcp_server", ""),
            "severity": _telemetry_severity("", 0, body_text),
            "time_ns": observed_ns,
            "rel_ms": round((observed_ns - anchor_ns) / 1e6, 1) if anchor_ns and observed_ns else 0,
            "body": body_text[:2000],
            "attrs": _sanitize_telemetry_attrs(attrs),
        })
    services = {
        service
        for service in [
            *(
                str(span.get("service") or span.get("agent") or "")
                for span in telemetry_spans
            ),
            *(str(log.get("service") or log.get("agent") or "") for log in telemetry_logs),
        ]
        if service
    }
    errors = sum(1 for step in steps if step["status"] == "error") + sum(
        1 for log in telemetry_logs if log.get("severity") in {"ERROR", "FATAL"}
    )
    tool_inventory = _add_skill_hints_to_inventory(
        tool_inventory,
        {
            skill_name
            for event in conversation
            if event.get("type") in {"prompt", "response"}
            for skill_name in _extract_skill_names_from_text(str(event.get("preview") or ""))
        },
    )
    tool_inventory = _add_subagent_hints_to_inventory(
        tool_inventory,
        {
            subagent_name
            for event in conversation
            if event.get("type") in {"prompt", "response"}
            for subagent_name in _extract_subagent_names_from_text(str(event.get("preview") or ""))
        },
    )
    return {
        "session_id": session_id,
        "conversation": conversation,
        "tool_inventory": tool_inventory,
        "telemetry": {
            "summary": {
                "spans": len(telemetry_spans),
                "logs": int(raw_log_count or 0),
                "errors": errors,
                "warnings": sum(1 for log in telemetry_logs if log.get("severity") == "WARN"),
                "services": len(services),
                "duration_ms": 0,
                "truncated_spans": 0,
                "truncated_logs": max(0, int(raw_log_count or 0) - len(telemetry_logs)),
            },
            "spans": telemetry_spans,
            "logs": telemetry_logs,
            "warnings": [],
        },
        "warnings": [],
    }


def _start_publish_server(
    stats: TelemetryStats,
    *,
    db_path: Path | None = None,
    sql_only: bool = False,
    preparation_worker: BackgroundPreparationWorker | None = None,
) -> None:
    """Start a local FastAPI server and open the dashboard in a browser.

    Blocks until Ctrl-C. Uses ``?report=api/data`` so the dashboard
    fetches JSON from the API — no URL encoding at all.
    """
    port = int(os.environ.get("REFLECT_PORT", "8765"))
    docs_dir = _dashboard_docs_dir()
    _start_publish_server_inline(
        stats,
        port,
        docs_dir,
        db_path=db_path,
        sql_only=sql_only,
        preparation_worker=preparation_worker,
    )


def _build_dashboard_app(
    stats: TelemetryStats,
    *,
    docs_dir: Path,
    db_path: Path | None = None,
    sql_only: bool = False,
    preparation_worker: BackgroundPreparationWorker | None = None,
):
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    globals()["Request"] = Request

    app = FastAPI(title="reflect dashboard", docs_url=None, redoc_url=None)
    if db_path is not None:
        if preparation_worker is not None:
            dashboard_cache = DashboardDataCache(
                lambda: _sql_dashboard_payload(
                    db_path,
                    limit=50,
                    offset=0,
                    lazy_all_tabs=True,
                ),
                refresh_loader=lambda: _sql_dashboard_payload(
                    db_path,
                    limit=50,
                    offset=0,
                    lazy_heavy_tabs=True,
                ),
            )
        else:
            dashboard_cache = DashboardDataCache(
                lambda: _sql_dashboard_payload(db_path, limit=50, offset=0)
            )
    elif sql_only:
        raise ValueError("sql_only requires db_path")
    else:
        import json as _json

        def load_stats_payload() -> dict[str, object]:
            payload = _json.loads(_build_dashboard_json(stats))
            payload["comparison"] = None
            return payload

        dashboard_cache = DashboardDataCache(load_stats_payload)
    if preparation_worker is not None:
        preparation_worker.add_completion_callback(lambda _result: dashboard_cache.refresh())

    @app.get("/api/data")
    def api_data(request: Request):
        perf_start = _perf_start()
        perf_kind = "unknown"
        params = request.query_params
        q = (params.get("q") or "").strip()
        session_id = (params.get("session") or "").strip()
        agents = {agent for agent in (params.get("agents") or "").split(",") if agent}
        legacy_agent = (params.get("agent") or "").strip()
        if legacy_agent and legacy_agent != "all":
            agents.add(legacy_agent)
        model = params.get("model") or "all"
        status = params.get("status") or "all"
        range_name = params.get("range") or "all"
        active_tab = (params.get("tab") or "sessions").strip().lower()
        filtered_base_tabs = {
            "overview": {"activity", "models", "costs", "mcp"},
            "tools": {"tools"},
            "compare": {"agents"},
            "observations": {"activity", "models", "costs", "tools", "mcp", "agents"},
        }.get(active_tab, set())
        if session_id:
            filtered_base_tabs = {"activity", "models", "costs", "tools", "mcp", "agents"}
        has_filter = any([q, session_id, agents, model != "all", status != "all", range_name != "all"])
        try:
            if db_path is not None:
                if not has_filter:
                    perf_kind = "cached"
                    return JSONResponse(dashboard_cache.get())
                if session_id and not any([q, agents, model != "all", status != "all", range_name != "all"]):
                    perf_kind = "session"
                    return JSONResponse(_sql_dashboard_session_payload(db_path, session_id))
                perf_kind = "filtered"
                return JSONResponse(_sql_dashboard_payload(
                    db_path,
                    limit=50,
                    offset=0,
                    q=q,
                    session_id=session_id,
                    agents=agents,
                    model=model,
                    status=status,
                    range_name=range_name,
                    lazy_heavy_tabs=True,
                    include_comparison=active_tab == "compare",
                    base_tab_names=filtered_base_tabs,
                ))
            if not has_filter:
                perf_kind = "cached-no-sql"
                return JSONResponse(dashboard_cache.get())
            perf_kind = "filter-no-sql"
            return JSONResponse(
                {
                    "error": "Filtered report views require the local SQLite report store. Re-run `reflect` to prepare it.",
                    "sql_backed": False,
                },
                status_code=409,
            )
        finally:
            _perf_finish(
                "api.data",
                perf_start,
                kind=perf_kind,
                session=bool(session_id),
                agents=",".join(sorted(agents)),
            )

    @app.get("/api/sql/overview")
    def api_sql_overview():
        if db_path is None:
            return JSONResponse({"error": "SQLite report view is not configured"}, status_code=404)
        try:
            return JSONResponse(_sql_report_payload(db_path, limit=0, offset=0)["overview"])
        except Exception as exc:
            return JSONResponse({"error": str(exc), "db_path": str(db_path)}, status_code=500)

    @app.get("/api/sql/sessions")
    def api_sql_sessions(request: Request):
        if db_path is None:
            return JSONResponse({"error": "SQLite report view is not configured"}, status_code=404)
        params = request.query_params
        from reflect.store.migrate import migrate
        from reflect.store.sqlite import connect_sqlite
        from reflect.views.sessions import list_sessions

        conn = connect_sqlite(db_path)
        try:
            migrate(conn)
            page = list_sessions(
                conn,
                limit=int(params.get("limit") or 50),
                offset=int(params.get("offset") or 0),
                agent=params.get("agent") or None,
                repo=params.get("repo") or None,
                model=params.get("model") or None,
                status=params.get("status") or None,
                date_from=params.get("date_from") or None,
                date_to=params.get("date_to") or None,
                min_cost=_optional_float(params.get("min_cost")),
                max_cost=_optional_float(params.get("max_cost")),
                min_failures=_optional_int(params.get("min_failures")),
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc), "db_path": str(db_path)}, status_code=500)
        finally:
            conn.close()
        return JSONResponse(page.model_dump())

    @app.get("/api/tabs/{tab_name}")
    def api_tab(tab_name: str, request: Request):
        perf_start = _perf_start()
        if db_path is None:
            try:
                return JSONResponse({"error": "SQLite report view is not configured"}, status_code=404)
            finally:
                _perf_finish("api.tabs", perf_start, tab=tab_name, status=404)
        session_id = (request.query_params.get("session") or "").strip()
        try:
            return JSONResponse(_sql_dashboard_tab_payload(db_path, tab_name, session_id=session_id))
        except ValueError as exc:
            return JSONResponse({"error": str(exc), "tab": tab_name}, status_code=404)
        except Exception as exc:
            return JSONResponse({"error": str(exc), "db_path": str(db_path)}, status_code=500)
        finally:
            _perf_finish("api.tabs", perf_start, tab=tab_name, session=bool(session_id))

    @app.get("/api/session/{session_id:path}")
    def api_session(session_id: str):
        if db_path is not None:
            detail = _load_sql_session_detail(db_path, session_id)
            if detail is None:
                return JSONResponse({"error": f"Session {session_id} not found"}, status_code=404)
            return JSONResponse(detail, headers={"Access-Control-Allow-Origin": "*"})
        detail = _load_session_detail(session_id, stats)
        if detail is None:
            return JSONResponse({"error": f"Session {session_id} not found"}, status_code=404)
        return JSONResponse(detail, headers={"Access-Control-Allow-Origin": "*"})

    @app.get("/api/status")
    def api_status():
        snapshot = (
            preparation_worker.snapshot()
            if preparation_worker is not None
            else PreparationSnapshot(state=PreparationState.IDLE, generation=0)
        )
        return JSONResponse({"preparation": snapshot.as_dict()})

    @app.get("/")
    def index():
        html_file = docs_dir / "report.html"
        if not html_file.exists():
            html_file = docs_dir / "index.html"
        return FileResponse(html_file, media_type="text/html")

    if docs_dir.exists():
        app.mount("/", StaticFiles(directory=str(docs_dir)), name="static")

    return app


def _optional_float(value: str | None) -> float | None:
    return None if value in (None, "") else float(value)


def _optional_int(value: str | None) -> int | None:
    return None if value in (None, "") else int(value)


def _start_publish_server_inline(
    stats: TelemetryStats,
    port: int,
    docs_dir: Path,
    *,
    db_path: Path | None = None,
    sql_only: bool = False,
    preparation_worker: BackgroundPreparationWorker | None = None,
) -> None:
    """Inline FastAPI server for the local `reflect` browser report."""
    import threading
    import webbrowser

    try:
        import uvicorn
        __import__("fastapi")
    except ImportError:
        logger.warning("FastAPI/uvicorn not installed. Install with: pip install fastapi uvicorn")
        logger.warning("Falling back to writing artifact file...")
        artifact = docs_dir / "_reflect_data.json"
        if db_path is not None:
            artifact.write_text(json.dumps(_sql_dashboard_payload(db_path)), encoding="utf-8")
        else:
            artifact.write_text(_build_dashboard_json(stats), encoding="utf-8")
        print(f"Wrote: {artifact}")
        return

    app = _build_dashboard_app(
        stats,
        docs_dir=docs_dir,
        db_path=db_path,
        sql_only=sql_only,
        preparation_worker=preparation_worker,
    )
    url = f"http://127.0.0.1:{port}/?report=api/data"
    threading.Timer(0.5, webbrowser.open, args=[url]).start()
    print(f"\n  Serving at: {url}")
    print("  Press Ctrl-C to stop\n")
    if preparation_worker is not None:
        preparation_worker.start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        if preparation_worker is not None:
            preparation_worker.close()


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
