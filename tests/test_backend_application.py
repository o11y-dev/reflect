from contextlib import closing

from test_dashboard_sql_api import _seed_sql_report_db

from reflect.application import ReflectApplication
from reflect.backend_runtime import action_store_path
from reflect.store.snapshots import PublishedSnapshotStore
from reflect.store.sqlite import connect_sqlite, mark_snapshot
from reflect.store.user_state import UserStateStore
from reflect.store.user_state_migration import UserStateMigration


def test_application_creates_and_reads_draft_while_evidence_writer_is_busy(tmp_path):
    path = tmp_path / 'reflect.db'
    _seed_sql_report_db(path)
    state = UserStateStore(action_store_path(path))
    UserStateMigration(path, state).run()
    with closing(connect_sqlite(path)) as conn:
        mark_snapshot(conn, ready=True)
    PublishedSnapshotStore(path, reserve_bytes=0).publish(evidence_revision='initial')
    app = ReflectApplication(path)
    with closing(connect_sqlite(path)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        created = app.create_workflow(content={
            'slug': 'independent-draft', 'description': 'Saved while refreshing',
            'steps': ['Verify inputs'], 'verification': [], 'abstain_when': [],
        })
        with app.read() as service:
            assert service.workflows.show(created['id']).content['steps'] == ['Verify inputs']
            import sqlite3

            from reflect.improvements.repository import ImprovementRepository
            with closing(sqlite3.connect(':memory:')) as unrelated:
                assert ImprovementRepository(unrelated).review_state is None
            assert service.repository.review_state is not None
        writer.rollback()
