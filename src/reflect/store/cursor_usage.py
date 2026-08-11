"""Reconcile and estimate Cursor usage in the canonical SQLite store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from reflect.utils import _flatten_text_content, _load_json_lines


def _stable_id(prefix: str, *parts: object) -> str:
    payload = ":".join(str(part or "") for part in parts)
    digest = hashlib.sha1(payload.encode()).hexdigest()
    return f"{prefix}_{digest}"


def _rough_token_count(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, len(stripped) // 4)


def estimate_cursor_transcript_usage(file_path: Path) -> dict[str, int]:
    input_tokens = 0
    output_tokens = 0
    for event in _load_json_lines(file_path):
        role = event.get("role")
        if role not in {"user", "assistant"}:
            continue
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        text = _flatten_text_content(message.get("content"))
        tokens = _rough_token_count(text)
        if role == "user":
            input_tokens += tokens
        elif role == "assistant":
            output_tokens += tokens
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def _session_token_state(conn: sqlite3.Connection, session_id: str) -> tuple[bool, bool] | None:
    row = conn.execute(
        """
        SELECT lower(COALESCE(a.name, '')), s.token_provenance
        FROM sessions s
        LEFT JOIN agents a ON a.id = s.agent_id
        WHERE s.id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]) == "cursor", str(row[1]) != "unavailable"


def repair_misattributed_cursor_transcript_usage(
    conn: sqlite3.Connection,
) -> dict[str, int | list[str]]:
    """Remove Cursor-only estimates from sessions attributed to another agent."""

    rows = conn.execute(
        """
        SELECT DISTINCT st.session_id
        FROM steps st
        JOIN sessions s ON s.id = st.session_id
        LEFT JOIN agents a ON a.id = s.agent_id
        WHERE json_extract(st.raw_attrs_json, '$."reflect.token.source"')
                = 'estimated_cursor_transcript'
          AND lower(COALESCE(a.name, '')) <> 'cursor'
        ORDER BY st.session_id
        """
    ).fetchall()
    session_ids = [str(row[0]) for row in rows]
    timestamp = datetime.now(tz=UTC).isoformat()
    for session_id in session_ids:
        usage = conn.execute(
            """
            WITH canonical_usage AS (
              SELECT DISTINCT
                COALESCE(NULLIF(response_model, ''), NULLIF(request_model, ''), '') AS model,
                input_tokens,
                output_tokens,
                cache_creation_input_tokens,
                cache_read_input_tokens,
                reasoning_output_tokens
              FROM llm_calls
              WHERE session_id = ?
                AND input_tokens + output_tokens + cache_creation_input_tokens
                    + cache_read_input_tokens + reasoning_output_tokens > 0
            )
            SELECT
              COALESCE(SUM(input_tokens), 0),
              COALESCE(SUM(output_tokens), 0),
              COALESCE(SUM(cache_creation_input_tokens), 0),
              COALESCE(SUM(cache_read_input_tokens), 0),
              COALESCE(SUM(reasoning_output_tokens), 0)
            FROM canonical_usage
            """,
            (session_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE sessions
            SET input_tokens = ?, output_tokens = ?, cache_creation_tokens = ?,
                cache_read_tokens = ?, reasoning_tokens = ?,
                estimated_cost_usd = 0,
                token_provenance = CASE
                  WHEN ? > 0 THEN 'local_telemetry' ELSE 'unavailable'
                END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                *[int(value or 0) for value in usage],
                sum(int(value or 0) for value in usage),
                timestamp,
                session_id,
            ),
        )
        conn.execute(
            """
            DELETE FROM steps
            WHERE session_id = ?
              AND json_extract(raw_attrs_json, '$."reflect.token.source"')
                  = 'estimated_cursor_transcript'
            """,
            (session_id,),
        )
    conn.commit()
    return {"repaired": len(session_ids), "session_ids": session_ids}


def _insert_provenance_step(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    file_path: Path,
    input_tokens: int,
    output_tokens: int,
    timestamp: str,
) -> None:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(seq), -1) + 1
        FROM steps
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    seq = int(row[0] or 0)
    attrs = {
        "gen_ai.client.name": "cursor",
        "reflect.adapter.name": "cursor_native_session",
        "reflect.adapter.source": "cursor_transcript",
        "reflect.token.source": "estimated_cursor_transcript",
        "reflect.token.estimate_algorithm": "len(text)/4",
        "reflect.token.scope": "session",
        "reflect.token.input_tokens": input_tokens,
        "reflect.token.output_tokens": output_tokens,
        "reflect.source.file": str(file_path),
    }
    conn.execute(
        """
        INSERT OR IGNORE INTO steps(
          id, session_id, seq, type, started_at, ended_at, duration_ms,
          status, summary, origin_kind, raw_attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _stable_id("step", "cursor-usage", session_id, file_path),
            session_id,
            seq,
            "token_estimate",
            timestamp,
            timestamp,
            0,
            "ok",
            "cursor.transcript.token_estimate",
            "native_session",
            json.dumps(attrs, sort_keys=True),
            timestamp,
            timestamp,
        ),
    )


def apply_cursor_transcript_usage_estimates(
    conn: sqlite3.Connection,
    file_paths: list[Path] | tuple[Path, ...],
) -> dict[str, int | list[str]]:
    updated = 0
    skipped = 0
    missing = 0
    updated_session_ids: list[str] = []
    timestamp = datetime.now(tz=UTC).isoformat()
    for file_path in file_paths:
        session_id = file_path.stem

        state = _session_token_state(conn, session_id)
        if state is None:
            missing += 1
            continue
        is_cursor, has_tokens = state
        if not is_cursor or has_tokens:
            skipped += 1
            continue

        usage = estimate_cursor_transcript_usage(file_path)
        input_tokens = usage["input_tokens"]
        output_tokens = usage["output_tokens"]

        if input_tokens <= 0 and output_tokens <= 0:
            skipped += 1
            continue

        result = conn.execute(
            """
            UPDATE sessions
            SET input_tokens = ?,
                output_tokens = ?,
                token_provenance = 'estimated_cursor_transcript',
                updated_at = ?
            WHERE id = ?
              AND EXISTS (
                SELECT 1 FROM agents a
                WHERE a.id = sessions.agent_id AND lower(a.name) = 'cursor'
              )
            """,
            (input_tokens, output_tokens, timestamp, session_id),
        )
        if int(result.rowcount or 0) == 0:
            missing += 1
            continue
        _insert_provenance_step(
            conn,
            session_id=session_id,
            file_path=file_path,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            timestamp=timestamp,
        )
        updated += 1
        updated_session_ids.append(session_id)
    conn.commit()
    return {
        "updated": updated,
        "skipped": skipped,
        "missing": missing,
        "session_ids": sorted(updated_session_ids),
    }
