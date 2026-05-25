from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from reflect.parsing import (
    _iter_claude_session_spans,
    _iter_codex_log_spans,
    _iter_copilot_session_spans,
    _iter_cursor_session_spans,
    _iter_gemini_log_spans,
    _iter_gemini_session_spans,
    _load_json_lines,
    _load_otlp_logs,
    _load_otlp_traces,
)


def _iso8601_from_ns(value_ns: int) -> str:
    if value_ns <= 0:
        return datetime.now(tz=UTC).isoformat()
    return datetime.fromtimestamp(value_ns / 1_000_000_000, tz=UTC).isoformat()


def _event_hash(span: dict) -> str:
    payload = {
        "traceId": span.get("traceId", ""),
        "spanId": span.get("spanId", ""),
        "parentSpanId": span.get("parentSpanId", ""),
        "name": span.get("name", ""),
        "start_time_ns": span.get("start_time_ns", 0),
        "end_time_ns": span.get("end_time_ns", 0),
        "attributes": span.get("attributes", {}),
    }
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def _session_id(attrs: dict) -> str | None:
    return (
        attrs.get("session.id")
        or attrs.get("gen_ai.session.id")
        or attrs.get("gen_ai.client.session_id")
        or attrs.get("session_id")
        or None
    )


def _insert_raw_span(
    db_conn,
    *,
    span: dict,
    source: str,
    source_type: str,
    created_at: str,
) -> bool:
    attrs = span.get("attributes", {}) or {}
    observed_at = _iso8601_from_ns(int(span.get("start_time_ns", 0) or 0))
    received_at = _iso8601_from_ns(int(span.get("end_time_ns", 0) or 0))
    content_hash = _event_hash(span)
    event_id = hashlib.sha1(f"{source}:{content_hash}".encode()).hexdigest()

    cursor = db_conn.execute(
        """
        INSERT OR IGNORE INTO raw_events(
          id, source_id, source_type, event_type, trace_id, span_id, parent_span_id,
          session_id, observed_at, received_at, attrs_json, body_json,
          normalized_status, normalization_error, content_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            source,
            source_type,
            span.get("name", "unknown"),
            span.get("traceId", ""),
            span.get("spanId", ""),
            span.get("parentSpanId", ""),
            _session_id(attrs),
            observed_at,
            received_at,
            json.dumps(attrs, sort_keys=True),
            json.dumps(span.get("body", {}) or {}, sort_keys=True),
            "pending",
            None,
            content_hash,
            created_at,
        ),
    )
    return cursor.rowcount != 0


def _ingest_spans(
    db_conn,
    *,
    spans,
    source: str,
    source_type: str,
) -> dict[str, int]:
    inserted = 0
    skipped = 0
    created_at = datetime.now(tz=UTC).isoformat()
    for span in spans:
        if _insert_raw_span(
            db_conn,
            span=span,
            source=source,
            source_type=source_type,
            created_at=created_at,
        ):
            inserted += 1
        else:
            skipped += 1

    db_conn.commit()
    return {"inserted": inserted, "skipped": skipped}


def ingest_otlp_traces_file(db_conn, *, file_path: Path, source_id: str | None = None) -> dict[str, int]:
    source = source_id or str(file_path)
    return _ingest_spans(
        db_conn,
        spans=_load_otlp_traces(file_path),
        source=source,
        source_type="otlp_traces_json",
    )


def ingest_otlp_logs_file(db_conn, *, file_path: Path, source_id: str | None = None) -> dict[str, int]:
    source = source_id or str(file_path)
    records = list(_load_otlp_logs(file_path))

    def spans():
        yield from _iter_codex_log_spans(records)
        yield from _iter_gemini_log_spans(records)

    return _ingest_spans(
        db_conn,
        spans=spans(),
        source=source,
        source_type="otlp_logs_json",
    )


def ingest_local_spans_file(db_conn, *, file_path: Path, source_id: str | None = None) -> dict[str, int]:
    source = source_id or str(file_path)
    return _ingest_spans(
        db_conn,
        spans=_load_json_lines(file_path),
        source=source,
        source_type="local_spans_jsonl",
    )


def ingest_native_session_file(
    db_conn,
    *,
    file_path: Path,
    agent: str,
    source_id: str | None = None,
    skip_existing_sessions: bool = False,
) -> dict[str, int]:
    source = source_id or f"native_session:{agent}:{file_path}"
    if agent == "copilot":
        spans = _iter_copilot_session_spans(file_path)
    elif agent == "cursor":
        spans = _iter_cursor_session_spans(file_path)
    elif agent == "claude":
        spans = _iter_claude_session_spans(file_path)
    elif agent == "gemini":
        spans = _iter_gemini_session_spans(file_path)
    else:
        spans = ()
    if skip_existing_sessions:
        spans = list(spans)
        session_ids = sorted({
            str((span.get("attributes") or {}).get("session.id") or "")
            for span in spans
            if (span.get("attributes") or {}).get("session.id")
        })
        if session_ids:
            placeholders = ", ".join("?" for _ in session_ids)
            existing = db_conn.execute(
                f"SELECT 1 FROM raw_events WHERE session_id IN ({placeholders}) LIMIT 1",
                session_ids,
            ).fetchone()
            if existing:
                return {"inserted": 0, "skipped": len(spans)}
    return _ingest_spans(
        db_conn,
        spans=spans,
        source=source,
        source_type="native_session",
    )
