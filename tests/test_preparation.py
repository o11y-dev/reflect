import sqlite3
import threading

import pytest

from reflect.preparation import (
    BackgroundPreparationWorker,
    CallableSnapshotRefresher,
    CommandPreparationPolicy,
    PreparationProgress,
    PreparationStage,
    PreparationState,
    SnapshotAction,
    SnapshotLifecycleService,
    SnapshotStatus,
    SnapshotUnavailableError,
    SQLiteSnapshotInspector,
    report_preparation_progress,
)
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite, connect_sqlite_read_only


def test_background_preparation_worker_completes_and_runs_callbacks():
    callback_results = []
    worker = BackgroundPreparationWorker(lambda: {"sessions": 3})
    worker.add_completion_callback(callback_results.append)

    assert worker.start() is True
    assert worker.wait(timeout=2) is True

    snapshot = worker.snapshot()
    assert snapshot.state is PreparationState.COMPLETE
    assert snapshot.generation == 1
    assert snapshot.result == {"sessions": 3}
    assert callback_results == [{"sessions": 3}]


def test_background_preparation_worker_rejects_duplicate_running_start():
    release = threading.Event()
    worker = BackgroundPreparationWorker(lambda: release.wait(timeout=2) or {})

    assert worker.start() is True
    assert worker.start() is False
    release.set()
    assert worker.wait(timeout=2) is True


def test_background_preparation_worker_exposes_failures():
    def fail():
        raise RuntimeError("preparation failed")

    worker = BackgroundPreparationWorker(fail)
    assert worker.start() is True
    assert worker.wait(timeout=2) is True

    snapshot = worker.snapshot()
    assert snapshot.state is PreparationState.FAILED
    assert snapshot.error == "preparation failed"


def test_report_preparation_progress_emits_a_typed_stage():
    progress = []

    report_preparation_progress(
        progress.append,
        PreparationStage.INGESTING_TRACES,
        "Reading new OTLP traces...",
    )

    assert progress == [
        PreparationProgress(
            stage=PreparationStage.INGESTING_TRACES,
            message="Reading new OTLP traces...",
        )
    ]


def test_snapshot_inspection_is_query_only_and_does_not_create_a_missing_store(
    tmp_path,
):
    db_path = tmp_path / "missing.db"

    status = SQLiteSnapshotInspector(db_path).inspect()

    assert status.state == "missing"
    assert not db_path.exists()


def test_snapshot_inspector_reports_current_schema_and_read_only_connection(
    tmp_path,
):
    db_path = tmp_path / "reflect.db"
    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        now = "2026-07-30T10:00:00+00:00"
        conn.execute(
            "INSERT INTO agents(id, name, created_at, updated_at) VALUES ('agent', 'codex', ?, ?)",
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions(id, agent_id, started_at, status, created_at, updated_at)
            VALUES ('session', 'agent', ?, 'completed', ?, ?)
            """,
            (now, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    status = SQLiteSnapshotInspector(db_path).inspect()
    read_conn = connect_sqlite_read_only(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            read_conn.execute("DELETE FROM sessions")
    finally:
        read_conn.close()

    assert status.ready is True
    assert status.pending_migrations == ()


def test_snapshot_inspector_runs_query_only_command_readiness_probes(tmp_path):
    db_path = tmp_path / "reflect.db"
    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    class StaleProbe:
        def inspect(self, conn):
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
            return ("derived usage state is stale",)

    status = SQLiteSnapshotInspector(
        db_path,
        readiness_probes=(StaleProbe(),),
    ).inspect()

    assert status.state == "stale"
    assert status.schema_ready is False
    assert status.stale_reasons == ("derived usage state is stale",)


def test_snapshot_inspector_reports_pending_migrations_without_applying_them(
    tmp_path,
):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE schema_migrations (
              version INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              applied_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO schema_migrations(version, name, applied_at)
            VALUES (1, '001_initial.sql', '2026-07-30T10:00:00+00:00')
            """
        )
        conn.commit()
    finally:
        conn.close()

    status = SQLiteSnapshotInspector(db_path).inspect()
    conn = sqlite3.connect(db_path)
    try:
        applied = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        conn.close()

    assert status.state == "outdated"
    assert status.pending_migrations
    assert applied == [(1,)]


def test_snapshot_lifecycle_refreshes_only_when_explicitly_requested():
    statuses = [
        SnapshotStatus(exists=False, has_sessions=False),
        SnapshotStatus(
            exists=True,
            has_sessions=True,
            schema_current=True,
        ),
    ]

    class Inspector:
        def inspect(self):
            return statuses.pop(0)

    refresh_calls = []
    service = SnapshotLifecycleService(
        Inspector(),
        refresher=CallableSnapshotRefresher(
            lambda: refresh_calls.append(True) or {"sessions": 1}
        ),
    )

    with pytest.raises(SnapshotUnavailableError, match="missing"):
        service.prepare(requested_refresh=None)
    assert refresh_calls == []

    statuses[:] = [
        SnapshotStatus(exists=False, has_sessions=False),
        SnapshotStatus(
            exists=True,
            has_sessions=True,
            schema_current=True,
        ),
    ]
    result = service.prepare(requested_refresh=True)

    assert result.action is SnapshotAction.REFRESH
    assert result.refreshed is True
    assert refresh_calls == [True]


def test_schema_only_policy_allows_an_empty_current_registry():
    status = SnapshotStatus(
        exists=True,
        has_sessions=False,
        schema_current=True,
    )

    assert CommandPreparationPolicy().decide(
        requested_refresh=None,
        snapshot=status,
    ) is SnapshotAction.ERROR
    assert CommandPreparationPolicy(require_sessions=False).decide(
        requested_refresh=None,
        snapshot=status,
    ) is SnapshotAction.READ
