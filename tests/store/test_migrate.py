import json
from concurrent.futures import ThreadPoolExecutor

from reflect.store.migrate import load_migrations, migrate
from reflect.store.sqlite import connect_sqlite


def test_migrate_applies_initial_schema(tmp_path):
    db_path = tmp_path / "reflect.db"
    conn = connect_sqlite(db_path)
    try:
        applied = migrate(conn)
        assert applied == [
            1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19
        ]
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
        assert "mcp_task_runs" in tables
        assert "change_reviews" in tables
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        assert migrate(conn) == [
            1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19
        ]
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

    assert sorted(len(result) for result in results) == [0, 19]
    assert sorted(version for result in results for version in result) == list(range(1, 20))


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
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19
    ]


def test_migrate_adds_task_reconciliation_state_without_losing_phase_one_runs(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        for migration in load_migrations():
            if migration.version > 16:
                break
            conn.executescript(migration.sql)
            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
                VALUES (?, ?, '2026-07-24T00:00:00+00:00')
                """,
                (migration.version, migration.name),
            )
        conn.execute(
            """
            INSERT INTO mcp_task_runs(
              id, workspace_path, question_hash, selected_skills_json,
              status, started_at, created_at, updated_at
            ) VALUES ('mcp_task_existing', '/workspace/repo', 'hash', '[]',
                      'started', '2026-07-24T00:00:00+00:00',
                      '2026-07-24T00:00:00+00:00', '2026-07-24T00:00:00+00:00')
            """
        )
        conn.commit()

        assert migrate(conn) == [17, 18, 19]
        row = conn.execute(
            """
            SELECT id, session_linked_at, session_outcome_recorded,
                   skill_usage_recorded_count
            FROM mcp_task_runs
            """
        ).fetchone()

        assert tuple(row) == ("mcp_task_existing", None, 0, 0)
    finally:
        conn.close()


def test_migrate_adds_change_reviews_without_changing_phase_two_task_runs(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        for migration in load_migrations():
            if migration.version > 17:
                break
            conn.executescript(migration.sql)
            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
                VALUES (?, ?, '2026-07-25T00:00:00+00:00')
                """,
                (migration.version, migration.name),
            )
        conn.execute(
            """
            INSERT INTO mcp_task_runs(
              id, workspace_path, question_hash, selected_skills_json,
              status, started_at, created_at, updated_at,
              session_outcome_recorded, skill_usage_recorded_count
            ) VALUES ('mcp_task_phase_two', '/workspace/repo', 'hash', '[]',
                      'completed', '2026-07-25T00:00:00+00:00',
                      '2026-07-25T00:00:00+00:00',
                      '2026-07-25T00:00:00+00:00', 1, 2)
            """
        )
        conn.commit()

        assert migrate(conn) == [18, 19]
        assert tuple(
            conn.execute(
                """
                SELECT id, status, session_outcome_recorded,
                       skill_usage_recorded_count
                FROM mcp_task_runs
                """
            ).fetchone()
        ) == ("mcp_task_phase_two", "completed", 1, 2)
        assert conn.execute(
            "SELECT COUNT(*) FROM change_reviews"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_migrate_reingests_codex_desktop_logs_and_removes_noisy_trace_session(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        for migration in load_migrations():
            if migration.version > 18:
                break
            conn.executescript(migration.sql)
            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
                VALUES (?, ?, '2026-07-26T00:00:00+00:00')
                """,
                (migration.version, migration.name),
            )
        now = "2026-07-26T00:00:00+00:00"
        conn.execute(
            """
            INSERT INTO agents(id, name, kind, raw_json, created_at, updated_at)
            VALUES ('codex-desktop', 'Codex Desktop', 'native', '{}', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions(
              id, agent_id, started_at, status, source_kind, source_ref,
              created_at, updated_at
            ) VALUES (
              'session-noisy', 'codex-desktop', ?, 'ok', 'otlp_traces_json',
              '/tmp/otel-traces.json', ?, ?
            )
            """,
            (now, now, now),
        )
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, raw_attrs_json,
              created_at, updated_at
            ) VALUES (
              'step-noisy', 'session-noisy', 1, 'FramedRead::poll_next', ?,
              'ok', '{"service.name":"Codex Desktop","code.module.name":"h2::codec"}',
              ?, ?
            )
            """,
            (now, now, now),
        )
        conn.execute(
            """
            INSERT INTO raw_events(
              id, source_id, source_type, event_type, trace_id, span_id,
              observed_at, received_at, attrs_json, body_json,
              normalized_status, content_hash, created_at
            ) VALUES (
              'raw-noisy', '/tmp/otel-traces.json', 'otlp_traces_json',
              'FramedRead::poll_next', 'trace', 'span', ?, ?,
              '{"service.name":"Codex Desktop","code.module.name":"h2::codec"}',
              '{}', 'ok', 'hash', ?
            )
            """,
            (now, now, now),
        )
        conn.execute(
            """
            INSERT INTO session_rollups(session_id, agent, started_at, updated_at)
            VALUES ('session-noisy', 'Codex Desktop', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO graph_nodes(
              id, kind, label, session_id, attrs_json, created_at, updated_at
            ) VALUES ('node-noisy', 'session', 'noisy', 'session-noisy', '{}', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO daily_rollups(
              day, agent, session_count, prompt_count, updated_at
            ) VALUES ('2026-07-26', 'claude', 1, 1, ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO tool_rollups(
              tool_name, agent, call_count, success_count, updated_at
            ) VALUES ('Read', 'claude', 1, 1, ?)
            """,
            (now,),
        )
        conn.executemany(
            """
            INSERT INTO source_ingestion_state(
              source_id, source_type, size_bytes, modified_ns, updated_at
            ) VALUES (?, ?, 100, 100, ?)
            """,
            [
                ("/tmp/otel-traces.json", "otlp_traces_json", now),
                ("/tmp/otel-logs.json", "otlp_logs_json", now),
            ],
        )
        conn.commit()

        assert migrate(conn) == [19]
        assert conn.execute(
            "SELECT COUNT(*) FROM source_ingestion_state"
        ).fetchone()[0] == 0
        raw = conn.execute(
            "SELECT normalized_status, session_id, attrs_json FROM raw_events"
        ).fetchone()
        assert raw[:2] == ("ignored", None)
        assert json.loads(raw[2])["reflect.telemetry.classification"] == "runtime_internal"
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM session_rollups").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0] == 0
        assert conn.execute(
            "SELECT agent FROM daily_rollups"
        ).fetchone()[0] == "claude"
        assert conn.execute(
            "SELECT agent FROM tool_rollups"
        ).fetchone()[0] == "claude"
        assert conn.execute(
            """
            SELECT COUNT(*) FROM maintenance_tasks
            WHERE task = 'rebuild_rollups_after_codex_desktop_otel'
            """
        ).fetchone()[0] == 1
    finally:
        conn.close()
