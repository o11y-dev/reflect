"""Authoritative human decisions, independent of evidence preparation writes."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reflect.store.sqlite import connect_sqlite, connect_sqlite_read_only
from reflect.utils import _json_dumps, _json_loads


class UserStateConflict(ValueError):
    """A reviewed draft changed before a requested edit was committed."""


@dataclass(frozen=True)
class StoredWorkflowDraft:
    id: str
    title: str
    content: dict[str, Any]
    evidence: dict[str, Any]
    revision: int
    updated_at: str


@dataclass(frozen=True)
class StoredWorkflowReview:
    candidate_id: str
    title: str
    content: dict[str, Any]
    contract_signature: str
    revision_hash: str
    status: str
    checks: dict[str, Any]
    updated_at: str
    evidence_revision_hash: str


class UserStateStore:
    """Short, versioned transactions; never acquire the preparation lock."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def initialize(self, *, migrating: bool = False) -> None:
        # Called only by explicit startup/migration, never by read methods.
        with closing(connect_sqlite(self.path, strict_durability=True)) as conn:
            conn.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS user_state_metadata (
                    key TEXT PRIMARY KEY, value INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO user_state_metadata VALUES ('material_revision', 0);
                CREATE TABLE IF NOT EXISTS workflow_dismissals (
                    contract_signature TEXT PRIMARY KEY,
                    dismissed INTEGER NOT NULL CHECK(dismissed IN (0,1)),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_drafts (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
            ''')
            conn.execute("INSERT INTO user_state_metadata(key,value) VALUES ('cutover_complete',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (0 if migrating else 1,))
            conn.commit()

    @staticmethod
    def _require_ready(conn) -> None:
        row = conn.execute("SELECT value FROM user_state_metadata WHERE key='cutover_complete'").fetchone()
        if not row or row[0] != 1:
            raise UserStateConflict('The authoritative action-store migration is not complete')

    @contextmanager
    def workflow_action(self, evidence_path: Path, candidate_id: str, *, expected_revision_hash: str, action_kind: str = "apply"):
        """Bind an action to reviewed content and one immutable evidence file."""
        with closing(connect_sqlite(self.path, strict_durability=True, timeout_ms=1000)) as conn:
            ready = conn.execute("SELECT value FROM user_state_metadata WHERE key='cutover_complete'").fetchone()
            if not ready or ready[0] != 1:
                raise UserStateConflict('The authoritative action-store migration is not complete')
            if action_kind not in {'apply', 'rollback', 'edit', 'review'}:
                raise ValueError('Unsupported workflow action kind')
            attach_evidence(conn, evidence_path)
            conn.execute('BEGIN IMMEDIATE')
            try:
                self._require_ready(conn)
                self._seed_review_context(conn, candidate_id, action_kind=action_kind)
                row = conn.execute('SELECT revision_hash FROM workflow_candidates WHERE id=?', (candidate_id,)).fetchone()
                if row is None or row[0] != expected_revision_hash:
                    raise UserStateConflict('The workflow changed; review its current content before enabling it')
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _seed_review_context(conn, candidate_id: str, *, action_kind: str) -> None:
        from reflect.store.user_state_migration import _quoted

        owned = conn.execute('SELECT evidence_revision_hash FROM workflow_review_ownership WHERE candidate_id=?',
                             (candidate_id,)).fetchone()
        if action_kind == 'rollback':
            if not owned:
                raise UserStateConflict('No authoritative workflow review exists for rollback')
            return
        columns = [row[1] for row in conn.execute('PRAGMA evidence.table_info(workflow_candidates)')]
        values = conn.execute('SELECT * FROM evidence.workflow_candidates WHERE id=?', (candidate_id,)).fetchone()
        if values is None:
            raise UserStateConflict('The workflow source is unavailable; save and review an independent draft first')
        candidate = dict(zip(columns, values, strict=True))
        if owned and action_kind != 'review':
            if owned[0] != candidate['revision_hash']:
                raise UserStateConflict('Workflow evidence changed since review; review the updated suggestion first')
            return
        if owned and action_kind == 'review' and conn.execute(
            "SELECT 1 FROM interventions i JOIN workflow_versions v ON v.id=i.workflow_version_id "
            "WHERE v.candidate_id=? AND i.status='active' LIMIT 1", (candidate_id,),
        ).fetchone():
            raise UserStateConflict('Roll back the active workflow before reviewing replacement instructions')

        def copy_context(table, where, params):
            fields = list(conn.execute(f'PRAGMA evidence.table_info({_quoted(table)})'))
            names = [row[1] for row in fields]
            keys = [row[1] for row in sorted(fields, key=lambda row: row[5]) if row[5]]
            update = ','.join(f'{_quoted(name)}=excluded.{_quoted(name)}' for name in names if name not in keys)
            sql = (f'INSERT INTO main.{_quoted(table)} (' + ','.join(_quoted(name) for name in names)
                   + ') VALUES (' + ','.join('?' for _ in names) + ') ON CONFLICT ('
                   + ','.join(_quoted(key) for key in keys) + ') DO UPDATE SET ' + update)
            for row in conn.execute(f'SELECT * FROM evidence.{_quoted(table)} WHERE {where}', params).fetchall():
                conn.execute(sql, row)

        observation_id = candidate['observation_id']
        rule = conn.execute('SELECT rule_id,rule_version FROM evidence.observations WHERE id=?', (observation_id,)).fetchone()
        if rule is None:
            raise UserStateConflict('The workflow observation is unavailable')
        copy_context('rule_definitions', 'id=? AND version=?', rule)
        if candidate['task_archetype_id']:
            copy_context('task_archetypes', 'id=?', (candidate['task_archetype_id'],))
        copy_context('observations', 'id=?', (observation_id,))
        copy_context('workflow_candidates', 'id=?', (candidate_id,))
        conn.execute('INSERT INTO workflow_review_ownership(candidate_id,evidence_revision_hash) VALUES (?,?) '
                     'ON CONFLICT(candidate_id) DO UPDATE SET evidence_revision_hash=excluded.evidence_revision_hash, '
                     'revision=workflow_review_ownership.revision+1',
                     (candidate_id, candidate['revision_hash']))

    def snapshot(self, *, connection=None, schema: str = "main") -> UserStateSnapshot:
        if schema not in {'main', 'user_state'}:
            raise ValueError('Unsupported user-state schema')
        if connection is not None:
            return self._snapshot(connection, schema)
        with closing(connect_sqlite_read_only(self.path, profile=None)) as conn:
            return self._snapshot(conn, 'main')

    @staticmethod
    def _snapshot(conn, schema: str) -> UserStateSnapshot:
        revision = int(conn.execute(f"SELECT value FROM {schema}.user_state_metadata WHERE key='material_revision'").fetchone()[0])
        dismissed = frozenset(row[0] for row in conn.execute(
            f'SELECT contract_signature FROM {schema}.workflow_dismissals WHERE dismissed=1'))
        drafts = tuple(UserStateStore._draft(row) for row in conn.execute(f'SELECT * FROM {schema}.workflow_drafts ORDER BY id'))
        reviews = ()
        if conn.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE name='workflow_review_ownership'").fetchone():
            reviews = tuple(StoredWorkflowReview(row[0], row[1], _json_loads(row[2]), row[3], row[4],
                                                 row[5], _json_loads(row[6]), row[7], row[8])
                            for row in conn.execute(f"""SELECT c.id,c.title,c.content_json,c.contract_signature,
                                c.revision_hash,c.status,c.checks_json,c.updated_at,o.evidence_revision_hash
                                FROM {schema}.workflow_candidates c
                                JOIN {schema}.workflow_review_ownership o ON o.candidate_id=c.id"""))
        return UserStateSnapshot(revision, dismissed, drafts, reviews)

    def revision(self) -> int:
        with closing(connect_sqlite_read_only(self.path, profile=None)) as conn:
            return int(conn.execute("SELECT value FROM user_state_metadata WHERE key='material_revision'").fetchone()[0])

    @staticmethod
    def _advance(conn) -> None:
        conn.execute("UPDATE user_state_metadata SET value=value+1 WHERE key='material_revision'")

    def is_dismissed(self, contract_signature: str) -> bool:
        with closing(connect_sqlite_read_only(self.path, profile=None)) as conn:
            row = conn.execute('SELECT dismissed FROM workflow_dismissals WHERE contract_signature=?',
                               (contract_signature,)).fetchone()
            return bool(row and row[0])

    def dismiss(self, contract_signature: str) -> None:
        self._dismissal(contract_signature, True)

    def restore(self, contract_signature: str) -> None:
        self._dismissal(contract_signature, False)

    def _dismissal(self, contract_signature: str, dismissed: bool) -> None:
        if not contract_signature:
            raise ValueError('A workflow contract signature is required')
        with closing(connect_sqlite(self.path, strict_durability=True, timeout_ms=1000)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._require_ready(conn)
            row = conn.execute('SELECT dismissed FROM workflow_dismissals WHERE contract_signature=?',
                               (contract_signature,)).fetchone()
            if bool(row and row[0]) == dismissed:
                return
            conn.execute('''INSERT INTO workflow_dismissals VALUES (?,?,?)
                ON CONFLICT(contract_signature) DO UPDATE SET
                dismissed=excluded.dismissed, updated_at=excluded.updated_at''',
                (contract_signature, int(dismissed), datetime.now(UTC).isoformat()))
            self._advance(conn)
            conn.commit()

    @staticmethod
    def _draft(row) -> StoredWorkflowDraft | None:
        if row is None:
            return None
        return StoredWorkflowDraft(row[0], row[1], _json_loads(row[2]), _json_loads(row[3]), row[4], row[5])

    def draft(self, draft_id: str) -> StoredWorkflowDraft | None:
        with closing(connect_sqlite_read_only(self.path, profile=None)) as conn:
            return self._draft(conn.execute('SELECT * FROM workflow_drafts WHERE id=?', (draft_id,)).fetchone())

    def save_draft(
        self, draft_id: str, *, title: str, content: dict[str, Any],
        evidence: dict[str, Any], expected_revision: int,
    ) -> StoredWorkflowDraft:
        if not draft_id or not title.strip():
            raise ValueError('A draft ID and title are required')
        with closing(connect_sqlite(self.path, strict_durability=True, timeout_ms=1000)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._require_ready(conn)
            previous = self._draft(conn.execute('SELECT * FROM workflow_drafts WHERE id=?', (draft_id,)).fetchone())
            revision = previous.revision if previous else 0
            if revision != expected_revision:
                raise UserStateConflict('The workflow draft changed; reload it before saving')
            if previous and (previous.title, previous.content, previous.evidence) == (title, content, evidence):
                return previous
            updated_at = datetime.now(UTC).isoformat()
            conn.execute('''INSERT INTO workflow_drafts VALUES (?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                content_json=excluded.content_json, evidence_json=excluded.evidence_json,
                revision=excluded.revision, updated_at=excluded.updated_at''',
                (draft_id, title, _json_dumps(content), _json_dumps(evidence), revision + 1, updated_at))
            self._advance(conn)
            conn.commit()
            return StoredWorkflowDraft(draft_id, title, content, evidence, revision + 1, updated_at)


@dataclass(frozen=True)
class UserStateSnapshot:
    """One material revision shared by every query composing a response."""

    revision: int
    dismissed_contracts: frozenset[str]
    drafts: tuple[StoredWorkflowDraft, ...]
    reviews: tuple[StoredWorkflowReview, ...] = ()

    def overlay(self, candidate):
        from reflect.improvements.models import WorkflowStatus
        from reflect.improvements.workflow_identity import WorkflowIdentity

        reviewed = next((item for item in self.reviews if item.candidate_id == candidate.id), None)
        if reviewed and not reviewed.checks.get('dismissal'):
            status = WorkflowStatus(reviewed.status)
            candidate = candidate.model_copy(update={
                'title': reviewed.title, 'content': reviewed.content,
                'contract_signature': reviewed.contract_signature,
                'revision_hash': reviewed.revision_hash, 'status': status,
                'checks': {**reviewed.checks, 'source_changed': candidate.revision_hash != reviewed.evidence_revision_hash},
                'updated_at': reviewed.updated_at,
                'lifecycle': candidate.lifecycle.model_copy(update={
                    'review': status,
                    'display': status.value if status in {WorkflowStatus.PENDING, WorkflowStatus.REJECTED, WorkflowStatus.STALE}
                               else candidate.lifecycle.display,
                }),
            })
        draft = next((item for item in self.drafts if item.id == candidate.id), None)
        if draft:
            identity = WorkflowIdentity.from_content(draft.title, draft.content)
            candidate = candidate.model_copy(update={
                'title': draft.title, 'content': draft.content,
                'contract_signature': identity.contract_signature,
                'revision_hash': identity.revision_hash,
                'status': WorkflowStatus.PENDING,
                'checks': {'schema': 'valid', 'review_required': True},
                'updated_at': draft.updated_at,
                'lifecycle': candidate.lifecycle.model_copy(update={
                    'review': WorkflowStatus.PENDING, 'display': 'pending',
                }),
            })
        if (candidate.contract_signature in self.dismissed_contracts
                and candidate.lifecycle.deployment.value != 'active'
                and candidate.status in {WorkflowStatus.PENDING, WorkflowStatus.STALE}):
            candidate = candidate.model_copy(update={
                'status': WorkflowStatus.REJECTED,
                'checks': {**candidate.checks, 'dismissal': {'contract_signature': candidate.contract_signature}},
                'lifecycle': candidate.lifecycle.model_copy(update={
                    'review': WorkflowStatus.REJECTED, 'display': 'rejected',
                }),
            })
        return candidate


def attach_evidence(conn, evidence_path: Path, *, immutable: bool = True) -> None:
    """Attach one explicit evidence generation to an action connection."""
    uri = Path(evidence_path).resolve().as_uri() + '?mode=ro'
    if immutable:
        uri += '&immutable=1'
    conn.execute('ATTACH DATABASE ? AS evidence', (uri,))
