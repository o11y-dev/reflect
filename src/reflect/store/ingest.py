from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from reflect.parsing import (
    _iter_claude_log_spans,
    _iter_claude_session_spans,
    _iter_codex_log_spans,
    _iter_codex_session_spans,
    _iter_copilot_session_spans,
    _iter_cursor_session_spans,
    _iter_gemini_log_spans,
    _iter_gemini_session_spans,
    _load_json_lines,
    _load_otlp_logs,
    _load_otlp_traces,
)
from reflect.store.provenance import apply_origin_kind, classify_origin_kind, stable_hash_attrs


@dataclass(frozen=True)
class SourceFingerprint:
    size_bytes: int
    modified_ns: int

    @classmethod
    def from_path(cls, path: Path) -> SourceFingerprint:
        stat = path.stat()
        return cls(size_bytes=stat.st_size, modified_ns=stat.st_mtime_ns)


class SourceIngestionState:
    """Persist cheap file fingerprints so repeated report preparation can skip unchanged inputs."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def matches(self, source_id: str, source_type: str, fingerprint: SourceFingerprint) -> bool:
        row = self._conn.execute(
            """
            SELECT size_bytes, modified_ns
            FROM source_ingestion_state
            WHERE source_id = ? AND source_type = ?
            """,
            (source_id, source_type),
        ).fetchone()
        return bool(
            row
            and int(row[0]) == fingerprint.size_bytes
            and int(row[1]) == fingerprint.modified_ns
        )

    def record(self, source_id: str, source_type: str, fingerprint: SourceFingerprint) -> None:
        self._conn.execute(
            """
            INSERT INTO source_ingestion_state(
              source_id, source_type, size_bytes, modified_ns, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_id, source_type) DO UPDATE SET
              size_bytes = excluded.size_bytes,
              modified_ns = excluded.modified_ns,
              updated_at = excluded.updated_at
            """,
            (
                source_id,
                source_type,
                fingerprint.size_bytes,
                fingerprint.modified_ns,
                datetime.now(tz=UTC).isoformat(),
            ),
        )


def _iso8601_from_ns(value_ns: int) -> str:
    if value_ns <= 0:
        return datetime.now(tz=UTC).isoformat()
    return datetime.fromtimestamp(value_ns / 1_000_000_000, tz=UTC).isoformat()


def _event_hash(span: dict) -> str:
    attrs = span.get("attributes", {}) or {}
    payload = {
        "traceId": span.get("traceId", ""),
        "spanId": span.get("spanId", ""),
        "parentSpanId": span.get("parentSpanId", ""),
        "name": span.get("name", ""),
        "start_time_ns": span.get("start_time_ns", 0),
        "end_time_ns": span.get("end_time_ns", 0),
        "attributes": stable_hash_attrs(attrs),
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
    origin_kind = classify_origin_kind(source_type, attrs)
    attrs = apply_origin_kind(attrs, origin_kind)
    observed_at = _iso8601_from_ns(int(span.get("start_time_ns", 0) or 0))
    received_at = _iso8601_from_ns(int(span.get("end_time_ns", 0) or 0))
    content_hash = _event_hash({**span, "attributes": attrs})
    event_id = hashlib.sha1(f"{source}:{content_hash}".encode()).hexdigest()

    cursor = db_conn.execute(
        """
        INSERT OR IGNORE INTO raw_events(
          id, source_id, source_type, event_type, trace_id, span_id, parent_span_id,
          session_id, observed_at, received_at, origin_kind, attrs_json, body_json,
          normalized_status, normalization_error, content_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            origin_kind,
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


def _ingest_file_spans(
    db_conn: sqlite3.Connection,
    *,
    file_path: Path,
    source: str,
    source_type: str,
    spans_factory: Callable[[], Iterable[dict]],
    skip_unchanged: bool,
) -> dict[str, int]:
    fingerprint = SourceFingerprint.from_path(file_path)
    state = SourceIngestionState(db_conn)
    if skip_unchanged and state.matches(source, source_type, fingerprint):
        return {"inserted": 0, "skipped": 0, "unchanged": 1}
    result = _ingest_spans(
        db_conn,
        spans=spans_factory(),
        source=source,
        source_type=source_type,
    )
    state.record(source, source_type, fingerprint)
    db_conn.commit()
    if skip_unchanged:
        result["unchanged"] = 0
    return result


def ingest_otlp_traces_file(
    db_conn,
    *,
    file_path: Path,
    source_id: str | None = None,
    skip_unchanged: bool = False,
) -> dict[str, int]:
    source = source_id or str(file_path)
    return _ingest_file_spans(
        db_conn,
        file_path=file_path,
        source=source,
        source_type="otlp_traces_json",
        spans_factory=lambda: _load_otlp_traces(file_path),
        skip_unchanged=skip_unchanged,
    )


def ingest_otlp_logs_file(
    db_conn,
    *,
    file_path: Path,
    source_id: str | None = None,
    skip_unchanged: bool = False,
) -> dict[str, int]:
    source = source_id or str(file_path)

    def spans():
        records = list(_load_otlp_logs(file_path))
        yield from _iter_claude_log_spans(records)
        yield from _iter_codex_log_spans(records)
        yield from _iter_gemini_log_spans(records)

    return _ingest_file_spans(
        db_conn,
        file_path=file_path,
        source=source,
        source_type="otlp_logs_json",
        spans_factory=spans,
        skip_unchanged=skip_unchanged,
    )


def ingest_local_spans_file(
    db_conn,
    *,
    file_path: Path,
    source_id: str | None = None,
    skip_unchanged: bool = False,
) -> dict[str, int]:
    source = source_id or str(file_path)
    return _ingest_file_spans(
        db_conn,
        file_path=file_path,
        source=source,
        source_type="local_spans_jsonl",
        spans_factory=lambda: _load_json_lines(file_path),
        skip_unchanged=skip_unchanged,
    )


def ingest_native_session_file(
    db_conn,
    *,
    file_path: Path,
    agent: str,
    source_id: str | None = None,
    skip_existing_sessions: bool = False,
    skip_unchanged: bool = False,
) -> dict[str, int]:
    source = source_id or f"native_session:{agent}:{file_path}"
    if agent == "codex":
        spans = _iter_codex_session_spans(file_path)
    elif agent == "copilot":
        spans = _iter_copilot_session_spans(file_path)
    elif agent == "cursor":
        spans = _iter_cursor_session_spans(file_path)
    elif agent == "claude":
        spans = _iter_claude_session_spans(file_path)
    elif agent == "gemini":
        spans = _iter_gemini_session_spans(file_path)
    else:
        spans = ()
    return _ingest_file_spans(
        db_conn,
        file_path=file_path,
        source=source,
        source_type="native_session",
        spans_factory=lambda: spans,
        skip_unchanged=skip_unchanged,
    )
