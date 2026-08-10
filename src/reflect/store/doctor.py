from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reflect.store.migrate import load_migrations
from reflect.store.sqlite import (
    DEFAULT_BUSY_TIMEOUT_MS,
    DEFAULT_WAL_AUTOCHECKPOINT,
    connect_sqlite_read_only,
)


@dataclass(frozen=True)
class SegmentPreparationStatus:
    pending_count: int
    pending_bytes: int
    processed_on_disk_count: int
    processed_on_disk_bytes: int
    unclassified_count: int
    unclassified_bytes: int
    last_normalized_at: str | None
    store_available: bool


def inspect_segment_preparation(
    db_path: str | Path,
    closed_paths: Iterable[Path],
) -> SegmentPreparationStatus:
    """Compare closed raw segments with the read-only ingestion checkpoint ledger."""

    snapshots: list[tuple[str, int]] = []
    for path in closed_paths:
        try:
            snapshots.append((str(path), path.stat().st_size))
        except OSError:
            continue

    unavailable = SegmentPreparationStatus(
        pending_count=0,
        pending_bytes=0,
        processed_on_disk_count=0,
        processed_on_disk_bytes=0,
        unclassified_count=len(snapshots),
        unclassified_bytes=sum(size for _, size in snapshots),
        last_normalized_at=None,
        store_available=False,
    )
    try:
        conn = connect_sqlite_read_only(db_path)
    except (OSError, sqlite3.DatabaseError):
        return unavailable
    try:
        rows = conn.execute(
            """
            SELECT source_id, normalized_at
            FROM source_ingestion_state
            WHERE source_type IN ('otlp_traces_json', 'otlp_logs_json')
            """
        ).fetchall()
    except sqlite3.DatabaseError:
        return unavailable
    finally:
        conn.close()

    normalized = {
        str(source_id): str(normalized_at)
        for source_id, normalized_at in rows
        if normalized_at
    }
    pending = [
        (source_id, size)
        for source_id, size in snapshots
        if source_id not in normalized
    ]
    processed = [
        (source_id, size)
        for source_id, size in snapshots
        if source_id in normalized
    ]
    return SegmentPreparationStatus(
        pending_count=len(pending),
        pending_bytes=sum(size for _, size in pending),
        processed_on_disk_count=len(processed),
        processed_on_disk_bytes=sum(size for _, size in processed),
        unclassified_count=0,
        unclassified_bytes=0,
        last_normalized_at=max(normalized.values(), default=None),
        store_available=True,
    )


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _pragma(conn: sqlite3.Connection, key: str) -> Any:
    return conn.execute(f"PRAGMA {key};").fetchone()[0]


def inspect_database(conn: sqlite3.Connection) -> dict[str, Any]:
    """Inspect SQLite store health without applying migrations."""
    expected = [migration.version for migration in load_migrations()]
    applied = (
        sorted(row[0] for row in conn.execute("SELECT version FROM schema_migrations"))
        if _table_exists(conn, "schema_migrations")
        else []
    )
    expected_set = set(expected)
    applied_set = set(applied)

    foreign_key_issues = [
        {
            "table": row[0],
            "rowid": row[1],
            "parent": row[2],
            "fkid": row[3],
        }
        for row in conn.execute("PRAGMA foreign_key_check;")
    ]
    pragmas = {
        "foreign_keys": _pragma(conn, "foreign_keys"),
        "journal_mode": str(_pragma(conn, "journal_mode")).lower(),
        "synchronous": _pragma(conn, "synchronous"),
        "wal_autocheckpoint": _pragma(conn, "wal_autocheckpoint"),
        "busy_timeout": _pragma(conn, "busy_timeout"),
    }
    pragma_ok = (
        pragmas["foreign_keys"] == 1
        and pragmas["journal_mode"] == "wal"
        and pragmas["synchronous"] in {1, 2}
        and pragmas["wal_autocheckpoint"] == DEFAULT_WAL_AUTOCHECKPOINT
        and pragmas["busy_timeout"] == DEFAULT_BUSY_TIMEOUT_MS
    )

    pending = [version for version in expected if version not in applied_set]
    unknown = [version for version in applied if version not in expected_set]
    ok = not pending and not unknown and not foreign_key_issues and pragma_ok
    return {
        "ok": ok,
        "expected_migrations": expected,
        "applied_migrations": applied,
        "pending_migrations": pending,
        "unknown_migrations": unknown,
        "foreign_key_issues": foreign_key_issues,
        "pragmas": pragmas,
        "pragma_ok": pragma_ok,
    }
