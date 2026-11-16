from contextlib import closing

from test_dashboard_sql_api import _seed_sql_report_db

from reflect.store.evidence_revision import install_evidence_revision_tracking
from reflect.store.snapshots import PublishedSnapshotStore
from reflect.store.sqlite import connect_sqlite, mark_snapshot


def test_derived_change_publishes_without_new_sessions_but_timestamps_do_not(tmp_path):
    db = tmp_path / 'reflect.db'
    _seed_sql_report_db(db)
    with closing(connect_sqlite(db)) as conn:
        conn.execute('CREATE TABLE derived_value (id INTEGER PRIMARY KEY, value INTEGER, updated_at TEXT)')
        conn.execute("INSERT INTO derived_value VALUES (1, 2, 'before')")
        install_evidence_revision_tracking(conn)
        mark_snapshot(conn, ready=True)
        conn.commit()
        def revision():
            return conn.execute("SELECT value FROM store_metadata WHERE key='canonical_mutation_revision'").fetchone()[0]
        initial = revision()
        store = PublishedSnapshotStore(db, reserve_bytes=0)
        first = store.publish(evidence_revision=initial)
        conn.execute("UPDATE derived_value SET updated_at='after'")
        conn.commit()
        assert revision() == initial
        assert store.publish(evidence_revision=revision()).generation == first.generation
        conn.execute('UPDATE derived_value SET value=3')
        conn.commit()
        assert revision() != initial
        second = store.publish(evidence_revision=revision())
        assert second.generation != first.generation
        with closing(connect_sqlite(second.path)) as published:
            assert published.execute('SELECT value FROM derived_value').fetchone()[0] == 3
