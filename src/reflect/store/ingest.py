from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import chain
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

CHECKPOINT_HASH_WINDOW_BYTES = 64 * 1024
JSONL_SCAN_CHUNK_BYTES = 64 * 1024

IngestionResult = dict[str, int | str]


@dataclass(frozen=True)
class SourceFingerprint:
    size_bytes: int
    modified_ns: int

    @classmethod
    def from_path(cls, path: Path) -> SourceFingerprint:
        stat = path.stat()
        return cls(size_bytes=stat.st_size, modified_ns=stat.st_mtime_ns)


@dataclass(frozen=True)
class SourceCheckpoint:
    size_bytes: int
    modified_ns: int
    processed_offset_bytes: int
    checkpoint_tail_sha256: str


class SourceIngestionState:
    """Persist cheap file fingerprints so repeated report preparation can skip unchanged inputs."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def load(self, source_id: str, source_type: str) -> SourceCheckpoint | None:
        row = self._conn.execute(
            """
            SELECT size_bytes, modified_ns, processed_offset_bytes,
                   COALESCE(checkpoint_tail_sha256, '')
            FROM source_ingestion_state
            WHERE source_id = ? AND source_type = ?
            """,
            (source_id, source_type),
        ).fetchone()
        if row is None:
            return None
        return SourceCheckpoint(
            size_bytes=int(row[0]),
            modified_ns=int(row[1]),
            processed_offset_bytes=int(row[2]),
            checkpoint_tail_sha256=str(row[3] or ""),
        )

    def matches(self, source_id: str, source_type: str, fingerprint: SourceFingerprint) -> bool:
        checkpoint = self.load(source_id, source_type)
        return bool(
            checkpoint
            and checkpoint.size_bytes == fingerprint.size_bytes
            and checkpoint.modified_ns == fingerprint.modified_ns
            and (
                checkpoint.processed_offset_bytes == fingerprint.size_bytes
                or (
                    checkpoint.processed_offset_bytes == 0 and not checkpoint.checkpoint_tail_sha256
                )
            )
        )

    def record(
        self,
        source_id: str,
        source_type: str,
        fingerprint: SourceFingerprint,
        *,
        processed_offset_bytes: int,
        checkpoint_tail_sha256: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO source_ingestion_state(
              source_id, source_type, size_bytes, modified_ns, updated_at,
              processed_offset_bytes, checkpoint_tail_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, NULLIF(?, ''))
            ON CONFLICT(source_id, source_type) DO UPDATE SET
              size_bytes = excluded.size_bytes,
              modified_ns = excluded.modified_ns,
              updated_at = excluded.updated_at,
              processed_offset_bytes = excluded.processed_offset_bytes,
              checkpoint_tail_sha256 = excluded.checkpoint_tail_sha256
            """,
            (
                source_id,
                source_type,
                fingerprint.size_bytes,
                fingerprint.modified_ns,
                datetime.now(tz=UTC).isoformat(),
                processed_offset_bytes,
                checkpoint_tail_sha256,
            ),
        )


def _checkpoint_tail_sha256(file_path: Path, offset: int) -> str:
    if offset <= 0:
        return ""
    start = max(0, offset - CHECKPOINT_HASH_WINDOW_BYTES)
    with file_path.open("rb") as handle:
        handle.seek(start)
        content = handle.read(offset - start)
    return hashlib.sha256(content).hexdigest()


def _complete_jsonl_offset(file_path: Path, size_bytes: int) -> int:
    if size_bytes <= 0:
        return 0
    with file_path.open("rb") as handle:
        handle.seek(size_bytes - 1)
        if handle.read(1) == b"\n":
            return size_bytes
        scan_end = size_bytes
        record_start = 0
        while scan_end > 0:
            scan_start = max(0, scan_end - JSONL_SCAN_CHUNK_BYTES)
            handle.seek(scan_start)
            chunk = handle.read(scan_end - scan_start)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                record_start = scan_start + newline + 1
                break
            scan_end = scan_start
        handle.seek(record_start)
        final_record = handle.read(size_bytes - record_start).strip()
    if not final_record:
        return size_bytes
    try:
        payload = json.loads(final_record)
    except (TypeError, ValueError):
        return record_start
    return size_bytes if isinstance(payload, dict) else record_start


def _append_start_offset(
    file_path: Path,
    fingerprint: SourceFingerprint,
    checkpoint: SourceCheckpoint | None,
) -> int | None:
    if (
        checkpoint is None
        or checkpoint.processed_offset_bytes <= 0
        or checkpoint.processed_offset_bytes >= fingerprint.size_bytes
        or not checkpoint.checkpoint_tail_sha256
    ):
        return None
    current_tail = _checkpoint_tail_sha256(file_path, checkpoint.processed_offset_bytes)
    if current_tail != checkpoint.checkpoint_tail_sha256:
        return None
    return checkpoint.processed_offset_bytes


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
        or attrs.get("conversation.id")
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
    runtime_internal = attrs.get("reflect.telemetry.classification") == "runtime_internal"
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
            None if runtime_internal else _session_id(attrs),
            observed_at,
            received_at,
            origin_kind,
            json.dumps(attrs, sort_keys=True),
            json.dumps(span.get("body", {}) or {}, sort_keys=True),
            "ignored" if runtime_internal else "pending",
            None,
            content_hash,
            created_at,
        ),
    )
    return cursor.rowcount != 0


def _session_owned_by_other_source(
    db_conn: sqlite3.Connection,
    *,
    span: dict,
    source: str,
    source_type: str,
) -> bool:
    session_id = _session_id(span.get("attributes", {}) or {})
    if not session_id:
        return False
    row = db_conn.execute(
        "SELECT source_kind, source_ref FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if row:
        return (str(row[0] or ""), str(row[1] or "")) != (source_type, source)
    raw_owner = db_conn.execute(
        """
        SELECT source_type, source_id
        FROM raw_events
        WHERE session_id = ?
        ORDER BY created_at, id
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    return bool(
        raw_owner
        and (str(raw_owner[0] or ""), str(raw_owner[1] or "")) != (source_type, source)
    )


def _ingest_spans(
    db_conn,
    *,
    spans,
    source: str,
    source_type: str,
    respect_session_ownership: bool = False,
) -> dict[str, int]:
    inserted = 0
    skipped = 0
    created_at = datetime.now(tz=UTC).isoformat()
    for span in spans:
        if respect_session_ownership and _session_owned_by_other_source(
            db_conn,
            span=span,
            source=source,
            source_type=source_type,
        ):
            skipped += 1
            continue
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
    spans_factory: Callable[[int, int | None], Iterable[dict]],
    skip_unchanged: bool,
    append_only_jsonl: bool = False,
    skip_existing_session: bool = False,
    respect_session_ownership: bool = False,
) -> IngestionResult:
    fingerprint = SourceFingerprint.from_path(file_path)
    state = SourceIngestionState(db_conn)
    if skip_unchanged and state.matches(source, source_type, fingerprint):
        checkpoint = state.load(source, source_type)
        if (
            append_only_jsonl
            and checkpoint is not None
            and checkpoint.processed_offset_bytes != fingerprint.size_bytes
        ):
            processed_offset = _complete_jsonl_offset(file_path, fingerprint.size_bytes)
            state.record(
                source,
                source_type,
                fingerprint,
                processed_offset_bytes=processed_offset,
                checkpoint_tail_sha256=_checkpoint_tail_sha256(
                    file_path,
                    processed_offset,
                ),
            )
            db_conn.commit()
        else:
            processed_offset = fingerprint.size_bytes
        result = {"inserted": 0, "skipped": 0, "unchanged": 1}
        if append_only_jsonl:
            result.update(
                {
                    "mode": "unchanged",
                    "bytes_read": 0,
                    "processed_offset_bytes": processed_offset,
                    "pending_bytes": fingerprint.size_bytes - processed_offset,
                }
            )
        return result

    start_offset = 0
    end_offset = (
        _complete_jsonl_offset(file_path, fingerprint.size_bytes)
        if append_only_jsonl
        else None
    )
    mode = "full"
    checkpoint = state.load(source, source_type)
    if skip_unchanged and append_only_jsonl:
        append_start = _append_start_offset(file_path, fingerprint, checkpoint)
        if append_start is not None:
            start_offset = append_start
            mode = "append"
        assert end_offset is not None
        if end_offset < start_offset:
            start_offset = 0
            mode = "full"
    if end_offset is not None and end_offset == start_offset:
        result = {"inserted": 0, "skipped": 0}
        if skip_unchanged:
            result.update(
                {
                    "unchanged": 0,
                    "mode": mode,
                    "bytes_read": 0,
                    "processed_offset_bytes": start_offset,
                    "pending_bytes": fingerprint.size_bytes - start_offset,
                }
            )
        return result

    spans = iter(spans_factory(start_offset, end_offset))
    if skip_existing_session:
        first_span = next(spans, None)
        if first_span is not None:
            if _session_owned_by_other_source(
                db_conn,
                span=first_span,
                source=source,
                source_type=source_type,
            ):
                processed_offset = (
                    end_offset if end_offset is not None else fingerprint.size_bytes
                )
                state.record(
                    source,
                    source_type,
                    fingerprint,
                    processed_offset_bytes=processed_offset,
                    checkpoint_tail_sha256=(
                        _checkpoint_tail_sha256(file_path, processed_offset)
                        if append_only_jsonl
                        else ""
                    ),
                )
                db_conn.commit()
                result = {"inserted": 0, "skipped": 0}
                if skip_unchanged:
                    result["unchanged"] = 1
                return result
            spans = chain((first_span,), spans)
    result = _ingest_spans(
        db_conn,
        spans=spans,
        source=source,
        source_type=source_type,
        respect_session_ownership=respect_session_ownership,
    )
    processed_offset = end_offset if end_offset is not None else fingerprint.size_bytes
    state.record(
        source,
        source_type,
        fingerprint,
        processed_offset_bytes=processed_offset,
        checkpoint_tail_sha256=(
            _checkpoint_tail_sha256(file_path, processed_offset) if append_only_jsonl else ""
        ),
    )
    db_conn.commit()
    if skip_unchanged:
        result["unchanged"] = 0
        if append_only_jsonl:
            result.update(
                {
                    "mode": mode,
                    "bytes_read": processed_offset - start_offset,
                    "processed_offset_bytes": processed_offset,
                    "pending_bytes": fingerprint.size_bytes - processed_offset,
                }
            )
    return result


def ingest_otlp_traces_file(
    db_conn,
    *,
    file_path: Path,
    source_id: str | None = None,
    skip_unchanged: bool = False,
) -> IngestionResult:
    source = source_id or str(file_path)
    return _ingest_file_spans(
        db_conn,
        file_path=file_path,
        source=source,
        source_type="otlp_traces_json",
        spans_factory=lambda start, end: _load_otlp_traces(
            file_path,
            start_offset=start,
            end_offset=end,
        ),
        skip_unchanged=skip_unchanged,
        append_only_jsonl=True,
    )


def ingest_otlp_logs_file(
    db_conn,
    *,
    file_path: Path,
    source_id: str | None = None,
    skip_unchanged: bool = False,
) -> IngestionResult:
    source = source_id or str(file_path)

    def spans(start_offset: int, end_offset: int | None):
        records = list(
            _load_otlp_logs(
                file_path,
                start_offset=start_offset,
                end_offset=end_offset,
            )
        )
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
        append_only_jsonl=True,
        respect_session_ownership=True,
    )


def ingest_local_spans_file(
    db_conn,
    *,
    file_path: Path,
    source_id: str | None = None,
    skip_unchanged: bool = False,
) -> IngestionResult:
    source = source_id or str(file_path)
    return _ingest_file_spans(
        db_conn,
        file_path=file_path,
        source=source,
        source_type="local_spans_jsonl",
        spans_factory=lambda _start, _end: _load_json_lines(file_path),
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
) -> IngestionResult:
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
        spans_factory=lambda _start, _end: spans,
        skip_unchanged=skip_unchanged,
        skip_existing_session=skip_existing_sessions,
    )
