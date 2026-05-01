from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from reflect.parsing import _load_otlp_traces


def _iso8601_from_ns(value_ns: int) -> str:
    if value_ns <= 0:
        return datetime.now(tz=timezone.utc).isoformat()
    return datetime.fromtimestamp(value_ns / 1_000_000_000, tz=timezone.utc).isoformat()


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
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def ingest_otlp_traces_file(db_conn, *, file_path: Path, source_id: str | None = None) -> dict[str, int]:
    source = source_id or str(file_path)
    inserted = 0
    skipped = 0

    for span in _load_otlp_traces(file_path):
        attrs = span.get("attributes", {}) or {}
        session_id = (
            attrs.get("session.id")
            or attrs.get("gen_ai.session.id")
            or attrs.get("session_id")
            or None
        )

        observed_at = _iso8601_from_ns(int(span.get("start_time_ns", 0) or 0))
        received_at = _iso8601_from_ns(int(span.get("end_time_ns", 0) or 0))
        created_at = datetime.now(tz=timezone.utc).isoformat()
        content_hash = _event_hash(span)
        event_id = hashlib.sha1(f"{source}:{content_hash}".encode("utf-8")).hexdigest()

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
                "otlp_traces_json",
                span.get("name", "unknown"),
                span.get("traceId", ""),
                span.get("spanId", ""),
                span.get("parentSpanId", ""),
                session_id,
                observed_at,
                received_at,
                json.dumps(attrs, sort_keys=True),
                "{}",
                "pending",
                None,
                content_hash,
                created_at,
            ),
        )
        if cursor.rowcount == 0:
            skipped += 1
        else:
            inserted += 1

    db_conn.commit()
    return {"inserted": inserted, "skipped": skipped}
