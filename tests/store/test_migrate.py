from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


def test_migrate_applies_initial_schema(tmp_path):
    db_path = tmp_path / "reflect.db"
    conn = connect_sqlite(db_path)
    try:
        applied = migrate(conn)
        assert applied == [1, 2, 3, 4]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "raw_events" in tables
        assert "schema_migrations" in tables
        assert "agents" in tables
        assert "repos" in tables
        assert "files" in tables
        assert "sessions" in tables
        assert "steps" in tables
        assert "llm_calls" in tables
        assert "tool_calls" in tables
        assert "mcp_calls" in tables
        assert "specs" in tables
        assert "requirements" in tables
        assert "evidence" in tables
        assert "memories" in tables
        assert "privacy_findings" in tables
        assert "session_rollups" in tables
        assert "daily_rollups" in tables
        assert "tool_rollups" in tables
        assert "graph_nodes" in tables
        assert "graph_edges" in tables
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        assert migrate(conn) == [1, 2, 3, 4]
        assert migrate(conn) == []
    finally:
        conn.close()


def test_migrate_creates_rollup_indexes(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        indexes = {row[1] for row in conn.execute("PRAGMA index_list('session_rollups')")}
        assert "idx_session_rollups_agent_started" in indexes

        indexes = {row[1] for row in conn.execute("PRAGMA index_list('tool_rollups')")}
        assert "idx_tool_rollups_call_count" in indexes
    finally:
        conn.close()


def test_migrate_creates_graph_foreign_keys(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        foreign_keys = {
            (row[2], row[3], row[4])
            for row in conn.execute("PRAGMA foreign_key_list('graph_edges')")
        }
        assert ("graph_nodes", "target_node_id", "id") in foreign_keys
        assert ("graph_nodes", "source_node_id", "id") in foreign_keys
    finally:
        conn.close()


def test_migrate_creates_canonical_indexes(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        session_indexes = {row[1] for row in conn.execute("PRAGMA index_list('sessions')")}
        assert "idx_sessions_agent_started" in session_indexes
        assert "idx_sessions_repo_started" in session_indexes

        step_indexes = {row[1] for row in conn.execute("PRAGMA index_list('steps')")}
        assert "idx_steps_session_seq" in step_indexes

        llm_indexes = {row[1] for row in conn.execute("PRAGMA index_list('llm_calls')")}
        assert "idx_llm_calls_provider_model" in llm_indexes

        memory_indexes = {row[1] for row in conn.execute("PRAGMA index_list('memories')")}
        assert "idx_live_memories" in memory_indexes
    finally:
        conn.close()


def test_migrate_creates_canonical_foreign_keys(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        step_foreign_keys = {
            (row[2], row[3], row[4])
            for row in conn.execute("PRAGMA foreign_key_list('steps')")
        }
        assert ("sessions", "session_id", "id") in step_foreign_keys

        tool_foreign_keys = {
            (row[2], row[3], row[4])
            for row in conn.execute("PRAGMA foreign_key_list('tool_calls')")
        }
        assert ("steps", "step_id", "id") in tool_foreign_keys
        assert ("sessions", "session_id", "id") in tool_foreign_keys

        evidence_foreign_keys = {
            (row[2], row[3], row[4])
            for row in conn.execute("PRAGMA foreign_key_list('evidence')")
        }
        assert ("requirements", "requirement_id", "id") in evidence_foreign_keys
        assert ("files", "file_id", "id") in evidence_foreign_keys
    finally:
        conn.close()
