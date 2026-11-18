"""Durable mutation revision for canonical evidence and derived read models."""
from __future__ import annotations

import sqlite3

# Metadata/readiness and intake bookkeeping do not change published evidence.
_EXCLUDED = {'store_metadata', 'schema_migrations', 'raw_events', 'ingestion_checkpoints'}
_TIMESTAMPS = {'created_at', 'updated_at', 'last_seen_at', 'first_seen_at', 'measured_at'}


def install_evidence_revision_tracking(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR IGNORE INTO store_metadata(key,value,updated_at) VALUES ('canonical_mutation_revision','0','')")
    tables = conn.execute("SELECT name FROM main.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND sql NOT LIKE 'CREATE VIRTUAL%'").fetchall()
    for (table,) in tables:
        if table in _EXCLUDED or table.startswith(('memory_fts', 'session_fts')):
            continue
        quoted = '"' + table.replace('"', '""') + '"'
        columns = [row[1] for row in conn.execute(f'PRAGMA main.table_info({quoted})') if row[1] not in _TIMESTAMPS]
        changed = ' OR '.join('OLD."' + column.replace('"', '""') + '" IS NOT NEW."' + column.replace('"', '""') + '"' for column in columns)
        for action in ('INSERT', 'UPDATE', 'DELETE'):
            name = '"evidence_revision_' + table.replace('"', '""') + '_' + action.lower() + '"'
            condition = f' WHEN {changed}' if action == 'UPDATE' and changed else ''
            conn.execute(f'CREATE TRIGGER IF NOT EXISTS {name} AFTER {action} ON {quoted}{condition} BEGIN '
                         "UPDATE store_metadata SET value=CAST(value AS INTEGER)+1 WHERE key='canonical_mutation_revision'; END")
