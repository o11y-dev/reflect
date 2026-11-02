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


def _migration_statements(sql: str) -> list[str]:
    statements: list[str] = []
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            if pending.strip():
                statements.append(pending)
            pending = ""
    if pending.strip():
        raise ValueError("Migration SQL ended with an incomplete statement")
    return statements


def migrate(conn: sqlite3.Connection, *, commit: bool = True) -> list[int]:
    """Apply pending migrations, optionally inside a caller-owned transaction."""
    from reflect.improvements.workflow_identity import workflow_identity_from_json
    from reflect.tool_outcomes import tool_failure
    from reflect.utils import _json_dumps, _json_loads

    def failure_json(event: str, raw_attrs: str) -> str | None:
        try:
            attrs = _json_loads(raw_attrs)
        except (ValueError, TypeError):
            return None
        if not isinstance(attrs, dict):
            return None
        failure = tool_failure(event, attrs)
        return _json_dumps({"type": failure.error_type, "message": failure.message}) if failure else None

    conn.create_function("reflect_tool_failure", 2, failure_json, deterministic=True)

    conn.create_function(
        "reflect_workflow_contract_signature",
        2,
        lambda title, content: workflow_identity_from_json(
            title, content
        ).contract_signature,
        deterministic=True,
    )
    conn.create_function(
        "reflect_workflow_revision_hash",
        2,
        lambda title, content: workflow_identity_from_json(title, content).revision_hash,
        deterministic=True,
    )
    migrations = load_migrations()
    try:
        applied_fast = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations")
        }
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        applied_fast = set()
    if all(migration.version in applied_fast for migration in migrations):
        return []
    savepoint = "reflect_schema_migrate"
    if commit:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
    elif not conn.in_transaction:
        raise ValueError("commit=False requires a caller-owned transaction")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    applied_now: list[int] = []
    try:
        applied = applied_migration_versions(conn)
        for migration in migrations:
            if migration.version in applied:
                continue
            for statement in _migration_statements(migration.sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, datetime.now(UTC).isoformat()),
            )
            applied_now.append(migration.version)
        if commit:
            conn.commit()
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return applied_now
    except Exception:
        if commit:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
