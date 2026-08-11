from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from reflect.dashboard_query_common import (
    dict_rows,
    empty_sql_lazy_tabs,
    load_json_dict,
    quality_rules_payload,
    session_card_from_row,
    sql_attr,
    sql_insight_payload,
    sql_session_first_prompts,
)
from reflect.graph import _compute_weekly_trends
from reflect.store.hook_facts import HookFactRepository
from reflect.telemetry_facts import (
    clean_subagent_name,
    extract_skill_name_from_path,
    extract_skill_name_from_preview,
    extract_skill_names_from_text,
    extract_subagent_name_from_tool,
    extract_subagent_names_from_text,
)
from reflect.utils import logger


def _telemetry_severity(
    severity_text: str | None,
    severity_number: int | None,
    body_text: str | None = None,
) -> str:
    if severity_text:
        return str(severity_text).upper()
    value = int(severity_number or 0)
    match = next(
        (
            label
            for threshold, label in (
                (21, "FATAL"),
                (17, "ERROR"),
                (13, "WARN"),
                (9, "INFO"),
                (5, "DEBUG"),
            )
            if value >= threshold
        ),
        None,
    )
    if match:
        return match
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
        if key not in allowed_keys:
            continue
        if isinstance(value, (bool, int, float)):
            safe[key] = value
        elif text := str(value).strip():
            safe[key] = text[:280] + ("…" if len(text) > 280 else "")
    return safe


def _extract_file_path_from_attrs(attrs: dict[str, object]) -> str:
    return str(
        sql_attr(
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
        preview = str(
            event.get("input_preview") or event.get("preview") or event.get("input") or ""
        ).strip()
        if preview and len(tool_examples[tool_name]) < 3:
            tool_examples[tool_name].append(preview[:500])
        file_path = str(event.get("file_path") or "").strip()
        path_skill = extract_skill_name_from_path(file_path)
        if path_skill:
            skills[path_skill] += count
            skill_tools[path_skill][tool_name] += count
        if tool_name == "skill":
            skill_name = extract_skill_name_from_preview(preview)
            if skill_name:
                skills[skill_name] += count
                skill_tools[skill_name][tool_name] += count
        attrs = event.get("attrs")
        if not isinstance(attrs, dict):
            attrs = {}
        subagent_name = extract_subagent_name_from_tool(tool_name, attrs, preview)
        if subagent_name:
            subagents[subagent_name] += count
            subagent_sources[subagent_name][tool_name] += count

    for event in mcp_events or []:
        tool_name = str(event.get("tool_name") or "").strip()
        server_name = str(event.get("server") or event.get("server_name") or "").strip()
        label = (
            f"{server_name}/{tool_name}" if server_name and tool_name else tool_name or server_name
        )
        if label:
            mcp_tools[label] += 1
        if server_name:
            mcp_servers[server_name] += 1

    for event in subagent_events or []:
        name = clean_subagent_name(event.get("name")) or "unknown"
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
        "mcp_tools": [{"name": name, "count": count} for name, count in mcp_tools.most_common()],
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
            {"name": name, "count": count} for name, count in mcp_servers.most_common()
        ],
        "total_tool_calls": sum(tools.values()),
        "total_skill_calls": sum(skills.values()),
        "total_mcp_calls": sum(mcp_tools.values()),
        "total_subagent_starts": sum(subagents.values()),
        "total_subagent_stops": sum(subagent_stops.values()),
    }


def _add_skill_hints_to_inventory(
    inventory: dict[str, object], skill_names: set[str]
) -> dict[str, object]:
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


def _add_subagent_hints_to_inventory(
    inventory: dict[str, object], subagent_names: set[str]
) -> dict[str, object]:
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
        inventory["total_subagent_starts"] = int(inventory.get("total_subagent_starts") or 0) + len(
            hinted
        )
    return inventory


def _sql_step_id_for_raw_event(raw_event_id: object) -> str:
    digest = hashlib.sha1(str(raw_event_id or "").encode("utf-8")).hexdigest()
    return f"step_{digest}"


def _sql_attr_text(attrs: dict[str, object], *keys: str, limit: int = 500) -> str:
    value = sql_attr(attrs, *keys)
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


def _conversation_event_time_ns(event: dict[str, object]) -> int:
    return _iso_to_epoch_ns(event.get("timestamp") or event.get("ts"))


def _conversation_events_match(
    telemetry_event: dict[str, object],
    native_event: dict[str, object],
) -> bool:
    event_type = str(telemetry_event.get("type") or "")
    if event_type != str(native_event.get("type") or ""):
        return False
    telemetry_tool_id = str(telemetry_event.get("tool_use_id") or "")
    native_tool_id = str(native_event.get("tool_use_id") or "")
    if telemetry_tool_id and native_tool_id:
        return telemetry_tool_id == native_tool_id
    if event_type in {"tool_call", "tool_result"}:
        telemetry_tool = str(telemetry_event.get("tool_name") or "").casefold()
        native_tool = str(native_event.get("tool_name") or "").casefold()
        if telemetry_tool and native_tool and telemetry_tool != native_tool:
            return False
    telemetry_text = str(
        telemetry_event.get("content") or telemetry_event.get("preview") or ""
    ).strip()
    native_text = str(native_event.get("content") or native_event.get("preview") or "").strip()
    if telemetry_text and native_text and telemetry_text == native_text:
        return True
    telemetry_time = _conversation_event_time_ns(telemetry_event)
    native_time = _conversation_event_time_ns(native_event)
    return bool(
        telemetry_time and native_time and abs(telemetry_time - native_time) <= 120 * 1_000_000_000
    )


def _merge_native_conversation_events(
    telemetry_events: list[dict[str, object]],
    native_events: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Enrich telemetry chronology with native text without dropping execution evidence."""
    merged = [dict(event) for event in telemetry_events]
    claimed_telemetry_indexes: set[int] = set()
    text_fields = (
        "content",
        "timestamp",
        "tool_name",
        "tool_use_id",
        "model",
        "server",
        "subagent_type",
    )
    numeric_fields = ("input_tokens", "output_tokens", "cache_read_tokens", "duration_ms")

    for native_event in native_events:
        candidates = [
            index
            for index, telemetry_event in enumerate(merged)
            if index not in claimed_telemetry_indexes
            and _conversation_events_match(telemetry_event, native_event)
        ]
        if not candidates:
            merged.append(dict(native_event))
            continue
        native_time = _conversation_event_time_ns(native_event)
        match_index = min(
            candidates,
            key=lambda index: abs(_conversation_event_time_ns(merged[index]) - native_time),
        )
        claimed_telemetry_indexes.add(match_index)
        enriched = dict(merged[match_index])
        for field in text_fields:
            value = native_event.get(field)
            if value not in (None, ""):
                enriched[field] = value
        for field in numeric_fields:
            value = native_event.get(field)
            if isinstance(value, (int, float)) and value > 0:
                enriched[field] = value
        merged[match_index] = enriched

    return sorted(
        merged,
        key=lambda event: _conversation_event_time_ns(event) or 2**63,
    )


def _sql_log_body_text(
    body: dict[str, object], attrs: dict[str, object], event_type: object
) -> str:
    for key in ("message", "body", "text", "content", "error.message", "exception.message"):
        value = body.get(key)
        if value not in (None, ""):
            return _sql_attr_text(body, key, limit=2000)
    for key in ("error.message", "exception.message"):
        value = attrs.get(key)
        if value not in (None, ""):
            return _sql_attr_text(attrs, key, limit=2000)
    event = str(attrs.get("gen_ai.client.hook.event") or event_type or "").strip()
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


def build_session_payload(db_path: Path, session_id: str) -> dict[str, object]:
    from reflect.store.sqlite import connect_sqlite_read_only
    from reflect.views import (
        build_report_tab,
        display_mcp_server_name,
        list_sessions,
        list_source_provenance,
    )

    conn = connect_sqlite_read_only(db_path)
    try:
        row = dict_rows(
            conn.execute(
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
              (SELECT COUNT(*) FROM steps st WHERE st.session_id = s.id) AS event_count,
              COALESCE(sr.prompt_count, 0) AS prompt_count,
              COALESCE(sr.tool_call_count, 0) AS tool_call_count,
              COALESCE(sr.error_count, s.failure_count, 0) AS failure_count,
              COALESCE(sr.input_tokens, s.input_tokens, 0) AS input_tokens,
              COALESCE(sr.output_tokens, s.output_tokens, 0) AS output_tokens,
              COALESCE(sr.cache_write_tokens, s.cache_creation_tokens, 0) AS cache_creation_tokens,
              COALESCE(sr.cache_read_tokens, s.cache_read_tokens, 0) AS cache_read_tokens,
              s.token_provenance,
              COALESCE(sr.total_cost, s.estimated_cost_usd, 0) AS estimated_cost_usd
            FROM sessions s
            LEFT JOIN agents a ON a.id = s.agent_id
            LEFT JOIN session_rollups sr ON sr.session_id = s.id
            WHERE s.id = ?
            """,
                (session_id,),
            )
        )
        if not row:
            return {
                "sql_backed": True,
                "focused_session_id": session_id,
                "sessions": [],
                "first_event_ts": "",
                "last_event_ts": "",
                "sqlite": {
                    "db_path": str(db_path),
                    "overview": {"session_count": 0},
                    "sessions": {"rows": [], "total": 0, "limit": 1, "offset": 0},
                    "tabs": empty_sql_lazy_tabs(),
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
        for prompt_row in dict_rows(
            conn.execute(
                """
            SELECT raw_attrs_json
            FROM steps
            WHERE session_id = ?
            ORDER BY seq
            LIMIT 50
            """,
                (session_id,),
            )
        ):
            attrs = load_json_dict(prompt_row["raw_attrs_json"])
            first_prompt = str(
                sql_attr(
                    attrs,
                    "gen_ai.client.prompt",
                    "gen_ai.client.prompt.text",
                    "prompt",
                    "input",
                )
                or ""
            ).strip()
            if first_prompt:
                break
        tool_rows = dict_rows(
            conn.execute(
                """
            SELECT tool_name, COUNT(*) AS count
            FROM tool_calls
            WHERE session_id = ?
            GROUP BY tool_name
            ORDER BY count DESC, tool_name ASC
            LIMIT 10
            """,
                (session_id,),
            )
        )
        mcp_rows = dict_rows(
            conn.execute(
                """
            SELECT mc.server_name, COUNT(*) AS count
            FROM mcp_calls AS mc
            JOIN tool_calls AS tc ON tc.id = mc.tool_call_id
            WHERE tc.session_id = ?
              AND mc.server_name IS NOT NULL
              AND mc.server_name <> ''
            GROUP BY mc.server_name
            ORDER BY count DESC, mc.server_name ASC
            LIMIT 10
            """,
                (session_id,),
            )
        )
        event_rows = dict_rows(
            conn.execute(
                """
            SELECT type, COUNT(*) AS count
            FROM steps
            WHERE session_id = ?
            GROUP BY type
            ORDER BY count DESC, type ASC
            """,
                (session_id,),
            )
        )
        operational_tabs = {
            tab_name: build_report_tab(conn, tab_name, session_ids={session_id})
            for tab_name in ("activity", "models", "costs", "tools", "mcp", "agents")
        }
        source_provenance = list_source_provenance(conn, session_ids={session_id})
        navigation_page = list_sessions(conn, limit=100, offset=0).model_dump()
    finally:
        conn.close()

    navigation_first_prompts = sql_session_first_prompts(
        db_path,
        {str(row["session_id"]) for row in navigation_page["rows"]},
    )

    tools_by_count = {str(row["tool_name"]): int(row["count"] or 0) for row in tool_rows}
    mcp_servers: Counter[str] = Counter()
    for row in mcp_rows:
        server = display_mcp_server_name(row["server_name"])
        if server:
            mcp_servers[server] += int(row["count"] or 0)
    mcp_servers_by_count = dict(mcp_servers)
    events_by_type = {str(row["type"]): int(row["count"] or 0) for row in event_rows}
    cost = float(session_row["estimated_cost_usd"] or 0.0)
    session_card = session_card_from_row(
        session_row,
        first_prompt=first_prompt,
        primary_model=primary_model,
        tools=tools_by_count,
    )
    quality_score = float(session_card["quality_score"])
    reported_quality_score = quality_score if session_card["quality_available"] else 0.0
    total_tokens = int(session_card["total_tokens"])
    navigation_cards = [
        session_card
        if str(navigation_row["session_id"]) == session_id
        else session_card_from_row(
            dict(navigation_row),
            first_prompt=navigation_first_prompts.get(str(navigation_row["session_id"]), ""),
        )
        for navigation_row in navigation_page["rows"]
    ]
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
    tabs = empty_sql_lazy_tabs()
    tabs["usage"].update(
        {
            "avg_quality_score": reported_quality_score,
            "unique_sessions": 1,
            "first_event_ts": session_row["started_at"] or "",
            "prompt_submits": scoped_overview["prompt_count"],
            "tool_calls": scoped_overview["tool_call_count"],
            "tool_to_prompt_ratio": (
                f"{scoped_overview['tool_call_count'] / scoped_overview['prompt_count']:.1f}"
                if scoped_overview["prompt_count"]
                else "0.0"
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
            "pricing_unit": "usd",
            "pricing_source": "local",
            "model_costs": {primary_model: cost} if primary_model else {},
        }
    )
    tabs["activity"]["events_by_type"] = events_by_type
    tabs["models"] = {
        "models_by_count": {primary_model: 1} if primary_model else {},
        "unique_models": 1 if primary_model else 0,
    }
    tabs["costs"].update(
        {
            "model_costs": {primary_model: cost} if primary_model else {},
            "model_costs_usd": {primary_model: cost} if primary_model else {},
            "total_cache_creation_tokens": int(session_row["cache_creation_tokens"] or 0),
            "total_cache_read_tokens": int(session_row["cache_read_tokens"] or 0),
        }
    )
    tabs["tools"]["tools_by_count"] = tools_by_count
    tabs["mcp"].update(
        {
            "mcp_calls": sum(mcp_servers_by_count.values()),
            "mcp_servers_by_count": mcp_servers_by_count,
            "mcp_server_before": mcp_servers_by_count,
            "mcp_server_after": mcp_servers_by_count,
        }
    )
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
        "avg_quality": reported_quality_score,
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
    failure_rate_pct = (
        round(
            100 * scoped_overview["failure_count"] / scoped_overview["tool_call_count"],
            1,
        )
        if scoped_overview["tool_call_count"]
        else 0.0
    )
    weekly_trends = _compute_weekly_trends(Counter(activity_view["activity_by_day"]))
    tabs["activity"]["weekly_trends"] = weekly_trends
    session_card["skills"] = tools_view["skills_by_count"]
    tabs["usage"].update(
        {
            "failure_rate_pct": failure_rate_pct,
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
            "source_provenance": source_provenance,
            "total_cache_creation_tokens": costs_view["total_cache_creation_tokens"],
            "total_cache_read_tokens": costs_view["total_cache_read_tokens"],
            "input_cost_usd": costs_view["cost_breakdown"]["input_cost_usd"],
            "output_cost_usd": costs_view["cost_breakdown"]["output_cost_usd"],
            "cache_creation_cost_usd": costs_view["cost_breakdown"]["cache_creation_cost_usd"],
            "cache_read_cost_usd": costs_view["cost_breakdown"]["cache_read_cost_usd"],
            "model_costs": costs_view["model_costs"],
            "agent_cost_over_time": costs_view["agent_cost_over_time"],
        }
    )
    insight_payload = sql_insight_payload(
        scoped_overview,
        [session_card],
        {
            "total_cache_creation_tokens": int(session_row["cache_creation_tokens"] or 0),
            "total_cache_read_tokens": int(session_row["cache_read_tokens"] or 0),
            "mcp_calls": mcp_view["mcp_calls"],
            "mcp_servers_by_count": mcp_view["mcp_servers_by_count"],
            "subagent_total_starts": tools_view["subagent_total_starts"],
            "file_reads": tools_view["file_reads"],
            "unique_models": models_view["unique_models"],
            "unique_commands": tools_view["unique_commands"],
        },
    )
    tabs["observations"] = {
        "strengths": insight_payload["strengths"],
        "observations": insight_payload["observations"],
        "recommendations": insight_payload["recommendations"],
        "practical_examples": insight_payload["practical_examples"],
        "achievements": insight_payload["achievements"],
        "token_economy": insight_payload["token_economy"],
    }
    tabs["cohort_comparison"] = {
        "comparison": None,
        "agent_comparison": [agent_payload],
    }
    return {
        "sql_backed": True,
        "sqlite": {
            "db_path": str(db_path),
            "overview": scoped_overview,
            "sessions": navigation_page,
            "tabs": tabs,
        },
        "sessions": navigation_cards,
        "quality_rules": quality_rules_payload(),
        "session_list_total": navigation_page["total"],
        "focused_session_id": session_id,
        "first_event_ts": session_row["started_at"] or "",
        "last_event_ts": session_row["started_at"] or "",
    }


def load_session_detail(db_path: Path, session_id: str) -> dict[str, object] | None:
    from reflect.store.sqlite import connect_sqlite_read_only

    conn = connect_sqlite_read_only(db_path)
    try:
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
        columns = [
            column[0]
            for column in conn.execute(
                """
            SELECT s.*, COALESCE(a.name, '') AS agent
            FROM sessions s
            LEFT JOIN agents a ON a.id = s.agent_id
            WHERE s.id = ?
            """,
                (session_id,),
            ).description
        ]
        session_row = dict(zip(columns, session, strict=True))
        steps = dict_rows(
            conn.execute(
                """
            SELECT *
            FROM steps
            WHERE session_id = ?
            ORDER BY seq
            """,
                (session_id,),
            )
        )
        llm_by_step = {
            row["step_id"]: row
            for row in dict_rows(
                conn.execute("SELECT * FROM llm_calls WHERE session_id = ?", (session_id,))
            )
        }
        hook_facts = HookFactRepository(conn).load_session(session_id)
        tool_rows = dict_rows(
            conn.execute("SELECT * FROM tool_calls WHERE session_id = ?", (session_id,))
        )
        tools_by_step = {row["step_id"]: row for row in tool_rows}
        mcp_rows = dict_rows(
            conn.execute(
                """
            SELECT
              mc.*,
              tc.step_id,
              tc.session_id,
              tc.status,
              tc.duration_ms,
              tc.raw_attrs_json
            FROM mcp_calls AS mc
            JOIN tool_calls AS tc ON tc.id = mc.tool_call_id
            WHERE tc.session_id = ?
            """,
                (session_id,),
            )
        )
        mcp_by_step = {row["step_id"]: row for row in mcp_rows}
        raw_span_rows = dict_rows(
            conn.execute(
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
            )
        )
        raw_log_rows = dict_rows(
            conn.execute(
                """
            SELECT *
            FROM raw_events
            WHERE session_id = ? AND source_type LIKE '%log%'
            ORDER BY observed_at, id
            LIMIT 500
            """,
                (session_id,),
            )
        )
        raw_log_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM raw_events
            WHERE session_id = ? AND source_type LIKE '%log%'
            """,
            (session_id,),
        ).fetchone()[0]
        native_source = conn.execute(
            """
            SELECT source_id
            FROM raw_events
            WHERE session_id = ? AND source_type = 'native_session'
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if native_source and native_source[0]:
            session_row["source_kind"] = "native_session"
            session_row["source_ref"] = native_source[0]
        tool_inventory = _build_tool_inventory(
            [
                {
                    "tool_name": row.get("tool_name"),
                    "status": row.get("status"),
                    "duration_ms": row.get("duration_ms") or 0,
                    "input_preview": row.get("input_preview_redacted") or "",
                    "file_path": _extract_file_path_from_attrs(
                        attrs := load_json_dict(row.get("raw_attrs_json"))
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
                    "name": sql_attr(
                        attrs := load_json_dict(step.get("raw_attrs_json")),
                        "gen_ai.client.subagent_type",
                        "ide.subagent_type",
                        "subagent.type",
                    )
                    or "unknown",
                    "status": "stop"
                    if "stop"
                    in str(
                        sql_attr(attrs, "gen_ai.client.hook.event", "ide.hook.event")
                        or step.get("summary")
                        or ""
                    ).lower()
                    else "start",
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
    anchor_ns = min(
        (
            value
            for value in [
                *(_iso_to_epoch_ns(step["started_at"]) for step in steps),
                *(_iso_to_epoch_ns(row["observed_at"]) for row in raw_span_rows),
                *(_iso_to_epoch_ns(row["observed_at"]) for row in raw_log_rows),
            ]
            if value > 0
        ),
        default=0,
    )
    for step in steps:
        attrs = load_json_dict(step["raw_attrs_json"])
        event_type = str(
            sql_attr(attrs, "gen_ai.client.hook.event") or step["summary"] or step["type"]
        )
        raw_span = (
            raw_by_step_id.get(step["id"])
            or raw_by_time_event.get((str(step["started_at"] or ""), event_type))
            or {}
        )
        span_id = str(raw_span.get("span_id") or "")
        if span_id:
            step_id_by_span_id[span_id] = step["id"]
    seen_prompts: set[tuple[str, str]] = set()
    seen_responses: set[tuple[object, ...]] = set()
    seen_tools: set[tuple[str, str, str]] = set()
    for step in steps:
        attrs = load_json_dict(step["raw_attrs_json"])
        event_type = str(
            sql_attr(attrs, "gen_ai.client.hook.event") or step["summary"] or step["type"]
        )
        event_lc = event_type.lower()
        is_prompt_event = "userpromptsubmit" in event_lc or event_lc.endswith(".prompt")
        is_response_event = event_lc == "stop" or event_lc.endswith(".stop")
        base_ts = step["started_at"] or ""
        generation_id = str(
            sql_attr(attrs, "gen_ai.client.generation_id", "gen_ai.generation.id") or ""
        )
        prompt = _sql_attr_text(
            attrs,
            "gen_ai.client.prompt",
            "gen_ai.client.prompt.text",
            "prompt",
            "input",
            limit=5000,
        )
        prompt_fact = hook_facts.prompt_for_step(step["id"])
        if prompt_fact:
            prompt = str(prompt_fact.get("content_preview_redacted") or prompt)
        if is_prompt_event:
            prompt_hash = str(
                (prompt_fact or {}).get("content_hash")
                or sql_attr(attrs, "gen_ai.client.prompt.sha256")
                or ""
            )
            prompt_key = (generation_id or prompt_hash, prompt)
            if prompt_key not in seen_prompts:
                seen_prompts.add(prompt_key)
                conversation.append(
                    {
                        "type": "prompt",
                        "ts": base_ts,
                        "preview": prompt
                        or "Prompt text was not captured for this turn; metadata is available.",
                        "content_hash": prompt_hash,
                        "content_length": int((prompt_fact or {}).get("content_length") or 0),
                    }
                )
        response_facts = hook_facts.responses_for_step(step["id"])
        for response_fact in response_facts:
            response_hash = str(response_fact.get("content_hash") or "")
            response_identity = response_hash or str(response_fact.get("id") or step["id"])
            response_key = (response_identity, "hook", 0, 0)
            if response_key in seen_responses:
                continue
            seen_responses.add(response_key)
            conversation.append(
                {
                    "type": "response",
                    "ts": base_ts,
                    "preview": response_fact.get("content_preview_redacted")
                    or "Assistant turn completed, but response text was not captured.",
                    "content_hash": response_hash,
                    "content_length": int(response_fact.get("content_length") or 0),
                }
            )
        if step["id"] in llm_by_step and not response_facts:
            call = llm_by_step[step["id"]]
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
                    conversation.append(
                        {
                            "type": "response",
                            "ts": base_ts,
                            "model": model,
                            "input_tokens": call["input_tokens"],
                            "output_tokens": call["output_tokens"],
                            "preview": _sql_response_preview(attrs, call)
                            if call["output_tokens"]
                            else "Assistant turn completed, but response text was not captured.",
                        }
                    )
        if step["id"] in tools_by_step:
            tool = tools_by_step[step["id"]]
            tool_use_id = str(
                sql_attr(attrs, "gen_ai.client.tool_use_id", "tool.id") or step["id"]
            )
            tool_key = (tool_use_id, str(tool["tool_name"] or ""), str(tool["status"] or ""))
            if tool_key not in seen_tools:
                seen_tools.add(tool_key)
                conversation.append(
                    {
                        "type": "tool_call",
                        "ts": base_ts,
                        "tool_name": tool["tool_name"],
                        "tool_use_id": tool_use_id,
                        "preview": tool["input_preview_redacted"]
                        or _sql_attr_text(
                            attrs,
                            "gen_ai.client.tool.input",
                            "tool.input",
                            "input",
                            limit=2000,
                        ),
                    }
                )
                conversation.append(
                    {
                        "type": "tool_result",
                        "ts": step["ended_at"] or base_ts,
                        "tool_name": tool["tool_name"],
                        "tool_use_id": tool_use_id,
                        "success": tool["status"] != "error",
                        "duration_ms": tool["duration_ms"] or 0,
                        "preview": tool["output_preview_redacted"]
                        or _sql_attr_text(
                            attrs,
                            "gen_ai.client.tool.output",
                            "tool.output",
                            "output",
                            "error.message",
                            limit=2000,
                        ),
                    }
                )
        if step["id"] in mcp_by_step:
            mcp = mcp_by_step[step["id"]]
            conversation.append(
                {
                    "type": "mcp_call",
                    "ts": base_ts,
                    "tool_name": mcp["tool_name"] or "",
                    "server": mcp["server_name"] or "",
                    "success": mcp["status"] != "error",
                }
            )
        if agent_event := hook_facts.agent_event_for_step(step["id"]):
            conversation.append(
                {
                    "type": "subagent_stop"
                    if str(agent_event.get("event_name") or "").lower().endswith("stop")
                    else "subagent_start",
                    "ts": base_ts,
                    "subagent_type": agent_event.get("agent_type") or "unknown",
                    "agent_id": agent_event.get("agent_id") or "",
                    "parent_agent_id": agent_event.get("parent_agent_id") or "",
                    "status": agent_event.get("status") or "",
                    "preview": agent_event.get("task_preview_redacted") or "",
                    "content_hash": agent_event.get("task_hash") or "",
                    "content_length": int(agent_event.get("task_length") or 0),
                }
            )
        raw_span = (
            raw_by_step_id.get(step["id"])
            or raw_by_time_event.get((str(step["started_at"] or ""), event_type))
            or {}
        )
        trace_id = str(raw_span.get("trace_id") or "")
        span_id = str(raw_span.get("span_id") or "")
        parent_span_id = str(raw_span.get("parent_span_id") or "")
        parent_id = str(step.get("parent_step_id") or "")
        if not parent_id and parent_span_id:
            parent_id = step_id_by_span_id.get(parent_span_id, "")
        started_at = str(step.get("started_at") or raw_span.get("observed_at") or "")
        ended_at = str(step.get("ended_at") or "")
        start_time_ns = _iso_to_epoch_ns(started_at)
        end_time_ns = _iso_to_epoch_ns(ended_at)
        duration_ms = step["duration_ms"] or 0
        if not end_time_ns and start_time_ns and duration_ms:
            end_time_ns = start_time_ns + round(float(duration_ms) * 1e6)
        elif not duration_ms and start_time_ns and end_time_ns >= start_time_ns:
            duration_ms = round((end_time_ns - start_time_ns) / 1e6, 1)
        telemetry_spans.append(
            {
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
                "hook_event_id": step.get("hook_event_id") or "",
                "telemetry_source": step.get("telemetry_source") or "",
                "hook_schema_version": step.get("hook_schema_version"),
                "provider_adapter": step.get("hook_provider_adapter") or "",
                "native_trace_id": step.get("native_trace_id") or "",
                "native_span_id": step.get("native_span_id") or "",
                "agent_id": step.get("agent_invocation_id") or "",
                "parent_agent_id": step.get("parent_agent_id") or "",
                "started_at": started_at,
                "ended_at": ended_at,
                "start_time_ns": start_time_ns,
                "end_time_ns": end_time_ns,
                "rel_ms": round((start_time_ns - anchor_ns) / 1e6, 1)
                if anchor_ns and start_time_ns
                else 0,
                "duration_ms": duration_ms,
                "attrs": attrs,
            }
        )
    telemetry_logs: list[dict[str, object]] = []
    for row in raw_log_rows:
        attrs = load_json_dict(row["attrs_json"])
        body = load_json_dict(row["body_json"])
        body_text = _sql_log_body_text(body, attrs, row.get("event_type"))
        observed_ns = _iso_to_epoch_ns(row["observed_at"])
        telemetry_logs.append(
            {
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
                "rel_ms": round((observed_ns - anchor_ns) / 1e6, 1)
                if anchor_ns and observed_ns
                else 0,
                "body": body_text[:2000],
                "attrs": _sanitize_telemetry_attrs(attrs),
            }
        )
    timeline_end_ns = max(
        [
            *(int(span.get("end_time_ns") or span.get("start_time_ns") or 0) for span in telemetry_spans),
            *(int(log.get("time_ns") or 0) for log in telemetry_logs),
        ],
        default=anchor_ns,
    )
    timeline_duration_ms = (
        round((timeline_end_ns - anchor_ns) / 1e6, 1)
        if anchor_ns and timeline_end_ns >= anchor_ns
        else 0
    )
    services = {
        service
        for service in [
            *(str(span.get("service") or span.get("agent") or "") for span in telemetry_spans),
            *(str(log.get("service") or log.get("agent") or "") for log in telemetry_logs),
        ]
        if service
    }
    errors = sum(1 for step in steps if step["status"] == "error") + sum(
        1 for log in telemetry_logs if log.get("severity") in {"ERROR", "FATAL"}
    )
    hook_summary = hook_facts.summary(steps)
    conversation_source = "telemetry"
    conversation_warnings: list[str] = []
    native_path = _native_session_path(session_row)
    if native_path is not None:
        from reflect.conversation_adapters import DEFAULT_CONVERSATION_ADAPTERS

        agent = str(session_row.get("agent") or "")
        if DEFAULT_CONVERSATION_ADAPTERS.supports(agent):
            try:
                native_transcript = DEFAULT_CONVERSATION_ADAPTERS.load(
                    session_id,
                    agent,
                    native_path,
                )
                native_events = [event.as_dict() for event in native_transcript.events]
                if any(
                    event.get("type") == "response" and str(event.get("content") or "").strip()
                    for event in native_events
                ):
                    conversation = _merge_native_conversation_events(conversation, native_events)
                    conversation_source = native_transcript.source
                    conversation_warnings.extend(native_transcript.warnings)
            except (OSError, ValueError, TypeError) as exc:
                logger.warning(
                    "Native conversation adapter failed for session %s (%s): %s",
                    session_id,
                    agent,
                    exc,
                )
                conversation_warnings.append(
                    "The local native transcript could not be loaded; showing telemetry-derived events."
                )

    tool_inventory = _add_skill_hints_to_inventory(
        tool_inventory,
        {
            skill_name
            for event in conversation
            if event.get("type") in {"prompt", "response"}
            for skill_name in extract_skill_names_from_text(
                str(event.get("preview") or event.get("content") or "")
            )
        },
    )
    tool_inventory = _add_subagent_hints_to_inventory(
        tool_inventory,
        {
            subagent_name
            for event in conversation
            if event.get("type") in {"prompt", "response"}
            for subagent_name in extract_subagent_names_from_text(
                str(event.get("preview") or event.get("content") or "")
            )
        },
    )
    return {
        "session_id": session_id,
        "conversation": conversation,
        "conversation_source": conversation_source,
        "tool_inventory": tool_inventory,
        "telemetry": {
            "summary": {
                "spans": len(telemetry_spans),
                "logs": int(raw_log_count or 0),
                "errors": errors,
                "warnings": sum(1 for log in telemetry_logs if log.get("severity") == "WARN"),
                "services": len(services),
                "anchor_ns": anchor_ns,
                "duration_ms": timeline_duration_ms,
                "truncated_spans": 0,
                "truncated_logs": max(0, int(raw_log_count or 0) - len(telemetry_logs)),
                **hook_summary,
            },
            "spans": telemetry_spans,
            "logs": telemetry_logs,
            "warnings": [],
        },
        "warnings": conversation_warnings,
    }


def _native_session_path(session_row: dict[str, object]) -> Path | None:
    """Resolve a local native transcript path from canonical session provenance."""
    if str(session_row.get("source_kind") or "") != "native_session":
        return None
    source_ref = str(session_row.get("source_ref") or "")
    prefix, separator, remainder = source_ref.partition(":")
    if prefix != "native_session" or not separator:
        return None
    _agent, separator, path_text = remainder.partition(":")
    if not separator or not path_text:
        return None
    path = Path(path_text).expanduser()
    return path if path.is_file() else None
