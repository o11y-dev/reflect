from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from reflect.store.provenance import apply_origin_kind, classify_origin_kind
from reflect.store.workspaces import backfill_session_context


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


def _first_text(attrs: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _first_hash(attrs: dict[str, Any], text: str | None, *keys: str) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if isinstance(value, str) and value:
            return value
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


def backfill_tool_call_hashes(conn: sqlite3.Connection) -> dict[str, int]:
    """Fill privacy-safe fingerprints from already-redacted tool previews.

    Older canonical rows predate tool input/output fingerprint persistence. The
    preview is already the local, redacted value used by the dashboard, so this
    repair stores only its SHA-256 digest and never introduces new raw content.
    """
    rows = conn.execute(
        """
        SELECT id, input_preview_redacted, output_preview_redacted
        FROM tool_calls
        WHERE (NULLIF(input_hash, '') IS NULL AND NULLIF(input_preview_redacted, '') IS NOT NULL)
           OR (NULLIF(output_hash, '') IS NULL AND NULLIF(output_preview_redacted, '') IS NOT NULL)
        """
    ).fetchall()
    updated = 0
    for tool_call_id, input_preview, output_preview in rows:
        input_hash = (
            hashlib.sha256(str(input_preview).encode("utf-8")).hexdigest()
            if input_preview not in (None, "")
            else None
        )
        output_hash = (
            hashlib.sha256(str(output_preview).encode("utf-8")).hexdigest()
            if output_preview not in (None, "")
            else None
        )
        cursor = conn.execute(
            """
            UPDATE tool_calls
            SET input_hash = COALESCE(NULLIF(input_hash, ''), ?),
                output_hash = COALESCE(NULLIF(output_hash, ''), ?),
                updated_at = CASE
                  WHEN (NULLIF(input_hash, '') IS NULL AND ? IS NOT NULL)
                    OR (NULLIF(output_hash, '') IS NULL AND ? IS NOT NULL)
                  THEN ? ELSE updated_at END
            WHERE id = ?
            """,
            (input_hash, output_hash, input_hash, output_hash, _now(), tool_call_id),
        )
        updated += max(0, cursor.rowcount)
    return {"updated": updated}


def _extract_mcp_server_and_tool(attrs: dict[str, Any]) -> tuple[str | None, str | None]:
    server = _first_text(
        attrs,
        "gen_ai.client.mcp_server",
        "mcp.server",
        "gen_ai.client.tool.input.server",
        "tool.input.server",
    )
    tool = _first_text(
        attrs,
        "gen_ai.client.mcp_tool",
        "mcp.tool",
        "gen_ai.client.tool.input.toolName",
        "gen_ai.client.tool.input.tool",
        "tool.input.toolName",
        "tool.input.tool",
    )
    payload = _load_json(
        str(
            attrs.get("gen_ai.client.tool.input")
            or attrs.get("tool.input")
            or ""
        )
    )
    if server is None:
        for key in ("server", "serverName", "mcpServer"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                server = value
                break
    if tool is None:
        for key in ("toolName", "tool", "mcpTool"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                tool = value
                break
    return server, tool


def _agent_name(attrs: dict[str, Any]) -> str:
    return str(
        attrs.get("gen_ai.client.name")
        or attrs.get("ide.name")
        or attrs.get("agent.name")
        or attrs.get("service.name")
        or "unknown"
    )


def _step_type(event_type: str, attrs: dict[str, Any]) -> str:
    event = _event_name(event_type, attrs).lower()
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
    event = _event_name(event_type, attrs).lower()
    if status in {"ok", "error", "skipped", "unknown"}:
        return status
    if status in {"completed", "complete", "success", "succeeded"}:
        return "ok"
    if status in {"failed", "failure"}:
        return "error"
    if "error" in event or "fail" in event:
        return "error"
    if event.startswith("after") or event.startswith("post") or event in {"stop", "sessionend"}:
        return "ok"
    return "unknown"


def _event_name(event_type: str, attrs: dict[str, Any]) -> str:
    event = (
        attrs.get("gen_ai.client.hook.event")
        or attrs.get("ide.hook.event")
        or attrs.get("event.name")
        or event_type
    )
    return str(event).rsplit(".", 1)[-1]


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
          status = CASE
            WHEN sessions.status = 'error' OR excluded.status = 'error' THEN 'error'
            WHEN excluded.status = 'ok' THEN 'ok'
            WHEN sessions.status IN ('ok', 'active') THEN sessions.status
            ELSE excluded.status
          END,
          source_kind = CASE
            WHEN excluded.source_kind = 'native_session' THEN excluded.source_kind
            ELSE COALESCE(sessions.source_kind, excluded.source_kind)
          END,
          source_ref = CASE
            WHEN excluded.source_kind = 'native_session' THEN excluded.source_ref
            ELSE COALESCE(sessions.source_ref, excluded.source_ref)
          END,
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
          status, summary, origin_kind, raw_attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            raw_event["origin_kind"],
            raw_event["attrs_json"],
            timestamp,
            timestamp,
        ),
    )
    return step_id


def repair_telemetry_provenance(conn: sqlite3.Connection) -> dict[str, int]:
    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, source_type, origin_kind, attrs_json
            FROM raw_events
            WHERE origin_kind IS NULL
               OR COALESCE(NULLIF(json_extract(attrs_json, '$."reflect.telemetry.origin"'), ''), '') = ''
            ORDER BY observed_at, id
            """
        ).fetchall()
        repaired_raw_events = 0
        repaired_steps = 0
        for row in rows:
            attrs = _load_json(row["attrs_json"])
            origin_kind = classify_origin_kind(str(row["source_type"] or ""), attrs)
            if not origin_kind:
                continue
            repaired_attrs = apply_origin_kind(attrs, origin_kind)
            attrs_json = json.dumps(repaired_attrs, sort_keys=True)
            conn.execute(
                "UPDATE raw_events SET origin_kind = ?, attrs_json = ? WHERE id = ?",
                (origin_kind, attrs_json, row["id"]),
            )
            repaired_raw_events += 1
            step_id = _stable_id("step", row["id"])
            step_result = conn.execute(
                "UPDATE steps SET origin_kind = ?, raw_attrs_json = ? WHERE id = ?",
                (origin_kind, attrs_json, step_id),
            )
            repaired_steps += int(step_result.rowcount or 0)
        conn.commit()
        return {"raw_events": repaired_raw_events, "steps": repaired_steps}
    finally:
        conn.row_factory = previous_row_factory


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
        prompt_preview = _first_text(
            attrs,
            "gen_ai.client.prompt.text",
            "gen_ai.client.prompt",
            "gen_ai.input.messages",
            "prompt",
        )
        response_preview = _first_text(
            attrs,
            "gen_ai.client.output",
            "gen_ai.response.text",
            "gen_ai.response.content",
            "gen_ai.output.messages",
            "response",
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO llm_calls(
              id, step_id, session_id, provider, request_model, response_model,
              operation_name, input_tokens, output_tokens, cache_creation_input_tokens,
              cache_read_input_tokens, reasoning_output_tokens, latency_ms,
              prompt_hash, response_hash, prompt_preview_redacted, response_preview_redacted,
              raw_attrs_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                _first_hash(attrs, prompt_preview, "gen_ai.client.prompt.sha256", "gen_ai.prompt.sha256"),
                _first_hash(attrs, response_preview, "gen_ai.response.sha256"),
                prompt_preview,
                response_preview,
                raw_event["attrs_json"],
                timestamp,
                timestamp,
            ),
        )
    elif step_type == "tool_call":
        tool_name = str(attrs.get("gen_ai.client.tool_name") or attrs.get("gen_ai.client.command") or raw_event["event_type"])
        input_preview = _first_text(attrs, "gen_ai.client.tool.input", "tool.input")
        output_preview = _first_text(attrs, "gen_ai.client.tool.output", "tool.output")
        conn.execute(
            """
            INSERT OR IGNORE INTO tool_calls(
              id, step_id, session_id, tool_name, tool_type, status, duration_ms,
              input_hash, output_hash, input_preview_redacted, output_preview_redacted, error_type,
              error_message_redacted, raw_attrs_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _stable_id("tool", raw_event["id"]),
                step_id,
                session_id,
                tool_name,
                attrs.get("gen_ai.client.tool_type"),
                status,
                duration,
                _first_hash(
                    attrs,
                    input_preview,
                    "gen_ai.client.tool.input.sha256",
                    "gen_ai.client.tool.input_hash",
                    "tool.input.sha256",
                ),
                _first_hash(
                    attrs,
                    output_preview,
                    "gen_ai.client.tool.output.sha256",
                    "gen_ai.client.tool.output_hash",
                    "tool.output.sha256",
                ),
                input_preview,
                output_preview,
                attrs.get("error.type"),
                attrs.get("error.message"),
                raw_event["attrs_json"],
                timestamp,
                timestamp,
            ),
        )
    elif step_type == "mcp_call":
        mcp_server, mcp_tool = _extract_mcp_server_and_tool(attrs)
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
                mcp_server,
                mcp_tool or attrs.get("gen_ai.client.tool_name"),
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


def _refresh_session_statuses(conn: sqlite3.Connection, session_ids: set[str], timestamp: str) -> None:
    if not session_ids:
        return
    for session_id in sorted(session_ids):
        row = conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0) AS error_count,
              COALESCE(SUM(CASE
                WHEN lower(COALESCE(json_extract(raw_attrs_json, '$."gen_ai.client.hook.event"'), summary, ''))
                  IN ('stop', 'sessionend', 'session_end', 'subagentstop', 'subagent_stop')
                  THEN 1
                WHEN lower(COALESCE(summary, '')) IN (
                  'gen_ai.client.hook.stop',
                  'gen_ai.client.hook.sessionend',
                  'gen_ai.client.hook.subagentstop',
                  'ide.hook.stop'
                )
                  THEN 1
                ELSE 0
              END), 0) AS terminal_count
            FROM steps
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if not row:
            continue
        error_count = int(row["error_count"] or 0)
        terminal_count = int(row["terminal_count"] or 0)
        if error_count > 0:
            status = "error"
        elif terminal_count > 0:
            status = "ok"
        else:
            status = "unknown"
        conn.execute(
            """
            UPDATE sessions
            SET status = ?, failure_count = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, error_count, timestamp, session_id),
        )


def refresh_session_statuses(
    conn: sqlite3.Connection,
    session_ids: set[str],
) -> dict[str, int]:
    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        normalized_ids = {str(session_id) for session_id in session_ids if session_id}
        _refresh_session_statuses(conn, normalized_ids, _now())
        return {"sessions": len(normalized_ids)}
    finally:
        conn.row_factory = previous_row_factory


def refresh_all_session_statuses(conn: sqlite3.Connection) -> dict[str, int]:
    session_ids = {str(row[0]) for row in conn.execute("SELECT id FROM sessions").fetchall()}
    return refresh_session_statuses(conn, session_ids)


def normalize_pending_raw_events(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    changed_session_ids: set[str] | None = None,
) -> dict[str, int]:
    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        repair_telemetry_provenance(conn)
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
        backfill_session_context(
            conn,
            timestamp=timestamp,
            changed_session_ids=processed_session_ids,
            session_ids=processed_session_ids,
        )
        backfill_tool_call_hashes(conn)
        _refresh_session_statuses(conn, processed_session_ids, timestamp)
        if changed_session_ids is not None:
            changed_session_ids.update(processed_session_ids)
        conn.commit()
        return {"processed": processed, "failed": failed, "skipped": 0}
    finally:
        conn.row_factory = previous_row_factory
