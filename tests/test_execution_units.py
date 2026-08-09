from __future__ import annotations

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


def test_explicit_task_runs_split_one_long_lived_session(tmp_path):
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

        assert result == {"execution_units": 2, "explicit": 2, "inferred": 0, "assigned_steps": 2}
        assert archetypes["classified_execution_units"] == 2
        rows = conn.execute(
            """
            SELECT tr.id, tus.step_id, tua.task_archetype_id, tu.eligible
            FROM mcp_task_runs tr
            JOIN execution_units tu ON tu.id = tr.execution_unit_id
            JOIN execution_unit_steps tus ON tus.execution_unit_id = tu.id
            JOIN execution_unit_archetypes tua ON tua.execution_unit_id = tu.id
            ORDER BY tr.id
            """
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("run-review", "review-step", "review", 1),
            ("run-ticket", "ticket-step", "ticket_creation", 1),
        ]
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
