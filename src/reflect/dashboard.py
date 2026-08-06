from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from reflect.graph import _compute_weekly_trends
from reflect.preparation import BackgroundPreparationWorker, PreparationSnapshot, PreparationState
from reflect.session_rules import DEFAULT_SESSION_RULE_SCORER, context_from_summary
from reflect.store.hook_facts import HookFactRepository
from reflect.utils import _safe_ratio, logger


def _perf_start() -> float:
    return time.perf_counter() if os.environ.get("REFLECT_DEBUG_PERF") else 0.0


def _perf_finish(name: str, start: float, **fields: object) -> None:
    if not start:
        return
    duration_ms = (time.perf_counter() - start) * 1000
    field_text = " ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, ""))
    suffix = f" {field_text}" if field_text else ""
    logger.info("reflect.dashboard.perf %s duration_ms=%.1f%s", name, duration_ms, suffix)


def _telemetry_severity(
    severity_text: str | None,
    severity_number: int | None,
    body_text: str | None = None,
) -> str:
    if severity_text:
        return str(severity_text).upper()
    value = int(severity_number or 0)
    match = next((label for threshold, label in ((21, "FATAL"), (17, "ERROR"), (13, "WARN"), (9, "INFO"), (5, "DEBUG")) if value >= threshold), None)
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
        "service.name", "service.version", "gen_ai.client.name",
        "gen_ai.client.hook.event", "gen_ai.client.tool_name",
        "gen_ai.client.mcp_tool", "gen_ai.client.mcp_server",
        "gen_ai.request.model", "error.type", "error.message",
        "exception.type", "exception.message", "code.function",
        "code.filepath", "code.lineno",
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
    return DEFAULT_SESSION_RULE_SCORER.rules_payload()


def _sql_quality_breakdown(row: dict[str, object], recovered: int = 0) -> list[dict[str, object]]:
    return DEFAULT_SESSION_RULE_SCORER.breakdown(
        context_from_summary(row, recovered=recovered)
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


def _dashboard_docs_dir() -> Path:
    # Prefer repo-level docs/ (development), fall back to packaged data/ (pip install)
    repo_docs = Path(__file__).resolve().parents[2] / "docs"
    if (repo_docs / "index.html").exists():
        return repo_docs
    pkg_data = Path(__file__).resolve().parent / "data"
    if (pkg_data / "index.html").exists():
        return pkg_data
    return repo_docs  # caller handles missing file


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
        return {
            "db_path": str(db_path),
            "overview": overview,
            "sessions": list_sessions(conn, limit=limit, offset=offset).model_dump(),
            "tabs": _add_canonical_dashboard_tab_aliases(
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
    from reflect.store.sqlite import connect_sqlite_read_only

    ids = sorted(session_ids)
    placeholders = ", ".join("?" for _ in ids)
    conn = connect_sqlite_read_only(db_path)
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
    from reflect.store.sqlite import connect_sqlite_read_only

    ids = sorted(session_ids)
    placeholders = ", ".join("?" for _ in ids)
    conn = connect_sqlite_read_only(db_path)
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
    native_text = str(
        native_event.get("content") or native_event.get("preview") or ""
    ).strip()
    if telemetry_text and native_text and telemetry_text == native_text:
        return True
    telemetry_time = _conversation_event_time_ns(telemetry_event)
    native_time = _conversation_event_time_ns(native_event)
    return bool(
        telemetry_time
        and native_time
        and abs(telemetry_time - native_time) <= 120 * 1_000_000_000
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
    from reflect.store.sqlite import connect_sqlite_read_only
    from reflect.views.overview import list_source_provenance
    from reflect.views.report_tabs import build_report_tab, build_report_tabs

    scoped = session_ids is not None
    scoped_ids = sorted(session_ids or [])

    conn = connect_sqlite_read_only(db_path)
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
                if "activity" in selected_base_tabs else []
            )
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
    primary_compat = _sql_cohort_compat_payload(db_path, primary_ids)
    baseline_compat = _sql_cohort_compat_payload(db_path, baseline_ids)
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
        _cohort_agent_comparison(baseline_sessions),
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


def _sql_cohort_compat_payload(
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
        tool_rows = _dict_rows(conn.execute(
            f"""
            SELECT tool_name, COUNT(*) AS call_count
            FROM tool_calls
            WHERE session_id IN ({placeholders})
            GROUP BY tool_name
            ORDER BY call_count DESC, tool_name ASC
            LIMIT 5
            """,
            ordered_ids,
        ))
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
            f"SELECT COUNT(*) FROM mcp_calls WHERE session_id IN ({placeholders})",
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
        rows.append({
            "name": name,
            "sessions": len(agent_sessions),
            "prompts": sum(int(item.get("prompt_count") or 0) for item in agent_sessions),
            "tools": sum(int(item.get("tool_calls") or 0) for item in agent_sessions),
            "failures": sum(int(item.get("failure_count") or 0) for item in agent_sessions),
            "tokens": sum(
                int(item.get("input_tokens") or 0) + int(item.get("output_tokens") or 0)
                for item in agent_sessions
            ),
            "total_cost": sum(float(item.get("total_cost_usd") or 0) for item in agent_sessions),
            "total_cost_usd": sum(float(item.get("total_cost_usd") or 0) for item in agent_sessions),
            "avg_quality": sum(quality) / len(quality) if quality else 0.0,
        })
    return rows


def _empty_sql_lazy_tabs() -> dict[str, object]:
    return {
        "overview": {
            **dict.fromkeys(
                (
                    "avg_quality_score", "unique_sessions", "prompt_submits", "tool_calls",
                    "failure_rate_pct", "tool_failures", "mcp_calls", "subagent_launches",
                    "file_edits", "shell_executions", "unique_commands",
                    "signature_command_count", "peak_hour_count", "unique_models",
                    "total_input_tokens", "total_output_tokens", "total_cache_creation_tokens",
                    "total_cache_read_tokens", "total_cost_usd", "input_cost_usd",
                    "output_cost_usd", "cache_creation_cost_usd", "cache_read_cost_usd",
                ),
                0,
            ),
            "first_event_ts": "", "tool_to_prompt_ratio": "0.0", "signature_command": "",
            "peak_hour": -1, "pricing_source": "local", "mcp_servers_by_count": {},
            "subagent_types_by_count": {}, "models_by_count": {}, "events_by_type": {},
            "model_costs": {}, "source_provenance": [], "agent_cost_over_time": [],
        },
        "activity": {
            "events_by_type": {}, "activity_by_day": {},
            "activity_by_hour": {str(hour): 0 for hour in range(24)},
            "peak_hour": -1, "peak_hour_count": 0,
        },
        "models": {"models_by_count": {}, "unique_models": 0},
        "costs": {
            "model_costs": {}, "model_costs_usd": {},
            "cost_breakdown": dict.fromkeys(
                ("total_cost_usd", "input_cost_usd", "output_cost_usd", "cache_creation_cost_usd", "cache_read_cost_usd"),
                0.0,
            ),
            "total_cache_creation_tokens": 0, "total_cache_read_tokens": 0,
            "agent_cost_over_time": [],
        },
        "tools": {
            "tools_by_count": {}, "tool_percentiles": [], "skills_by_count": {},
            "subagent_types_by_count": {}, "subagent_stops_by_type": {},
            "subagent_launches": 0, "subagent_total_starts": 0, "subagent_total_stops": 0,
            "top_commands": [], "unique_commands": 0, "signature_command": "",
            "signature_command_count": 0, "shell_executions": 0, "file_edits": 0, "file_reads": 0,
        },
        "mcp": {
            "mcp_calls": 0, "mcp_servers_by_count": {},
            "mcp_server_before": {}, "mcp_server_after": {},
        },
        "agents": {"agent_comparison": [], "agents": {}},
        "graphs": {
            "graph_tool_transitions": [], "graph_cooccurrence": {"tools": [], "matrix": []},
            "graph_dep": {"nodes": [], "edges": [], "top_mcp_servers": []},
            "graph_session_timeline": [], "graph_semantic": {"nodes": [], "edges": [], "sessions": [], "legend": []},
        },
        "specs": {
            "total_specs": 0, "specs_by_status": {}, "requirements_by_status": {},
            "evidence_by_kind": {}, "specs": [],
        },
        "memory": {
            "total_memories": 0, "memories_by_scope": {}, "memories_by_type": {},
            "memories_by_sensitivity": {}, "memories_by_source": {}, "recent_memories": [],
        },
        "privacy": {
            "total_findings": 0, "findings_by_type": {}, "findings_by_severity": {},
            "findings_by_action": {}, "recent_findings": [],
        },
        "exports": {
            "row_counts": dict.fromkeys(
                ("sessions", "steps", "llm_calls", "tool_calls", "mcp_calls", "memories", "privacy_findings", "evidence"),
                0,
            ),
            "scoped": True,
        },
    }


def _add_canonical_dashboard_tab_aliases(tabs: dict[str, object]) -> dict[str, object]:
    """Expose product-language keys while retaining legacy payload compatibility."""
    aliases = {
        "usage": "overview",
        "graph": "graphs",
        "cohort_comparison": "compare",
        "inbox": "observations",
    }
    for canonical, legacy in aliases.items():
        if legacy in tabs:
            tabs[canonical] = tabs[legacy]
    return tabs


def _sql_dashboard_session_payload(db_path: Path, session_id: str) -> dict[str, object]:
    from reflect.store.sqlite import connect_sqlite_read_only
    from reflect.views.overview import list_source_provenance
    from reflect.views.report_tabs import _display_mcp_server_name, build_report_tab
    from reflect.views.sessions import list_sessions

    conn = connect_sqlite_read_only(db_path)
    try:
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
              (SELECT COUNT(*) FROM steps st WHERE st.session_id = s.id) AS event_count,
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
        source_provenance = list_source_provenance(conn, session_ids={session_id})
        navigation_page = list_sessions(conn, limit=100, offset=0).model_dump()
    finally:
        conn.close()

    navigation_first_prompts = _sql_session_first_prompts(
        db_path,
        {str(row["session_id"]) for row in navigation_page["rows"]},
    )

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
        "event_count": int(session_row["event_count"] or 0),
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
            "first_prompt": navigation_first_prompts.get(navigation_id, "")
            or navigation_row.get("title")
            or "",
            "started_at": navigation_row["started_at"],
            "ended_at": navigation_row.get("ended_at"),
            "created_at": navigation_row["started_at"],
            "duration_ms": navigation_row.get("duration_ms") or 0,
            "event_count": int(navigation_row["event_count"] or 0),
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
    failure_rate_pct = round(
        100 * scoped_overview["failure_count"] / scoped_overview["tool_call_count"],
        1,
    ) if scoped_overview["tool_call_count"] else 0.0
    weekly_trends = _compute_weekly_trends(Counter(activity_view["activity_by_day"]))
    session_card["skills"] = tools_view["skills_by_count"]
    tabs["overview"].update({
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
    _add_canonical_dashboard_tab_aliases(tabs)
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
        "source_provenance": source_provenance,
        "failure_rate_pct": failure_rate_pct,
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
        "weekly_trends": weekly_trends,
        "agent_cost_over_time": costs_view["agent_cost_over_time"],
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
    from reflect.store.sqlite import connect_sqlite_read_only
    from reflect.views.report_tabs import build_report_tab

    scoped_ids = {session_id} if session_id else None
    conn = connect_sqlite_read_only(db_path)
    try:
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


_EXPLORE_VIEW_TABS: dict[str, tuple[str, ...]] = {
    "usage": ("activity", "models", "costs", "usage_tools", "mcp"),
    "tools": ("tools", "mcp"),
    "graph": ("graphs",),
    "context": ("specs", "memory", "privacy", "exports"),
}

def _canonical_explore_view(view_name: str) -> str:
    return view_name.strip().lower().replace("-", "_")


def _sql_dashboard_explore_payload(
    db_path: Path,
    view_name: str,
    *,
    session_id: str = "",
    q: str = "",
    agents: set[str] | None = None,
    model: str = "all",
    status: str = "all",
    range_name: str = "all",
) -> dict[str, object]:
    canonical_view = _canonical_explore_view(view_name)
    tab_names = _EXPLORE_VIEW_TABS.get(canonical_view)
    if tab_names is None:
        raise ValueError(f"Unknown Explore view: {view_name}")

    if canonical_view == "context":
        session_id, q, agents, model, status, range_name = "", "", set(), "all", "all", "all"
    has_scope = bool(q or session_id or agents or model != "all" or status != "all" or range_name != "all")

    payload: dict[str, object] = {
        "sql_backed": True, "view": canonical_view, "scoped": has_scope, "session_id": session_id,
    }
    filtered_tabs = None
    if has_scope:
        filtered_tabs = _sql_dashboard_payload(
            db_path,
            q=q,
            session_id=session_id,
            agents=agents,
            model=model,
            status=status,
            range_name=range_name,
            lazy_heavy_tabs=True,
            include_comparison=False,
            base_tab_names=set(tab_names),
        )["sqlite"]["tabs"]
    for tab_name in tab_names:
        if filtered_tabs is not None:
            content = filtered_tabs["tools" if tab_name == "usage_tools" else tab_name]
        else:
            tab_payload = _sql_dashboard_tab_payload(db_path, tab_name, session_id=session_id)
            content = {
                key: value
                for key, value in tab_payload.items()
                if key not in {"sql_backed", "tab", "scoped", "session_id"}
            }
        if canonical_view in {"usage", "tools", "graph"}:
            payload.update(content)
        else:
            payload[tab_name] = content
    return payload


def _dashboard_scope(query: Mapping[str, str]) -> tuple[str, str, set[str], str, str, str]:
    agents = {agent for agent in (query.get("agents") or "").split(",") if agent}
    legacy_agent = (query.get("agent") or "").strip()
    if not agents and legacy_agent and legacy_agent != "all":
        agents.add(legacy_agent)
    return (
        (query.get("q") or "").strip(),
        (query.get("session") or "").strip(),
        agents,
        query.get("model") or "all",
        query.get("status") or "all",
        query.get("range") or "all",
    )


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
            "event_count": row["event_count"],
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
    failure_rate_pct = round(
        100 * scoped_overview["failure_count"] / scoped_overview["tool_call_count"],
        1,
    ) if scoped_overview["tool_call_count"] else 0.0
    weekly_trends = _compute_weekly_trends(Counter(compat["activity_by_day"]))
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
            "failure_rate_pct": failure_rate_pct,
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
            "agent_cost_over_time": compat["agent_cost_over_time"],
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
            "agent_cost_over_time": compat["agent_cost_over_time"],
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
    _add_canonical_dashboard_tab_aliases(sqlite_payload["tabs"])
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
        "failure_rate_pct": failure_rate_pct,
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
        "weekly_trends": weekly_trends,
        "agent_cost_over_time": compat["agent_cost_over_time"],
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
        hook_facts = HookFactRepository(conn).load_session(session_id)
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
    seen_responses: set[tuple[object, ...]] = set()
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
        prompt_fact = hook_facts.prompt_for_step(step["id"])
        if prompt_fact:
            prompt = str(prompt_fact.get("content_preview_redacted") or prompt)
        if is_prompt_event:
            prompt_hash = str(
                (prompt_fact or {}).get("content_hash")
                or _sql_attr(attrs, "gen_ai.client.prompt.sha256")
                or ""
            )
            prompt_key = (generation_id or prompt_hash, prompt)
            if prompt_key not in seen_prompts:
                seen_prompts.add(prompt_key)
                conversation.append({
                    "type": "prompt",
                    "ts": base_ts,
                    "preview": prompt or "Prompt text was not captured for this turn; metadata is available.",
                    "content_hash": prompt_hash,
                    "content_length": int((prompt_fact or {}).get("content_length") or 0),
                })
        response_facts = hook_facts.responses_for_step(step["id"])
        for response_fact in response_facts:
            response_hash = str(response_fact.get("content_hash") or "")
            response_identity = response_hash or str(response_fact.get("id") or step["id"])
            response_key = (response_identity, "hook", 0, 0)
            if response_key in seen_responses:
                continue
            seen_responses.add(response_key)
            conversation.append({
                "type": "response",
                "ts": base_ts,
                "preview": response_fact.get("content_preview_redacted")
                or "Assistant turn completed, but response text was not captured.",
                "content_hash": response_hash,
                "content_length": int(response_fact.get("content_length") or 0),
            })
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
                    "tool_use_id": tool_use_id,
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
                    "tool_use_id": tool_use_id,
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
        if agent_event := hook_facts.agent_event_for_step(step["id"]):
            conversation.append({
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
            "hook_event_id": step.get("hook_event_id") or "",
            "telemetry_source": step.get("telemetry_source") or "",
            "hook_schema_version": step.get("hook_schema_version"),
            "provider_adapter": step.get("hook_provider_adapter") or "",
            "native_trace_id": step.get("native_trace_id") or "",
            "native_span_id": step.get("native_span_id") or "",
            "agent_id": step.get("agent_invocation_id") or "",
            "parent_agent_id": step.get("parent_agent_id") or "",
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
    hook_summary = hook_facts.summary(steps)
    conversation_source = "telemetry"
    conversation_warnings: list[str] = []
    native_path = _native_session_path(session_row)
    if native_path is not None:
        from reflect.session_adapters import DEFAULT_SESSION_ADAPTERS

        agent = str(session_row.get("agent") or "")
        if DEFAULT_SESSION_ADAPTERS.supports(agent):
            try:
                native_transcript = DEFAULT_SESSION_ADAPTERS.load(session_id, agent, native_path)
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
            for skill_name in _extract_skill_names_from_text(
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
            for subagent_name in _extract_subagent_names_from_text(
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
                "duration_ms": 0,
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


def _start_publish_server(
    *,
    db_path: Path,
    preparation_worker: BackgroundPreparationWorker | None = None,
    open_browser: bool = True,
) -> None:
    """Start a local FastAPI server and open the dashboard in a browser.

    Blocks until Ctrl-C. Uses ``?report=api/data`` so the dashboard
    fetches JSON from the API — no URL encoding at all.
    """
    port = int(os.environ.get("REFLECT_PORT", "8765"))
    docs_dir = _dashboard_docs_dir()
    _start_publish_server_inline(
        port,
        docs_dir,
        db_path=db_path,
        preparation_worker=preparation_worker,
        open_browser=open_browser,
    )


def _build_dashboard_app(
    *,
    docs_dir: Path,
    db_path: Path,
    preparation_worker: BackgroundPreparationWorker | None = None,
    project_root: Path | None = None,
):
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    globals()["Request"] = Request

    app = FastAPI(title="reflect dashboard", docs_url=None, redoc_url=None)
    workflow_project_root = (project_root or Path.cwd()).expanduser().resolve()

    def resolve_workflow_project_root(value: object = None) -> Path:
        requested = str(value or "").strip()
        return Path(requested).expanduser().resolve() if requested else workflow_project_root

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
                lazy_heavy_tabs=True,
                base_tab_names=set(_EXPLORE_VIEW_TABS["usage"]),
            ),
        )
    else:
        dashboard_cache = DashboardDataCache(
            lambda: _sql_dashboard_payload(db_path, lazy_heavy_tabs=True, base_tab_names=set(_EXPLORE_VIEW_TABS["usage"]))
        )
    if preparation_worker is not None:
        preparation_worker.add_completion_callback(lambda _result: dashboard_cache.refresh())

    @app.get("/api/data")
    def api_data(request: Request):
        perf_start = _perf_start()
        perf_kind = "unknown"
        params = request.query_params
        q, session_id, agents, model, status, range_name = _dashboard_scope(params)
        active_tab = (params.get("tab") or "sessions").strip().lower()
        explore_view = _canonical_explore_view(params.get("view") or "usage")
        filtered_base_tabs = {
            ("explore", "usage"): {"activity", "models", "costs", "usage_tools", "mcp"},
            ("explore", "tools"): {"tools", "mcp"},
            ("inbox", "usage"): {"activity", "models", "costs", "tools", "mcp", "agents"},
        }.get((active_tab, explore_view), set())
        if session_id:
            filtered_base_tabs = {"activity", "models", "costs", "tools", "mcp", "agents"}
        has_filter = any([q, session_id, agents, model != "all", status != "all", range_name != "all"])
        try:
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
                include_comparison=active_tab == "explore" and explore_view == "usage",
                base_tab_names=filtered_base_tabs,
            ))
        finally:
            _perf_finish(
                "api.data",
                perf_start,
                kind=perf_kind,
                session=bool(session_id),
                agents=",".join(sorted(agents)),
            )

    @app.get("/api/explore/{view_name}")
    def api_explore(view_name: str, request: Request):
        perf_start = _perf_start()
        q, session_id, agents, model, status, range_name = _dashboard_scope(request.query_params)
        try:
            return JSONResponse(
                _sql_dashboard_explore_payload(
                    db_path,
                    view_name,
                    session_id=session_id,
                    q=q,
                    agents=agents,
                    model=model,
                    status=status,
                    range_name=range_name,
                )
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc), "view": view_name}, status_code=404)
        except Exception as exc:
            return JSONResponse({"error": str(exc), "db_path": str(db_path)}, status_code=500)
        finally:
            _perf_finish("api.explore", perf_start, view=view_name, session=bool(session_id))

    @app.get("/api/session/{session_id:path}")
    def api_session(session_id: str):
        detail = _load_sql_session_detail(db_path, session_id)
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
        return JSONResponse({
            "preparation": snapshot.as_dict(),
            "refresh_available": preparation_worker is not None,
        })

    @app.post("/api/refresh")
    def api_refresh():
        if preparation_worker is None:
            return JSONResponse(
                {
                    "error": "This report server is snapshot-only. Start Reflect normally or use `reflect server --refresh start`.",
                    "refresh_available": False,
                },
                status_code=409,
            )
        started = preparation_worker.start()
        return JSONResponse(
            {
                "started": started,
                "refresh_available": True,
                "preparation": preparation_worker.snapshot().as_dict(),
            },
            status_code=202 if started else 200,
        )

    @app.get("/api/inbox")
    def api_inbox(request: Request):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        params = request.query_params
        conn = connect_sqlite_read_only(db_path)
        try:
            service = ImprovementService(conn, initialize_schema=False)
            status = (params.get("status") or "").strip() or None
            include_resolved = (params.get("include_resolved") or "").lower() in {
                "1",
                "true",
                "yes",
            }
            limit = min(500, max(1, int(params.get("limit") or 100)))
            findings = service.list_inbox_findings(
                limit=500,
                status=status,
                include_resolved=include_resolved,
            )
            summary = service.repository.summary(limit=0)
            if status:
                raw_observation_count = summary.counts_by_status.get(status, 0)
            elif include_resolved:
                raw_observation_count = sum(summary.counts_by_status.values())
            else:
                raw_observation_count = sum(
                    summary.counts_by_status.get(item, 0)
                    for item in (
                        "new",
                        "acknowledged",
                        "proposal_ready",
                        "approved",
                        "active",
                        "regressed",
                    )
                )
            return JSONResponse(
                {
                    "generated_at": summary.generated_at,
                    "findings": [
                        item.model_dump(mode="json") for item in findings[:limit]
                    ],
                    "inbox_total_count": len(findings),
                    "raw_observation_count": raw_observation_count,
                    "counts_by_status": summary.counts_by_status,
                    "pending_workflows": summary.pending_workflows,
                    "active_interventions": summary.active_interventions,
                    "verified_improvement_rate": summary.verified_improvement_rate,
                }
            )
        except (ValueError, sqlite3.Error) as exc:
            return JSONResponse({"error": str(exc), "db_path": str(db_path)}, status_code=500)
        finally:
            conn.close()

    @app.get("/api/rules")
    def api_improvement_rules():
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            rules = ImprovementService(conn, initialize_schema=False).repository.list_rule_summaries()
            return JSONResponse(
                {
                    "rules": [rule.model_dump(mode="json") for rule in rules],
                    "extension": {
                        "kind": "code_backed",
                        "module": "reflect.improvements",
                        "base_class": "BaseImprovementRule",
                        "registry": "DEFAULT_RULE_REGISTRY",
                        "registration": "RuleRegistry.register",
                    },
                }
            )
        finally:
            conn.close()

    @app.get("/api/inbox/{finding_id}")
    def api_inbox_detail(finding_id: str):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            observation = ImprovementService(conn, initialize_schema=False).repository.get_observation(finding_id)
            if observation is None:
                return JSONResponse({"error": f"Finding {finding_id} not found"}, status_code=404)
            return JSONResponse(observation.model_dump(mode="json"))
        finally:
            conn.close()

    @app.get("/api/inbox/{observation_id}/sessions")
    def api_inbox_sessions(observation_id: str, request: Request):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            ledger = ImprovementService(conn, initialize_schema=False).finding_session_ledger(
                observation_id,
                limit=min(200, max(1, int(request.query_params.get("limit") or 50))),
            )
            return JSONResponse(ledger.model_dump(mode="json"))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        finally:
            conn.close()

    @app.get("/api/workflows")
    def api_workflows(request: Request):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            behavior_type = request.query_params.get("type")
            status = request.query_params.get("status")
            service = ImprovementService(conn, initialize_schema=False)
            candidates = service.workflows.list(
                behavior_types={behavior_type} if behavior_type else None,
                statuses={status} if status else None,
            )
            serialized = []
            for candidate in candidates:
                item = candidate.model_dump(mode="json")
                try:
                    item["skill_id"] = service.skills.skill_for_candidate(candidate.id).id
                except KeyError:
                    item["skill_id"] = None
                serialized.append(item)
            return JSONResponse(
                {"workflows": serialized}
            )
        finally:
            conn.close()

    @app.get("/api/loops")
    def api_loops(request: Request):
        from reflect.improvements.models import LoopKind, LoopStatus
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            service = ImprovementService(conn, initialize_schema=False)
            kind = request.query_params.get("kind")
            status = request.query_params.get("status")
            records = service.loops.list(
                kind=LoopKind(kind) if kind else None,
                status=LoopStatus(status) if status else None,
                limit=min(500, max(1, int(request.query_params.get("limit") or 100))),
            )
            return JSONResponse(
                {
                    "loops": [record.model_dump(mode="json") for record in records],
                }
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        finally:
            conn.close()

    @app.get("/api/loops/{loop_id}")
    def api_loop_detail(loop_id: str):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            service = ImprovementService(conn, initialize_schema=False)
            return JSONResponse(service.loops.show(loop_id).model_dump(mode="json"))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        finally:
            conn.close()

    @app.get("/api/skills")
    def api_skills(request: Request):
        from reflect.improvements.models import SkillLifecycleState
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            service = ImprovementService(conn, initialize_schema=False)
            status = request.query_params.get("status")
            include_stale = (
                request.query_params.get("include_stale") or ""
            ).lower() in {"1", "true", "yes"}
            lifecycle = SkillLifecycleState(status) if status else None
            counts_by_lifecycle = service.skills.counts_by_lifecycle()
            records = service.skills.list(
                lifecycle=lifecycle,
                include_stale=include_stale,
                limit=min(500, max(1, int(request.query_params.get("limit") or 100))),
            )
            if lifecycle:
                total_count = counts_by_lifecycle.get(lifecycle.value, 0)
            elif include_stale:
                total_count = sum(counts_by_lifecycle.values())
            else:
                total_count = sum(
                    counts_by_lifecycle.get(item.value, 0)
                    for item in (SkillLifecycleState.ACTIVE, SkillLifecycleState.PENDING)
                )
            return JSONResponse(
                {
                    "skills": [record.model_dump(mode="json") for record in records],
                    "total_count": total_count,
                    "archived_count": counts_by_lifecycle.get(SkillLifecycleState.STALE.value, 0),
                    "counts_by_lifecycle": counts_by_lifecycle,
                }
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        finally:
            conn.close()

    @app.get("/api/skills/{skill_id}")
    def api_skill_detail(skill_id: str):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            service = ImprovementService(conn, initialize_schema=False)
            return JSONResponse(service.skills.show(skill_id).model_dump(mode="json"))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        finally:
            conn.close()

    @app.get("/api/workflows/{candidate_id}/sessions")
    def api_workflow_sessions(candidate_id: str, request: Request):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            ledger = ImprovementService(conn, initialize_schema=False).repository.workflow_session_ledger(
                candidate_id,
                limit=min(200, max(1, int(request.query_params.get("limit") or 50))),
            )
            return JSONResponse(ledger.model_dump(mode="json"))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        finally:
            conn.close()

    @app.get("/api/observations/{observation_id}/sessions")
    def api_observation_sessions(observation_id: str, request: Request):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            service = ImprovementService(conn, initialize_schema=False)
            observation_ids = service.resolve_finding_observation_ids(observation_id)
            ledger = service.repository.observation_session_ledger(
                observation_id,
                limit=min(200, max(1, int(request.query_params.get("limit") or 50))),
                observation_ids=observation_ids,
            )
            return JSONResponse(ledger.model_dump(mode="json"))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        finally:
            conn.close()

    @app.get("/api/workflows/{candidate_id}/preview")
    def api_workflow_preview(candidate_id: str, request: Request):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            return JSONResponse(
                ImprovementService(conn, initialize_schema=False).workflows.preview(
                    candidate_id,
                    project_root=resolve_workflow_project_root(
                        request.query_params.get("project_root")
                    ),
                )
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (OSError, RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        finally:
            conn.close()

    @app.put("/api/workflows/{candidate_id}")
    def api_workflow_edit(candidate_id: str, body: dict):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite

        content = body.get("content")
        if not isinstance(content, dict):
            return JSONResponse({"error": "A structured workflow content object is required"}, status_code=422)
        conn = connect_sqlite(db_path)
        try:
            candidate = ImprovementService(conn, initialize_schema=False).workflows.edit(
                candidate_id, content=content
            )
            return JSONResponse(candidate.model_dump(mode="json"))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        finally:
            conn.close()

    @app.post("/api/workflows/{candidate_id}/apply")
    def api_workflow_apply(candidate_id: str, body: dict | None = None):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite

        conn = connect_sqlite(db_path)
        try:
            return JSONResponse(
                ImprovementService(conn, initialize_schema=False).workflows.apply(
                    candidate_id,
                    project_root=resolve_workflow_project_root((body or {}).get("project_root")),
                )
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (OSError, RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        finally:
            conn.close()

    @app.post("/api/workflows/{candidate_id}/rollback")
    def api_workflow_rollback(candidate_id: str):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite

        conn = connect_sqlite(db_path)
        try:
            return JSONResponse(
                ImprovementService(conn, initialize_schema=False).workflows.rollback(candidate_id)
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        finally:
            conn.close()

    @app.post("/api/workflows/{candidate_id}/reject")
    def api_workflow_reject(candidate_id: str, body: dict | None = None):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite

        conn = connect_sqlite(db_path)
        try:
            candidate = ImprovementService(conn, initialize_schema=False).workflows.reject(
                candidate_id,
                reason=str((body or {}).get("reason") or "operator_rejected")[:200],
            )
            return JSONResponse(candidate.model_dump(mode="json"))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        finally:
            conn.close()

    @app.post("/api/feedback/{session_id:path}")
    def api_session_feedback(session_id: str, body: dict):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite

        outcome = str(body.get("outcome") or "")
        reason = body.get("reason")
        conn = connect_sqlite(db_path)
        try:
            feedback_id = ImprovementService(conn, initialize_schema=False).repository.record_feedback(
                session_id,
                outcome,
                reason_redacted=str(reason) if reason is not None else None,
            )
            return JSONResponse(
                {"id": feedback_id, "session_id": session_id, "outcome": outcome},
                status_code=201,
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        finally:
            conn.close()

    @app.get("/api/impact")
    def api_impact():
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            service = ImprovementService(conn, initialize_schema=False)
            return JSONResponse({"impact_checks": service.measurements.list()})
        finally:
            conn.close()

    @app.get("/api/impact/{impact_id}/sessions")
    def api_impact_sessions(impact_id: str):
        from reflect.improvements.service import ImprovementService
        from reflect.store.sqlite import connect_sqlite_read_only

        conn = connect_sqlite_read_only(db_path)
        try:
            service = ImprovementService(conn, initialize_schema=False)
            return JSONResponse(service.measurements.sessions(impact_id))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        finally:
            conn.close()

    @app.get("/")
    def index():
        html_file = docs_dir / "report.html"
        if not html_file.exists():
            html_file = docs_dir / "index.html"
        return FileResponse(html_file, media_type="text/html")

    if docs_dir.exists():
        app.mount("/", StaticFiles(directory=str(docs_dir)), name="static")

    return app


def _start_publish_server_inline(
    port: int,
    docs_dir: Path,
    *,
    db_path: Path,
    preparation_worker: BackgroundPreparationWorker | None = None,
    open_browser: bool = True,
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
        artifact.write_text(json.dumps(_sql_dashboard_payload(db_path)), encoding="utf-8")
        print(f"Wrote: {artifact}")
        return

    app = _build_dashboard_app(
        docs_dir=docs_dir,
        db_path=db_path,
        preparation_worker=preparation_worker,
    )
    url = f"http://127.0.0.1:{port}/?report=api/data"
    if open_browser:
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
