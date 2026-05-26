from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _stable_id(prefix: str, *parts: object) -> str:
    payload = ":".join(str(part or "") for part in parts)
    digest = hashlib.sha1(payload.encode()).hexdigest()
    return f"{prefix}_{digest}"


def _load_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _to_int(value: object) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _duration_ms(observed_at: str, received_at: str) -> int | None:
    try:
        start = datetime.fromisoformat(observed_at)
        end = datetime.fromisoformat(received_at)
    except ValueError:
        return None
    delta = int((end - start).total_seconds() * 1000)
    return max(delta, 0)


def _agent_name(attrs: dict[str, Any]) -> str:
    return str(
        attrs.get("gen_ai.client.name")
        or attrs.get("ide.name")
        or attrs.get("agent.name")
        or attrs.get("service.name")
        or "unknown"
    )


def _step_type(event_type: str, attrs: dict[str, Any]) -> str:
    event = event_type.lower()
    if attrs.get("gen_ai.memory.id"):
        return "memory_event"
    if "mcp" in event or attrs.get("gen_ai.client.mcp_server") or attrs.get("gen_ai.client.mcp_tool"):
        return "mcp_call"
    if attrs.get("gen_ai.client.command"):
        return "shell_command"
    if attrs.get("gen_ai.client.tool_name") or attrs.get("ide.tool_name") or "tool" in event:
        return "tool_call"
    if (
        attrs.get("gen_ai.request.model")
        or attrs.get("gen_ai.response.model")
        or attrs.get("gen_ai.usage.input_tokens")
        or attrs.get("gen_ai.usage.output_tokens")
        or "prompt" in event
        or "llm" in event
    ):
        return "llm_call"
    if "error" in event or "fail" in event:
        return "error"
    return "unknown"


def _status(event_type: str, attrs: dict[str, Any]) -> str:
    status = str(attrs.get("gen_ai.client.status") or attrs.get("status") or "").lower()
    event = event_type.lower()
    if status in {"ok", "error", "skipped"}:
        return status
    if "error" in event or "fail" in event:
        return "error"
    if event.startswith("after") or event.startswith("post") or event in {"stop", "sessionend"}:
        return "ok"
    return "unknown"


def _next_step_seq(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), -1) + 1 FROM steps WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row[0])


def _insert_agent(conn: sqlite3.Connection, attrs: dict[str, Any], timestamp: str) -> str:
    name = _agent_name(attrs)
    agent_id = _stable_id("agent", name)
    conn.execute(
        """
        INSERT OR IGNORE INTO agents(id, name, kind, raw_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (agent_id, name, attrs.get("gen_ai.client.kind"), json.dumps(attrs, sort_keys=True), timestamp, timestamp),
    )
    return agent_id


def _upsert_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    agent_id: str,
    raw_event: sqlite3.Row,
    attrs: dict[str, Any],
    timestamp: str,
) -> None:
    input_tokens = _to_int(attrs.get("gen_ai.usage.input_tokens"))
    output_tokens = _to_int(attrs.get("gen_ai.usage.output_tokens"))
    cache_creation = _to_int(attrs.get("gen_ai.usage.cache_creation.input_tokens"))
    cache_read = _to_int(attrs.get("gen_ai.usage.cache_read.input_tokens"))
    reasoning = _to_int(attrs.get("gen_ai.usage.reasoning_output_tokens"))
    conn.execute(
        """
        INSERT INTO sessions(
          id, agent_id, started_at, ended_at, status, input_tokens, output_tokens,
          cache_creation_tokens, cache_read_tokens, reasoning_tokens, source_kind,
          source_ref, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          agent_id = COALESCE(sessions.agent_id, excluded.agent_id),
          started_at = MIN(sessions.started_at, excluded.started_at),
          ended_at = CASE
            WHEN sessions.ended_at IS NULL THEN excluded.ended_at
            WHEN excluded.ended_at IS NULL THEN sessions.ended_at
            ELSE MAX(sessions.ended_at, excluded.ended_at)
          END,
          input_tokens = sessions.input_tokens + excluded.input_tokens,
          output_tokens = sessions.output_tokens + excluded.output_tokens,
          cache_creation_tokens = sessions.cache_creation_tokens + excluded.cache_creation_tokens,
          cache_read_tokens = sessions.cache_read_tokens + excluded.cache_read_tokens,
          reasoning_tokens = sessions.reasoning_tokens + excluded.reasoning_tokens,
          updated_at = excluded.updated_at
        """,
        (
            session_id,
            agent_id,
            raw_event["observed_at"],
            raw_event["received_at"],
            _status(raw_event["event_type"], attrs),
            input_tokens,
            output_tokens,
            cache_creation,
            cache_read,
            reasoning,
            raw_event["source_type"],
            raw_event["source_id"],
            timestamp,
            timestamp,
        ),
    )


def _insert_step(
    conn: sqlite3.Connection,
    *,
    raw_event: sqlite3.Row,
    attrs: dict[str, Any],
    session_id: str,
    step_type: str,
    timestamp: str,
) -> str:
    step_id = _stable_id("step", raw_event["id"])
    conn.execute(
        """
        INSERT OR IGNORE INTO steps(
          id, session_id, seq, type, started_at, ended_at, duration_ms,
          status, summary, raw_attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            step_id,
            session_id,
            _next_step_seq(conn, session_id),
            step_type,
            raw_event["observed_at"],
            raw_event["received_at"],
            _duration_ms(raw_event["observed_at"], raw_event["received_at"]),
            _status(raw_event["event_type"], attrs),
            raw_event["event_type"],
            raw_event["attrs_json"],
            timestamp,
            timestamp,
        ),
    )
    return step_id


def _insert_call_record(
    conn: sqlite3.Connection,
    *,
    raw_event: sqlite3.Row,
    attrs: dict[str, Any],
    session_id: str,
    step_id: str,
    step_type: str,
    timestamp: str,
) -> None:
    duration = _duration_ms(raw_event["observed_at"], raw_event["received_at"])
    status = _status(raw_event["event_type"], attrs)
    if step_type == "llm_call":
        conn.execute(
            """
            INSERT OR IGNORE INTO llm_calls(
              id, step_id, session_id, provider, request_model, response_model,
              operation_name, input_tokens, output_tokens, cache_creation_input_tokens,
              cache_read_input_tokens, reasoning_output_tokens, latency_ms,
              raw_attrs_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _stable_id("llm", raw_event["id"]),
                step_id,
                session_id,
                attrs.get("gen_ai.system"),
                attrs.get("gen_ai.request.model"),
                attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model"),
                raw_event["event_type"],
                _to_int(attrs.get("gen_ai.usage.input_tokens")),
                _to_int(attrs.get("gen_ai.usage.output_tokens")),
                _to_int(attrs.get("gen_ai.usage.cache_creation.input_tokens")),
                _to_int(attrs.get("gen_ai.usage.cache_read.input_tokens")),
                _to_int(attrs.get("gen_ai.usage.reasoning_output_tokens")),
                duration,
                raw_event["attrs_json"],
                timestamp,
                timestamp,
            ),
        )
    elif step_type == "tool_call":
        tool_name = str(attrs.get("gen_ai.client.tool_name") or attrs.get("gen_ai.client.command") or raw_event["event_type"])
        conn.execute(
            """
            INSERT OR IGNORE INTO tool_calls(
              id, step_id, session_id, tool_name, tool_type, status, duration_ms,
              input_preview_redacted, output_preview_redacted, error_type,
              error_message_redacted, raw_attrs_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _stable_id("tool", raw_event["id"]),
                step_id,
                session_id,
                tool_name,
                attrs.get("gen_ai.client.tool_type"),
                status,
                duration,
                attrs.get("gen_ai.client.tool.input"),
                attrs.get("gen_ai.client.tool.output"),
                attrs.get("error.type"),
                attrs.get("error.message"),
                raw_event["attrs_json"],
                timestamp,
                timestamp,
            ),
        )
    elif step_type == "mcp_call":
        conn.execute(
            """
            INSERT OR IGNORE INTO mcp_calls(
              id, step_id, session_id, mcp_session_id, mcp_protocol_version,
              transport, server_name, tool_name, status, duration_ms,
              raw_attrs_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _stable_id("mcp", raw_event["id"]),
                step_id,
                session_id,
                attrs.get("gen_ai.client.mcp_session_id"),
                attrs.get("gen_ai.client.mcp_protocol_version"),
                attrs.get("gen_ai.client.mcp_transport"),
                attrs.get("gen_ai.client.mcp_server"),
                attrs.get("gen_ai.client.mcp_tool") or attrs.get("gen_ai.client.tool_name"),
                status,
                duration,
                raw_event["attrs_json"],
                timestamp,
                timestamp,
            ),
        )


def _insert_memory_record(
    conn: sqlite3.Connection,
    *,
    raw_event: sqlite3.Row,
    attrs: dict[str, Any],
    session_id: str,
    step_id: str,
    timestamp: str,
) -> None:
    memory_id = attrs.get("gen_ai.memory.id")
    if not memory_id:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO memories(
          id, scope, type, session_id, step_id, content_hash,
          content_preview_redacted, confidence, sensitivity, source, expires_at,
          last_seen_at, raw_attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            attrs.get("gen_ai.memory.scope") or "agent",
            attrs.get("gen_ai.memory.type") or "unknown",
            session_id,
            step_id,
            attrs.get("gen_ai.memory.content_hash"),
            attrs.get("gen_ai.memory.content_preview"),
            float(attrs.get("gen_ai.memory.confidence") or 0.5),
            attrs.get("gen_ai.memory.sensitivity") or "unknown",
            attrs.get("gen_ai.memory.source") or "opentelemetry_hook",
            attrs.get("gen_ai.memory.expires_at"),
            attrs.get("gen_ai.memory.last_seen_at") or raw_event["observed_at"],
            raw_event["attrs_json"],
            timestamp,
            timestamp,
        ),
    )


def _insert_privacy_finding(
    conn: sqlite3.Connection,
    *,
    raw_event: sqlite3.Row,
    attrs: dict[str, Any],
    session_id: str,
    step_id: str,
    timestamp: str,
) -> None:
    finding_type = attrs.get("gen_ai.privacy.finding_type")
    if not finding_type:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO privacy_findings(
          id, session_id, step_id, finding_type, severity, field_name,
          action_taken, detail_redacted, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _stable_id("privacy", raw_event["id"], finding_type),
            session_id,
            step_id,
            finding_type,
            attrs.get("gen_ai.privacy.severity") or "unknown",
            attrs.get("gen_ai.privacy.field_name"),
            attrs.get("gen_ai.privacy.action_taken") or "detected",
            attrs.get("gen_ai.privacy.detail_redacted"),
            timestamp,
        ),
    )


def _backfill_parent_step_ids(conn: sqlite3.Connection, session_ids: set[str]) -> None:
    if not session_ids:
        return
    for session_id in sorted(session_ids):
        rows = conn.execute(
            """
            SELECT id, span_id, parent_span_id
            FROM raw_events
            WHERE session_id = ?
              AND COALESCE(span_id, '') <> ''
              AND COALESCE(parent_span_id, '') <> ''
            """,
            (session_id,),
        ).fetchall()
        span_to_step_id = {
            str(row["span_id"]): _stable_id("step", row["id"])
            for row in conn.execute(
                """
                SELECT id, span_id
                FROM raw_events
                WHERE session_id = ? AND COALESCE(span_id, '') <> ''
                """,
                (session_id,),
            ).fetchall()
        }
        for row in rows:
            step_id = _stable_id("step", row["id"])
            parent_step_id = span_to_step_id.get(str(row["parent_span_id"] or ""))
            if not parent_step_id or parent_step_id == step_id:
                continue
            conn.execute(
                """
                UPDATE steps
                SET parent_step_id = COALESCE(parent_step_id, ?)
                WHERE id = ?
                  AND EXISTS (SELECT 1 FROM steps parent WHERE parent.id = ?)
                """,
                (parent_step_id, step_id, parent_step_id),
            )


def normalize_pending_raw_events(conn: sqlite3.Connection, *, limit: int | None = None) -> dict[str, int]:
    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        params: list[int] = []
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(limit)
        rows = conn.execute(
            """
            SELECT *
            FROM raw_events
            WHERE normalized_status = 'pending'
            ORDER BY observed_at, id
            """ + limit_sql,
            params,
        ).fetchall()

        processed = 0
        failed = 0
        processed_session_ids: set[str] = set()
        timestamp = _now()
        for index, row in enumerate(rows):
            savepoint = f"normalize_raw_event_{index}"
            session_id = ""
            try:
                conn.execute(f"SAVEPOINT {savepoint}")
                attrs = _load_json(row["attrs_json"])
                session_id = row["session_id"] or attrs.get("session.id") or attrs.get("gen_ai.client.session_id")
                if not session_id:
                    session_id = _stable_id("session", row["source_id"])
                session_id = str(session_id)
                agent_id = _insert_agent(conn, attrs, timestamp)
                _upsert_session(
                    conn,
                    session_id=session_id,
                    agent_id=agent_id,
                    raw_event=row,
                    attrs=attrs,
                    timestamp=timestamp,
                )
                step_type = _step_type(row["event_type"], attrs)
                step_id = _insert_step(
                    conn,
                    raw_event=row,
                    attrs=attrs,
                    session_id=session_id,
                    step_type=step_type,
                    timestamp=timestamp,
                )
                _insert_call_record(
                    conn,
                    raw_event=row,
                    attrs=attrs,
                    session_id=session_id,
                    step_id=step_id,
                    step_type=step_type,
                    timestamp=timestamp,
                )
                _insert_memory_record(
                    conn,
                    raw_event=row,
                    attrs=attrs,
                    session_id=session_id,
                    step_id=step_id,
                    timestamp=timestamp,
                )
                _insert_privacy_finding(
                    conn,
                    raw_event=row,
                    attrs=attrs,
                    session_id=session_id,
                    step_id=step_id,
                    timestamp=timestamp,
                )
                conn.execute(
                    "UPDATE raw_events SET normalized_status = 'ok', normalization_error = NULL WHERE id = ?",
                    (row["id"],),
                )
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                processed_session_ids.add(session_id)
                processed += 1
            except Exception as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                conn.execute(
                    "UPDATE raw_events SET normalized_status = 'failed', normalization_error = ? WHERE id = ?",
                    (str(exc), row["id"]),
                )
                failed += 1
        _backfill_parent_step_ids(conn, processed_session_ids)
        conn.commit()
        return {"processed": processed, "failed": failed, "skipped": 0}
    finally:
        conn.row_factory = previous_row_factory
