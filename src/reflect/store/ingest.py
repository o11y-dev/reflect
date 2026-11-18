from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from itertools import chain, islice
from pathlib import Path

from reflect.opencode_store import OpenCodeSessionStore
from reflect.parsing import (
    _iter_claude_log_spans,
    _iter_claude_session_spans,
    _iter_codex_context_spans,
    _iter_codex_log_spans,
    _iter_codex_session_spans,
    _iter_copilot_session_spans,
    _iter_cursor_session_spans,
    _iter_gemini_log_spans,
    _iter_gemini_session_spans,
    _iter_opencode_records_spans,
    _load_json_lines,
    _load_otlp_logs,
    _load_otlp_traces,
)
from reflect.raw_segments import closed_segment_paths, readable_segment_paths
from reflect.store.provenance import (
    apply_origin_kind,
    classify_origin_kind,
    stable_hash_attrs,
    telemetry_session_id,
    unassigned_codex_classification,
)

CHECKPOINT_HASH_WINDOW_BYTES = 64 * 1024
JSONL_SCAN_CHUNK_BYTES = 64 * 1024
SOURCE_DECODER_VERSION = 2
NATIVE_DECODER_VERSION = 5
logger = logging.getLogger(__name__)

IngestionResult = dict[str, int | str]


class AppendOnlyReplayPolicy(StrEnum):
    """Control whether an interactive read may replay a replaced JSONL source."""

    REPLAY = "replay"
    DEFER = "defer"


@dataclass(frozen=True)
class SourceFingerprint:
    size_bytes: int
    modified_ns: int

    @classmethod
    def from_path(cls, path: Path) -> SourceFingerprint:
        stat = path.stat()
        size_bytes = stat.st_size
        modified_ns = stat.st_mtime_ns
        if path.suffix in {".db", ".sqlite", ".sqlite3"}:
            wal_path = Path(f"{path}-wal")
            try:
                wal_stat = wal_path.stat()
            except OSError:
                pass
            else:
                size_bytes += wal_stat.st_size
                modified_ns = max(modified_ns, wal_stat.st_mtime_ns)
        return cls(size_bytes=size_bytes, modified_ns=modified_ns)


@dataclass(frozen=True)
class SourceCheckpoint:
    size_bytes: int
    modified_ns: int
    processed_offset_bytes: int
    checkpoint_tail_sha256: str
    decoder_version: int
    record_cursor_time: int | None
    record_cursor_id: str


@dataclass(frozen=True)
class SegmentBatchIngestion:
    source_type: str
    active_path: Path
    source_results: dict[str, IngestionResult]
    processed_closed_paths: tuple[Path, ...]

    @property
    def inserted(self) -> int:
        return sum(int(item.get("inserted", 0)) for item in self.source_results.values())

    @property
    def skipped(self) -> int:
        return sum(int(item.get("skipped", 0)) for item in self.source_results.values())

    def as_dict(self) -> dict[str, object]:
        modes = {
            str(item.get("mode"))
            for item in self.source_results.values()
            if item.get("mode")
        }
        mode = next(
            (
                candidate
                for candidate in ("replay_deferred", "append", "full", "unchanged")
                if candidate in modes
            ),
            "unchanged",
        )
        return {
            "source_type": self.source_type,
            "active_path": str(self.active_path),
            "files": len(self.source_results),
            "inserted": self.inserted,
            "skipped": self.skipped,
            "unchanged": sum(
                int(item.get("unchanged", 0)) for item in self.source_results.values()
            ),
            "mode": mode,
            "bytes_read": sum(
                int(item.get("bytes_read", 0)) for item in self.source_results.values()
            ),
            "pending_bytes": sum(
                int(item.get("pending_bytes", 0)) for item in self.source_results.values()
            ),
            "replay_required": sum(
                int(item.get("replay_required", 0))
                for item in self.source_results.values()
            ),
            "sources": self.source_results,
            "processed_closed_segments": len(self.processed_closed_paths),
        }


class SourceIngestionState:
    """Persist cheap file fingerprints so repeated report preparation can skip unchanged inputs."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def load(self, source_id: str, source_type: str) -> SourceCheckpoint | None:
        row = self._conn.execute(
            """
            SELECT size_bytes, modified_ns, processed_offset_bytes,
                   COALESCE(checkpoint_tail_sha256, ''), decoder_version,
                   record_cursor_time, COALESCE(record_cursor_id, '')
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
            decoder_version=int(row[4] or 0),
            record_cursor_time=int(row[5]) if row[5] is not None else None,
            record_cursor_id=str(row[6] or ""),
        )

    def matches(self, source_id: str, source_type: str, fingerprint: SourceFingerprint) -> bool:
        checkpoint = self.load(source_id, source_type)
        return bool(
            checkpoint
            and checkpoint.decoder_version == (NATIVE_DECODER_VERSION if source_type == "native_session" else SOURCE_DECODER_VERSION)
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
        record_cursor_time: int | None = None,
        record_cursor_id: str = "",
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO source_ingestion_state(
              source_id, source_type, size_bytes, modified_ns, updated_at,
              processed_offset_bytes, checkpoint_tail_sha256, decoder_version,
              record_cursor_time, record_cursor_id,
              normalized_at, raw_deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULLIF(?, ''), ?, ?, NULLIF(?, ''), NULL, NULL)
            ON CONFLICT(source_id, source_type) DO UPDATE SET
              size_bytes = excluded.size_bytes,
              modified_ns = excluded.modified_ns,
              updated_at = excluded.updated_at,
              processed_offset_bytes = excluded.processed_offset_bytes,
              checkpoint_tail_sha256 = excluded.checkpoint_tail_sha256,
              decoder_version = excluded.decoder_version,
              record_cursor_time = COALESCE(
                excluded.record_cursor_time,
                source_ingestion_state.record_cursor_time
              ),
              record_cursor_id = COALESCE(
                excluded.record_cursor_id,
                source_ingestion_state.record_cursor_id
              ),
              normalized_at = NULL,
              raw_deleted_at = NULL
            """,
            (
                source_id,
                source_type,
                fingerprint.size_bytes,
                fingerprint.modified_ns,
                datetime.now(tz=UTC).isoformat(),
                processed_offset_bytes,
                checkpoint_tail_sha256,
                NATIVE_DECODER_VERSION if source_type == "native_session" else SOURCE_DECODER_VERSION,
                record_cursor_time,
                record_cursor_id,
            ),
        )

    def mark_normalized(self, sources: Iterable[Path], *, raw_deleted: bool) -> None:
        now = datetime.now(tz=UTC).isoformat()
        self._conn.executemany(
            """
            UPDATE source_ingestion_state
            SET normalized_at = ?,
                raw_deleted_at = CASE WHEN ? THEN ? ELSE raw_deleted_at END,
                updated_at = ?
            WHERE source_id = ?
            """,
            [
                (now, int(raw_deleted), now, now, str(source))
                for source in sources
            ],
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
        or checkpoint.decoder_version != SOURCE_DECODER_VERSION
        or checkpoint.processed_offset_bytes <= 0
        or checkpoint.processed_offset_bytes > fingerprint.size_bytes
        or not checkpoint.checkpoint_tail_sha256
    ):
        return None
    current_tail = _checkpoint_tail_sha256(file_path, checkpoint.processed_offset_bytes)
    if current_tail != checkpoint.checkpoint_tail_sha256:
        return None
    return checkpoint.processed_offset_bytes


def _iso8601_from_ns(value_ns: int) -> str:
    if value_ns <= 0:
        return datetime(1970, 1, 1, tzinfo=UTC).isoformat()
    return datetime.fromtimestamp(value_ns / 1_000_000_000, tz=UTC).isoformat()


def _source_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    return parsed if parsed.year >= 2000 else None


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


@dataclass(frozen=True)
class _PreparedRawSpan:
    attrs: dict
    origin_kind: str
    classification: str | None
    content_hash: str
    event_id: str


def _prepare_raw_span(span: dict, source: str, source_type: str) -> _PreparedRawSpan:
    attrs = span.get("attributes", {}) or {}
    origin_kind = classify_origin_kind(source_type, attrs)
    attrs = apply_origin_kind(attrs, origin_kind)
    classification = unassigned_codex_classification(attrs)
    if classification:
        attrs["reflect.telemetry.classification"] = classification
    content_hash = _event_hash({**span, "attributes": attrs})
    if (
        (source_type == "otlp_traces_json" or attrs.get("reflect.usage.codex_log_enrichment"))
        and span.get("traceId")
        and span.get("spanId")
    ):
        # Segment rotation changes the file path, not the identity of an OTLP event.
        event_id = hashlib.sha1(
            f"otlp-span:{span['traceId']}:{span['spanId']}:{content_hash}".encode()
        ).hexdigest()
    else:
        event_id = hashlib.sha1(f"{source}:{content_hash}".encode()).hexdigest()
    return _PreparedRawSpan(attrs, origin_kind, classification, content_hash, event_id)


def _insert_raw_span(
    db_conn,
    *,
    span: dict,
    source: str,
    source_type: str,
    created_at: str,
) -> bool:
    prepared = _prepare_raw_span(span, source, source_type)
    attrs = prepared.attrs
    runtime_internal = attrs.get("reflect.telemetry.classification") == "runtime_internal"
    if runtime_internal:
        return False
    observed_at = _iso8601_from_ns(int(span.get("start_time_ns", 0) or 0))
    received_at = _iso8601_from_ns(int(span.get("end_time_ns", 0) or 0))
    session_id = telemetry_session_id(attrs)
    if session_id:
        tombstone = db_conn.execute(
            """
            SELECT last_observed_at
            FROM pruned_sessions
            WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        if tombstone is not None:
            last_observed_at = _source_timestamp(str(tombstone[0] or ""))
            observed_timestamp = _source_timestamp(observed_at)
            if observed_timestamp is None or (
                last_observed_at is not None
                and observed_timestamp <= last_observed_at
            ):
                return False
            db_conn.execute(
                """
                UPDATE pruned_sessions
                SET resurrected_at = ?, newer_observed_at = ?
                WHERE id = ?
                """,
                (created_at, observed_at, session_id),
            )
    event_id = prepared.event_id

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
            session_id,
            observed_at,
            received_at,
            prepared.origin_kind,
            json.dumps(attrs, sort_keys=True),
            json.dumps(span.get("body", {}) or {}, sort_keys=True),
            "ignored" if prepared.classification else "pending",
            None,
            prepared.content_hash,
            created_at,
        ),
    )
    if not cursor.rowcount and source_type == "native_session":
        cursor = db_conn.execute(
            "UPDATE raw_events SET normalized_status = 'pending', normalization_error = NULL "
            "WHERE id = ? AND normalized_status = 'superseded'",
            (event_id,),
        )
    return cursor.rowcount != 0


def _session_owned_by_other_source(
    db_conn: sqlite3.Connection,
    *,
    span: dict,
    source: str,
    source_type: str,
) -> bool:
    session_id = telemetry_session_id(span.get("attributes", {}) or {})
    if not session_id:
        return False
    row = db_conn.execute(
        """
        SELECT source_kind, source_ref, input_tokens, output_tokens,
               cache_creation_tokens, cache_read_tokens, reasoning_tokens
        FROM sessions
        WHERE id = ?
        """,
        (session_id,),
    ).fetchone()
    if row:
        existing_source = (str(row[0] or ""), str(row[1] or ""))
        if existing_source == (source_type, source):
            return False
        has_token_usage = any(int(value or 0) > 0 for value in row[2:])
        return not (
            source_type == "native_session"
            and existing_source[0] != "native_session"
            and not has_token_usage
        )
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
    if source_type == "native_session" and raw_owner and str(raw_owner[0]) != "native_session":
        has_usage = db_conn.execute(
            """
            SELECT 1 FROM raw_events WHERE session_id = ? AND source_id <> ?
            AND normalized_status <> 'superseded' AND (
              json_extract(attrs_json, '$."gen_ai.usage.input_tokens"') IS NOT NULL OR
              json_extract(attrs_json, '$."gen_ai.usage.output_tokens"') IS NOT NULL OR
              json_extract(attrs_json, '$."gen_ai.usage.cache_read.input_tokens"') IS NOT NULL
            ) LIMIT 1
            """, (session_id, source),
        ).fetchone()
        if not has_usage:
            return False
    return bool(
        raw_owner
        and (str(raw_owner[0] or ""), str(raw_owner[1] or "")) != (source_type, source)
    )


def _codex_trace_usage_enrichment(conn: sqlite3.Connection, span: dict) -> dict | None:
    """Let native response logs supply accounting to a trace-only Codex session."""
    attrs = span["attributes"]
    if (
        attrs.get("gen_ai.client.name") != "codex"
        or attrs.get("reflect.native.event_name") != "codex.sse_event"
        or attrs.get("reflect.usage.grain") != "request"
        or any(attrs.get(key) is None for key in (
            "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.cache_read.input_tokens",
        ))
    ):
        return None
    session_id = telemetry_session_id(attrs)
    session = conn.execute(
        """SELECT source_kind, max(input_tokens, output_tokens, cache_creation_tokens,
                  cache_read_tokens, reasoning_tokens) > 0
           FROM sessions WHERE id = ?""",
        (session_id,),
    ).fetchone()
    source_kind, has_usage = session if session is not None else (None, False)
    if session is None:
        raw_owner = conn.execute(
            "SELECT source_type FROM raw_events WHERE session_id = ? ORDER BY created_at, id LIMIT 1",
            (session_id,),
        ).fetchone()
        source_kind = raw_owner[0] if raw_owner else None
    if source_kind != "otlp_traces_json":
        return None

    usage_sources = {row[0] for row in conn.execute(
        """SELECT DISTINCT source_type FROM raw_events
           WHERE session_id = ? AND normalized_status NOT IN ('superseded', 'ignored', 'failed')
             AND (json_extract(attrs_json, '$."gen_ai.usage.input_tokens"') IS NOT NULL
               OR json_extract(attrs_json, '$."gen_ai.usage.output_tokens"') IS NOT NULL
               OR json_extract(attrs_json, '$."gen_ai.usage.cache_read.input_tokens"') IS NOT NULL
               OR json_extract(attrs_json, '$."gen_ai.usage.cache_creation.input_tokens"') IS NOT NULL
               OR json_extract(attrs_json, '$."gen_ai.usage.cache_write.input_tokens"') IS NOT NULL)""",
        (session_id,),
    )}
    if usage_sources - {"otlp_logs_json"} or (has_usage and not usage_sources):
        return None
    # The parsed span ID includes its position in the read batch. Use the
    # completion timestamp for a stable ID across replay and segment rotation.
    if not span.get("start_time_ns"):
        return None
    stable_id = hashlib.sha256(f"codex-log:{session_id}:{span['start_time_ns']}".encode()).hexdigest()[:16]
    return {**span, "spanId": stable_id, "attributes": {**attrs, "reflect.usage.codex_log_enrichment": True}}


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
    spans = iter(spans)
    while batch := list(islice(spans, 500)):
        # Parse outside the transaction. Checkpoints advance only after the file
        # completes; stable event IDs make replay of committed batches safe.
        try:
            if not db_conn.in_transaction:
                db_conn.execute("BEGIN IMMEDIATE")
            for span in batch:
                if respect_session_ownership and _session_owned_by_other_source(
                    db_conn, span=span, source=source, source_type=source_type,
                ):
                    if source_type == "native_session":
                        span = {**span, "attributes": {**(span.get("attributes") or {}), "reflect.usage.enrichment_only": True}}
                    elif source_type == "otlp_logs_json" and (enrichment := _codex_trace_usage_enrichment(db_conn, span)):
                        span = enrichment
                    else:
                        skipped += 1
                        continue
                if _insert_raw_span(
                    db_conn, span=span, source=source, source_type=source_type,
                    created_at=created_at,
                ):
                    inserted += 1
                else:
                    skipped += 1
            db_conn.commit()
        except BaseException:
            db_conn.rollback()
            raise
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
    replay_policy: AppendOnlyReplayPolicy = AppendOnlyReplayPolicy.REPLAY,
    skip_existing_session: bool = False,
    respect_session_ownership: bool = False,
    decoded_fingerprint: SourceFingerprint | None = None,
) -> IngestionResult:
    # Already-decoded native projections must checkpoint the version they read,
    # not a later append that was never included in the projection.
    fingerprint = decoded_fingerprint or SourceFingerprint.from_path(file_path)
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
        elif (
            checkpoint is not None
            and checkpoint.processed_offset_bytes > 0
            and replay_policy is AppendOnlyReplayPolicy.DEFER
        ):
            assert end_offset is not None
            return {
                "inserted": 0,
                "skipped": 0,
                "unchanged": 0,
                "mode": "replay_deferred",
                "bytes_read": 0,
                "processed_offset_bytes": 0,
                "pending_bytes": end_offset,
                "replay_required": 1,
            }
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
    replay_policy: AppendOnlyReplayPolicy = AppendOnlyReplayPolicy.REPLAY,
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
        replay_policy=replay_policy,
    )


def ingest_otlp_logs_file(
    db_conn,
    *,
    file_path: Path,
    source_id: str | None = None,
    skip_unchanged: bool = False,
    replay_policy: AppendOnlyReplayPolicy = AppendOnlyReplayPolicy.REPLAY,
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
        replay_policy=replay_policy,
        respect_session_ownership=True,
    )


def _ingest_segment_batch(
    db_conn: sqlite3.Connection,
    *,
    active_path: Path,
    source_type: str,
    ingest_file: Callable[[Path], IngestionResult],
) -> SegmentBatchIngestion:
    closed = set(closed_segment_paths(active_path))
    source_results: dict[str, IngestionResult] = {}
    processed_closed: list[Path] = []
    for segment_path in readable_segment_paths(active_path):
        result = ingest_file(segment_path)
        source_results[str(segment_path)] = result
        if (
            segment_path in closed
            and int(result.get("processed_offset_bytes", -1))
            == segment_path.stat().st_size
        ):
            processed_closed.append(segment_path)
    return SegmentBatchIngestion(
        source_type=source_type,
        active_path=active_path,
        source_results=source_results,
        processed_closed_paths=tuple(processed_closed),
    )


def ingest_otlp_trace_segments(
    db_conn: sqlite3.Connection,
    *,
    active_path: Path,
    replay_policy: AppendOnlyReplayPolicy = AppendOnlyReplayPolicy.REPLAY,
) -> SegmentBatchIngestion:
    return _ingest_segment_batch(
        db_conn,
        active_path=active_path,
        source_type="otlp_traces_json",
        ingest_file=lambda path: ingest_otlp_traces_file(
            db_conn,
            file_path=path,
            skip_unchanged=True,
            replay_policy=replay_policy,
        ),
    )


def ingest_otlp_log_segments(
    db_conn: sqlite3.Connection,
    *,
    active_path: Path,
    replay_policy: AppendOnlyReplayPolicy = AppendOnlyReplayPolicy.REPLAY,
) -> SegmentBatchIngestion:
    return _ingest_segment_batch(
        db_conn,
        active_path=active_path,
        source_type="otlp_logs_json",
        ingest_file=lambda path: ingest_otlp_logs_file(
            db_conn,
            file_path=path,
            skip_unchanged=True,
            replay_policy=replay_policy,
        ),
    )


def finalize_processed_segments(
    db_conn: sqlite3.Connection,
    batches: Iterable[SegmentBatchIngestion],
    *,
    keep_processed_raw: bool,
) -> dict[str, int]:
    """Retain or remove only closed segments whose events normalized successfully."""

    candidates = tuple(
        path
        for batch in batches
        for path in batch.processed_closed_paths
    )
    if keep_processed_raw:
        SourceIngestionState(db_conn).mark_normalized(candidates, raw_deleted=False)
        db_conn.commit()
        return {"deleted_segments": 0, "deleted_bytes": 0, "retained_segments": len(candidates)}

    deleted: list[Path] = []
    deleted_bytes = 0
    for path in candidates:
        if not path.is_file():
            continue
        size = path.stat().st_size
        path.unlink()
        deleted.append(path)
        deleted_bytes += size
    state = SourceIngestionState(db_conn)
    state.mark_normalized(deleted, raw_deleted=True)
    state.mark_normalized(
        (path for path in candidates if path not in deleted),
        raw_deleted=False,
    )
    db_conn.commit()
    return {
        "deleted_segments": len(deleted),
        "deleted_bytes": deleted_bytes,
        "retained_segments": len(candidates) - len(deleted),
    }


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


def ingest_opencode_session_store(
    db_conn: sqlite3.Connection,
    *,
    file_path: Path,
    source_id: str | None = None,
    skip_unchanged: bool = False,
    session_ids: tuple[str, ...] = (),
) -> IngestionResult:
    """Ingest only OpenCode sessions beyond the durable relational cursor."""

    source = source_id or f"native_session:opencode:{file_path}"
    checkpoint_source = source + (":sessions:" + ",".join(sorted(session_ids)) if session_ids else "")
    source_type = "native_session"
    fingerprint = SourceFingerprint.from_path(file_path)
    state = SourceIngestionState(db_conn)
    if skip_unchanged and state.matches(checkpoint_source, source_type, fingerprint):
        return {
            "inserted": 0,
            "skipped": 0,
            "unchanged": 1,
            "scanned_sessions": 0,
        }

    checkpoint = state.load(checkpoint_source, source_type)
    cursor_time = 0
    cursor_id = ""
    if (
        skip_unchanged
        and checkpoint is not None
        and checkpoint.decoder_version == NATIVE_DECODER_VERSION
    ):
        cursor_time = checkpoint.record_cursor_time or 0
        cursor_id = checkpoint.record_cursor_id

    try:
        sessions = tuple(record for session_id in session_ids for record in OpenCodeSessionStore(file_path).load(session_id)) if session_ids else OpenCodeSessionStore(file_path).load(
            after_updated_ms=cursor_time,
            after_session_id=cursor_id,
        )
    except (OSError, sqlite3.DatabaseError) as exc:
        logger.warning("Failed to read OpenCode session store %s: %s", file_path, exc)
        return {
            "inserted": 0,
            "skipped": 0,
            "unchanged": 0,
            "scanned_sessions": 0,
            "failed": 1,
        }

    result = _ingest_spans(
        db_conn,
        spans=_iter_opencode_records_spans(sessions),
        source=source,
        source_type=source_type,
    )
    if sessions:
        cursor_time = sessions[-1].updated_ms
        cursor_id = sessions[-1].id
    state.record(
        checkpoint_source,
        source_type,
        fingerprint,
        processed_offset_bytes=fingerprint.size_bytes,
        checkpoint_tail_sha256="",
        record_cursor_time=cursor_time or None,
        record_cursor_id=cursor_id,
    )
    db_conn.commit()
    result.update({
        "unchanged": 0,
        "scanned_sessions": len(sessions),
    })
    return result


def _retire_native_projection(
    conn: sqlite3.Connection, source: str, *, keep_event_ids: set[str] | None = None,
) -> int:
    """Retire changed evidence while keeping exact retained raw/canonical identities."""
    from reflect.store.normalize import (
        _logical_tool_call_id,
        _stable_id,
        _status,
        refresh_session_usage,
    )
    from reflect.store.pending_refresh import PendingRefreshState
    from reflect.tool_outcomes import with_tool_outcome

    rows = conn.execute(
        "SELECT id, session_id, attrs_json FROM raw_events WHERE source_id = ? AND source_type = 'native_session' "
        "AND normalized_status <> 'superseded'", (source,),
    ).fetchall()
    if keep_event_ids is not None:
        rows = [row for row in rows if row[0] not in keep_event_ids]
    if not rows:
        return 0
    retired_ids = {row[0] for row in rows}
    # A tool call merges invocation and result evidence. Retracting either side
    # requires rebuilding that call from its still-retained counterparts only.
    affected_calls = {
        (row[1], call_id) for row in rows
        if (call_id := _logical_tool_call_id(json.loads(row[2]), ""))
    }
    for session_id in {sid for sid, _ in affected_calls}:
        retained = conn.execute(
            "SELECT id, event_type, attrs_json FROM raw_events WHERE session_id = ? "
            "AND normalized_status NOT IN ('superseded', 'ignored')", (session_id,),
        ).fetchall()
        for event_id, event_type, attrs_json in retained:
            attrs = json.loads(attrs_json)
            if event_id in retired_ids or (session_id, _logical_tool_call_id(attrs, "")) not in affected_calls:
                continue
            conn.execute(
                "UPDATE raw_events SET normalized_status = 'pending', normalization_error = NULL WHERE id = ?",
                (event_id,),
            )
            conn.execute(
                "UPDATE steps SET status = ? WHERE id = ?",
                (_status(event_type, with_tool_outcome(event_type, attrs)), _stable_id("step", event_id)),
            )
    for session_id, call_id in affected_calls:
        conn.execute("DELETE FROM tool_calls WHERE session_id = ? AND logical_call_id = ?", (session_id, call_id))
    step_ids = [_stable_id("step", row[0]) for row in rows]
    for offset in range(0, len(step_ids), 400):
        batch = step_ids[offset:offset + 400]
        placeholders = ",".join("?" for _ in batch)
        conn.execute(f"UPDATE steps SET parent_step_id = NULL WHERE parent_step_id IN ({placeholders})", batch)
        conn.execute(f"DELETE FROM steps WHERE id IN ({placeholders})", batch)
    for batch_start in range(0, len(rows), 400):
        ids = [row[0] for row in rows[batch_start:batch_start + 400]]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            "UPDATE raw_events SET normalized_status = 'superseded', normalization_error = ? "
            f"WHERE id IN ({placeholders})", (f"native_decoder:{NATIVE_DECODER_VERSION}", *ids),
        )
    affected_session_ids = {str(row[1]) for row in rows if row[1]}
    refresh_session_usage(conn, affected_session_ids)
    PendingRefreshState(conn).mark(affected_session_ids)
    return len(rows)


def ingest_native_session_file(
    db_conn,
    *,
    file_path: Path,
    agent: str,
    source_id: str | None = None,
    skip_existing_sessions: bool = False,
    skip_unchanged: bool = False,
    session_ids: tuple[str, ...] = (),
) -> IngestionResult:
    source = source_id or f"native_session:{agent}:{file_path}"
    if agent == "opencode":
        return ingest_opencode_session_store(
            db_conn,
            file_path=file_path,
            source_id=source,
            skip_unchanged=skip_unchanged,
            session_ids=session_ids,
        )
    fingerprint = SourceFingerprint.from_path(file_path)
    state = SourceIngestionState(db_conn)
    checkpoint = state.load(source, "native_session")
    if state.matches(source, "native_session", fingerprint):
        if skip_unchanged:
            return {"inserted": 0, "skipped": 0, "unchanged": 1}
        count = db_conn.execute(
            "SELECT COUNT(*) FROM raw_events WHERE source_id = ? AND normalized_status <> 'superseded'", (source,),
        ).fetchone()[0]
        return {"inserted": 0, "skipped": count}
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
    # Decode before retiring the old projection. A missing/empty retained source
    # cannot establish that previous evidence ceased to exist.
    projected = list(spans)
    if not projected:
        return {"inserted": 0, "skipped": 0, "unchanged": 0}
    usage_owner_is_other: dict[str, bool] = {}
    for span in projected:
        session_id = telemetry_session_id(span.get("attributes") or {})
        if session_id not in usage_owner_is_other:
            usage_owner_is_other[session_id] = _session_owned_by_other_source(
                db_conn, span=span, source=source, source_type="native_session",
            )
    for span in projected:
        if usage_owner_is_other.get(telemetry_session_id(span.get("attributes") or {})):
            span["attributes"] = {**span["attributes"], "reflect.usage.enrichment_only": True}
    retained_ids: set[str] = set()
    if checkpoint is not None and checkpoint.decoder_version == NATIVE_DECODER_VERSION:
        projected_ids = {
            _prepare_raw_span(span, source, "native_session").event_id: span for span in projected
        }
        active_ids = {
            row[0] for row in db_conn.execute(
                "SELECT id FROM raw_events WHERE source_id = ? AND source_type = 'native_session' "
                "AND normalized_status <> 'superseded'", (source,),
            )
        }
        retained_ids = active_ids.intersection(projected_ids)
        retired = _retire_native_projection(db_conn, source, keep_event_ids=set(projected_ids))
        projected = [span for event_id, span in projected_ids.items() if event_id not in retained_ids]
        if retired and not projected:
            # A pure retraction still needs the existing normalization pipeline
            # to refresh session assessments/costs. Prefer a zero-usage boundary;
            # timestamp-free sources may only retain a request. Reprocessing its
            # stable ID is idempotent and normalization reconciles usage totals.
            db_conn.execute(
                "UPDATE raw_events SET normalized_status = 'pending' WHERE id IN ("
                "SELECT COALESCE(MIN(CASE WHEN event_type = 'gen_ai.client.hook.SessionStart' THEN id END), MIN(id)) "
                "FROM raw_events WHERE source_id = ? AND source_type = 'native_session' AND normalized_status = 'ok' "
                "GROUP BY session_id)", (source,),
            )
    else:
        retired = _retire_native_projection(db_conn, source)
    result = _ingest_file_spans(
        db_conn,
        file_path=file_path,
        source=source,
        source_type="native_session",
        spans_factory=lambda _start, _end: projected,
        skip_unchanged=False,
        decoded_fingerprint=fingerprint,
    )
    result["skipped"] += len(retained_ids)
    if skip_unchanged:
        result["unchanged"] = 0
    if retired:
        result.update({"replaced_native_events": retired, "decoder_version": NATIVE_DECODER_VERSION})
    return result


def ingest_codex_context_file(
    db_conn: sqlite3.Connection,
    *,
    file_path: Path,
    skip_unchanged: bool = True,
) -> IngestionResult:
    """Ingest only the bounded, privacy-safe context preamble from a Codex session."""

    return _ingest_file_spans(
        db_conn,
        file_path=file_path,
        source=f"native_context:codex:{file_path}",
        source_type="native_context",
        spans_factory=lambda _start, _end: _iter_codex_context_spans(file_path),
        skip_unchanged=skip_unchanged,
    )
