from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations() -> list[Migration]:
    migrations_pkg = resources.files("reflect.store.migrations")
    files = sorted(
        p for p in migrations_pkg.iterdir() if p.name.endswith(".sql") and p.name[:3].isdigit()
    )
    loaded: list[Migration] = []
    for file in files:
        version = int(file.name.split("_", 1)[0])
        loaded.append(Migration(version=version, name=file.name, sql=file.read_text(encoding="utf-8")))
    return loaded


def applied_migration_versions(conn: sqlite3.Connection) -> set[int]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          applied_at TEXT NOT NULL
        )
        """
    )
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def migrate(conn: sqlite3.Connection) -> list[int]:
    applied = applied_migration_versions(conn)
    applied_now: list[int] = []
    for migration in load_migrations():
        if migration.version in applied:
            continue
        conn.executescript(migration.sql)
        conn.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, datetime.now(UTC).isoformat()),
        )
        applied_now.append(migration.version)
    conn.commit()
    return applied_now
