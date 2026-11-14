"""Restartable, explicit cutover of human-owned records to the action store.

This initialization step is deliberately separate from migrate(commit=False).
It requires stopped old writers and a completed working snapshot. Canonical
references to moved records become provenance IDs, not cross-database FKs.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from reflect.preparation import PreparationLock
from reflect.store.sqlite import connect_sqlite, snapshot_incomplete
from reflect.store.user_state import UserStateStore
from reflect.utils import _json_dumps

USER_TABLES = frozenset({
    'workflow_versions', 'interventions', 'skills', 'skill_versions',
    'skill_installations', 'skill_evidence', 'operator_feedback',
    'nudge_policies', 'change_reviews', 'evaluations',
})
REVIEW_CONTEXT_TABLES = frozenset({'rule_definitions', 'task_archetypes', 'observations', 'workflow_candidates', 'improvement_events'})
CUTOVER_KEY = 'user_state_cutover_v1'


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _schema(conn: sqlite3.Connection, table: str, *, name: str, allowed_references: set[str]) -> str:
    columns = list(conn.execute(f'PRAGMA table_info({_quoted(table)})'))
    definitions = []
    for _, column, kind, required, default, _ in columns:
        definition = f'{_quoted(column)} {kind}'
        if required:
            definition += ' NOT NULL'
        if default is not None:
            definition += f' DEFAULT {default}'
        definitions.append(definition)
    primary = sorted((row[5], row[1]) for row in columns if row[5])
    if primary:
        definitions.append('PRIMARY KEY (' + ','.join(_quoted(column) for _, column in primary) + ')')
    for _, index, unique, origin, _ in conn.execute(f'PRAGMA index_list({_quoted(table)})'):
        if unique and origin == 'u':
            fields = [row[2] for row in conn.execute(f'PRAGMA index_info({_quoted(index)})')]
            definitions.append('UNIQUE (' + ','.join(_quoted(field) for field in fields) + ')')
    foreign_keys: dict[int, list[tuple]] = {}
    for row in conn.execute(f'PRAGMA foreign_key_list({_quoted(table)})'):
        if row[2] in allowed_references:
            foreign_keys.setdefault(row[0], []).append(row)
    for rows in foreign_keys.values():
        rows.sort(key=lambda row: row[1])
        definitions.append(
            'FOREIGN KEY (' + ','.join(_quoted(row[3]) for row in rows) + ') REFERENCES '
            + _quoted(rows[0][2]) + '(' + ','.join(_quoted(row[4]) for row in rows) + ')'
            + f' ON UPDATE {rows[0][5]} ON DELETE {rows[0][6]}'
        )
    source_sql = conn.execute('SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone()[0]
    if re.search(r'\bCHECK\s*\(|\bWITHOUT\s+ROWID\b|\bGENERATED\s+ALWAYS\b', source_sql, re.IGNORECASE):
        raise ValueError(f'{table} needs an explicit user-state migration schema')
    return f'CREATE TABLE {_quoted(name)} (' + ','.join(definitions) + ')'


def _digest(conn: sqlite3.Connection, table: str) -> tuple[int, str]:
    columns = list(conn.execute(f'PRAGMA table_info({_quoted(table)})'))
    primary = sorted((row[5], row[1]) for row in columns if row[5])
    order = ','.join(_quoted(column) for _, column in primary) or 'rowid'
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(f'SELECT * FROM {_quoted(table)} ORDER BY {order}'):
        digest.update(_json_dumps(list(row)).encode())
        digest.update(b'\n')
        count += 1
    return count, digest.hexdigest()


class UserStateMigration:
    def __init__(self, db_path: Path, state: UserStateStore):
        self.db_path = Path(db_path)
        self.state = state

    def run(self) -> bool:
        with PreparationLock(self.db_path), closing(connect_sqlite(self.db_path, strict_durability=True, attach_user_state=False)) as source:
            marker = source.execute('SELECT value FROM store_metadata WHERE key=?', (CUTOVER_KEY,)).fetchone()
            if marker:
                if Path(marker[0]).resolve() != self.state.path.resolve() or not self.state.path.exists():
                    raise RuntimeError('The authoritative user-state store is missing or does not match')
                self._mark_ready()
                return False
            if snapshot_incomplete(source):
                raise RuntimeError('User-state cutover requires a completed evidence snapshot')
            self.state.initialize(migrating=True)
            source.execute('PRAGMA foreign_keys=OFF')
            source.execute('BEGIN IMMEDIATE')
            try:
                self._copy_and_verify(source)
                self._cutover(source)
                source.commit()
            except BaseException:
                source.rollback()
                raise
            finally:
                source.execute('PRAGMA foreign_keys=ON')
            self._mark_ready()
            return True

    def _mark_ready(self) -> None:
        with closing(connect_sqlite(self.state.path, strict_durability=True)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables >= (USER_TABLES | REVIEW_CONTEXT_TABLES):
                raise RuntimeError('The migrated action store is incomplete')
            if conn.execute('PRAGMA foreign_key_check').fetchone():
                raise RuntimeError('The migrated action store has broken references')
            conn.execute("INSERT INTO user_state_metadata(key,value) VALUES ('cutover_complete',1) "
                         "ON CONFLICT(key) DO UPDATE SET value=1")
            conn.commit()

    def _copy_and_verify(self, source: sqlite3.Connection) -> None:
        with closing(connect_sqlite(self.state.path, strict_durability=True)) as target:
            target.execute('PRAGMA foreign_keys=OFF')
            target.execute('BEGIN IMMEDIATE')
            try:
                existing = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in sorted((USER_TABLES | REVIEW_CONTEXT_TABLES) & existing):
                    # A pre-cutover restart can replace only migration-owned
                    # tables. Standalone authored draft records are untouched.
                    target.execute(f'DROP TABLE IF EXISTS {_quoted(table)}')
                    target.execute(_schema(source, table, name=table, allowed_references=set(USER_TABLES | REVIEW_CONTEXT_TABLES)))
                    columns = list(source.execute(f'PRAGMA table_info({_quoted(table)})'))
                    placeholders = ','.join('?' for _ in columns)
                    target.executemany(f'INSERT INTO {_quoted(table)} VALUES ({placeholders})',
                                       source.execute(f'SELECT * FROM {_quoted(table)}'))
                    for sql, in source.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,)):
                        target.execute(sql)
                    if _digest(source, table) != _digest(target, table):
                        raise RuntimeError(f'User-state migration verification failed for {table}')
                target.execute("""CREATE TABLE IF NOT EXISTS workflow_review_ownership (
                    candidate_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 1,
                    evidence_revision_hash TEXT NOT NULL
                )""")
                target.execute("""INSERT OR IGNORE INTO workflow_review_ownership(candidate_id,evidence_revision_hash)
                    SELECT id,revision_hash FROM workflow_candidates WHERE reviewer IS NOT NULL
                       OR json_extract(checks_json,'$.dismissal') IS NOT NULL
                       OR json_extract(provenance_json,'$.source') IN ('skill_extraction','agent_authored','manual_skill_file')
                       OR id IN (SELECT entity_id FROM improvement_events WHERE event_type='edited_pending')""")
                if target.execute('PRAGMA foreign_key_check').fetchone():
                    raise RuntimeError('User-state migration produced broken internal references')
                target.execute("""INSERT INTO workflow_dismissals(contract_signature,dismissed,updated_at)
                    SELECT contract_signature,1,MAX(updated_at) FROM workflow_candidates
                    WHERE json_extract(checks_json,'$.dismissal') IS NOT NULL
                    GROUP BY contract_signature
                    ON CONFLICT(contract_signature) DO NOTHING""")
                self._install_revision_triggers(target)
                target.commit()
            except BaseException:
                target.rollback()
                raise

    @staticmethod
    def _install_revision_triggers(conn: sqlite3.Connection) -> None:
        # Human-owned tables advance cache revisions in the SAME transaction
        # as existing domain commands, including commands which commit early.
        for table in sorted(USER_TABLES | {'workflow_review_ownership'}):
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info({_quoted(table)})')
                       if row[1] not in {'created_at', 'updated_at', 'last_seen_at', 'first_seen_at'}]
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                name = f'user_state_revision_{table}_{operation.lower()}'
                conn.execute(f'DROP TRIGGER IF EXISTS {_quoted(name)}')
                condition = (' WHEN ' + ' OR '.join(f'OLD.{_quoted(column)} IS NOT NEW.{_quoted(column)}'
                                                   for column in columns)) if operation == 'UPDATE' else ''
                conn.execute(f'CREATE TRIGGER {_quoted(name)} AFTER {operation} ON {_quoted(table)}'
                             + condition + " BEGIN UPDATE user_state_metadata SET value=value+1 "
                             "WHERE key='material_revision'; END")
        for table, owner_condition in (
            ('workflow_candidates', 'candidate_id=NEW.id'),
            ('observations', 'candidate_id IN (SELECT id FROM workflow_candidates WHERE observation_id=NEW.id)'),
        ):
            name = f'user_state_revision_{table}_review'
            conn.execute(f'DROP TRIGGER IF EXISTS {_quoted(name)}')
            columns = ('status', 'title', 'content_json', 'checks_json') if table == 'workflow_candidates' else ('status', 'suppression_reason', 'suppressed_until')
            changed = ' OR '.join(f'OLD.{_quoted(column)} IS NOT NEW.{_quoted(column)}' for column in columns)
            conn.execute(f'CREATE TRIGGER {_quoted(name)} AFTER UPDATE ON {_quoted(table)} '
                         f'WHEN ({changed}) AND EXISTS(SELECT 1 FROM workflow_review_ownership WHERE {owner_condition}) '
                         "BEGIN UPDATE user_state_metadata SET value=value+1 WHERE key='material_revision'; END")

        conn.execute("""CREATE TRIGGER IF NOT EXISTS user_state_contract_dismissal
            AFTER UPDATE OF checks_json ON workflow_candidates
            WHEN json_extract(NEW.checks_json,'$.dismissal') IS NOT NULL
            BEGIN INSERT INTO workflow_dismissals(contract_signature,dismissed,updated_at)
                VALUES(NEW.contract_signature,1,NEW.updated_at)
                ON CONFLICT(contract_signature) DO UPDATE SET dismissed=1,updated_at=excluded.updated_at;
            END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS user_state_contract_restore
            AFTER UPDATE OF checks_json ON workflow_candidates
            WHEN json_extract(OLD.checks_json,'$.dismissal') IS NOT NULL
             AND json_extract(NEW.checks_json,'$.dismissal') IS NULL
            BEGIN UPDATE workflow_dismissals SET dismissed=0,updated_at=NEW.updated_at
                WHERE contract_signature=OLD.contract_signature;
            END""")

    def _cutover(self, source: sqlite3.Connection) -> None:
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in sorted(tables - USER_TABLES):
            foreign = list(source.execute(f'PRAGMA foreign_key_list({_quoted(table)})'))
            if not any(row[2] in USER_TABLES for row in foreign):
                continue
            replacement = f'_user_state_cutover_{table}'
            indexes = [row[0] for row in source.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]
            source.execute(_schema(source, table, name=replacement, allowed_references=tables - USER_TABLES))
            source.execute(f'INSERT INTO {_quoted(replacement)} SELECT * FROM {_quoted(table)}')
            if _digest(source, table) != _digest(source, replacement):
                raise RuntimeError(f'Provenance migration verification failed for {table}')
            source.execute(f'DROP TABLE {_quoted(table)}')
            source.execute(f'ALTER TABLE {_quoted(replacement)} RENAME TO {_quoted(table)}')
            for sql in indexes:
                source.execute(sql)
        for table in sorted(USER_TABLES & tables):
            source.execute(f'DROP TABLE {_quoted(table)}')
        if source.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Canonical evidence references failed validation after cutover')
        # Review decisions now belong to the action store. Restore the
        # generated row's pre-dismissal state; readers apply the contract policy.
        source.execute("""UPDATE workflow_candidates SET
            status=COALESCE(json_extract(checks_json,'$.dismissal.previous_status'),'pending'),
            reviewer=NULL,reviewed_at=NULL,checks_json=json_remove(checks_json,'$.dismissal')
            WHERE json_extract(checks_json,'$.dismissal') IS NOT NULL""")
        source.execute('INSERT INTO store_metadata(key,value,updated_at) VALUES (?,?,?)',
                       (CUTOVER_KEY, str(self.state.path.resolve()), datetime.now(UTC).isoformat()))
