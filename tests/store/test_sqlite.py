import sqlite3

from reflect.store.sqlite import connect_sqlite, optimize


def _pragma(conn: sqlite3.Connection, key: str):
    return conn.execute(f"PRAGMA {key};").fetchone()[0]


def test_connect_sqlite_applies_default_pragmas(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        assert _pragma(conn, "foreign_keys") == 1
        assert _pragma(conn, "journal_mode") == "wal"
        assert _pragma(conn, "synchronous") == 1  # NORMAL
        assert _pragma(conn, "wal_autocheckpoint") == 1000
        assert _pragma(conn, "busy_timeout") == 5000
    finally:
        conn.close()


def test_connect_sqlite_strict_durability(tmp_path):
    conn = connect_sqlite(tmp_path / "strict.db", strict_durability=True)
    try:
        assert _pragma(conn, "synchronous") == 2  # FULL
    finally:
        conn.close()


def test_optimize_runs(tmp_path):
    conn = connect_sqlite(tmp_path / "opt.db")
    try:
        optimize(conn)
    finally:
        conn.close()
