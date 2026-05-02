from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


def test_migrate_applies_initial_schema(tmp_path):
    db_path = tmp_path / "reflect.db"
    conn = connect_sqlite(db_path)
    try:
        applied = migrate(conn)
        assert applied == [1]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "raw_events" in tables
        assert "schema_migrations" in tables
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        assert migrate(conn) == [1]
        assert migrate(conn) == []
    finally:
        conn.close()
