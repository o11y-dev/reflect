from __future__ import annotations

import json
import sqlite3

import pytest

from reflect.execution_units import ExecutionUnitService
from reflect.improvements.archetypes import TaskArchetypeService
from reflect.improvements.service import ImprovementService
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite

NOW = "2026-08-02T10:00:00+00:00"


def _base(conn) -> None:
    conn.execute(
        "INSERT INTO agents(id, name, created_at, updated_at) VALUES ('agent', 'codex', ?, ?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO repos(id, full_name, created_at, updated_at) VALUES ('repo', 'o11y/reflect', ?, ?)",
        (NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO workspaces(
          id, root_path, path_hash, label, repo_id, source_key, confidence,
          raw_json, created_at, updated_at
        ) VALUES ('workspace', '/workspace/reflect', 'hash', 'reflect', 'repo',
                  'test', 1, '{}', ?, ?)
        """,
        (NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO sessions(
          id, agent_id, workspace_id, repo_id, started_at, ended_at, status,
          created_at, updated_at
        ) VALUES ('session', 'agent', 'workspace', 'repo', ?,
                  '2026-08-02T12:00:00+00:00', 'completed', ?, ?)
        """,
        (NOW, NOW, NOW),
    )


def _step(
    conn,
    step_id: str,
    seq: int,
    started_at: str,
    summary: str,
    *,
    session_id: str = "session",
) -> None:
    conn.execute(
        """
        INSERT INTO steps(
          id, session_id, seq, type, started_at, status, summary,
          raw_attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, 'tool_call', ?, 'ok', ?, '{}', ?, ?)
        """,
        (step_id, session_id, seq, started_at, summary, NOW, NOW),
    )


def test_reports_do_not_partition_or_verify_one_long_lived_session(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _base(conn)
        _step(conn, "review-step", 1, "2026-08-02T10:10:00+00:00", "Review merge request")
        _step(conn, "ticket-step", 2, "2026-08-02T11:10:00+00:00", "Create Jira ticket")
        conn.executemany(
            """
            INSERT INTO mcp_task_runs(
              id, runtime_session_id, runtime_agent, workspace_path,
              question_hash, selected_skills_json, status, outcome,
              verification_passed, started_at, completed_at, created_at, updated_at
            ) VALUES (?, 'session', 'codex', '/workspace/reflect', ?, '[]',
                      'completed', 'success', 1, ?, ?, ?, ?)
            """,
            [
                (
                    "run-review",
                    "review-hash",
                    "2026-08-02T10:00:00+00:00",
                    "2026-08-02T10:30:00+00:00",
                    NOW,
                    NOW,
                ),
                (
                    "run-ticket",
                    "ticket-hash",
                    "2026-08-02T11:00:00+00:00",
                    "2026-08-02T11:30:00+00:00",
                    NOW,
                    NOW,
                ),
            ],
        )
        conn.commit()

        result = ExecutionUnitService(conn).refresh()
        archetypes = TaskArchetypeService(conn).refresh()

        assert result == {"execution_units": 1, "explicit": 0, "inferred": 1, "assigned_steps": 2}
        assert archetypes["classified_execution_units"] == 1
        unit = conn.execute("SELECT outcome, verification_passed FROM execution_units WHERE eligible = 1").fetchone()
        assert tuple(unit) == (None, None)
        assert conn.execute("SELECT COUNT(DISTINCT execution_unit_id) FROM mcp_task_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM execution_unit_steps").fetchone()[0] == 2
    finally:
        conn.close()


def test_prompt_boundaries_create_conservative_legacy_execution_units(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _base(conn)
        _step(conn, "prompt-review", 1, "2026-08-02T10:05:00+00:00", "Review pull request")
        _step(conn, "work-review", 2, "2026-08-02T10:10:00+00:00", "Review code")
        _step(conn, "prompt-ticket", 3, "2026-08-02T11:05:00+00:00", "Create Jira ticket")
        conn.executemany(
            """
            INSERT INTO conversation_facts(
              id, step_id, session_id, kind, role, content_hash, content_length,
              raw_attrs_json, created_at, updated_at
            ) VALUES (?, ?, 'session', 'prompt', 'user', ?, 20, '{}', ?, ?)
            """,
            [
                ("fact-review", "prompt-review", "review-hash", NOW, NOW),
                ("fact-ticket", "prompt-ticket", "ticket-hash", NOW, NOW),
            ],
        )
        conn.commit()

        result = ExecutionUnitService(conn).refresh()
        TaskArchetypeService(conn).refresh()

        assert result["inferred"] == 2
        rows = conn.execute(
            """
            SELECT tu.source, COUNT(tus.step_id), tua.task_archetype_id, tu.eligible
            FROM execution_units tu
            JOIN execution_unit_steps tus ON tus.execution_unit_id = tu.id
            JOIN execution_unit_archetypes tua ON tua.execution_unit_id = tu.id
            GROUP BY tu.id
            ORDER BY tu.started_at
            """
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("prompt_fallback", 2, "review", 1),
            ("prompt_fallback", 1, "ticket_creation", 1),
        ]
    finally:
        conn.close()


def test_workflow_evidence_refresh_is_scoped_to_selected_sessions(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _base(conn)
        conn.execute(
            """
            INSERT INTO sessions(
              id, agent_id, workspace_id, repo_id, started_at, ended_at, status,
              created_at, updated_at
            ) VALUES ('other-session', 'agent', 'workspace', 'repo', ?,
                      '2026-08-02T12:00:00+00:00', 'completed', ?, ?)
            """,
            (NOW, NOW, NOW),
        )
        _step(conn, "selected-1", 1, NOW, "Review pull request")
        _step(
            conn,
            "other-1",
            1,
            NOW,
            "Create Jira ticket",
            session_id="other-session",
        )
        conn.commit()

        service = ImprovementService(conn)
        service.prepare_workflow_evidence()
        other_before = conn.execute(
            """
            SELECT eu.updated_at, GROUP_CONCAT(eus.step_id)
            FROM execution_units eu
            JOIN execution_unit_steps eus ON eus.execution_unit_id = eu.id
            WHERE eu.session_id = 'other-session'
            GROUP BY eu.id
            """
        ).fetchone()

        _step(conn, "selected-2", 2, "2026-08-02T10:05:00+00:00", "Review code")
        conn.execute(
            """
            DELETE FROM execution_unit_archetypes
            WHERE execution_unit_id IN (
              SELECT id FROM execution_units WHERE session_id = 'session'
            )
            """
        )
        conn.commit()

        result = service.prepare_workflow_evidence(session_ids={"session"})

        assert result["assigned_steps"] == 2
        assert result["classified_execution_units"] == 1
        assert conn.execute(
            """
            SELECT COUNT(*)
            FROM execution_unit_archetypes eua
            JOIN execution_units eu ON eu.id = eua.execution_unit_id
            WHERE eu.session_id = 'session'
            """
        ).fetchone()[0] == 1
        other_after = conn.execute(
            """
            SELECT eu.updated_at, GROUP_CONCAT(eus.step_id)
            FROM execution_units eu
            JOIN execution_unit_steps eus ON eus.execution_unit_id = eu.id
            WHERE eu.session_id = 'other-session'
            GROUP BY eu.id
            """
        ).fetchone()
        assert tuple(other_after) == tuple(other_before)
    finally:
        conn.close()


def test_native_turns_precede_reports_and_late_edits_invalidate_checks(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _base(conn)
        for seq, (step_id, turn, terminal) in enumerate([
            ("check", "turn-a", False), ("edit", "turn-a", False),
            ("stop", "turn-a", True), ("other", "turn-b", True),
        ], 1):
            _step(conn, step_id, seq, f"2026-08-02T10:0{seq}:00+00:00", step_id)
            conn.execute("UPDATE steps SET raw_attrs_json = ? WHERE id = ?", (
                json.dumps({"gen_ai.client.turn_id": turn, "reflect.turn.terminal": terminal}), step_id,
            ))
        conn.execute("""INSERT INTO tool_calls(id, step_id, session_id, tool_name,
            raw_attrs_json, created_at, updated_at) VALUES ('check', 'check', 'session',
            'exec_command', ?, ?, ?)""", (
                json.dumps({"gen_ai.client.command": "pytest", "process.exit.code": 0}), NOW, NOW,
            ))
        conn.execute("""INSERT INTO tool_calls(id, step_id, session_id, tool_name,
            raw_attrs_json, created_at, updated_at) VALUES ('edit', 'edit', 'session',
            'Edit', ?, ?, ?)""", (
                json.dumps({"gen_ai.client.file_path": "src/app.py"}), NOW, NOW,
            ))
        first = ExecutionUnitService(conn).refresh()
        assert first["explicit"] == 2
        row = conn.execute("SELECT outcome, verification_passed, boundary_json FROM execution_units WHERE id IN (SELECT execution_unit_id FROM execution_unit_steps WHERE step_id='check')").fetchone()
        assert tuple(row[:2]) == (None, None)
        assert json.loads(row[2])["assessment"]["verification"] == "invalidated"
        first_metadata = row[2]
        ExecutionUnitService(conn).refresh()
        assert conn.execute("SELECT boundary_json FROM execution_units WHERE id IN (SELECT execution_unit_id FROM execution_unit_steps WHERE step_id='check')").fetchone()[0] == first_metadata
        assert conn.execute("SELECT COUNT(*) FROM execution_unit_steps").fetchone()[0] == 4
    finally:
        conn.close()


def test_approved_scoped_contract_supports_outcome_from_observed_checks(tmp_path):
    from reflect.improvements.models import ObservationDraft, RuleDefinition, Severity
    from reflect.improvements.repository import ImprovementRepository

    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _base(conn)
        _step(conn, "check", 1, NOW, "Run checks")
        conn.execute("UPDATE steps SET raw_attrs_json = ? WHERE id='check'", (
            json.dumps({"gen_ai.client.turn_id": "turn", "reflect.turn.terminal": True}),
        ))
        conn.execute("""INSERT INTO tool_calls(id, step_id, session_id, tool_name,
            raw_attrs_json, created_at, updated_at) VALUES ('check', 'check', 'session',
            'exec_command', ?, ?, ?)""", (
                json.dumps({"gen_ai.client.command": "pytest", "process.exit.code": 0}), NOW, NOW,
            ))
        repository = ImprovementRepository(conn)
        repository.sync_rule_definitions([RuleDefinition(id="test", version=1, category="test", title="test", description="test")], now=NOW)
        observation = repository.upsert_observation(ObservationDraft(
            rule_id="test", rule_version=1, scope_type="repo", scope_id="repo", repo_id="repo",
            fingerprint="test", category="test", title="test", summary="test",
            metric_name="test", metric_value=1, metric_unit="runs", metric_direction="increase",
            impact_score=0, severity=Severity.LOW, confidence=1,
        ), now=NOW)
        contract = {"slug": "run-tests", "workflow_contract": {"signature_hash": "run-tests", "applicability": {"repo_id": "repo"}, "milestones": [{"id": "verify"}]}}
        conn.execute("""INSERT INTO workflow_candidates(id, observation_id, action_type, title,
            hypothesis, scope, content_json, target_metric, created_at, updated_at)
            VALUES ('candidate', ?, 'skill', 'test', 'test', 'repo', ?, 'test', ?, ?)""",
            (observation, json.dumps(contract), NOW, NOW))
        conn.execute("""INSERT INTO workflow_versions(id, candidate_id, version, content_json,
            content_hash, status, created_at) VALUES ('version', 'candidate', 1, ?, 'hash', 'pending', ?)""",
            (json.dumps(contract), NOW))
        service = ExecutionUnitService(conn)
        service.refresh()
        service.assess_contracts()
        assert conn.execute("SELECT outcome FROM execution_units WHERE eligible=1").fetchone()[0] is None
        conn.execute("UPDATE workflow_versions SET status='approved'")
        membership = conn.execute("SELECT * FROM execution_unit_steps").fetchall()
        incremental = ImprovementService(conn, rules=[])
        assert incremental.refresh(session_ids=set(), analysis_session_ids=set())["execution_units"] == 0
        assert conn.execute("SELECT * FROM execution_unit_steps").fetchall() == membership
        assert conn.execute("SELECT outcome FROM execution_units WHERE eligible=1").fetchone()[0] == "success"
        conn.execute("UPDATE workflow_versions SET status='pending'")
        incremental.refresh(session_ids=set(), analysis_session_ids=set())
        assert conn.execute("SELECT outcome FROM execution_units WHERE eligible=1").fetchone()[0] is None
        conn.execute("UPDATE workflow_versions SET status='approved'")
        # A failed mutation attempt is useful for discovery, never acceptance.
        contract["workflow_contract"]["milestones"].insert(0, {"id": "act"})
        conn.execute("UPDATE workflow_versions SET content_json=?", (json.dumps(contract),))
        conn.execute("INSERT INTO tool_calls(id,step_id,session_id,tool_name,status,created_at,updated_at) VALUES('attempt','check','session','Edit','error',?,?)", (NOW,NOW))
        service.assess_contracts()
        assert conn.execute("SELECT outcome FROM execution_units WHERE eligible=1").fetchone()[0] is None
        conn.execute("DELETE FROM tool_calls WHERE id='attempt'")
        contract["workflow_contract"]["milestones"].pop(0)
        conn.execute("UPDATE workflow_versions SET content_json=?", (json.dumps(contract),))
        conn.execute("UPDATE tool_calls SET raw_attrs_json = ?", (json.dumps({"gen_ai.client.command": "pytest", "process.exit.code": 1}),))
        service.refresh()
        service.assess_contracts()
        assert conn.execute("SELECT outcome FROM execution_units WHERE eligible=1").fetchone()[0] == "failure"
    finally:
        conn.close()


def _additional_session(conn, session_id, *, workspace_id="workspace", repo_id="repo"):
    conn.execute(
        """INSERT INTO sessions(id,agent_id,workspace_id,repo_id,started_at,ended_at,status,created_at,updated_at)
           VALUES (?, 'agent', ?, ?, ?, '2026-08-02T12:00:00+00:00', 'completed', ?, ?)""",
        (session_id, workspace_id, repo_id, NOW, NOW, NOW),
    )


def test_incremental_refresh_leaves_unrelated_membership_and_empty_scope_untouched(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _base(conn)
        _additional_session(conn, "unrelated", workspace_id=None, repo_id=None)
        _step(conn, "initial", 1, NOW, "Review code")
        _step(conn, "unrelated-step", 1, NOW, "Review code", session_id="unrelated")
        service = ImprovementService(conn, rules=[])
        assert service.refresh()["execution_units"] == 2
        conn.execute("UPDATE execution_units SET updated_at='untouched' WHERE session_id='unrelated'")
        conn.execute("""CREATE TEMP TRIGGER preserve_unrelated BEFORE DELETE ON execution_unit_steps
                        WHEN OLD.session_id='unrelated' BEGIN SELECT RAISE(ABORT, 'unrelated membership rewritten'); END""")
        _step(conn, "new", 2, "2026-08-02T10:01:00+00:00", "Review code")
        assert service.refresh(session_ids={"session"})["execution_units"] == 1
        assert conn.execute("SELECT updated_at FROM execution_units WHERE session_id='unrelated'").fetchone()[0] == "untouched"
        before = conn.execute("SELECT * FROM execution_unit_steps ORDER BY step_id").fetchall()
        assert service.refresh(session_ids=set())["execution_units"] == 0
        assert conn.execute("SELECT * FROM execution_unit_steps ORDER BY step_id").fetchall() == before
        conn.execute("DROP TRIGGER preserve_unrelated")
        assert service.refresh()["execution_units"] == 2
        assert conn.execute("SELECT updated_at FROM execution_units WHERE session_id='unrelated'").fetchone()[0] != "untouched"
    finally:
        conn.close()


def test_unchanged_full_refresh_preserves_every_membership_without_writes(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _base(conn)
        _step(conn, "first", 1, NOW, "Review code")
        _step(conn, "second", 2, "2026-08-02T10:01:00+00:00", "Check code")
        conn.execute("UPDATE sessions SET input_tokens=123,output_tokens=45")
        service = ExecutionUnitService(conn)
        service.refresh()
        before = conn.execute("SELECT * FROM execution_unit_steps ORDER BY step_id").fetchall()
        for operation in ("INSERT", "UPDATE", "DELETE"):
            conn.execute(f"""CREATE TEMP TRIGGER preserve_membership_{operation}
                AFTER {operation} ON execution_unit_steps
                BEGIN SELECT RAISE(ABORT, 'unchanged membership rewritten'); END""")
        assert service.refresh()["assigned_steps"] == 2
        assert service.refresh(session_ids={"session"})["assigned_steps"] == 2
        assert conn.execute("SELECT * FROM execution_unit_steps ORDER BY step_id").fetchall() == before
        assert tuple(conn.execute("SELECT input_tokens,output_tokens FROM sessions").fetchone()) == (123, 45)
    finally:
        conn.close()


@pytest.mark.parametrize("scoped", [False, True])
def test_changed_membership_is_reconciled_before_assessment_and_supersession(tmp_path, scoped):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _base(conn)
        for seq, (step_id, turn, terminal) in enumerate([
            ("anchor", "a", False), ("check", "a", False),
            ("stop-a", "a", True), ("stop-b", "b", True),
        ]):
            _step(conn, step_id, seq, f"2026-08-02T10:0{seq}:00+00:00", step_id)
            conn.execute("UPDATE steps SET raw_attrs_json=? WHERE id=?", (
                json.dumps({"gen_ai.client.turn_id": turn, "reflect.turn.terminal": terminal}), step_id,
            ))
        conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,
            raw_attrs_json,created_at,updated_at) VALUES('check','check','session','exec_command',?,?,?)""",
            (json.dumps({"gen_ai.client.command": "pytest", "process.exit.code": 0}), NOW, NOW))
        service = ExecutionUnitService(conn)
        service.refresh()
        original = dict(conn.execute("SELECT step_id,execution_unit_id FROM execution_unit_steps"))
        first, second = original["anchor"], original["stop-b"]
        scope = {"session_ids": {"session"}} if scoped else {}

        conn.execute("""UPDATE steps SET raw_attrs_json=json_set(raw_attrs_json,
            '$."gen_ai.client.turn_id"','b') WHERE id='check'""")
        service.refresh(**scope)
        assert conn.execute("SELECT verification_passed FROM execution_units WHERE id=?", (first,)).fetchone()[0] is None
        assert conn.execute("SELECT verification_passed FROM execution_units WHERE id=?", (second,)).fetchone()[0] == 1
        assert conn.execute("SELECT execution_unit_id FROM execution_unit_steps WHERE step_id='check'").fetchone()[0] == second

        conn.execute("""UPDATE steps SET raw_attrs_json=json_set(raw_attrs_json,
            '$."gen_ai.client.turn_id"','b') WHERE id IN ('anchor','stop-a')""")
        service.refresh(**scope)
        retired = conn.execute("SELECT eligible,boundary_json FROM execution_units WHERE id=?", (first,)).fetchone()
        assert retired[0] == 0
        assert json.loads(retired[1])["superseded_by"] == [second]
        conn.execute("DELETE FROM steps WHERE id='check'")
        service.refresh(**scope)
        assert dict(conn.execute("SELECT step_id,execution_unit_id FROM execution_unit_steps")) == {
            "anchor": second, "stop-a": second, "stop-b": second,
        }
        assert conn.execute("SELECT verification_passed FROM execution_units WHERE id=?", (second,)).fetchone()[0] is None
    finally:
        conn.close()


def test_membership_reconciliation_remains_in_refresh_transaction(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _base(conn)
        _step(conn, "work", 1, NOW, "Work")
        service = ExecutionUnitService(conn)
        service.refresh()
        before = conn.execute("SELECT * FROM execution_unit_steps").fetchall()
        conn.execute("UPDATE steps SET raw_attrs_json=? WHERE id='work'", (
            json.dumps({"gen_ai.client.turn_id": "new-boundary"}),
        ))
        conn.execute("""CREATE TEMP TRIGGER reject_assignment AFTER INSERT ON execution_unit_steps
            BEGIN SELECT RAISE(ABORT, 'simulated assignment failure'); END""")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="simulated assignment failure"):
            service.refresh()
        conn.rollback()
        assert conn.execute("SELECT * FROM execution_unit_steps").fetchall() == before
    finally:
        conn.close()


@pytest.mark.parametrize("target", ["before-source", "z-after-source"])
def test_full_refresh_moves_step_between_sessions_in_either_processing_order(tmp_path, target):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _base(conn)
        _additional_session(conn, target)
        _step(conn, "moved", 1, NOW, "Work")
        service = ExecutionUnitService(conn)
        service.refresh()
        original = conn.execute("SELECT execution_unit_id FROM execution_unit_steps").fetchone()[0]
        conn.execute("UPDATE steps SET session_id=? WHERE id='moved'", (target,))
        service.refresh()
        membership = conn.execute("SELECT execution_unit_id,session_id FROM execution_unit_steps").fetchone()
        assert membership[1] == target
        assert membership[0] != original
        retired = conn.execute("SELECT eligible,boundary_json FROM execution_units WHERE id=?", (original,)).fetchone()
        assert retired[0] == 0
        assert json.loads(retired[1])["superseded_by"] == [membership[0]]
    finally:
        conn.close()


@pytest.mark.parametrize("consumer_workspace", ["workspace", None])
def test_incremental_late_edit_reassesses_closed_peer_and_retraction_recovers(tmp_path, consumer_workspace):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _base(conn)
        conn.execute("UPDATE sessions SET workspace_id=? WHERE id='session'", (consumer_workspace,))
        _additional_session(conn, "changed")
        _additional_session(conn, "later")
        _step(conn, "check", 1, "2026-08-02T10:01:00+00:00", "Run checks")
        _step(conn, "stop", 2, "2026-08-02T10:05:00+00:00", "Done")
        conn.execute("UPDATE steps SET raw_attrs_json=? WHERE id='check'", (json.dumps({"gen_ai.client.turn_id": "verified"}),))
        conn.execute("UPDATE steps SET raw_attrs_json=? WHERE id='stop'", (json.dumps({"gen_ai.client.turn_id": "verified", "reflect.turn.terminal": True}),))
        conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,raw_attrs_json,created_at,updated_at)
                        VALUES ('check','check','session','exec_command',?,?,?)""",
                     (json.dumps({"gen_ai.client.command": "pytest", "process.exit.code": 0}), NOW, NOW))
        _step(conn, "later-change", 1, "2026-08-02T10:20:00+00:00", "Work", session_id="changed")
        _step(conn, "independent", 1, "2026-08-02T11:00:00+00:00", "Work", session_id="later")
        service = ImprovementService(conn, rules=[])
        service.refresh()
        query = "SELECT verification_passed FROM execution_units WHERE session_id='session' AND eligible=1"
        assert conn.execute(query).fetchone()[0] == 1
        conn.execute("UPDATE execution_units SET updated_at='untouched' WHERE session_id='later'")
        _step(conn, "late-edit", 2, "2026-08-02T10:03:00+00:00", "Edit", session_id="changed")
        conn.execute("UPDATE steps SET ended_at=started_at WHERE id='late-edit'")
        conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,raw_attrs_json,created_at,updated_at)
                        VALUES ('late-edit','late-edit','changed','Edit',?,?,?)""",
                     (json.dumps({"gen_ai.client.file_path": "src/app.py"}), NOW, NOW))
        assert service.execution_units.affected_session_ids({"changed"}) == {"session", "changed"}
        service.refresh(session_ids={"changed"})
        assert conn.execute(query).fetchone()[0] is None
        assert conn.execute("SELECT updated_at FROM execution_units WHERE session_id='later'").fetchone()[0] == "untouched"
        conn.execute("DELETE FROM steps WHERE id='late-edit'")
        conn.execute("UPDATE sessions SET workspace_id=NULL,repo_id=NULL WHERE id='changed'")
        assert service.execution_units.affected_session_ids({"changed"}) == {"session", "changed"}
        service.refresh(session_ids={"changed"})
        assert conn.execute(query).fetchone()[0] == 1
        assert conn.execute("SELECT updated_at FROM execution_units WHERE session_id='later'").fetchone()[0] == "untouched"
    finally:
        conn.close()


def test_report_only_incremental_refresh_relinks_annotation_without_verifying_it(tmp_path):
    from reflect.execution_units import ExecutionUnitRepository

    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _base(conn)
        _step(conn, "observed", 1, NOW, "Review code")
        service = ImprovementService(conn, rules=[])
        service.refresh()
        observed = conn.execute("SELECT id FROM execution_units WHERE eligible=1").fetchone()[0]
        conn.execute("""INSERT INTO mcp_task_runs(id,runtime_session_id,runtime_agent,workspace_path,
            question_hash,selected_skills_json,selected_memories_json,status,outcome,started_at,created_at,updated_at)
            VALUES ('report','session','codex','/workspace/reflect','hash','[]','[]','completed','success',?,?,?)""",
                     (NOW, NOW, NOW))
        placeholder = ExecutionUnitRepository(conn).sync_task_run("report", "session", now=NOW)
        assert placeholder != observed
        service.refresh(session_ids={"session"})
        assert conn.execute("SELECT execution_unit_id FROM mcp_task_runs WHERE id='report'").fetchone()[0] == observed
        assert tuple(conn.execute("SELECT outcome,verification_passed FROM execution_units WHERE id=?", (observed,)).fetchone()) == (None, None)
        assert conn.execute("SELECT eligible FROM execution_units WHERE id=?", (placeholder,)).fetchone()[0] == 0
    finally:
        conn.close()
