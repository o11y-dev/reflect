from concurrent.futures import ThreadPoolExecutor

from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


def test_migrate_applies_initial_schema(tmp_path):
    db_path = tmp_path / "reflect.db"
    conn = connect_sqlite(db_path)
    try:
        applied = migrate(conn)
        assert applied == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "raw_events" in tables
        assert "schema_migrations" in tables
        assert "agents" in tables
        assert "repos" in tables
        assert "workspaces" in tables
        assert "files" in tables
        assert "sessions" in tables
        assert "steps" in tables
        assert "llm_calls" in tables
        assert "tool_calls" in tables
        assert "mcp_calls" in tables
        assert "conversation_facts" in tables
        assert "agent_events" in tables
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
        assert "memory_fts" in tables
        assert "memory_candidates" in tables
        assert "source_ingestion_state" in tables
        assert "rule_definitions" in tables
        assert "observations" in tables
        assert "observation_evidence" in tables
        assert "workflow_candidates" in tables
        assert "workflow_versions" in tables
        assert "interventions" in tables
        assert "measurements" in tables
        assert "operator_feedback" in tables
        assert "session_task_archetypes" in tables
        assert "evaluations" in tables
        assert "loop_patterns" in tables
        assert "loop_occurrences" in tables
        assert "skills" in tables
        assert "skill_versions" in tables
        assert "skill_evidence" in tables
        assert "skill_installations" in tables
        assert "skill_usage" in tables
        assert "skill_measurements" in tables
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        assert migrate(conn) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
        assert migrate(conn) == []
    finally:
        conn.close()


def test_migrate_serializes_concurrent_background_requests(tmp_path):
    db_path = tmp_path / "reflect.db"

    def run_migration():
        conn = connect_sqlite(db_path)
        try:
            return migrate(conn)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run_migration(), range(2)))

    assert sorted(len(result) for result in results) == [0, 15]
    assert sorted(version for result in results for version in result) == list(range(1, 16))


def test_migrate_uses_read_only_fast_path_when_schema_is_current(tmp_path):
    db_path = tmp_path / "reflect.db"
    writer = connect_sqlite(db_path)
    reader = connect_sqlite(db_path)
    try:
        migrate(writer)
        writer.execute("BEGIN IMMEDIATE")

        assert migrate(reader) == []
    finally:
        writer.rollback()
        reader.close()
        writer.close()


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
        assert "idx_sessions_workspace_started" in session_indexes
        assert "idx_sessions_parent_started" in session_indexes

        step_indexes = {row[1] for row in conn.execute("PRAGMA index_list('steps')")}
        assert "idx_steps_session_seq" in step_indexes
        assert "idx_steps_session_type" in step_indexes
        assert "idx_steps_hook_event_id" in step_indexes
        assert "idx_steps_hook_contract" in step_indexes
        assert "idx_steps_native_context" in step_indexes
        assert "idx_steps_origin_kind" in step_indexes

        raw_indexes = {row[1] for row in conn.execute("PRAGMA index_list('raw_events')")}
        assert "idx_raw_events_origin_kind" in raw_indexes
        assert "idx_raw_events_session_source_time" in raw_indexes

        llm_indexes = {row[1] for row in conn.execute("PRAGMA index_list('llm_calls')")}
        assert "idx_llm_calls_provider_model" in llm_indexes
        assert "idx_llm_calls_session_request_model" in llm_indexes

        tool_indexes = {row[1] for row in conn.execute("PRAGMA index_list('tool_calls')")}
        assert "idx_tool_calls_session_status" in tool_indexes
        assert "idx_tool_calls_input_fingerprint" in tool_indexes

        mcp_indexes = {row[1] for row in conn.execute("PRAGMA index_list('mcp_calls')")}
        assert "idx_mcp_calls_session_status" in mcp_indexes
        assert "idx_mcp_calls_session_tool_call" in mcp_indexes

        graph_indexes = {row[1] for row in conn.execute("PRAGMA index_list('graph_nodes')")}
        assert "idx_graph_nodes_session_kind" in graph_indexes
        assert "idx_graph_nodes_kind_identity" in graph_indexes
        graph_edge_indexes = {row[1] for row in conn.execute("PRAGMA index_list('graph_edges')")}
        assert "idx_graph_edges_session_kind" in graph_edge_indexes

        memory_indexes = {row[1] for row in conn.execute("PRAGMA index_list('memories')")}
        assert "idx_live_memories" in memory_indexes
        assert "idx_memories_session_type_seen" in memory_indexes
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


def test_database_doctor_reports_healthy_migrated_store(tmp_path):
    from reflect.store.doctor import inspect_database

    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        status = inspect_database(conn)
    finally:
        conn.close()

    assert status["ok"] is True
    assert status["pending_migrations"] == []
    assert status["foreign_key_issues"] == []
    assert status["pragma_ok"] is True


def test_database_doctor_reports_pending_migrations(tmp_path):
    from reflect.store.doctor import inspect_database

    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        status = inspect_database(conn)
    finally:
        conn.close()

    assert status["ok"] is False
    assert status["applied_migrations"] == []
    assert status["pending_migrations"] == [
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15
    ]
