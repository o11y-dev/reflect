from __future__ import annotations

import json

import pytest

from reflect.context import ReflectContextService
from reflect.improvements.contracts import WorkflowMilestoneEvidence
from reflect.improvements.milestones import WorkflowMilestoneService
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite

NOW = "2026-08-02T10:00:00+00:00"


def _seed(conn) -> None:
    conn.execute(
        "INSERT INTO agents(id, name, created_at, updated_at) VALUES ('agent', 'codex', ?, ?)",
        (NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO sessions(
          id, agent_id, started_at, status, created_at, updated_at
        ) VALUES ('session', 'agent', ?, 'completed', ?, ?)
        """,
        (NOW, NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO skills(
          id, slug, name, description, origin, lifecycle_state,
          current_version_id, first_seen_at, last_seen_at, created_at, updated_at
        ) VALUES ('skill', 'proven-review', 'Proven review', 'Review safely',
                  'rule_blueprint', 'active', 'skill-version', ?, ?, ?, ?)
        """,
        (NOW, NOW, NOW, NOW),
    )
    workflow = {
        "workflow_contract": {
            "signature_hash": "signature",
            "milestones": [
                {"id": "observe", "role": "observe", "evidence": "corroborated"},
                {"id": "verify", "role": "verify", "evidence": "corroborated"},
            ],
        }
    }
    conn.execute(
        """
        INSERT INTO skill_versions(
          id, skill_id, version, content_markdown, content_hash, workflow_json,
          source_kind, status, created_at, updated_at
        ) VALUES ('skill-version', 'skill', 1, '# Skill', 'content-hash', ?,
                  'rule_blueprint', 'active', ?, ?)
        """,
        (json.dumps(workflow), NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO mcp_task_runs(
          id, runtime_session_id, runtime_agent, workspace_path, question_hash,
          selected_skills_json, status, started_at, created_at, updated_at
        ) VALUES ('task-run', 'session', 'codex', '/workspace', 'question-hash', ?,
                  'started', ?, ?, ?)
        """,
        (
            json.dumps(
                [
                    {
                        "skill_id": "skill",
                        "version_id": "skill-version",
                        "slug": "proven-review",
                    }
                ]
            ),
            NOW,
            NOW,
            NOW,
        ),
    )
    conn.commit()


def test_milestone_is_idempotent_and_visible_in_task_status(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _seed(conn)
        service = WorkflowMilestoneService(conn)
        evidence = WorkflowMilestoneEvidence(
            provider="gitlab",
            target_ref="project/merge-request/42",
            preview_fingerprint="preview-hash",
            approval_fingerprint="approval-hash",
        )

        created = service.record(
            task_run_id="task-run",
            skill_version_id="skill-version",
            milestone_id="observe",
            state="completed",
            idempotency_key="observe-complete",
            evidence=evidence,
        )
        repeated = service.record(
            task_run_id="task-run",
            skill_version_id="skill-version",
            milestone_id="observe",
            state="completed",
            idempotency_key="observe-complete",
            evidence=evidence,
        )

        assert created.created is True
        assert repeated.created is False
        assert created.event_id == repeated.event_id
        assert created.evidence.preview_fingerprint == "preview-hash"
        details = json.loads(
            conn.execute(
                "SELECT details_json FROM improvement_events WHERE id = ?",
                (created.event_id,),
            ).fetchone()[0]
        )
        assert details["evidence"]["approval_fingerprint"] == "approval-hash"
        status = ReflectContextService(conn).task_status("task-run")
        assert status.milestone_count == 1
        assert status.execution_unit_id is None
        conn.execute("UPDATE mcp_task_runs SET status = 'completed' WHERE id = 'task-run'")
        conn.commit()
        assert service.record(
            task_run_id="task-run",
            skill_version_id="skill-version",
            milestone_id="observe",
            state="completed",
            idempotency_key="observe-complete",
            evidence=evidence,
        ).created is False
        with pytest.raises(ValueError, match="idempotent replay"):
            service.record(
                task_run_id="task-run",
                skill_version_id="skill-version",
                milestone_id="verify",
                state="completed",
                idempotency_key="late-verify",
            )
    finally:
        conn.close()


def test_milestone_rejects_unselected_skill_unknown_id_and_rebound_key(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _seed(conn)
        service = WorkflowMilestoneService(conn)

        with pytest.raises(ValueError, match="not selected"):
            service.record(
                task_run_id="task-run",
                skill_version_id="another-version",
                milestone_id="observe",
                state="completed",
                idempotency_key="wrong-skill",
            )
        with pytest.raises(ValueError, match="Unknown workflow milestone"):
            service.record(
                task_run_id="task-run",
                skill_version_id="skill-version",
                milestone_id="act",
                state="completed",
                idempotency_key="unknown",
            )
        service.record(
            task_run_id="task-run",
            skill_version_id="skill-version",
            milestone_id="observe",
            state="completed",
            idempotency_key="bound",
        )
        with pytest.raises(ValueError, match="already bound"):
            service.record(
                task_run_id="task-run",
                skill_version_id="skill-version",
                milestone_id="verify",
                state="completed",
                idempotency_key="bound",
            )
    finally:
        conn.close()
