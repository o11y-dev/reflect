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


def _session_has_tokens(conn: sqlite3.Connection, session_id: str) -> bool:
    row = conn.execute(
        """
        SELECT input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, reasoning_tokens
        FROM sessions
        WHERE id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return False
    return any(int(value or 0) > 0 for value in row)


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
) -> dict[str, int]:
    updated = 0
    skipped = 0
    missing = 0
    timestamp = datetime.now(tz=UTC).isoformat()
    for file_path in file_paths:
        session_id = file_path.stem

        # OPTIMIZATION: Check if session already has tokens before reading file
        if _session_has_tokens(conn, session_id):
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
                updated_at = ?
            WHERE id = ?
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
    conn.commit()
    return {"updated": updated, "skipped": skipped, "missing": missing}
