"""Application-owned evidence reads and independently committed workflow actions."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from reflect.backend_runtime import action_store_path
from reflect.improvements.service import ImprovementService
from reflect.store.snapshots import PublishedSnapshotStore, SnapshotReadContext
from reflect.store.sqlite import SnapshotNotReadyError
from reflect.store.user_state import UserStateStore


class ReflectApplication:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.publications = PublishedSnapshotStore(self.db_path)
        self.user_state = UserStateStore(action_store_path(self.db_path))

    @contextmanager
    def read(self):
        if self.publications.current() is None:
            raise SnapshotNotReadyError('The first completed report is preparing')
        with SnapshotReadContext(self.db_path) as context:
            yield ImprovementService(context.connection, initialize_schema=False)

    @contextmanager
    def workflow_action(self, candidate_id: str, *, action_kind: str,
                        expected_revision: str | None = None):
        # Keep the publication leased across review read and short action commit.
        with self.publications.acquire() as publication:
            with SnapshotReadContext(self.db_path, snapshot=publication) as read:
                candidate = ImprovementService(read.connection, initialize_schema=False).workflows.show(candidate_id)
                revision = expected_revision or candidate.revision_hash
            with self.user_state.workflow_action(publication.path, candidate_id,
                                                  expected_revision_hash=revision,
                                                  action_kind=action_kind) as conn:
                yield ImprovementService(conn, initialize_schema=False)

    def create_workflow(self, *, content: dict, finding_id: str | None = None) -> dict:
        from contextlib import closing

        from reflect.store.sqlite import connect_sqlite
        from reflect.store.user_state import attach_evidence
        from reflect.store.user_state_migration import _quoted

        with self.publications.acquire() as publication, closing(
            connect_sqlite(self.user_state.path, strict_durability=True)
        ) as conn:
            self.user_state._require_ready(conn)
            attach_evidence(conn, publication.path)
            conn.execute('BEGIN IMMEDIATE')
            service = ImprovementService(conn, initialize_schema=False)
            if finding_id:
                # Only the selected observation is copied as saved provenance.
                # Aggregate/session evidence stays attached and read-only.
                observation = conn.execute('SELECT rule_id,rule_version '
                                           'FROM evidence.observations WHERE id=?', (finding_id,)).fetchone()
                if observation is None:
                    raise KeyError('Finding not found in the completed evidence')
                selections = [('rule_definitions', 'id=? AND version=?', observation[:2]),
                              ('observations', 'id=?', (finding_id,)),
                              ('observation_sessions', 'observation_id=?', (finding_id,)),
                              ('observation_evidence', 'observation_id=?', (finding_id,))]
                archetype = service.archetypes.dominant_for_observation(finding_id)
                if archetype:
                    selections.insert(1, ('task_archetypes', 'id=?', (archetype,)))
                for table, where, values in selections:
                    conn.execute(f'INSERT OR IGNORE INTO main.{_quoted(table)} '
                                 f'SELECT * FROM evidence.{_quoted(table)} WHERE {where}', values)
            result = service.create_workflow(content=content, finding_id=finding_id)
            conn.execute('INSERT INTO workflow_review_ownership(candidate_id,evidence_revision_hash) VALUES (?,?) '
                         'ON CONFLICT(candidate_id) DO NOTHING', (result['id'], result['revision_hash']))
            conn.commit()
            return result

    @contextmanager
    def state_action(self):
        """Short user-owned writes against a leased evidence generation."""
        from contextlib import closing

        from reflect.store.sqlite import connect_sqlite
        from reflect.store.user_state import attach_evidence

        with self.publications.acquire() as publication, closing(
            connect_sqlite(self.user_state.path, strict_durability=True, timeout_ms=1000)
        ) as conn:
            self.user_state._require_ready(conn)
            attach_evidence(conn, publication.path)
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield ImprovementService(conn, initialize_schema=False)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
