from __future__ import annotations

import sqlite3
from typing import Any

from reflect.store.migrate import load_migrations
from reflect.store.sqlite import DEFAULT_BUSY_TIMEOUT_MS, DEFAULT_WAL_AUTOCHECKPOINT


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
