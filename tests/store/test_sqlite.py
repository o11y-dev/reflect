import sqlite3

import pytest

import reflect.store.sqlite as sqlite_store
from reflect.store.sqlite import (
    DEFAULT_BUSY_TIMEOUT_MS,
    SQLiteBackupService,
    backup_sqlite,
    connect_sqlite,
    optimize,
)


def _pragma(conn: sqlite3.Connection, key: str):
    return conn.execute(f"PRAGMA {key};").fetchone()[0]


def test_connect_sqlite_applies_default_pragmas(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        assert _pragma(conn, "foreign_keys") == 1
        assert _pragma(conn, "journal_mode") == "wal"
        assert _pragma(conn, "synchronous") == 1  # NORMAL
        assert _pragma(conn, "wal_autocheckpoint") == 1000
        assert _pragma(conn, "busy_timeout") == DEFAULT_BUSY_TIMEOUT_MS
    finally:
        conn.close()


def test_connect_sqlite_strict_durability(tmp_path):
    conn = connect_sqlite(tmp_path / "strict.db", strict_durability=True)
    try:
        assert _pragma(conn, "synchronous") == 2  # FULL
    finally:
        conn.close()


def test_connect_sqlite_reads_while_wal_writer_is_active(tmp_path, monkeypatch):
    db_path = tmp_path / "concurrent.db"
    writer = connect_sqlite(db_path)
    writer.execute("CREATE TABLE values_table(value INTEGER)")
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO values_table VALUES (1)")
    monkeypatch.setattr(sqlite_store, "DEFAULT_BUSY_TIMEOUT_MS", 50)

    reader = connect_sqlite(db_path)
    try:
        assert reader.execute("SELECT COUNT(*) FROM values_table").fetchone()[0] == 0
    finally:
        reader.close()
        writer.rollback()
        writer.close()


def test_backup_service_does_not_publish_a_partial_backup(tmp_path):
    class FailingSource:
        def backup(self, target):
            target.execute("CREATE TABLE partial(value INTEGER)")
            target.commit()
            raise sqlite3.OperationalError("simulated backup failure")

        def close(self):
            return None

    target_path = tmp_path / "reflect.db.backup"
    service = SQLiteBackupService(source_factory=lambda _path: FailingSource())

    with pytest.raises(sqlite3.OperationalError, match="simulated backup failure"):
        service.create(tmp_path / "reflect.db", target_path)

    assert not target_path.exists()
    assert not any(path.name.endswith(".partial") for path in tmp_path.iterdir())


def test_backup_service_reports_page_progress(tmp_path):
    source_path = tmp_path / "reflect.db"
    target_path = tmp_path / "reflect.db.backup"
    conn = connect_sqlite(source_path)
    try:
        conn.execute("CREATE TABLE samples(value TEXT)")
        conn.executemany(
            "INSERT INTO samples(value) VALUES (?)",
            [(f"sample-{index}",) for index in range(100)],
        )
        conn.commit()
    finally:
        conn.close()

    progress = []
    backup_sqlite(source_path, target_path, progress=progress.append)

    assert progress
    assert progress[-1].remaining_pages == 0
    assert progress[-1].percent_complete == 100
    backup = sqlite3.connect(target_path)
    try:
        assert backup.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 100
    finally:
        backup.close()


def test_optimize_runs(tmp_path):
    conn = connect_sqlite(tmp_path / "opt.db")
    try:
        optimize(conn)
    finally:
        conn.close()
