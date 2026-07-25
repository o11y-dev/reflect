from datetime import UTC, datetime, timedelta

import pytest

from reflect.changes import ChangeAction, ChangeReviewService, ChangeReviewState
from reflect.context import ReflectContextService
from reflect.improvements.models import WorkflowStatus
from reflect.improvements.service import ImprovementService
from reflect.store.sqlite import connect_sqlite


def _candidate(conn) -> tuple[ImprovementService, str]:
    improvements = ImprovementService(conn)
    candidate_id = improvements.stage_extracted_skills(
        [
            {
                "name": "safe-release",
                "description": "Publish a release with a focused validation gate.",
                "content": (
                    "# Safe release\n\n"
                    "1. Run the focused release validation.\n"
                    "2. Publish only after it passes."
                ),
                "behavior_type": "verification",
            }
        ],
        session_ids=[],
        source_agent="codex",
    )[0]
    return improvements, candidate_id


def _token_factory():
    number = 0

    def next_token() -> str:
        nonlocal number
        number += 1
        return f"deterministic-token-{number}"

    return next_token


def test_change_review_uses_existing_apply_and_keeps_raw_token_out_of_sqlite(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    try:
        improvements, candidate_id = _candidate(conn)
        changes = ChangeReviewService(
            conn,
            workflows=improvements.workflows,
            skills=improvements.skills,
            token_factory=_token_factory(),
        )

        reviewed = changes.review(
            action=ChangeAction.APPLY_WORKFLOW,
            entity_id="safe-release",
            project_root=project_root,
        )
        target = project_root / ".agents" / "skills" / "safe-release" / "SKILL.md"

        assert reviewed.candidate_id == candidate_id
        assert reviewed.skill_slug == "safe-release"
        assert reviewed.state == ChangeReviewState.PENDING
        assert reviewed.diff.startswith("--- ")
        assert reviewed.next_action.tool == "reflect_apply_change"
        assert reviewed.explicit_user_approval_required is True
        assert not target.exists()
        token_hash = conn.execute(
            "SELECT token_hash FROM change_reviews WHERE id = ?",
            (reviewed.review_id,),
        ).fetchone()[0]
        assert token_hash != reviewed.approval_token
        assert reviewed.approval_token not in str(
            conn.execute(
                "SELECT payload_json FROM change_reviews WHERE id = ?",
                (reviewed.review_id,),
            ).fetchone()[0]
        )

        applied = changes.apply(reviewed.approval_token)
        repeated = changes.apply(reviewed.approval_token)

        assert applied.state == ChangeReviewState.APPLIED
        assert applied.idempotent is False
        assert repeated.idempotent is True
        assert target.is_file()
        assert improvements.workflows.show(candidate_id).status == WorkflowStatus.ACTIVE
        detail = improvements.skills.show("safe-release")
        assert detail.skill.lifecycle_state.value == "active"
        assert detail.skill.installation_count == 1
        assert conn.execute(
            """
            SELECT actor FROM improvement_events
            WHERE entity_type = 'intervention' AND event_type = 'applied'
            ORDER BY created_at DESC LIMIT 1
            """
        ).fetchone()[0] == "mcp_conversational_approval"
    finally:
        conn.close()


def test_revised_conversational_review_supersedes_the_previous_token(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    try:
        improvements, candidate_id = _candidate(conn)
        changes = ChangeReviewService(
            conn,
            workflows=improvements.workflows,
            skills=improvements.skills,
            token_factory=_token_factory(),
        )
        first = changes.review(
            action="apply_workflow",
            entity_id=candidate_id,
            project_root=project_root,
        )
        candidate = improvements.workflows.show(candidate_id)
        revised_content = {
            **candidate.content,
            "source_markdown": "",
            "steps": [
                "Keep the staging validation check.",
                "Run the focused release verification.",
            ],
        }

        revised = changes.review(
            action="apply_workflow",
            entity_id=candidate_id,
            project_root=project_root,
            revised_content=revised_content,
        )

        assert revised.revised_proposal_staged is True
        assert "Keep the staging validation check." in revised.diff
        assert improvements.workflows.show(candidate_id).content["steps"] == revised_content["steps"]
        with pytest.raises(RuntimeError, match="superseded"):
            changes.apply(first.approval_token)

        changes.apply(revised.approval_token)
        installed = project_root / ".agents" / "skills" / "safe-release" / "SKILL.md"
        assert "Keep the staging validation check." in installed.read_text(encoding="utf-8")
    finally:
        conn.close()


def test_change_review_refuses_stale_target_and_preserves_external_content(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    try:
        improvements, candidate_id = _candidate(conn)
        changes = ChangeReviewService(
            conn,
            workflows=improvements.workflows,
            skills=improvements.skills,
            token_factory=_token_factory(),
        )
        reviewed = changes.review(
            action="apply_workflow",
            entity_id=candidate_id,
            project_root=project_root,
        )
        target = project_root / ".agents" / "skills" / "safe-release" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("external change\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match="changed"):
            changes.apply(reviewed.approval_token)

        assert target.read_text(encoding="utf-8") == "external change\n"
        assert changes.inspect(reviewed.review_id)["state"] == "stale"
        assert improvements.workflows.show(candidate_id).status == WorkflowStatus.PENDING
    finally:
        conn.close()


def test_approve_apply_and_rollback_are_separate_exact_reviews(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    try:
        improvements, candidate_id = _candidate(conn)
        changes = ChangeReviewService(
            conn,
            workflows=improvements.workflows,
            skills=improvements.skills,
            token_factory=_token_factory(),
        )
        approval = changes.review(
            action="approve_workflow",
            entity_id=candidate_id,
        )
        approved = changes.apply(approval.approval_token)

        assert approved.result["status"] == "approved"
        assert improvements.workflows.show(candidate_id).status == WorkflowStatus.APPROVED
        assert not (project_root / ".agents").exists()

        installation = changes.review(
            action="apply_workflow",
            entity_id=candidate_id,
            project_root=project_root,
        )
        changes.apply(installation.approval_token)
        target = project_root / ".agents" / "skills" / "safe-release" / "SKILL.md"
        assert target.is_file()

        rollback = changes.review(
            action="rollback_workflow",
            entity_id=candidate_id,
        )
        rolled_back = changes.apply(rollback.approval_token)

        assert rollback.change_kind == "delete"
        assert rolled_back.result["status"] == "rolled_back"
        assert target.exists() is False
        assert improvements.workflows.show(candidate_id).status == WorkflowStatus.ROLLED_BACK
        detail = improvements.skills.show("safe-release")
        assert detail.skill.lifecycle_state.value == "retired"
        assert detail.installations[0].status == "rolled_back"
        explanation = ReflectContextService(conn).explain(rollback.review_id)
        assert explanation["kind"] == "change_review"
        assert "approval_token" not in explanation["entity"]
        assert improvements.workflows.rollback(candidate_id)["idempotent"] is True

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("external content after rollback\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="changed after rollback"):
            improvements.workflows.rollback(candidate_id)
    finally:
        conn.close()


def test_expired_change_review_cannot_be_applied(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    now = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    current = [now]
    try:
        improvements, candidate_id = _candidate(conn)
        changes = ChangeReviewService(
            conn,
            workflows=improvements.workflows,
            skills=improvements.skills,
            clock=lambda: current[0],
            token_factory=_token_factory(),
        )
        reviewed = changes.review(
            action="apply_workflow",
            entity_id=candidate_id,
            project_root=project_root,
            expires_in_minutes=5,
        )
        current[0] = now + timedelta(minutes=6)

        with pytest.raises(RuntimeError, match="expired"):
            changes.apply(reviewed.approval_token)

        assert changes.inspect(reviewed.review_id)["state"] == "expired"
        assert not (project_root / ".agents").exists()
    finally:
        conn.close()
