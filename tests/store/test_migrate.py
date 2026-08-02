import json
from concurrent.futures import ThreadPoolExecutor

from reflect.improvements.repository import _observation_id
from reflect.improvements.rules import MissingVerificationRule
from reflect.improvements.service import ImprovementService
from reflect.store.migrate import load_migrations, migrate
from reflect.store.sqlite import connect_sqlite


def _apply_migrations_through(conn, version: int) -> None:
    for migration in load_migrations():
        if migration.version > version:
            break
        conn.executescript(migration.sql)
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
            VALUES (?, ?, '2026-07-29T00:00:00+00:00')
            """,
            (migration.version, migration.name),
        )
    conn.commit()


def test_migrate_applies_initial_schema(tmp_path):
    db_path = tmp_path / "reflect.db"
    conn = connect_sqlite(db_path)
    try:
        applied = migrate(conn)
        assert applied == [
            1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22
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
        assert "observation_sessions" in tables
        assert "pruned_sessions" in tables
        assert "store_metadata" in tables
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
        session_columns = {
            row[1] for row in conn.execute("PRAGMA table_info('sessions')")
        }
        assert "last_observed_at" in session_columns
        outcome_columns = {
            row[1] for row in conn.execute("PRAGMA table_info('session_outcomes')")
        }
        feedback_columns = {
            row[1] for row in conn.execute("PRAGMA table_info('operator_feedback')")
        }
        assert "pruned_session_id" in outcome_columns
        assert "pruned_session_id" in feedback_columns
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        assert migrate(conn) == [
            1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22
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

    assert sorted(len(result) for result in results) == [0, 22]
    assert sorted(version for result in results for version in result) == list(range(1, 23))


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


def test_migrate_can_preserve_a_caller_owned_transaction(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        _apply_migrations_through(conn, 21)
        conn.execute("BEGIN IMMEDIATE")

        assert migrate(conn, commit=False) == [22]
        assert conn.in_transaction is True
        assert conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 22

        conn.rollback()
        assert conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 21
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

        feedback_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list('operator_feedback')")
        }
        assert "idx_operator_feedback_session" in feedback_indexes
        assert "idx_operator_feedback_pruned_session" in feedback_indexes
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
            1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22
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

        assert migrate(conn) == [17, 18, 19, 20, 21, 22]
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

        assert migrate(conn) == [18, 19, 20, 21, 22]
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

        assert migrate(conn) == [19, 20, 21, 22]
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


def test_retention_migration_keeps_unrecoverable_epoch_start(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        _apply_migrations_through(conn, 21)
        now = "2026-07-29T00:00:00+00:00"
        conn.execute(
            """
            INSERT INTO agents(id, name, created_at, updated_at)
            VALUES ('agent-epoch', 'codex', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions(
              id, agent_id, started_at, status, created_at, updated_at
            ) VALUES ('session-epoch', 'agent-epoch',
                      '1970-01-01T00:00:00+00:00', 'completed', ?, ?)
            """,
            (now, now),
        )
        conn.commit()

        assert migrate(conn) == [22]
        assert conn.execute(
            "SELECT started_at FROM sessions WHERE id = 'session-epoch'"
        ).fetchone()[0] == "1970-01-01T00:00:00+00:00"
    finally:
        conn.close()


def test_retention_migration_marks_capped_attribution_for_one_time_rebuild(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        _apply_migrations_through(conn, 20)
        now = "2026-07-29T00:00:00+00:00"
        conn.execute(
            """
            INSERT INTO agents(id, name, created_at, updated_at)
            VALUES ('agent-ledger', 'codex', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO repos(id, full_name, created_at, updated_at)
            VALUES ('repo-ledger', 'example/ledger', ?, ?)
            """,
            (now, now),
        )
        for index in range(25):
            session_id = f"session-ledger-{index:02d}"
            step_id = f"step-ledger-{index:02d}"
            conn.execute(
                """
                INSERT INTO sessions(
                  id, agent_id, repo_id, started_at, ended_at, status,
                  created_at, updated_at
                ) VALUES (?, 'agent-ledger', 'repo-ledger', ?, ?, 'completed', ?, ?)
                """,
                (session_id, now, now, now, now),
            )
            conn.execute(
                """
                INSERT INTO steps(
                  id, session_id, seq, type, started_at, status,
                  raw_attrs_json, created_at, updated_at
                ) VALUES (?, ?, 1, 'tool_call', ?, 'ok', '{}', ?, ?)
                """,
                (step_id, session_id, now, now, now),
            )
            conn.execute(
                """
                INSERT INTO tool_calls(
                  id, step_id, session_id, tool_name, status,
                  raw_attrs_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'Write', 'ok', '{}', ?, ?)
                """,
                (f"tool-ledger-{index:02d}", step_id, session_id, now, now),
            )
        conn.commit()

        rule = MissingVerificationRule()
        draft = rule.detect(conn)[0]
        observation_id = _observation_id(draft)
        conn.execute(
            """
            INSERT INTO rule_definitions(
              id, version, category, title, description, detector_config_json,
              required_signals_json, lifecycle_state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule.definition.id,
                rule.definition.version,
                rule.definition.category,
                rule.definition.title,
                rule.definition.description,
                json.dumps(rule.definition.detector_config),
                json.dumps(rule.definition.required_signals),
                rule.definition.lifecycle_state,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO observations(
              id, rule_id, rule_version, scope_type, scope_id, repo_id,
              category, title, summary, metric_name, metric_value, metric_unit,
              metric_direction, baseline_value, baseline_query_json,
              impact_score, severity, confidence, first_seen_at, last_seen_at,
              last_evaluated_at, occurrence_count, affected_session_count,
              status, actionability, fingerprint, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)
            """,
            (
                observation_id,
                draft.rule_id,
                draft.rule_version,
                draft.scope_type,
                draft.scope_id,
                draft.repo_id,
                draft.category,
                draft.title,
                draft.summary,
                draft.metric_name,
                draft.metric_value,
                draft.metric_unit,
                draft.metric_direction,
                draft.baseline_value,
                json.dumps(draft.baseline_query),
                draft.impact_score,
                draft.severity.value,
                draft.confidence,
                now,
                now,
                now,
                draft.occurrence_count,
                draft.affected_session_count,
                draft.actionability,
                draft.fingerprint,
                now,
                now,
            ),
        )
        for index, evidence in enumerate(draft.evidence):
            conn.execute(
                """
                INSERT INTO observation_evidence(
                  id, observation_id, entity_type, entity_id, session_id,
                  summary_redacted, confidence, attrs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"evidence-ledger-{index:02d}",
                    observation_id,
                    evidence.entity_type,
                    evidence.entity_id,
                    evidence.session_id,
                    evidence.summary_redacted,
                    evidence.confidence,
                    json.dumps(evidence.attrs),
                    now,
                ),
            )
        conn.commit()

        migration_21 = next(
            migration for migration in load_migrations() if migration.version == 21
        )
        conn.executescript(migration_21.sql)
        conn.execute(
            """
            INSERT INTO schema_migrations(version, name, applied_at)
            VALUES (21, ?, ?)
            """,
            (migration_21.name, now),
        )
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM observation_sessions WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()[0] == 20

        assert migrate(conn) == [22]
        assert conn.execute(
            """
            SELECT value FROM store_metadata
            WHERE key = 'observation_session_ledger_v1'
            """
        ).fetchone()[0] == "pending_rebuild"

        service = ImprovementService(conn)
        assert service.ensure_observation_session_ledger() is True
        assert conn.execute(
            "SELECT COUNT(*) FROM observation_sessions WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()[0] == 25
        assert conn.execute(
            """
            SELECT value FROM store_metadata
            WHERE key = 'observation_session_ledger_v1'
            """
        ).fetchone()[0] == "complete"
    finally:
        conn.close()
