from __future__ import annotations

import sqlite3

from reflect.store.doctor import inspect_segment_preparation


def _create_ingestion_ledger(db_path, rows: list[tuple[str, str | None]]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE source_ingestion_state (
              source_id TEXT PRIMARY KEY,
              source_type TEXT NOT NULL,
              normalized_at TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO source_ingestion_state(source_id, source_type, normalized_at)
            VALUES (?, 'otlp_traces_json', ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def test_inspect_segment_preparation_separates_pending_and_processed_files(tmp_path):
    pending = tmp_path / "otel-traces.0001.jsonl"
    processed = tmp_path / "otel-traces.0002.jsonl"
    pending.write_bytes(b"pending")
    processed.write_bytes(b"processed")
    db_path = tmp_path / "reflect.db"
    _create_ingestion_ledger(
        db_path,
        [
            (str(processed), "2026-08-10T12:00:00+00:00"),
            (str(tmp_path / "deleted.jsonl"), "2026-08-10T13:00:00+00:00"),
        ],
    )

    status = inspect_segment_preparation(db_path, (pending, processed))

    assert status.pending_count == 1
    assert status.pending_bytes == len(b"pending")
    assert status.processed_on_disk_count == 1
    assert status.processed_on_disk_bytes == len(b"processed")
    assert status.unclassified_count == 0
    assert status.last_normalized_at == "2026-08-10T13:00:00+00:00"
    assert status.store_available is True


def test_inspect_segment_preparation_treats_closed_files_as_pending_without_store(tmp_path):
    pending = tmp_path / "otel-logs.0001.jsonl"
    pending.write_bytes(b"pending")

    status = inspect_segment_preparation(tmp_path / "missing.db", (pending,))

    assert status.pending_count == 0
    assert status.pending_bytes == 0
    assert status.unclassified_count == 1
    assert status.unclassified_bytes == len(b"pending")
    assert status.last_normalized_at is None
    assert status.store_available is False
