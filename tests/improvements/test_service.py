from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from reflect.core import main
from reflect.improvements.models import (
    ImprovementSummary,
    ObservationDraft,
    ObservationStatus,
    WorkflowProposal,
    WorkflowStatus,
)
from reflect.improvements.nudge_exchange import NudgeFileExchange
from reflect.improvements.nudges import HookNudgeBridge, NudgeService
from reflect.improvements.service import ImprovementService
from reflect.improvements.team import TeamBundleService
from reflect.store.migrate import migrate
from reflect.store.normalize import backfill_tool_call_hashes
from reflect.store.sqlite import connect_sqlite

NOW = "2026-07-01T10:00:00+00:00"


def _seed(conn) -> None:
    conn.execute(
        """
        INSERT INTO agents(id, name, created_at, updated_at)
        VALUES ('agent-1', 'codex', ?, ?)
        """,
        (NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO repos(id, full_name, created_at, updated_at)
        VALUES ('repo-1', 'o11ydev/reflect', ?, ?)
        """,
        (NOW, NOW),
    )
    for index, session_id in enumerate(("session-1", "session-2"), start=1):
        conn.execute(
            """
            INSERT INTO sessions(
              id, agent_id, repo_id, started_at, ended_at, status,
              input_tokens, output_tokens, created_at, updated_at
            ) VALUES (?, 'agent-1', 'repo-1', ?, ?, 'completed', 1000, 200, ?, ?)
            """,
            (
                session_id,
                f"2026-07-0{index}T10:00:00+00:00",
                f"2026-07-0{index}T10:10:00+00:00",
                NOW,
                NOW,
            ),
        )

    calls = [
        ("session-1", "Read", "ok", "read-a", None),
        ("session-1", "exec", "failed", "same-command", "exit_nonzero"),
        ("session-1", "exec", "failed", "same-command", "exit_nonzero"),
        ("session-1", "exec", "failed", "same-command", "exit_nonzero"),
        ("session-2", "Edit", "ok", "edit-a", None),
        ("session-2", "exec", "failed", "other-command", "exit_nonzero"),
    ]
    for seq, (session_id, tool_name, status, input_hash, error_type) in enumerate(calls, start=1):
        step_id = f"step-{seq}"
        call_id = f"tool-{seq}"
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, raw_attrs_json,
              created_at, updated_at
            ) VALUES (?, ?, ?, 'tool_call', ?, ?, '{}', ?, ?)
            """,
            (step_id, session_id, seq, NOW, status, NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO tool_calls(
              id, step_id, session_id, tool_name, status, input_hash,
              input_preview_redacted, error_type, raw_attrs_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
            """,
            (
                call_id,
                step_id,
                session_id,
                tool_name,
                status,
                input_hash,
                f"{input_hash}-preview",
                error_type,
                NOW,
                NOW,
            ),
        )
    conn.commit()


def _service(tmp_path: Path) -> tuple[ImprovementService, object]:
    conn = connect_sqlite(tmp_path / "reflect.db")
    migrate(conn)
    _seed(conn)
    return ImprovementService(conn), conn


def test_refresh_persists_versioned_observations_and_pending_candidates(tmp_path):
    service, conn = _service(tmp_path)
    try:
        first = service.refresh()
        first_ids = {
            row[0]: row[1]
            for row in conn.execute("SELECT rule_id, id FROM observations").fetchall()
        }
        second = service.refresh()
        second_ids = {
            row[0]: row[1]
            for row in conn.execute("SELECT rule_id, id FROM observations").fetchall()
        }

        assert first["detected"] >= 3
        assert first["skills"] == len(service.workflows.list())
        assert second["detected"] == first["detected"]
        assert second_ids == first_ids
        assert "repeated_tool_failure_chain" in first_ids
        assert "retry_loop_without_state_change" in first_ids
        assert "missing_or_late_verification" in first_ids
        assert conn.execute("SELECT COUNT(*) FROM rule_definitions").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM workflow_candidates").fetchone()[0] < len(first_ids)
        assert conn.execute(
            """
            SELECT COUNT(*) FROM workflow_candidates wc
            JOIN observations o ON o.id = wc.observation_id
            WHERE o.rule_id = 'retry_loop_without_state_change'
            """
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM loop_patterns").fetchone()[0] >= 1
        assert {
            row[0] for row in conn.execute("SELECT DISTINCT status FROM workflow_candidates")
        } == {WorkflowStatus.PENDING.value}
        assert conn.execute("SELECT COUNT(*) FROM observation_evidence").fetchone()[0] > 0
    finally:
        conn.close()


def test_retry_loop_detection_uses_backfilled_redacted_input_fingerprints(tmp_path):
    service, conn = _service(tmp_path)
    try:
        conn.execute(
            """
            UPDATE tool_calls
            SET input_hash = NULL, input_preview_redacted = 'poetry run pytest -q'
            WHERE session_id = 'session-1' AND tool_name = 'exec'
            """
        )
        assert backfill_tool_call_hashes(conn) == {"updated": 3}

        service.refresh()
        loop = next(item for item in service.loops.list() if item.kind.value == "stalled")

        assert loop.tool_name == "exec"
        assert loop.affected_session_count == 1
        assert loop.occurrence_count == 3
        assert service.workflows.list(behavior_types={"loop"}) == []
    finally:
        conn.close()


def test_loop_ledger_groups_same_tool_across_sessions_without_creating_a_skill(tmp_path):
    service, conn = _service(tmp_path)
    try:
        conn.execute(
            """
            INSERT INTO sessions(
              id, agent_id, repo_id, started_at, ended_at, status,
              created_at, updated_at
            ) VALUES ('session-3', 'agent-1', 'repo-1', ?, ?, 'completed', ?, ?)
            """,
            (NOW, NOW, NOW, NOW),
        )
        for sequence in range(30, 33):
            step_id = f"group-step-{sequence}"
            conn.execute(
                """
                INSERT INTO steps(
                  id, session_id, seq, type, started_at, status, raw_attrs_json,
                  created_at, updated_at
                ) VALUES (?, 'session-3', ?, 'tool_call', ?, 'ok', '{}', ?, ?)
                """,
                (step_id, sequence, NOW, NOW, NOW),
            )
            conn.execute(
                """
                INSERT INTO tool_calls(
                  id, step_id, session_id, tool_name, status, input_hash,
                  raw_attrs_json, created_at, updated_at
                ) VALUES (?, ?, 'session-3', 'EXEC', 'ok', 'other-repeated-input', '{}', ?, ?)
                """,
                (f"group-tool-{sequence}", step_id, NOW, NOW),
            )
        conn.commit()

        service.refresh()

        loops = [item for item in service.loops.list() if item.tool_name == "exec"]
        assert len(loops) == 1
        assert loops[0].affected_session_count == 2
        detail = service.loops.show(loops[0].id)
        assert {item.session_id for item in detail.occurrences} == {"session-1", "session-3"}
        assert service.workflows.list(behavior_types={"loop"}) == []
    finally:
        conn.close()


def test_retry_loop_detection_normalizes_tool_name_case_before_fingerprinting(tmp_path):
    service, conn = _service(tmp_path)
    try:
        for sequence in range(20, 23):
            step_id = f"step-{sequence}"
            conn.execute(
                """
                INSERT INTO steps(
                  id, session_id, seq, type, started_at, status, raw_attrs_json, created_at, updated_at
                ) VALUES (?, 'session-1', ?, 'tool_call', ?, 'failed', '{}', ?, ?)
                """,
                (step_id, sequence, NOW, NOW, NOW),
            )
            conn.execute(
                """
                INSERT INTO tool_calls(
                  id, step_id, session_id, tool_name, status, input_hash,
                  error_type, raw_attrs_json, created_at, updated_at
                ) VALUES (?, ?, 'session-1', 'EXEC', 'failed', 'uppercase-command',
                          'exit_nonzero', '{}', ?, ?)
                """,
                (f"tool-{sequence}", step_id, NOW, NOW),
            )

        service.refresh()

        assert conn.execute(
            "SELECT COUNT(*) FROM observations WHERE rule_id = 'retry_loop_without_state_change'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_loop_detection_ignores_poll_transport_and_approval_metadata(tmp_path):
    service, conn = _service(tmp_path)
    try:
        for group_index, (tool_name, preview) in enumerate(
            (
                ("write_stdin", '{"session_id":123,"chars":""}'),
                ("apply_patch", '{"decision":"approved","source":"Config"}'),
            ),
        ):
            for index in range(3):
                sequence = 20 + group_index * 3 + index
                step_id = f"noise-step-{sequence}"
                conn.execute(
                    """
                    INSERT INTO steps(
                      id, session_id, seq, type, started_at, status, raw_attrs_json,
                      created_at, updated_at
                    ) VALUES (?, 'session-1', ?, 'tool_call', ?, 'ok', '{}', ?, ?)
                    """,
                    (step_id, sequence, NOW, NOW, NOW),
                )
                conn.execute(
                    """
                    INSERT INTO tool_calls(
                      id, step_id, session_id, tool_name, status, input_hash,
                      input_preview_redacted, raw_attrs_json, created_at, updated_at
                    ) VALUES (?, ?, 'session-1', ?, 'ok', ?, ?, '{}', ?, ?)
                    """,
                    (
                        f"noise-tool-{sequence}",
                        step_id,
                        tool_name,
                        f"noise-hash-{group_index}",
                        preview,
                        NOW,
                        NOW,
                    ),
                )
        conn.commit()

        service.loops.refresh()
        tools = {item.tool_name for item in service.loops.list()}

        assert "write_stdin" not in tools
        assert "apply_patch" not in tools
    finally:
        conn.close()


def test_refresh_backfills_behavior_metadata_on_legacy_non_pending_candidates(tmp_path):
    service, conn = _service(tmp_path)
    try:
        service.refresh()
        candidate_id = service.workflows.list()[0].id
        conn.execute(
            """
            UPDATE workflow_candidates
            SET status = 'stale',
                content_json = json_remove(
                  content_json,
                  '$.behavior_type',
                  '$.suggested_artifact',
                  '$.source.kind',
                  '$.source.rule_id'
                ),
                provenance_json = json_remove(provenance_json, '$.source')
            WHERE id = ?
            """,
            (candidate_id,),
        )

        service.refresh()
        content = service.workflows.show(candidate_id).content

        assert content["behavior_type"]
        assert content["suggested_artifact"] == "skill"
        assert content["source"]["kind"] == "rule_blueprint"
        assert content["source"]["rule_id"]
    finally:
        conn.close()


def test_workflow_lookup_and_grouping_include_candidates_beyond_first_page(tmp_path):
    service, conn = _service(tmp_path)
    try:
        service.refresh()
        target = service.workflows.list()[0]
        target_content = {**target.content, "slug": "overflow-target"}
        filler_content = {**target.content, "slug": "bulk-filler"}
        conn.execute(
            "UPDATE workflow_candidates SET content_json = ?, confidence = 0 WHERE id = ?",
            (json.dumps(target_content, sort_keys=True), target.id),
        )
        conn.executemany(
            """
            INSERT INTO workflow_candidates(
              id, observation_id, task_archetype_id, action_type, title, hypothesis,
              scope, risk, content_json, support_count, confidence, target_metric,
              target_value, measurement_window, status, checks_json, provenance_json,
              created_at, updated_at
            )
            SELECT ?, observation_id, task_archetype_id, ?, title, hypothesis,
                   scope, risk, ?, support_count, 1.0, target_metric,
                   target_value, measurement_window, status, checks_json, provenance_json,
                   created_at, updated_at
            FROM workflow_candidates WHERE id = ?
            """,
            [
                (
                    f"bulk-candidate-{index:03d}",
                    f"bulk-action-{index:03d}",
                    json.dumps(filler_content, sort_keys=True),
                    target.id,
                )
                for index in range(500)
            ],
        )
        conn.commit()

        assert target.id not in {
            item.id for item in service.repository.list_candidates(limit=500)
        }
        assert service.repository.get_candidate(target.id).id == target.id
        assert service.workflows.show(target.id).content["slug"] == "overflow-target"
        assert "overflow-target" in {
            item.content["slug"] for item in service.workflows.list()
        }
    finally:
        conn.close()


def test_refresh_resolves_a_finding_that_disappears(tmp_path):
    service, conn = _service(tmp_path)
    try:
        service.refresh()
        observation_id = conn.execute(
            "SELECT id FROM observations WHERE rule_id = 'repeated_tool_failure_chain'"
        ).fetchone()[0]
        conn.execute("UPDATE tool_calls SET status = 'ok', error_type = NULL")
        conn.commit()

        service.refresh()

        status = conn.execute("SELECT status FROM observations WHERE id = ?", (observation_id,)).fetchone()[0]
        assert status == ObservationStatus.RESOLVED.value
        candidate_status = conn.execute(
            "SELECT status FROM workflow_candidates WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()[0]
        assert candidate_status == WorkflowStatus.STALE.value

        conn.execute(
            """
            UPDATE tool_calls
            SET status = 'error', error_type = 'reactivated'
            WHERE tool_name = 'exec'
            """
        )
        conn.commit()
        service.refresh()

        reopened = conn.execute(
            """
            SELECT o.status, wc.status
            FROM observations o
            JOIN workflow_candidates wc ON wc.observation_id = o.id
            WHERE o.id = ?
            """,
            (observation_id,),
        ).fetchone()
        assert reopened == (ObservationStatus.PROPOSAL_READY.value, WorkflowStatus.PENDING.value)
    finally:
        conn.close()


def test_improve_returns_typed_summary_and_evidence(tmp_path):
    service, conn = _service(tmp_path)
    try:
        summary = service.improve()
        assert isinstance(summary, ImprovementSummary)
        assert summary.observations
        proposed = next(item for item in summary.observations if item.candidate_id)
        detail = service.improve(proposed.id, refresh=False)
        assert detail.evidence
        assert detail.candidate_status == WorkflowStatus.PENDING
    finally:
        conn.close()


def test_inbox_groups_scope_specific_observations_by_workflow(tmp_path):
    service, conn = _service(tmp_path)
    try:
        service.refresh()
        source = next(
            item
            for item in service.repository.list_observations(limit=500)
            if item.candidate_id
        )
        candidate = service.repository.get_candidate(source.candidate_id)
        assert candidate is not None
        clone_data = {
            name: getattr(source, name)
            for name in ObservationDraft.model_fields
        }
        clone_data.update(
            {
                "scope_id": f"{source.scope_id}-duplicate",
                "fingerprint": f"{source.fingerprint}-duplicate",
            }
        )
        clone_id = service.repository.upsert_observation(
            ObservationDraft.model_validate(clone_data),
            now=NOW,
        )
        service.repository.ensure_candidate(
            clone_id,
            proposal=WorkflowProposal(
                title=candidate.title,
                hypothesis=candidate.hypothesis,
                risk=candidate.risk,
                content=candidate.content,
                target_metric=candidate.target_metric,
                target_value=candidate.target_value,
                measurement_window=candidate.measurement_window,
            ),
            now=NOW,
        )
        conn.commit()

        observations = service.repository.list_observations(limit=500)
        candidates = {
            item.id: item for item in service.repository.list_candidates(limit=500)
        }
        target_slug = str(candidate.content["slug"])
        target_observations = [
            item
            for item in observations
            if item.candidate_id
            and str(candidates[item.candidate_id].content.get("slug")) == target_slug
        ]
        findings = service.list_inbox_findings(limit=500)
        target_finding = next(
            item
            for item in findings
            if item.candidate_id
            and str(candidates[item.candidate_id].content.get("slug")) == target_slug
        )

        assert len(target_observations) >= 2
        assert target_finding.observation_count == len(target_observations)
        assert target_finding.source_scope_count == len(
            {f"{item.scope_type}:{item.scope_id}" for item in target_observations}
        )
        assert len(findings) < len(observations)
    finally:
        conn.close()


def test_workflow_apply_and_rollback_are_hash_guarded(tmp_path):
    service, conn = _service(tmp_path)
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    try:
        service.refresh()
        candidate = service.workflows.list()[0]

        applied = service.workflows.apply(candidate.id, project_root=project_root)
        target = Path(applied["target_path"])
        assert target.exists()
        assert "Source observation:" not in target.read_text(encoding="utf-8")
        assert service.repository.workflow_session_ledger(candidate.id).observation_ids
        assert service.workflows.show(candidate.id).status == WorkflowStatus.ACTIVE
        assert conn.execute(
            "SELECT status FROM evaluations WHERE workflow_version_id = (SELECT workflow_version_id FROM interventions WHERE id = ?)",
            (applied["intervention_id"],),
        ).fetchone()[0] == "passed"

        rolled_back = service.workflows.rollback(candidate.id)
        assert rolled_back["status"] == "rolled_back"
        assert not target.exists()
        assert service.workflows.show(candidate.id).status == WorkflowStatus.ROLLED_BACK
    finally:
        conn.close()


def test_workflow_preview_is_exact_and_repeat_apply_is_idempotent(tmp_path):
    service, conn = _service(tmp_path)
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    try:
        service.refresh()
        candidate = service.workflows.list()[0]

        preview = service.workflows.preview(candidate.id, project_root=project_root)
        assert preview["would_change"] is True
        assert preview["diff"].startswith("--- ")
        assert candidate.observation_id not in preview["diff"]
        assert candidate.content["slug"] in preview["diff"]
        assert preview["application_repository"] == str(project_root)
        assert preview["target_relative_path"].startswith(".agents/skills/")
        assert preview["checks"]["target_owner"] is None
        assert 'description: "' in preview["content"]

        first = service.workflows.apply(candidate.id, project_root=project_root)
        second = service.workflows.apply(candidate.id, project_root=project_root)

        assert first["idempotent"] is False
        assert second["idempotent"] is True
        assert second["intervention_id"] == first["intervention_id"]
        assert service.workflows.preview(candidate.id, project_root=project_root)["checks"]["target_owner"] == {
            "candidate_id": candidate.id,
            "title": candidate.title,
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM interventions WHERE status = 'active'"
        ).fetchone()[0] == 1
        assert service.workflows.preview(candidate.id, project_root=project_root)["would_change"] is False
    finally:
        conn.close()


def test_workflow_targets_accept_non_git_folders_and_normalize_nested_git_paths(tmp_path):
    service, conn = _service(tmp_path)
    git_root = tmp_path / "git-project"
    nested_git_folder = git_root / "packages" / "app"
    (git_root / ".git").mkdir(parents=True)
    nested_git_folder.mkdir(parents=True)
    plain_project = tmp_path / "plain-project"
    plain_project.mkdir()
    try:
        service.refresh()
        candidate = service.workflows.list()[0]

        nested_preview = service.workflows.preview(
            candidate.id,
            project_root=nested_git_folder,
        )
        assert nested_preview["project_root"] == str(git_root)
        assert nested_preview["is_git_repository"] is True
        assert nested_preview["checks"]["apply_allowed"] is True

        plain_preview = service.workflows.preview(candidate.id, project_root=plain_project)
        assert plain_preview["project_root"] == str(plain_project)
        assert plain_preview["application_root"] == str(plain_project)
        assert plain_preview["is_git_repository"] is False
        assert plain_preview["checks"]["project_directory"] is True
        assert plain_preview["checks"]["writable"] is True
        assert plain_preview["checks"]["apply_allowed"] is True

        applied = service.workflows.apply(candidate.id, project_root=plain_project)
        assert Path(applied["target_path"]).is_file()
        assert Path(applied["target_path"]).is_relative_to(plain_project)
    finally:
        conn.close()


def test_workflow_target_checks_reject_missing_folders_and_path_collisions(tmp_path):
    service, conn = _service(tmp_path)
    missing_project = tmp_path / "missing-project"
    blocked_project = tmp_path / "blocked-project"
    blocked_project.mkdir()
    (blocked_project / ".agents").write_text("not a directory", encoding="utf-8")
    try:
        service.refresh()
        candidate = service.workflows.list()[0]

        missing_preview = service.workflows.preview(
            candidate.id,
            project_root=missing_project,
        )
        assert missing_preview["checks"]["apply_allowed"] is False
        assert "does not exist" in missing_preview["checks"]["issues"][0]

        blocked_preview = service.workflows.preview(
            candidate.id,
            project_root=blocked_project,
        )
        assert blocked_preview["checks"]["apply_allowed"] is False
        assert "not a directory" in blocked_preview["checks"]["issues"][0]
    finally:
        conn.close()


def test_workflow_target_checks_reject_broad_filesystem_roots(tmp_path, monkeypatch):
    service, conn = _service(tmp_path)
    fake_home = tmp_path / "operator-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    try:
        service.refresh()
        candidate = service.workflows.list()[0]

        home_preview = service.workflows.preview(candidate.id, project_root=fake_home)
        assert home_preview["checks"]["apply_allowed"] is False
        assert "home or filesystem root" in home_preview["checks"]["issues"][0]

        filesystem_preview = service.workflows.preview(
            candidate.id,
            project_root=Path(Path.cwd().anchor),
        )
        assert filesystem_preview["checks"]["apply_allowed"] is False
        assert "home or filesystem root" in filesystem_preview["checks"]["issues"][0]
    finally:
        conn.close()


def test_workflow_apply_blocks_another_active_candidate_for_the_same_target(tmp_path):
    service, conn = _service(tmp_path)
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    try:
        service.refresh()
        first, second = service.workflows.list()[:2]
        service.workflows.edit(
            second.id,
            content={**second.content, "slug": first.content["slug"]},
        )

        service.workflows.apply(first.id, project_root=project_root)
        preview = service.workflows.preview(second.id, project_root=project_root)

        assert preview["checks"]["apply_allowed"] is False
        assert preview["checks"]["active_conflicts"][0]["candidate_id"] == first.id
        with pytest.raises(RuntimeError, match="already owns"):
            service.workflows.apply(second.id, project_root=project_root)
    finally:
        conn.close()


def test_pending_workflow_can_be_edited_or_rejected_but_active_workflow_cannot(tmp_path):
    service, conn = _service(tmp_path)
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    try:
        service.refresh()
        first, second = service.workflows.list()[:2]
        edited_content = {
            **first.content,
            "steps": ["Inspect the exact evidence.", "Run the focused verification."],
        }
        edited = service.workflows.edit(first.id, content=edited_content)
        assert edited.content["steps"] == edited_content["steps"]
        assert edited.status == WorkflowStatus.PENDING

        rejected = service.workflows.reject(second.id)
        assert rejected.status == WorkflowStatus.REJECTED

        service.workflows.apply(first.id, project_root=project_root)
        with pytest.raises(RuntimeError, match="Roll back"):
            service.workflows.edit(first.id, content=edited_content)
        with pytest.raises(RuntimeError, match="Roll back"):
            service.workflows.reject(first.id)
    finally:
        conn.close()


def test_repeated_tool_failure_rule_requires_multiple_sessions(tmp_path):
    service, conn = _service(tmp_path)
    try:
        conn.execute(
            "UPDATE tool_calls SET status = 'ok', error_type = NULL WHERE session_id = 'session-2'"
        )
        conn.commit()

        service.refresh()

        assert conn.execute(
            "SELECT COUNT(*) FROM observations WHERE rule_id = 'repeated_tool_failure_chain'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_repeated_tool_failure_rule_normalizes_tool_name_case(tmp_path):
    service, conn = _service(tmp_path)
    try:
        conn.execute(
            "UPDATE tool_calls SET tool_name = 'EXEC' WHERE session_id = 'session-2' AND tool_name = 'exec'"
        )
        conn.commit()

        service.refresh()

        observations = conn.execute(
            """
            SELECT title, occurrence_count, affected_session_count
            FROM observations
            WHERE rule_id = 'repeated_tool_failure_chain'
            """
        ).fetchall()
        assert observations == [("Repeated EXEC failures", 4, 2)]
    finally:
        conn.close()


def test_task_archetypes_scope_workflow_adherence(tmp_path):
    service, conn = _service(tmp_path)
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    try:
        service.refresh()
        candidate = service.workflows.list()[0]
        applied = service.workflows.apply(candidate.id, project_root=project_root)
        slug = service.workflows.show(candidate.id).content["slug"]
        conn.execute(
            "UPDATE interventions SET exposure_started_at = '2026-06-01T00:00:00+00:00' WHERE id = ?",
            (applied["intervention_id"],),
        )
        conn.execute(
            "UPDATE steps SET raw_attrs_json = ? WHERE session_id = 'session-1'",
            (f'{{"skill":"{slug}"}}',),
        )
        conn.execute(
            "UPDATE tool_calls SET input_preview_redacted = 'poetry run pytest -q' WHERE session_id = 'session-1'"
        )
        conn.commit()

        result = service.adherence.refresh()
        refreshed = service.workflows.show(candidate.id)

        assert result["exposures"] == 2
        assert refreshed.task_archetype_id == "implementation"
        assert refreshed.exposure_counts == {"followed": 1, "ignored": 1}
    finally:
        conn.close()


def test_feedback_records_explicit_session_outcome(tmp_path):
    service, conn = _service(tmp_path)
    try:
        feedback_id = service.repository.record_feedback(
            "session-1",
            "corrected",
            reason_redacted="Repeated the same failed command",
        )

        assert feedback_id.startswith("feedback_")
        assert conn.execute(
            "SELECT outcome FROM session_outcomes WHERE session_id = 'session-1'"
        ).fetchone()[0] == "corrected"
    finally:
        conn.close()


def test_remaining_p0_rules_use_explicit_outcomes_and_canonical_signals(tmp_path):
    service, conn = _service(tmp_path)
    try:
        conn.execute(
            "UPDATE sessions SET recovered_failure_count = 1 WHERE id IN ('session-1', 'session-2')"
        )
        conn.execute(
            "UPDATE tool_calls SET error_type = 'permission_denied' WHERE id IN ('tool-2', 'tool-6')"
        )
        for session_id in ("session-1", "session-2"):
            conn.execute(
                """
                INSERT INTO session_outcomes(
                  id, session_id, outcome, source, confidence, verification_json,
                  created_at, updated_at
                ) VALUES (?, ?, 'corrected', 'operator_feedback', 1, '{}', ?, ?)
                """,
                (f"outcome-corrected-{session_id}", session_id, NOW, NOW),
            )
        for index in range(3, 8):
            session_id = f"session-{index}"
            conn.execute(
                """
                INSERT INTO sessions(
                  id, agent_id, repo_id, started_at, ended_at, status,
                  created_at, updated_at
                ) VALUES (?, 'agent-1', 'repo-1', ?, ?, 'completed', ?, ?)
                """,
                (
                    session_id,
                    f"2026-07-{index:02d}T10:00:00+00:00",
                    f"2026-07-{index:02d}T10:10:00+00:00",
                    NOW,
                    NOW,
                ),
            )
            if index <= 5:
                for seq, tool_name in enumerate(("Read", "Edit", "pytest"), start=1):
                    step_id = f"p0-step-{index}-{seq}"
                    conn.execute(
                        """
                        INSERT INTO steps(
                          id, session_id, seq, type, started_at, status, raw_attrs_json,
                          created_at, updated_at
                        ) VALUES (?, ?, ?, 'tool_call', ?, 'ok', '{}', ?, ?)
                        """,
                        (step_id, session_id, seq, NOW, NOW, NOW),
                    )
                    conn.execute(
                        """
                        INSERT INTO tool_calls(
                          id, step_id, session_id, tool_name, status, input_hash,
                          raw_attrs_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'ok', ?, '{}', ?, ?)
                        """,
                        (
                            f"p0-tool-{index}-{seq}",
                            step_id,
                            session_id,
                            tool_name,
                            f"p0-input-{index}-{seq}",
                            NOW,
                            NOW,
                        ),
                    )
            else:
                conn.execute(
                    """
                    INSERT INTO session_outcomes(
                      id, session_id, outcome, source, confidence, verification_json,
                      created_at, updated_at
                    ) VALUES (?, ?, 'no-change-correct', 'operator_feedback', 1, '{}', ?, ?)
                    """,
                    (f"outcome-no-change-{session_id}", session_id, NOW, NOW),
                )
        conn.commit()

        service.refresh()
        rule_ids = {
            row[0] for row in conn.execute("SELECT rule_id FROM observations").fetchall()
        }

        assert {
            "user_correction_after_completion",
            "constraint_or_instruction_ignored",
            "successful_recovery_sequence",
            "high_performing_repeated_workflow",
            "correct_no_change_outcome",
        } <= rule_ids
        productive = [item for item in service.loops.list() if item.kind.value == "productive"]
        assert productive
        assert productive[0].state_change_count >= 1
        assert conn.execute(
            """
            SELECT COUNT(*) FROM workflow_candidates wc
            JOIN observations o ON o.id = wc.observation_id
            WHERE o.rule_id = 'high_performing_repeated_workflow'
            """
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_refresh_automatically_measures_new_comparable_sessions_once(tmp_path):
    service, conn = _service(tmp_path)
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    try:
        service.refresh()
        candidate = next(
            item
            for item in service.workflows.list()
            if item.target_metric == "tool_failure_rate"
        )
        applied = service.workflows.apply(candidate.id, project_root=project_root)
        conn.execute(
            "UPDATE interventions SET exposure_started_at = '2026-07-10T00:00:00+00:00' WHERE id = ?",
            (applied["intervention_id"],),
        )
        for index in range(3, 11):
            before = index <= 5
            session_id = f"measure-session-{index}"
            started_at = (
                f"2026-07-0{index}T10:00:00+00:00"
                if before
                else f"2026-07-{index + 10:02d}T10:00:00+00:00"
            )
            status = "ok" if before else "failed"
            error_type = None if before else "exit_nonzero"
            conn.execute(
                """
                INSERT INTO sessions(
                  id, agent_id, repo_id, started_at, ended_at, status, failure_count,
                  created_at, updated_at
                ) VALUES (?, 'agent-1', 'repo-1', ?, ?, 'completed', ?, ?, ?)
                """,
                (session_id, started_at, started_at, int(not before), NOW, NOW),
            )
            step_id = f"measure-step-{index}"
            conn.execute(
                """
                INSERT INTO steps(
                  id, session_id, seq, type, started_at, status, raw_attrs_json,
                  created_at, updated_at
                ) VALUES (?, ?, 1, 'tool_call', ?, ?, '{}', ?, ?)
                """,
                (step_id, session_id, started_at, status, NOW, NOW),
            )
            conn.execute(
                """
                INSERT INTO tool_calls(
                  id, step_id, session_id, tool_name, status, input_hash, error_type,
                  raw_attrs_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'exec', ?, ?, ?, '{}', ?, ?)
                """,
                (
                    f"measure-tool-{index}",
                    step_id,
                    session_id,
                    status,
                    f"measure-input-{index}",
                    error_type,
                    NOW,
                    NOW,
                ),
            )
        conn.commit()

        result = service.refresh()
        measurement = service.measurements.list()[0]
        assert "before_session_ids" not in measurement["cohort"]
        assert "after_session_ids" not in measurement["cohort"]
        count_after_first_refresh = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
        second = service.refresh()

        assert result["measurements_created"] == 1
        assert result["regressions"] == 1
        assert measurement["before_count"] >= 5
        assert measurement["after_count"] >= 5
        assert measurement["verdict"] == "regressed"
        cohorts = service.measurements.sessions(measurement["id"])
        assert cohorts["candidate_id"] == candidate.id
        assert cohorts["snapshot_exact"] is True
        assert cohorts["before_count"] == measurement["before_count"]
        assert cohorts["after_count"] == measurement["after_count"]
        assert len(cohorts["before_sessions"]) == measurement["before_count"]
        assert len(cohorts["after_sessions"]) == measurement["after_count"]
        assert {item["session_id"] for item in cohorts["after_sessions"]} >= {
            "measure-session-6",
            "measure-session-10",
        }
        skill = service.skills.skill_for_candidate(candidate.id)
        assert skill.measurement_count == 1
        assert service.skills.show(skill.id).measurements[0].verdict == "regressed"
        assert second["measurements_created"] == 0
        assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == count_after_first_refresh
    finally:
        conn.close()


def test_measurement_refreshes_when_fixed_size_cohort_rotates(tmp_path):
    service, conn = _service(tmp_path)
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)

    def add_session(session_id: str, started_at: str, *, failed: bool = False) -> None:
        status = "failed" if failed else "ok"
        conn.execute(
            """
            INSERT INTO sessions(
              id, agent_id, repo_id, started_at, ended_at, status, failure_count,
              created_at, updated_at
            ) VALUES (?, 'agent-1', 'repo-1', ?, ?, 'completed', ?, ?, ?)
            """,
            (session_id, started_at, started_at, int(failed), NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, raw_attrs_json,
              created_at, updated_at
            ) VALUES (?, ?, 1, 'tool_call', ?, ?, '{}', ?, ?)
            """,
            (f"step-{session_id}", session_id, started_at, status, NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO tool_calls(
              id, step_id, session_id, tool_name, status, input_hash, error_type,
              raw_attrs_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'exec', ?, ?, ?, '{}', ?, ?)
            """,
            (
                f"tool-{session_id}",
                f"step-{session_id}",
                session_id,
                status,
                f"input-{session_id}",
                "exit_nonzero" if failed else None,
                NOW,
                NOW,
            ),
        )

    try:
        service.refresh()
        candidate = next(
            item for item in service.workflows.list() if item.target_metric == "tool_failure_rate"
        )
        applied = service.workflows.apply(candidate.id, project_root=project_root)
        conn.execute(
            "UPDATE interventions SET exposure_started_at = '2026-07-10T00:00:00+00:00' WHERE id = ?",
            (applied["intervention_id"],),
        )
        conn.execute(
            "UPDATE workflow_candidates SET task_archetype_id = NULL WHERE id = ?",
            (candidate.id,),
        )
        for index in range(3, 6):
            add_session(f"before-{index}", f"2026-07-0{index}T10:00:00+00:00")
        for index in range(50):
            add_session(f"after-{index:02d}", f"2026-07-11T00:{index:02d}:00+00:00")
        conn.commit()

        first = service.measurements.measure(candidate.id)
        add_session("after-newest", "2026-07-11T01:00:00+00:00", failed=True)
        conn.commit()
        second = service.measurements.measure(candidate.id, skip_unchanged=True)
        second_sessions = service.measurements.sessions(second["id"])

        assert first["after_count"] == second["after_count"] == 50
        assert second["created"] is True
        assert second["id"] != first["id"]
        assert second["after_value"] > first["after_value"]
        assert "after-newest" in {
            item["session_id"] for item in second_sessions["after_sessions"]
        }
    finally:
        conn.close()


def test_ask_labels_pending_guidance_as_unapproved(tmp_path):
    service, conn = _service(tmp_path)
    try:
        service.refresh()
        answer = service.ask("How should I stop retrying failed exec calls?")

        assert answer.evidence
        assert answer.guidance
        assert any("pending review" in limitation for limitation in answer.limitations)
    finally:
        conn.close()


def test_ask_returns_one_active_workflow_with_constraints_and_fallback(tmp_path):
    service, conn = _service(tmp_path)
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    try:
        service.refresh()
        candidate = next(
            item
            for item in service.workflows.list()
            if item.target_metric == "tool_failure_rate"
        )
        service.workflows.apply(candidate.id, project_root=project_root)

        answer = service.ask("How should I stop retrying an unchanged failed exec call?")

        assert answer.workflow_id == candidate.id
        assert answer.freshness
        assert answer.constraints
        assert answer.verification
        assert answer.fallback
        assert len([item for item in answer.evidence if item.kind == "workflow"]) == 1
    finally:
        conn.close()


def test_hook_nudges_are_opt_in_bounded_and_claimed_once(tmp_path):
    service, conn = _service(tmp_path)
    try:
        service.refresh()
        nudges = NudgeService(conn)
        nudges.configure("repeated_tool_failure_chain", 1, enabled=False)
        assert nudges.evaluate_session("session-1") == []

        nudges.configure(
            "repeated_tool_failure_chain",
            1,
            enabled=True,
            cooldown_seconds=900,
            max_per_session=1,
        )
        queued = nudges.evaluate_session("session-1")
        assert len(queued) == 1
        assert nudges.evaluate_session("session-1") == []

        bridge = HookNudgeBridge(nudges)
        first_poll = bridge.poll("session-1")
        second_poll = bridge.poll("session-1")
        assert queued[0] in first_poll
        assert '"transport": "opentelemetry_hooks_local_poll"' in first_poll
        assert '"nudges": []' in second_poll
    finally:
        conn.close()


def test_future_hook_exchange_is_disabled_private_atomic_and_metadata_only(tmp_path):
    root = tmp_path / "nudges"
    exchange = NudgeFileExchange(root)
    assert not root.exists()

    paths = exchange.prepare()
    contract = json.loads(paths.contract.read_text(encoding="utf-8"))
    assert contract["enabled"] is False
    assert contract["hook_integration"] == "not_configured"
    for directory in (paths.root, paths.outbox, paths.acknowledged, paths.rejected):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.contract.stat().st_mode) == 0o600

    staged = exchange.stage(
        "private-session-id",
        {
            "id": "nudge-1",
            "observation_id": "observation-1",
            "message": "Review the redacted failure evidence before retrying.",
            "created_at": NOW,
        },
    )
    payload = staged.read_text(encoding="utf-8")
    assert staged.parent.name == exchange.session_key("private-session-id")
    assert "private-session-id" not in str(staged)
    assert "private-session-id" not in payload
    assert '"message_redacted"' in payload
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600

    with pytest.raises(ValueError, match="forbidden field"):
        exchange.stage(
            "private-session-id",
            {
                "id": "nudge-2",
                "observation_id": "observation-1",
                "message": "safe",
                "created_at": NOW,
                "prompt": "must not cross the hook boundary",
            },
        )


def test_team_bundle_is_signed_aggregate_only_and_idempotent(tmp_path):
    service, conn = _service(tmp_path)
    key = b"a" * 32
    try:
        service.refresh()
        team = TeamBundleService(conn)
        bundle = team.export(signer_id="team-alpha", signing_key=key)
        serialized = str(bundle)

        assert "session-1" not in serialized
        assert "same-command" not in serialized
        assert bundle["payload"]["redaction_policy"]["aggregate_only"] is True
        assert team.import_bundle(bundle, signing_key=key)["status"] == "imported"
        assert team.import_bundle(bundle, signing_key=key)["status"] == "already_imported"

        tampered = {**bundle, "signature": "0" * 64}
        with pytest.raises(ValueError, match="signature"):
            team.import_bundle(tampered, signing_key=key)
    finally:
        conn.close()


def test_simplified_cli_contract_reads_the_durable_ledger(tmp_path):
    service, conn = _service(tmp_path)
    db_path = tmp_path / "reflect.db"
    try:
        service.refresh()
    finally:
        conn.close()
    runner = CliRunner()

    with patch("reflect.core._prepare_sql_report_db"):
        improve_result = runner.invoke(
            main,
            ["improve", "--json", "--db-path", str(db_path)],
        )
        ask_result = runner.invoke(
            main,
            ["ask", "How should I stop retry loops?", "--json", "--db-path", str(db_path)],
        )
        workflow_result = runner.invoke(
            main,
            ["workflows", "list", "--json", "--db-path", str(db_path)],
        )
        verification_result = runner.invoke(
            main,
            ["workflows", "list", "--type", "verification", "--json", "--db-path", str(db_path)],
        )

    assert improve_result.exit_code == 0
    assert '"observations"' in improve_result.output
    assert ask_result.exit_code == 0
    assert '"evidence"' in ask_result.output
    assert workflow_result.exit_code == 0
    assert '"status": "pending"' in workflow_result.output
    assert verification_result.exit_code == 0
    assert {
        item["content"]["behavior_type"] for item in json.loads(verification_result.output)
    } == {"verification"}


def test_workflows_add_stages_an_existing_skill_without_installing_it(tmp_path):
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text(
        "---\n"
        "name: diagnose-first\n"
        "description: Diagnose a repeated failure before retrying.\n"
        "---\n\n"
        "# Diagnose first\n\n1. Capture the failure.\n2. Change one condition.\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "reflect.db"

    result = CliRunner().invoke(
        main,
        [
            "workflows",
            "add",
            str(skill_file),
            "--type",
            "recovery",
            "--db-path",
            str(db_path),
        ],
    )

    assert result.exit_code == 0
    assert "Nothing was installed" in result.output
    assert not (tmp_path / ".agents").exists()
    conn = connect_sqlite(db_path)
    try:
        row = conn.execute(
            """
            SELECT status,
                   json_extract(content_json, '$.slug'),
                   json_extract(content_json, '$.behavior_type'),
                   json_extract(content_json, '$.source.kind'),
                   json_extract(content_json, '$.suggested_artifact'),
                   json_extract(provenance_json, '$.source')
            FROM workflow_candidates
            """
        ).fetchone()
        assert tuple(row) == (
            "pending",
            "diagnose-first",
            "recovery",
            "manual_skill_file",
            "skill",
            "manual_skill_file",
        )
    finally:
        conn.close()


def test_workflows_add_preserves_agent_and_source_workflow_provenance(tmp_path):
    service, conn = _service(tmp_path)
    db_path = tmp_path / "reflect.db"
    try:
        service.refresh()
        source = service.workflows.list()[0]
    finally:
        conn.close()
    skill_file = tmp_path / "AGENT-SKILL.md"
    skill_file.write_text(
        "---\n"
        "name: bounded-debug-loop\n"
        "description: Change state between bounded debugging iterations.\n"
        "---\n\n"
        "# Bounded debug loop\n\n1. Observe.\n2. Change state.\n3. Verify or stop.\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        [
            "workflows",
            "add",
            str(skill_file),
            "--type",
            "loop",
            "--source-agent",
            "codex",
            "--from-workflow",
            source.id,
            "--db-path",
            str(db_path),
        ],
    )

    assert result.exit_code == 0
    assert "Authorship: agent draft (codex)" in result.output
    assert f"from {source.id}" in result.output
    conn = connect_sqlite(db_path)
    try:
        row = conn.execute(
            """
            SELECT support_count,
                   json_extract(content_json, '$.source.kind'),
                   json_extract(content_json, '$.source.agent'),
                   json_extract(content_json, '$.source.workflow_id'),
                   json_extract(provenance_json, '$.source')
            FROM workflow_candidates
            WHERE json_extract(content_json, '$.slug') = 'bounded-debug-loop'
            """
        ).fetchone()
        assert tuple(row) == (
            source.support_count,
            "agent_authored",
            "codex",
            source.id,
            "agent_authored",
        )
    finally:
        conn.close()
