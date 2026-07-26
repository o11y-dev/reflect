import json

from reflect.context import ReflectContextService
from reflect.improvements.models import SkillLifecycleState
from reflect.improvements.service import ImprovementService
from reflect.inspection import PatternType, SkillAvailability
from reflect.store.ingest import ingest_local_spans_file
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.sqlite import connect_sqlite
from reflect.task_runs import TaskRunReconciler


def _stage_skill(
    conn,
    *,
    name: str,
    description: str,
    session_ids: list[str] | None = None,
) -> str:
    return ImprovementService(conn).stage_extracted_skills(
        [
            {
                "name": name,
                "description": description,
                "content": f"# {name}\n\n1. Follow the evidence.\n2. Verify the result.",
                "behavior_type": "verification",
            }
        ],
        session_ids=session_ids or [],
        source_agent="codex",
    )[0]


def test_agent_skill_search_filters_existing_registry_without_refreshing(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    try:
        improvements = ImprovementService(conn)
        now = "2026-07-24T10:00:00+00:00"
        conn.execute(
            "INSERT INTO agents(id, name, created_at, updated_at) VALUES ('agent-1', 'codex', ?, ?)",
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions(id, agent_id, started_at, status, title, created_at, updated_at)
            VALUES ('source-session', 'agent-1', ?, 'completed', 'Release source', ?, ?)
            """,
            (now, now, now),
        )
        release_id = _stage_skill(
            conn,
            name="safe-release",
            description="Publish a release with a focused validation gate.",
            session_ids=["source-session"],
        )
        _stage_skill(
            conn,
            name="dependency-review",
            description="Review dependency updates before merging.",
        )
        improvements.workflows.apply(release_id, project_root=project_root)
        improvements.skills.sync_workflow_candidates()
        release_skill = improvements.skills.show("safe-release")
        release_version_id = release_skill.skill.current_version_id
        conn.execute(
            """
            INSERT INTO skill_measurements(
              id, skill_id, skill_version_id, metric_name, before_value,
              after_value, verdict, confidence, measured_at, details_json,
              created_at, updated_at
            ) VALUES ('measurement-1', ?, ?, 'verification_pass_rate',
                      0.5, 1.0, 'improved', 0.9, ?, '{}', ?, ?)
            """,
            (release_skill.skill.id, release_version_id, now, now, now),
        )
        conn.commit()

        service = ReflectContextService(conn)
        answer = service.skills_search(
            query="release",
            lifecycle=SkillLifecycleState.ACTIVE,
            availability=SkillAvailability.INSTALLED,
            source_agent="codex",
            minimum_evidence=1,
        )

        assert answer.count == 1
        assert answer.truncated is False
        assert answer.skills[0].slug == "safe-release"
        assert answer.skills[0].installation_count == 1
        assert answer.skills[0].evidence_count >= 1
        explanation = service.explain(release_version_id)
        assert explanation["kind"] == "skill_version"
        assert explanation["entity"]["source_sessions"][0]["session_id"] == "source-session"
        assert explanation["entity"]["measurements"][0]["id"] == "measurement-1"
    finally:
        conn.close()


def test_agent_pattern_inspection_and_explain_cover_workflows_and_loops(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        workflow_id = _stage_skill(
            conn,
            name="release-recovery",
            description="Recover a release after a failed validation.",
        )
        now = "2026-07-24T12:00:00+00:00"
        loop_id = "loop_release_retry"
        conn.execute(
            """
            INSERT INTO loop_patterns(
              id, fingerprint, kind, title, summary, scope_type, scope_id,
              occurrence_count, affected_session_count, state_change_count,
              confidence, status, evidence_json, first_seen_at, last_seen_at,
              created_at, updated_at
            ) VALUES (?, 'release-retry', 'stalled', 'Release retry loop',
                      'Repeated release validation without a state change.',
                      'user', 'local', 3, 1, 0, 0.9, 'detected', '{}',
                      ?, ?, ?, ?)
            """,
            (loop_id, now, now, now, now),
        )
        conn.commit()

        service = ReflectContextService(conn)
        answer = service.patterns(
            pattern_type=PatternType.ALL,
            query="release",
        )

        assert answer.workflow_count == 1
        assert answer.loop_count == 1
        assert answer.workflows[0].id == workflow_id
        assert answer.loops[0].id == loop_id
        explanation = service.explain(loop_id)
        assert explanation["kind"] == "loop"
        assert explanation["entity"]["loop"]["id"] == loop_id
    finally:
        conn.close()


def test_completed_task_is_reconciled_when_runtime_session_is_ingested(
    tmp_path,
    monkeypatch,
):
    conn = connect_sqlite(tmp_path / "reflect.db")
    spans_path = tmp_path / "late-session.jsonl"
    try:
        candidate_id = _stage_skill(
            conn,
            name="safe-release",
            description="Publish a release with validation.",
        )
        conn.execute(
            "UPDATE workflow_candidates SET status = 'approved' WHERE id = ?",
            (candidate_id,),
        )
        conn.commit()
        monkeypatch.setenv("REFLECT_SESSION_ID", "late-session")

        service = ReflectContextService(conn)
        started = service.begin_task("Publish the safe release with validation", path=tmp_path)
        assert len(started.selected_skills) == 1
        completed = service.complete_task(
            started.task_run_id,
            outcome="success",
            verification_passed=True,
            summary_redacted="Release validation passed.",
        )
        before = service.task_status(started.task_run_id)

        assert completed.linked_to_session is False
        assert before.link_state == "pending_ingestion"
        assert before.session_outcome_recorded is False

        span = {
            "name": "UserPromptSubmit",
            "traceId": "trace-late-session",
            "spanId": "span-late-session",
            "parentSpanId": "",
            "start_time_ns": 100,
            "end_time_ns": 200,
            "attributes": {
                "gen_ai.client.name": "codex",
                "gen_ai.client.session_id": "late-session",
                "gen_ai.client.prompt.text": "Publish the safe release.",
            },
        }
        spans_path.write_text(json.dumps(span) + "\n", encoding="utf-8")
        ingest_local_spans_file(conn, file_path=spans_path)
        assert normalize_pending_raw_events(conn) == {
            "processed": 1,
            "failed": 0,
            "skipped": 0,
        }

        after = service.task_status(started.task_run_id)
        assert after.link_state == "linked"
        assert after.session_outcome_recorded is True
        assert after.skill_usage_recorded_count == 1
        assert conn.execute(
            """
            SELECT outcome FROM session_outcomes
            WHERE session_id = 'late-session' AND source = 'agent_completion'
            """
        ).fetchone()[0] == "success"
        assert conn.execute(
            "SELECT COUNT(*) FROM skill_usage WHERE session_id = 'late-session'"
        ).fetchone()[0] == 1

        repeated = TaskRunReconciler(conn).reconcile(session_ids={"late-session"})
        assert repeated.linked == 0
        assert repeated.unchanged == 1
        explanation = service.explain(started.task_run_id)
        assert explanation["kind"] == "task_run"
        assert explanation["entity"]["link_state"] == "linked"
    finally:
        conn.close()


def test_reconciliation_is_atomic_when_selected_skill_evidence_is_stale(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        now = "2026-07-24T12:00:00+00:00"
        conn.execute(
            "INSERT INTO agents(id, name, created_at, updated_at) VALUES ('agent-1', 'codex', ?, ?)",
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions(id, agent_id, started_at, status, created_at, updated_at)
            VALUES ('stale-session', 'agent-1', ?, 'completed', ?, ?)
            """,
            (now, now, now),
        )
        conn.execute(
            """
            INSERT INTO mcp_task_runs(
              id, runtime_session_id, workspace_path, question_hash,
              selected_skills_json, status, outcome, started_at, completed_at,
              created_at, updated_at
            ) VALUES ('mcp_task_stale', 'stale-session', '/workspace/repo', 'hash',
                      '[{"skill_id":"missing","version_id":"missing","slug":"missing"}]',
                      'completed', 'success', ?, ?, ?, ?)
            """,
            (now, now, now, now),
        )
        conn.commit()

        result = TaskRunReconciler(conn).reconcile(session_ids={"stale-session"})

        assert result.failed == 1
        assert result.linked == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM session_outcomes WHERE session_id = 'stale-session'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT session_linked_at FROM mcp_task_runs WHERE id = 'mcp_task_stale'"
        ).fetchone()[0] is None
    finally:
        conn.close()
