from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter

from reflect.models import TelemetryStats

_ACTIONABLE_RECOVERY_EVENTS = {"PreToolUse", "BeforeShellExecution", "BeforeMCPExecution"}
_SKILL_SESSION_LIMIT = 12
_SKILL_DEEP_CONTEXT_LIMIT = 4
_SKILL_PROMPT_SNIPPET_LIMIT = 3
_SKILL_SHELL_COMMAND_LIMIT = 5
_SKILL_RECOVERY_LIMIT = 3
_SKILL_TOOL_FLOW_LIMIT = 20
_SKILL_CONVERSATION_CONTEXT_LIMIT = 8
_SKILL_SPAN_CONTEXT_LIMIT = 10
_SKILL_PATTERN_LIMIT = 8
_SKILL_GRAPH_PATTERN_LIMIT = 10
_SKILL_GRAPH_SESSION_INSERT_CHUNK = 300


def _strip_json_fences(text: str) -> str:
    """Remove markdown code fences wrapping JSON output, if present."""
    stripped = text.strip()
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", stripped, re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped


def _load_extracted_skills(text: str) -> list[dict]:
    """Parse extracted skill JSON, tolerating trailing non-JSON text."""
    cleaned = _strip_json_fences(text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as original_exc:
        decoder = json.JSONDecoder()
        for index, char in enumerate(cleaned):
            if char not in "[{":
                continue
            try:
                parsed, _end = decoder.raw_decode(cleaned[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                return parsed
        raise original_exc
    if not isinstance(parsed, list):
        raise json.JSONDecodeError("Expected JSON array", cleaned, 0)
    return parsed


def _compress_tool_sequence(tools: list[str]) -> list[str]:
    """Collapse consecutive identical tool calls into Tool×N notation."""
    if not tools:
        return []
    result: list[str] = []
    cur, count = tools[0], 1
    for tool in tools[1:]:
        if tool == cur:
            count += 1
        else:
            result.append(f"{cur}×{count}" if count > 1 else cur)
            cur, count = tool, 1
    result.append(f"{cur}×{count}" if count > 1 else cur)
    return result


def _extract_recovery_chains(spans: list[dict]) -> list[str]:
    """Return failed-tool→next-actionable-tool pairs as error-recovery signals."""
    chains: list[str] = []
    ordered = sorted(
        spans,
        key=lambda span: (0, span["t"]) if span.get("t") is not None else (1, 0),
    )
    for index, span in enumerate(ordered):
        if span.get("ok", True):
            continue
        failed = span.get("tool")
        if not failed:
            continue
        for next_span in ordered[index + 1:]:
            if next_span.get("event") not in _ACTIONABLE_RECOVERY_EVENTS:
                continue
            recovered = next_span.get("tool")
            if recovered:
                chains.append(f"{failed}✗→{recovered}")
            break
    return chains


def _normalize_preview(value: object, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] if text else ""


def _primary_model(models: Counter | None) -> str:
    if not models:
        return "unknown"
    top = models.most_common(1)
    return top[0][0] if top else "unknown"


def _total_tokens(tokens: dict[str, int] | None) -> int:
    payload = tokens or {}
    return int(payload.get("input", 0) or 0) + int(payload.get("output", 0) or 0)


def _session_agent(stats: TelemetryStats, session_id: str) -> str:
    source_info = stats.session_source.get(session_id)
    if source_info and source_info[0]:
        return source_info[0]
    for agent_name, agent_stats in sorted(stats.agents.items()):
        if session_id in agent_stats.sessions_seen:
            return agent_name
    return ""


def _session_tool_flow(stats: TelemetryStats, session_id: str) -> list[str]:
    tool_seq = stats.session_tool_seq.get(session_id, [])
    if not tool_seq:
        return []
    sorted_seq = sorted(tool_seq, key=lambda item: item[0])
    return _compress_tool_sequence([tool for _, tool, _ in sorted_seq])[:_SKILL_TOOL_FLOW_LIMIT]


def _session_prompt_snippets(conversation: list[dict]) -> list[str]:
    snippets: list[str] = []
    for event in conversation:
        if event.get("type") != "prompt" or not event.get("preview"):
            continue
        snippet = _normalize_preview(event.get("preview"), limit=80)
        if snippet:
            snippets.append(snippet)
        if len(snippets) >= _SKILL_PROMPT_SNIPPET_LIMIT:
            break
    return snippets


def _conversation_context(conversation: list[dict]) -> list[dict]:
    selected: list[dict] = []
    for index, event in enumerate(conversation, start=1):
        event_type = str(event.get("type") or "")
        if event_type not in {"prompt", "tool_call", "response", "mcp_call"}:
            continue
        preview = _normalize_preview(event.get("preview"), limit=120)
        tool_name = str(event.get("tool_name") or "")
        if not preview and not tool_name:
            continue
        item: dict[str, object] = {
            "id": f"conversation-{index:02d}",
            "type": event_type,
        }
        if preview:
            item["preview"] = preview
        if tool_name:
            item["tool_name"] = tool_name
        model = str(event.get("model") or "")
        if model:
            item["model"] = model
        selected.append(item)
        if len(selected) >= _SKILL_CONVERSATION_CONTEXT_LIMIT:
            break
    return selected


def _span_context(spans: list[dict]) -> list[dict]:
    if not spans:
        return []
    ordered = sorted(
        spans,
        key=lambda span: (0, span["t"]) if span.get("t") is not None else (1, 0),
    )
    selected: list[dict] = []
    for index, span in enumerate(ordered, start=1):
        event = str(span.get("event") or "")
        tool = str(span.get("tool") or "")
        if not event or (not tool and event not in {"Stop", "SessionEnd", "SubagentStop"}):
            continue
        item: dict[str, object] = {
            "id": f"span-{index:02d}",
            "event": event,
            "ok": bool(span.get("ok", True)),
        }
        if tool:
            item["tool"] = tool
        duration = float(span.get("dur", 0.0) or 0.0)
        if duration > 0:
            item["duration_ms"] = round(duration, 1)
        timestamp = span.get("t")
        if timestamp is not None:
            item["timestamp_ns"] = int(timestamp)
        selected.append(item)
        if len(selected) >= _SKILL_SPAN_CONTEXT_LIMIT:
            break
    return selected


def _tool_failure_count(spans: list[dict]) -> int:
    return sum(1 for span in spans if not span.get("ok", True))


def _tool_use_count(spans: list[dict]) -> int:
    return sum(1 for span in spans if span.get("tool"))


def _loop_count(spans: list[dict]) -> int:
    tool_seq = [str(span.get("tool") or "") for span in spans if span.get("tool")]
    loops = 0
    for index in range(len(tool_seq) - 1):
        if tool_seq[index] == tool_seq[index + 1]:
            loops += 1
    return loops


def _session_improvement_targets(stats: TelemetryStats, session_id: str) -> list[dict]:
    spans = stats.session_span_details.get(session_id, [])
    tokens = stats.session_tokens.get(session_id, {})
    failures = _tool_failure_count(spans)
    tool_uses = _tool_use_count(spans)
    loops = _loop_count(spans)
    total_tokens = _total_tokens(tokens)
    recovered = int(stats.session_recovered_failures.get(session_id, 0) or 0)
    quality = float(stats.session_quality_scores.get(session_id, 0.0) or 0.0)
    completed = bool(stats.session_goal_completed.get(session_id, False))
    targets: list[dict] = []

    if failures:
        targets.append({
            "kind": "reliability",
            "why": (
                f"Repeated tool failures ({failures}) suggest a reusable workflow could "
                "front-load validation and reduce broken attempts."
            ),
        })
    if loops >= 2:
        targets.append({
            "kind": "exploration-churn",
            "why": (
                f"Back-to-back tool loops ({loops}) suggest a skill could narrow search scope "
                "and cut repeated exploration."
            ),
        })
    if recovered:
        targets.append({
            "kind": "recovery-playbook",
            "why": (
                f"Observed recovery chains ({recovered}) suggest a repeatable debug playbook could "
                "turn failures into faster recoveries."
            ),
        })
    if tool_uses and total_tokens and total_tokens / max(tool_uses, 1) >= 10_000:
        targets.append({
            "kind": "prompt-contract",
            "why": (
                "High token cost per action suggests a skill could enforce tighter goal/context/"
                "output contracts before tool use."
            ),
        })
    if not completed and quality < 70:
        targets.append({
            "kind": "completion-guardrails",
            "why": (
                "The session lacks a completion signal and scored weakly, suggesting a skill could "
                "make done-criteria and checkpoints explicit."
            ),
        })
    if completed and quality >= 80 and not failures and tool_uses:
        targets.append({
            "kind": "codify-effective-workflow",
            "why": (
                "This high-quality completed session looks like a strong workflow candidate worth "
                "codifying into a reusable skill."
            ),
        })
    return targets[:4]


def _session_signal_score(stats: TelemetryStats, session_id: str) -> float:
    spans = stats.session_span_details.get(session_id, [])
    failures = _tool_failure_count(spans)
    loops = _loop_count(spans)
    recovered = int(stats.session_recovered_failures.get(session_id, 0) or 0)
    quality = float(stats.session_quality_scores.get(session_id, 0.0) or 0.0)
    total_tokens = _total_tokens(stats.session_tokens.get(session_id, {}))
    completed = bool(stats.session_goal_completed.get(session_id, False))
    opportunities = len(_session_improvement_targets(stats, session_id))
    high_token_penalty = 8 if total_tokens >= 50_000 else 0
    quality_pressure = 10 if quality <= 50 else 6 if quality >= 85 else 0
    completion_pressure = 8 if not completed else 0
    event_weight = min(int(stats.session_events.get(session_id, 0) or 0), 200) / 20
    return (
        opportunities * 10
        + failures * 6
        + loops * 4
        + recovered * 5
        + high_token_penalty
        + quality_pressure
        + completion_pressure
        + event_weight
    )


def _aggregate_recurring_values(
    session_values: dict[str, list[str]],
    *,
    prefix: str,
    min_count: int = 2,
) -> list[dict]:
    by_value: dict[str, set[str]] = {}
    for session_id, values in session_values.items():
        seen: set[str] = set()
        for value in values:
            if not value or value in seen:
                continue
            by_value.setdefault(value, set()).add(session_id)
            seen.add(value)
    recurring = [
        {
            "id": f"{prefix}-{index:02d}",
            "value": value,
            "count": len(session_ids),
            "session_ids": sorted(session_ids),
        }
        for index, (value, session_ids) in enumerate(
            sorted(
                (
                    (value, session_ids)
                    for value, session_ids in by_value.items()
                    if len(session_ids) >= min_count
                ),
                key=lambda item: (-len(item[1]), item[0]),
            )[:_SKILL_PATTERN_LIMIT],
            start=1,
        )
    ]
    return recurring


def _build_skill_evidence_bundle(stats: TelemetryStats) -> dict:
    # Precompute per-session derived metrics once to avoid redundant span scans.
    per_session: dict[str, dict] = {}
    for session_id in stats.sessions_seen:
        spans = stats.session_span_details.get(session_id, [])
        failures = _tool_failure_count(spans)
        loops = _loop_count(spans)
        targets = _session_improvement_targets(stats, session_id)
        per_session[session_id] = {
            "spans": spans,
            "failures": failures,
            "loops": loops,
            "targets": targets,
        }

    def _score(session_id: str) -> float:
        session_metrics = per_session[session_id]
        recovered = int(stats.session_recovered_failures.get(session_id, 0) or 0)
        quality = float(stats.session_quality_scores.get(session_id, 0.0) or 0.0)
        total_tokens = _total_tokens(stats.session_tokens.get(session_id, {}))
        completed = bool(stats.session_goal_completed.get(session_id, False))
        opportunities = len(session_metrics["targets"])
        high_token_penalty = 8 if total_tokens >= 50_000 else 0
        quality_pressure = 10 if quality <= 50 else 6 if quality >= 85 else 0
        completion_pressure = 8 if not completed else 0
        event_weight = min(int(stats.session_events.get(session_id, 0) or 0), 200) / 20
        return (
            opportunities * 10
            + session_metrics["failures"] * 6
            + session_metrics["loops"] * 4
            + recovered * 5
            + high_token_penalty
            + quality_pressure
            + completion_pressure
            + event_weight
        )

    ranked_session_ids = sorted(
        stats.sessions_seen,
        key=lambda session_id: (
            -_score(session_id),
            -int(stats.session_events.get(session_id, 0) or 0),
            session_id,
        ),
    )[:_SKILL_SESSION_LIMIT]
    deep_context_ids = set(
        sorted(
            ranked_session_ids,
            key=lambda session_id: (
                -_score(session_id),
                float(stats.session_quality_scores.get(session_id, 0.0) or 0.0),
                session_id,
            ),
        )[:_SKILL_DEEP_CONTEXT_LIMIT]
    )

    sessions: list[dict] = []
    tool_flow_values: dict[str, list[str]] = {}
    shell_command_values: dict[str, list[str]] = {}
    recovery_values: dict[str, list[str]] = {}
    target_values: dict[str, list[str]] = {}

    for rank, session_id in enumerate(ranked_session_ids, start=1):
        session_metrics = per_session[session_id]
        spans = session_metrics["spans"]
        targets = session_metrics["targets"]
        conversation = stats.session_conversation.get(session_id, [])
        tokens = stats.session_tokens.get(session_id, {})
        tool_flow = _session_tool_flow(stats, session_id)
        shell_cmds = [
            command
            for command, _count in (stats.session_shell_commands.get(session_id) or Counter()).most_common(
                _SKILL_SHELL_COMMAND_LIMIT
            )
        ]
        prompts = _session_prompt_snippets(conversation)
        recoveries = _extract_recovery_chains(spans)[:_SKILL_RECOVERY_LIMIT]
        tool_flow_values[session_id] = [" → ".join(tool_flow)] if tool_flow else []
        shell_command_values[session_id] = shell_cmds
        recovery_values[session_id] = recoveries
        target_values[session_id] = [target["kind"] for target in targets]

        session_entry: dict[str, object] = {
            "id": session_id,
            "short_id": session_id[:8],
            "rank": rank,
            "signal_score": round(_score(session_id), 1),
            "refs": {
                "session": f"session://{session_id}",
                "telemetry": f"telemetry://{session_id}",
            },
            "agent": _session_agent(stats, session_id),
            "model": _primary_model(stats.session_models.get(session_id)),
            "event_count": int(stats.session_events.get(session_id, 0) or 0),
            "quality_score": round(float(stats.session_quality_scores.get(session_id, 0.0) or 0.0), 1),
            "goal_completed": bool(stats.session_goal_completed.get(session_id, False)),
            "recovered_failures": int(stats.session_recovered_failures.get(session_id, 0) or 0),
            "token_usage": {
                "input": int(tokens.get("input", 0) or 0),
                "output": int(tokens.get("output", 0) or 0),
                "total": _total_tokens(tokens),
            },
            "score_signals": {
                "tool_uses": _tool_use_count(spans),
                "tool_failures": session_metrics["failures"],
                "tool_loops": session_metrics["loops"],
            },
            "tool_flow": tool_flow,
            "shell_cmds": shell_cmds,
            "prompts": prompts,
            "error_recovery": recoveries,
            "improvement_targets": targets,
        }
        if session_id in deep_context_ids:
            session_entry["deep_context"] = {
                "conversation": _conversation_context(conversation),
                "spans": _span_context(spans),
            }
        sessions.append(session_entry)

    avg_quality = (
        sum(float(stats.session_quality_scores.get(session_id, 0.0) or 0.0) for session_id in ranked_session_ids)
        / len(ranked_session_ids)
        if ranked_session_ids
        else 0.0
    )

    return {
        "schema_version": 1,
        "selection_policy": {
            "session_limit": _SKILL_SESSION_LIMIT,
            "deep_context_limit": _SKILL_DEEP_CONTEXT_LIMIT,
            "prompt_snippet_limit": _SKILL_PROMPT_SNIPPET_LIMIT,
            "shell_command_limit": _SKILL_SHELL_COMMAND_LIMIT,
            "recovery_limit": _SKILL_RECOVERY_LIMIT,
        },
        "summary": {
            "total_sessions_seen": len(stats.sessions_seen),
            "included_sessions": len(ranked_session_ids),
            "deep_context_sessions": len(deep_context_ids),
            "average_quality_score": round(avg_quality, 1),
            "recurring_tool_flows": _aggregate_recurring_values(tool_flow_values, prefix="flow"),
            "recurring_shell_commands": _aggregate_recurring_values(shell_command_values, prefix="cmd"),
            "recurring_recovery_chains": _aggregate_recurring_values(recovery_values, prefix="recovery"),
            "recurring_improvement_targets": _aggregate_recurring_values(target_values, prefix="target"),
        },
        "sessions": sessions,
    }


def _graph_tables_exist(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM sqlite_master
        WHERE type = 'table' AND name IN ('graph_nodes', 'graph_edges')
        """
    ).fetchone()
    return bool(row and int(row[0]) == 2)


def _populate_skill_scope_table(conn: sqlite3.Connection, session_ids: list[str]) -> None:
    conn.execute("DROP TABLE IF EXISTS temp.skill_scope_sessions")
    conn.execute("CREATE TEMP TABLE skill_scope_sessions(session_id TEXT PRIMARY KEY)")
    for offset in range(0, len(session_ids), _SKILL_GRAPH_SESSION_INSERT_CHUNK):
        chunk = [(sid,) for sid in session_ids[offset:offset + _SKILL_GRAPH_SESSION_INSERT_CHUNK]]
        conn.executemany(
            "INSERT OR IGNORE INTO temp.skill_scope_sessions(session_id) VALUES (?)",
            chunk,
        )


def _build_graph_evidence(
    conn: sqlite3.Connection,
    *,
    session_ids: set[str] | None = None,
) -> dict:
    """Build bounded SQL graph evidence for skills extraction prompts."""
    if not _graph_tables_exist(conn):
        return {
            "source": "sql-graph",
            "scoped_session_count": 0,
            "recurring_patterns": [],
            "skill_clusters": [],
            "subagent_clusters": [],
        }

    scoped = sorted({sid for sid in (session_ids or set()) if sid})
    scope_clause = ""
    if scoped:
        _populate_skill_scope_table(conn, scoped)
        scope_clause = "AND ge.session_id IN (SELECT session_id FROM temp.skill_scope_sessions)"

    try:
        pattern_rows = conn.execute(
            f"""
            WITH edge_scope AS (
              SELECT
                ge.kind AS edge_kind,
                sn.kind AS source_kind,
                CASE
                  WHEN sn.kind IN ('Session', 'ToolCall', 'Step', 'Path', 'Folder') THEN sn.kind
                  ELSE sn.label
                END AS source_pattern,
                tn.kind AS target_kind,
                CASE
                  WHEN tn.kind IN ('Session', 'ToolCall', 'Step', 'Path', 'Folder') THEN tn.kind
                  ELSE tn.label
                END AS target_pattern,
                ge.session_id AS session_id
              FROM graph_edges ge
              JOIN graph_nodes sn ON sn.id = ge.source_node_id
              JOIN graph_nodes tn ON tn.id = ge.target_node_id
              WHERE ge.session_id IS NOT NULL
                AND ge.kind IN (
                  'used_skill',
                  'drove_tool_call',
                  'used_tool',
                  'spawned_subagent',
                  'launched_by_tool_call',
                  'achieved_outcome',
                  'produced_outcome',
                  'touched_folder',
                  'touched_path'
                )
                {scope_clause}
            )
            SELECT
              edge_kind,
              source_kind,
              source_pattern,
              target_kind,
              target_pattern,
              COUNT(*) AS occurrences,
              COUNT(DISTINCT session_id) AS session_support,
              GROUP_CONCAT(DISTINCT session_id) AS session_ids_csv
            FROM edge_scope
            GROUP BY edge_kind, source_kind, source_pattern, target_kind, target_pattern
            HAVING COUNT(DISTINCT session_id) >= 2
            ORDER BY session_support DESC, occurrences DESC, source_pattern ASC, target_pattern ASC
            LIMIT ?
            """,
            (_SKILL_GRAPH_PATTERN_LIMIT,),
        ).fetchall()

        skill_rows = conn.execute(
            f"""
            SELECT
              gn.label AS skill_name,
              COUNT(DISTINCT ge.session_id) AS session_support,
              COUNT(*) AS uses
            FROM graph_edges ge
            JOIN graph_nodes gn ON gn.id = ge.target_node_id
            WHERE ge.kind = 'used_skill'
              AND gn.kind = 'Skill'
              AND ge.session_id IS NOT NULL
              {scope_clause}
            GROUP BY gn.label
            HAVING COUNT(DISTINCT ge.session_id) >= 2
            ORDER BY session_support DESC, uses DESC, skill_name ASC
            LIMIT ?
            """,
            (_SKILL_PATTERN_LIMIT,),
        ).fetchall()

        subagent_rows = conn.execute(
            f"""
            SELECT
              gn.label AS subagent_name,
              COUNT(DISTINCT ge.session_id) AS session_support,
              COUNT(*) AS events
            FROM graph_edges ge
            JOIN graph_nodes gn ON gn.id = ge.target_node_id
            WHERE ge.kind IN ('spawned_subagent', 'stopped_subagent')
              AND gn.kind = 'Subagent'
              AND ge.session_id IS NOT NULL
              {scope_clause}
            GROUP BY gn.label
            HAVING COUNT(DISTINCT ge.session_id) >= 2
            ORDER BY session_support DESC, events DESC, subagent_name ASC
            LIMIT ?
            """,
            (_SKILL_PATTERN_LIMIT,),
        ).fetchall()
    finally:
        if scoped:
            conn.execute("DROP TABLE IF EXISTS temp.skill_scope_sessions")

    recurring_patterns: list[dict] = []
    for index, row in enumerate(pattern_rows, start=1):
        sessions = sorted(
            {item for item in str(row[7] or "").split(",") if item}
        )
        recurring_patterns.append(
            {
                "id": f"graph-{index:02d}",
                "edge_kind": str(row[0] or ""),
                "source": {"kind": str(row[1] or ""), "label": str(row[2] or "")},
                "target": {"kind": str(row[3] or ""), "label": str(row[4] or "")},
                "count": int(row[5] or 0),
                "session_support": int(row[6] or 0),
                "session_ids": sessions,
            }
        )

    return {
        "source": "sql-graph",
        "scoped_session_count": len(scoped),
        "recurring_patterns": recurring_patterns,
        "skill_clusters": [
            {
                "name": str(row[0] or ""),
                "session_support": int(row[1] or 0),
                "uses": int(row[2] or 0),
            }
            for row in skill_rows
        ],
        "subagent_clusters": [
            {
                "name": str(row[0] or ""),
                "session_support": int(row[1] or 0),
                "events": int(row[2] or 0),
            }
            for row in subagent_rows
        ],
    }


def _skills_sql_tables_exist(conn: sqlite3.Connection) -> bool:
    required = {"sessions", "steps", "tool_calls", "llm_calls"}
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('sessions','steps','tool_calls','llm_calls')"
    ).fetchall()
    present = {str(row[0]) for row in rows}
    return required <= present


def _sql_scope_session_ids(conn: sqlite3.Connection, session_ids: set[str] | None) -> list[str]:
    if session_ids:
        scoped = sorted({sid for sid in session_ids if sid})
        _populate_skill_scope_table(conn, scoped)
        try:
            rows = conn.execute(
                """
                SELECT s.id
                FROM sessions s
                JOIN temp.skill_scope_sessions scope ON scope.session_id = s.id
                ORDER BY COALESCE(s.started_at, s.created_at, ''), s.id
                """
            ).fetchall()
        finally:
            conn.execute("DROP TABLE IF EXISTS temp.skill_scope_sessions")
        return [str(row[0]) for row in rows]

    rows = conn.execute(
        """
        SELECT id
        FROM sessions
        ORDER BY COALESCE(started_at, created_at, '') DESC, id DESC
        LIMIT ?
        """,
        (_SKILL_SESSION_LIMIT * 4,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _sql_recovery_chains(tool_rows: list[tuple[str, str]]) -> list[str]:
    chains: list[str] = []
    for index, (tool_name, status) in enumerate(tool_rows):
        if status != "error":
            continue
        if not tool_name:
            continue
        for next_tool, next_status in tool_rows[index + 1:]:
            if not next_tool:
                continue
            if next_status == "ok":
                chains.append(f"{tool_name}✗→{next_tool}")
                break
    return chains


def _sql_improvement_targets(
    *,
    tool_uses: int,
    failures: int,
    loops: int,
    recovered: int,
    total_tokens: int,
    quality: float,
    completed: bool,
) -> list[dict]:
    targets: list[dict] = []
    if failures:
        targets.append(
            {
                "kind": "reliability",
                "why": (
                    f"Repeated tool failures ({failures}) suggest a reusable workflow could "
                    "front-load validation and reduce broken attempts."
                ),
            }
        )
    if loops >= 2:
        targets.append(
            {
                "kind": "exploration-churn",
                "why": (
                    f"Back-to-back tool loops ({loops}) suggest a skill could narrow search scope "
                    "and cut repeated exploration."
                ),
            }
        )
    if recovered:
        targets.append(
            {
                "kind": "recovery-playbook",
                "why": (
                    f"Observed recovery chains ({recovered}) suggest a repeatable debug playbook could "
                    "turn failures into faster recoveries."
                ),
            }
        )
    if tool_uses and total_tokens and total_tokens / max(tool_uses, 1) >= 10_000:
        targets.append(
            {
                "kind": "prompt-contract",
                "why": (
                    "High token cost per action suggests a skill could enforce tighter goal/context/"
                    "output contracts before tool use."
                ),
            }
        )
    if not completed and quality < 70:
        targets.append(
            {
                "kind": "completion-guardrails",
                "why": (
                    "The session lacks a completion signal and scored weakly, suggesting a skill could "
                    "make done-criteria and checkpoints explicit."
                ),
            }
        )
    if completed and quality >= 80 and not failures and tool_uses:
        targets.append(
            {
                "kind": "codify-effective-workflow",
                "why": (
                    "This high-quality completed session looks like a strong workflow candidate worth "
                    "codifying into a reusable skill."
                ),
            }
        )
    return targets[:4]


def _sql_signal_score(
    *,
    opportunities: int,
    failures: int,
    loops: int,
    recovered: int,
    total_tokens: int,
    quality: float,
    completed: bool,
    event_count: int,
) -> float:
    high_token_penalty = 8 if total_tokens >= 50_000 else 0
    quality_pressure = 10 if quality <= 50 else 6 if quality >= 85 else 0
    completion_pressure = 8 if not completed else 0
    event_weight = min(event_count, 200) / 20
    return (
        opportunities * 10
        + failures * 6
        + loops * 4
        + recovered * 5
        + high_token_penalty
        + quality_pressure
        + completion_pressure
        + event_weight
    )


def _build_skill_evidence_bundle_from_sql(
    conn: sqlite3.Connection,
    *,
    session_ids: set[str] | None = None,
) -> dict | None:
    """Build skills evidence from canonical SQL tables with graph augmentation."""
    if not _skills_sql_tables_exist(conn):
        return None

    scoped_session_ids = _sql_scope_session_ids(conn, session_ids)
    if not scoped_session_ids:
        return None

    session_rows = conn.execute(
        f"""
        SELECT
          s.id,
          COALESCE(a.name, '') AS agent_name,
          COALESCE(s.status, 'unknown') AS status,
          COALESCE(s.quality_score, 0) AS quality_score,
          COALESCE(s.recovered_failure_count, 0) AS recovered_failures,
          COALESCE(s.input_tokens, 0) AS input_tokens,
          COALESCE(s.output_tokens, 0) AS output_tokens,
          COALESCE(s.started_at, s.created_at, '') AS started_at
        FROM sessions s
        LEFT JOIN agents a ON a.id = s.agent_id
        WHERE s.id IN ({", ".join("?" for _ in scoped_session_ids)})
        ORDER BY started_at DESC, s.id DESC
        """,
        scoped_session_ids,
    ).fetchall()
    if not session_rows:
        return None

    per_session: list[dict] = []
    for row in session_rows:
        sid = str(row[0])
        tool_call_rows = conn.execute(
            """
            SELECT tc.tool_name, COALESCE(tc.status, 'unknown')
            FROM tool_calls tc
            JOIN steps st ON st.id = tc.step_id
            WHERE tc.session_id = ?
            ORDER BY st.seq ASC, tc.id ASC
            LIMIT 120
            """,
            (sid,),
        ).fetchall()
        ordered_tools = [str(item[0] or "") for item in tool_call_rows if item[0]]
        tool_flow = _compress_tool_sequence(ordered_tools)[:_SKILL_TOOL_FLOW_LIMIT]
        tool_uses = len(ordered_tools)
        failures = sum(1 for _tool, status in tool_call_rows if str(status) == "error")
        loops = sum(1 for idx in range(len(ordered_tools) - 1) if ordered_tools[idx] == ordered_tools[idx + 1])
        recoveries = _sql_recovery_chains(
            [(str(item[0] or ""), str(item[1] or "unknown")) for item in tool_call_rows]
        )[:_SKILL_RECOVERY_LIMIT]

        shell_rows = conn.execute(
            """
            SELECT
              COALESCE(json_extract(raw_attrs_json, '$."gen_ai.client.command"'), summary, '')
            FROM steps
            WHERE session_id = ? AND type = 'shell_command'
            ORDER BY seq ASC
            LIMIT 20
            """,
            (sid,),
        ).fetchall()
        shell_cmds: list[str] = []
        seen_shell: set[str] = set()
        for shell_row in shell_rows:
            rendered = _normalize_preview(shell_row[0], limit=120)
            if rendered and rendered not in seen_shell:
                seen_shell.add(rendered)
                shell_cmds.append(rendered)
            if len(shell_cmds) >= _SKILL_SHELL_COMMAND_LIMIT:
                break

        prompt_rows = conn.execute(
            """
            SELECT prompt_preview_redacted
            FROM llm_calls
            WHERE session_id = ? AND prompt_preview_redacted IS NOT NULL
            ORDER BY created_at ASC, id ASC
            LIMIT 10
            """,
            (sid,),
        ).fetchall()
        prompts: list[str] = []
        for prompt_row in prompt_rows:
            snippet = _normalize_preview(prompt_row[0], limit=80)
            if snippet:
                prompts.append(snippet)
            if len(prompts) >= _SKILL_PROMPT_SNIPPET_LIMIT:
                break

        model_row = conn.execute(
            """
            SELECT COALESCE(response_model, request_model, '')
            FROM llm_calls
            WHERE session_id = ? AND (response_model IS NOT NULL OR request_model IS NOT NULL)
            GROUP BY COALESCE(response_model, request_model, '')
            ORDER BY COUNT(*) DESC, COALESCE(response_model, request_model, '') ASC
            LIMIT 1
            """,
            (sid,),
        ).fetchone()
        model = str(model_row[0]) if model_row and model_row[0] else "unknown"

        event_row = conn.execute(
            "SELECT COUNT(*) FROM steps WHERE session_id = ?",
            (sid,),
        ).fetchone()
        event_count = int(event_row[0] or 0)

        input_tokens = int(row[5] or 0)
        output_tokens = int(row[6] or 0)
        total_tokens = input_tokens + output_tokens
        quality = float(row[3] or 0.0)
        recovered = int(row[4] or 0)
        completed = str(row[2] or "").lower() in {"ok", "completed", "success", "succeeded"}
        targets = _sql_improvement_targets(
            tool_uses=tool_uses,
            failures=failures,
            loops=loops,
            recovered=recovered,
            total_tokens=total_tokens,
            quality=quality,
            completed=completed,
        )
        signal_score = _sql_signal_score(
            opportunities=len(targets),
            failures=failures,
            loops=loops,
            recovered=recovered,
            total_tokens=total_tokens,
            quality=quality,
            completed=completed,
            event_count=event_count,
        )
        per_session.append(
            {
                "id": sid,
                "short_id": sid[:8],
                "agent": str(row[1] or ""),
                "model": model,
                "event_count": event_count,
                "quality_score": round(quality, 1),
                "goal_completed": completed,
                "recovered_failures": recovered,
                "token_usage": {"input": input_tokens, "output": output_tokens, "total": total_tokens},
                "score_signals": {
                    "tool_uses": tool_uses,
                    "tool_failures": failures,
                    "tool_loops": loops,
                },
                "tool_flow": tool_flow,
                "shell_cmds": shell_cmds,
                "prompts": prompts,
                "error_recovery": recoveries,
                "improvement_targets": targets,
                "signal_score": round(signal_score, 1),
            }
        )

    ranked = sorted(
        per_session,
        key=lambda item: (-float(item["signal_score"]), -int(item["event_count"]), str(item["id"])),
    )[:_SKILL_SESSION_LIMIT]

    tool_flow_values: dict[str, list[str]] = {}
    shell_command_values: dict[str, list[str]] = {}
    recovery_values: dict[str, list[str]] = {}
    target_values: dict[str, list[str]] = {}
    sessions_payload: list[dict] = []
    for rank, session in enumerate(ranked, start=1):
        sid = str(session["id"])
        tool_flow_values[sid] = [" → ".join(session["tool_flow"])] if session["tool_flow"] else []
        shell_command_values[sid] = list(session["shell_cmds"])
        recovery_values[sid] = list(session["error_recovery"])
        target_values[sid] = [target["kind"] for target in session["improvement_targets"]]
        sessions_payload.append(
            {
                "id": sid,
                "short_id": session["short_id"],
                "rank": rank,
                "signal_score": session["signal_score"],
                "refs": {"session": f"session://{sid}", "telemetry": f"telemetry://{sid}"},
                "agent": session["agent"],
                "model": session["model"],
                "event_count": session["event_count"],
                "quality_score": session["quality_score"],
                "goal_completed": session["goal_completed"],
                "recovered_failures": session["recovered_failures"],
                "token_usage": session["token_usage"],
                "score_signals": session["score_signals"],
                "tool_flow": session["tool_flow"],
                "shell_cmds": session["shell_cmds"],
                "prompts": session["prompts"],
                "error_recovery": session["error_recovery"],
                "improvement_targets": session["improvement_targets"],
            }
        )

    avg_quality = (
        sum(float(session["quality_score"]) for session in ranked) / len(ranked)
        if ranked
        else 0.0
    )
    bundle = {
        "schema_version": 1,
        "selection_policy": {
            "session_limit": _SKILL_SESSION_LIMIT,
            "deep_context_limit": 0,
            "prompt_snippet_limit": _SKILL_PROMPT_SNIPPET_LIMIT,
            "shell_command_limit": _SKILL_SHELL_COMMAND_LIMIT,
            "recovery_limit": _SKILL_RECOVERY_LIMIT,
            "evidence_source": "sql",
        },
        "summary": {
            "total_sessions_seen": len(session_rows),
            "included_sessions": len(ranked),
            "deep_context_sessions": 0,
            "average_quality_score": round(avg_quality, 1),
            "recurring_tool_flows": _aggregate_recurring_values(tool_flow_values, prefix="flow"),
            "recurring_shell_commands": _aggregate_recurring_values(shell_command_values, prefix="cmd"),
            "recurring_recovery_chains": _aggregate_recurring_values(recovery_values, prefix="recovery"),
            "recurring_improvement_targets": _aggregate_recurring_values(target_values, prefix="target"),
        },
        "sessions": sessions_payload,
        "graph_evidence": _build_graph_evidence(conn, session_ids={str(item["id"]) for item in ranked}),
    }
    return bundle


def _serialize_sessions_for_skills(stats: TelemetryStats | None = None, bundle: dict | None = None) -> str:
    """Serialize ranked sessions into a compact, human-readable extraction summary."""
    if bundle is None:
        if stats is None:
            raise ValueError("stats is required when bundle is not provided")
        bundle = _build_skill_evidence_bundle(stats)
    lines = [
        "Selection policy:",
        (
            "  sessions="
            f"{bundle['summary']['included_sessions']} deep_context={bundle['summary']['deep_context_sessions']} "
            f"avg_quality={bundle['summary']['average_quality_score']}"
        ),
    ]

    recurring_sections = [
        ("recurring_tool_flows", "Recurring tool flows:"),
        ("recurring_shell_commands", "Recurring shell commands:"),
        ("recurring_recovery_chains", "Recurring recovery chains:"),
        ("recurring_improvement_targets", "Recurring improvement targets:"),
    ]
    for key, title in recurring_sections:
        entries = bundle["summary"][key]
        if not entries:
            continue
        lines.append(title)
        for entry in entries:
            lines.append(
                f"  {entry['id']} count={entry['count']} sessions={','.join(entry['session_ids'])} value={entry['value']}"
            )

    graph_evidence = bundle.get("graph_evidence") if isinstance(bundle, dict) else None
    if isinstance(graph_evidence, dict):
        recurring_graph = graph_evidence.get("recurring_patterns") or []
        if recurring_graph:
            lines.append("Graph recurring patterns:")
            for pattern in recurring_graph[:_SKILL_GRAPH_PATTERN_LIMIT]:
                source = pattern.get("source") or {}
                target = pattern.get("target") or {}
                lines.append(
                    "  "
                    f"{pattern.get('id', 'graph')} support={pattern.get('session_support', 0)} "
                    f"sessions={','.join(pattern.get('session_ids') or [])} "
                    f"{source.get('label', '?')} -[{pattern.get('edge_kind', '?')}]-> {target.get('label', '?')}"
                )

    for session in bundle["sessions"]:
        lines.append(f"Session {session['short_id']}:")
        lines.append(
            "  "
            f"model={session['model']} events={session['event_count']} tokens={session['token_usage']['total']} "
            f"quality={session['quality_score']} completed={'yes' if session['goal_completed'] else 'no'} "
            f"signal={session['signal_score']}"
        )
        lines.append(f"  refs={session['refs']['session']}")

        if session["tool_flow"]:
            lines.append(f"  tool_flow={' → '.join(session['tool_flow'])}")
        if session["shell_cmds"]:
            lines.append(f"  shell_cmds={' | '.join(session['shell_cmds'])}")
        if session["prompts"]:
            lines.append(f"  prompts=[{' / '.join(session['prompts'])}]")
        if session["error_recovery"]:
            lines.append(f"  error_recovery={' | '.join(session['error_recovery'])}")
        if session["improvement_targets"]:
            rendered_targets = " | ".join(
                f"{target['kind']}: {target['why']}" for target in session["improvement_targets"]
            )
            lines.append(f"  improvement_targets={rendered_targets}")
        deep_context = session.get("deep_context") or {}
        if deep_context:
            lines.append(
                "  "
                f"deep_context=conversation[{len(deep_context.get('conversation', []))}] "
                f"spans[{len(deep_context.get('spans', []))}]"
            )

    return "\n".join(lines)


def _build_skills_extraction_prompt_from_bundle(prompt_text: str, bundle: dict) -> str:
    """Build the full prompt passed to the extraction agent."""
    summary = _serialize_sessions_for_skills(bundle=bundle)
    bundle_json = json.dumps(bundle, indent=2, sort_keys=True)
    return (
        prompt_text.rstrip()
        + "\n\nEvidence summary:\n"
        + summary
        + "\n\nEvidence JSON (authoritative):\n"
        + bundle_json
        + "\n"
    )


def _build_skills_extraction_prompt(prompt_text: str, stats: TelemetryStats, *, bundle: dict | None = None) -> str:
    """Build the full prompt passed to the extraction agent."""
    rendered_bundle = bundle if bundle is not None else _build_skill_evidence_bundle(stats)
    return _build_skills_extraction_prompt_from_bundle(prompt_text, rendered_bundle)
