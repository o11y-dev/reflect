from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from reflect.context import ReflectContextService
from reflect.core import main
from reflect.improvements.milestones import WorkflowMilestoneService
from reflect.improvements.models import (
    EvidenceRef,
    ImprovementSummary,
    ObservationDraft,
    ObservationStatus,
    RuleDefinition,
    Severity,
    WorkflowProposal,
    WorkflowStatus,
)
from reflect.improvements.nudge_exchange import NudgeFileExchange
from reflect.improvements.nudges import HookNudgeBridge, NudgeService
from reflect.improvements.rules import RetryLoopRule
from reflect.improvements.service import ImprovementService
from reflect.improvements.team import TeamBundleService
from reflect.improvements.workflow_identity import (
    workflow_contract_signature,
    workflow_revision_hash,
)
from reflect.store.migrate import migrate
from reflect.store.normalize import backfill_tool_call_hashes
from reflect.store.sqlite import connect_sqlite

NOW = "2026-07-01T10:00:00+00:00"
CONTRACT_SIGNALS = (("Read", "inspect"), ("Edit", "apply patch"), ("exec", "pytest"))


def test_improvement_summary_is_paginated_and_summary_first(tmp_path):
    service, conn = _service(tmp_path)
    try:
        service.repository.sync_rule_definitions(
            [
                RuleDefinition(
                    id="bounded_evidence",
                    version=1,
                    category="reliability",
                    title="Bounded evidence",
                    description="Exercise the bounded MCP response.",
                )
            ],
            now=NOW,
        )
        observation_id = service.repository.upsert_observation(
            ObservationDraft(
                rule_id="bounded_evidence",
                rule_version=1,
                scope_type="project",
                scope_id="repo-1",
                fingerprint="bounded-evidence",
                category="reliability",
                title="Bounded evidence",
                summary="A concise finding.",
                metric_name="calls",
                metric_value=1,
                metric_unit="calls",
                metric_direction="lower_is_better",
                impact_score=50,
                severity=Severity.MEDIUM,
                confidence=0.8,
                evidence=[
                    EvidenceRef(
                        entity_type="session",
                        entity_id="session-1",
                        session_id="session-1",
                        summary_redacted="Evidence row",
                    )
                ],
            ),
            now=NOW,
        )
        conn.commit()

        context = ReflectContextService(conn, initialize_schema=False)
        summary = context.improvements_summary(path=tmp_path, limit=1)
        full = context.improvements_summary(
            path=tmp_path,
            limit=1,
            detail="full",
            evidence_limit=1,
        )

        assert summary["count"] == 1
        assert summary["findings"][0]["id"] == observation_id
        assert "evidence" not in summary["findings"][0]
        assert summary["findings"][0]["evidence_count"] == 1
        assert summary["freshness"]["evidence_cutoff"] is not None
        assert summary["freshness"]["safe_for_before_after"] is False
        assert full["findings"][0]["evidence"][0]["session_id"] == "session-1"
    finally:
        conn.close()


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
    conn.execute(
        """
        INSERT INTO workspaces(
          id, root_path, path_hash, label, repo_id, source_key, confidence,
          raw_json, created_at, updated_at
        ) VALUES ('workspace-1', ?, 'workspace-1-hash', 'reflect', 'repo-1',
                  'test', 1, '{}', ?, ?)
        """,
        (str(tmp_path), NOW, NOW),
    )
    conn.execute(
        "UPDATE sessions SET workspace_id = 'workspace-1' WHERE repo_id = 'repo-1'"
    )
    conn.commit()
    return ImprovementService(conn), conn


def _workflow_contract(
    signature_hash: str,
    *,
    applicability: dict[str, str] | None = None,
    validation: dict[str, int] | None = None,
) -> dict[str, object]:
    contract: dict[str, object] = {
        "signature_hash": signature_hash,
        "milestones": [
            {"id": role, "role": role, "required": True, "evidence": "corroborated"}
            for role in ("observe", "act", "verify")
        ],
    }
    if applicability:
        contract["applicability"] = applicability
    if validation:
        contract["validation"] = validation
    return contract


def _configure_contract(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    intervention_id: str,
    contract: dict[str, object],
    exposed_at: str,
    target_metric: str | None = None,
    measurement_window: int | None = None,
    metric_direction: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE workflow_candidates
        SET target_metric = COALESCE(?, target_metric),
            task_archetype_id = 'implementation',
            measurement_window = COALESCE(?, measurement_window),
            content_json = json_set(content_json, '$.workflow_contract', json(?))
        WHERE id = ?
        """,
        (target_metric, measurement_window, json.dumps(contract), candidate_id),
    )
    conn.execute(
        """
        UPDATE workflow_versions
        SET content_json = json_set(content_json, '$.workflow_contract', json(?))
        WHERE id = (
          SELECT workflow_version_id FROM interventions WHERE id = ?
        )
        """,
        (json.dumps(contract), intervention_id),
    )
    if metric_direction:
        conn.execute(
            """
            UPDATE observations SET metric_direction = ?
            WHERE id = (SELECT observation_id FROM workflow_candidates WHERE id = ?)
            """,
            (metric_direction, candidate_id),
        )
    conn.execute(
        "UPDATE interventions SET exposure_started_at = ? WHERE id = ?",
        (exposed_at, intervention_id),
    )


def _insert_contract_execution(
    conn: sqlite3.Connection,
    execution_unit_id: str,
    *,
    started_at: str,
    sequence_base: int,
    verification_passed: bool = True,
    mcp_task_run_id: str | None = None,
    tool_count: int = len(CONTRACT_SIGNALS),
) -> None:
    conn.execute(
        """
        INSERT INTO execution_units(
          id, session_id, mcp_task_run_id, source, source_confidence,
          workspace_id, repo_id, agent_id, started_at, ended_at, status,
          outcome, verification_passed, eligible, boundary_json, created_at, updated_at
        ) VALUES (?, 'session-1', ?, 'mcp_task_run', 1, 'workspace-1', 'repo-1',
                  'agent-1', ?, ?, 'completed', 'success', ?, 1, '{}', ?, ?)
        """,
        (
            execution_unit_id,
            mcp_task_run_id,
            started_at,
            started_at,
            int(verification_passed),
            NOW,
            NOW,
        ),
    )
    conn.execute(
        """
        INSERT INTO execution_unit_archetypes(
          execution_unit_id, task_archetype_id, confidence, mixed, features_json,
          classified_at, updated_at
        ) VALUES (?, 'implementation', 1, 0, '{}', ?, ?)
        """,
        (execution_unit_id, NOW, NOW),
    )
    for offset, (tool_name, preview) in enumerate(CONTRACT_SIGNALS):
        step_id = f"{execution_unit_id}-step-{offset}"
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, raw_attrs_json,
              created_at, updated_at
            ) VALUES (?, 'session-1', ?, 'tool_call', ?, 'ok', '{}', ?, ?)
            """,
            (step_id, sequence_base + offset, started_at, NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO execution_unit_steps(execution_unit_id, step_id, session_id, created_at)
            VALUES (?, ?, 'session-1', ?)
            """,
            (execution_unit_id, step_id, NOW),
        )
        if offset < tool_count:
            conn.execute(
                """
                INSERT INTO tool_calls(
                  id, step_id, session_id, tool_name, status,
                  input_preview_redacted, raw_attrs_json, created_at, updated_at
                ) VALUES (?, ?, 'session-1', ?, 'ok', ?, '{}', ?, ?)
                """,
                (
                    f"{execution_unit_id}-tool-{offset}",
                    step_id,
                    tool_name,
                    preview,
                    NOW,
                    NOW,
                ),
            )


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

        assert first["detected"] >= 2
        assert first["skills"] == len(service.workflows.list())
        assert second["detected"] == first["detected"]
        assert second_ids == first_ids
        assert "repeated_tool_failure_chain" in first_ids
        assert "retry_loop_without_state_change" not in first_ids
        assert "missing_or_late_verification" in first_ids
        assert conn.execute("SELECT COUNT(*) FROM rule_definitions").fetchone()[0] == 9
        assert conn.execute("SELECT COUNT(*) FROM workflow_candidates").fetchone()[0] <= len(first_ids)
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
        ).fetchone()[0] == 0
        assert any(item.tool_name and item.tool_name.lower() == "exec" for item in service.loops.list())
    finally:
        conn.close()


def test_refresh_retires_legacy_retry_observations_without_deleting_history(tmp_path):
    service, conn = _service(tmp_path)
    try:
        definition = RetryLoopRule.definition
        service.repository.sync_rule_definitions((definition,), now=NOW)
        draft = ObservationDraft(
                rule_id=definition.id,
                rule_version=definition.version,
                scope_type="repository",
                scope_id="repo-1",
                repo_id="repo-1",
                fingerprint="legacy-retry",
                category=definition.category,
                title="exec retries repeat without a changed input",
                summary="Legacy retry observation.",
                metric_name="identical_retry_calls",
                metric_value=3,
                metric_unit="calls",
                metric_direction="lower_is_better",
                impact_score=80,
                severity="high",
                confidence=0.9,
                occurrence_count=3,
                affected_session_count=1,
            )
        observation_id = service.repository.upsert_observation(
            draft,
            now=NOW,
        )
        proposal = WorkflowProposal(
            title="Workflow: legacy retry",
            hypothesis="Changing state before retrying should reduce identical calls.",
            risk="low",
            content={
                "slug": "legacy-retry",
                "behavior_type": "loop",
                "suggested_artifact": "skill",
                "steps": ["Change relevant state before retrying."],
            },
            target_metric="identical_retry_calls",
            target_value=0,
            measurement_window=5,
        )
        pending_candidate_id = service.repository.ensure_candidate(
            observation_id,
            proposal=proposal,
            now=NOW,
        )
        active_observation_id = service.repository.upsert_observation(
            draft.model_copy(
                update={
                    "fingerprint": "legacy-retry-active",
                    "title": "active exec retries repeat without a changed input",
                }
            ),
            now=NOW,
        )
        active_candidate_id = service.repository.ensure_candidate(
            active_observation_id,
            proposal=proposal.model_copy(
                update={
                    "title": "Workflow: active legacy retry",
                    "content": {**proposal.content, "slug": "legacy-retry-active"},
                }
            ),
            now=NOW,
        )
        conn.execute(
            "UPDATE workflow_candidates SET status = 'approved' WHERE id = ?",
            (active_candidate_id,),
        )
        conn.commit()

        service.refresh()

        assert conn.execute(
            "SELECT lifecycle_state FROM rule_definitions WHERE id = ?",
            (definition.id,),
        ).fetchone()[0] == "retired"
        assert conn.execute(
            "SELECT status FROM observations WHERE id = ?",
            (observation_id,),
        ).fetchone()[0] == "resolved"
        assert conn.execute(
            "SELECT COUNT(*) FROM observations WHERE id = ?",
            (observation_id,),
        ).fetchone()[0] == 1
        pending_status, pending_checks = conn.execute(
            "SELECT status, checks_json FROM workflow_candidates WHERE id = ?",
            (pending_candidate_id,),
        ).fetchone()
        assert pending_status == "stale"
        assert json.loads(pending_checks)["stale_reason"] == "source_rule_retired"
        assert conn.execute(
            "SELECT status FROM workflow_candidates WHERE id = ?",
            (active_candidate_id,),
        ).fetchone()[0] == "approved"
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


def test_agent_native_loop_is_detected_once_and_sorted_before_promoted_history(tmp_path):
    service, conn = _service(tmp_path)
    try:
        conn.execute("UPDATE agents SET name = 'Cursor' WHERE id = 'agent-1'")
        conn.execute(
            """
            UPDATE tool_calls
            SET input_preview_redacted = '{"cmd":"echo AGENT_LOOP_WAKE_mrchase"}',
                input_hash = 'cursor-loop-wake'
            WHERE id = 'tool-1'
            """
        )
        conn.commit()

        refresh = service.loops.refresh()
        records = service.loops.list()
        native = next(item for item in records if item.kind.value == "agent_native")
        stalled = next(item for item in records if item.kind.value == "stalled")
        service.loops.mark_promoted(stalled.id, "skill-existing")
        ordered = service.loops.list()

        assert refresh["agent_native"] == 1
        assert native.evidence["command"] == "/loop"
        assert native.evidence["objective_label"] == "mrchase"
        assert service.loops.show(native.id).occurrences[0].session_id == "session-1"
        assert ordered[0].id == native.id
        assert ordered[-1].status.value == "promoted"
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
        target_content = {
            **target.content,
            "slug": "overflow-target",
            "description": "Overflow target procedure.",
        }
        filler_content = {
            **target.content,
            "slug": "bulk-filler",
            "description": "Bulk filler procedure.",
        }
        conn.execute(
            """
            UPDATE workflow_candidates
            SET content_json = ?, contract_signature = ?, revision_hash = ?, confidence = 0
            WHERE id = ?
            """,
            (
                json.dumps(target_content, sort_keys=True),
                workflow_contract_signature(target_content),
                workflow_revision_hash(target.title, target_content),
                target.id,
            ),
        )
        conn.executemany(
            """
            INSERT INTO workflow_candidates(
              id, observation_id, task_archetype_id, action_type, title, hypothesis,
              scope, risk, content_json, contract_signature, revision_hash,
              confidence, target_metric,
              target_value, measurement_window, status, checks_json, provenance_json,
              created_at, updated_at
            )
            SELECT ?, observation_id, task_archetype_id, ?, title, hypothesis,
                   scope, risk, ?, ?, ?, 1.0, target_metric,
                   target_value, measurement_window, status, checks_json, provenance_json,
                   created_at, updated_at
            FROM workflow_candidates WHERE id = ?
            """,
            [
                (
                        f"bulk-candidate-{index:03d}",
                        f"bulk-action-{index:03d}",
                        json.dumps(filler_content, sort_keys=True),
                        workflow_contract_signature(filler_content),
                        workflow_revision_hash(target.title, filler_content),
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
        rule = next(
            item
            for item in service.repository.list_rule_summaries()
            if item.id == source.rule_id
        )
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
        variant_data = dict(clone_data)
        variant_data.update(
            {
                "scope_id": f"{source.scope_id}-different-procedure",
                "fingerprint": f"{source.fingerprint}-different-procedure",
            }
        )
        variant_observation_id = service.repository.upsert_observation(
            ObservationDraft.model_validate(variant_data),
            now=NOW,
        )
        variant_content = {
            **candidate.content,
            "description": "Use when a different tool repeats without a changed input.",
        }
        variant_candidate_id = service.repository.ensure_candidate(
            variant_observation_id,
            proposal=WorkflowProposal(
                title="Workflow: Different tool retries repeat without a changed input",
                hypothesis=candidate.hypothesis,
                risk=candidate.risk,
                content=variant_content,
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
        target_signature = workflow_contract_signature(candidate.content)
        target_observations = [
            item
            for item in observations
            if item.candidate_id
            and workflow_contract_signature(candidates[item.candidate_id].content)
            == target_signature
        ]
        findings = service.list_findings(limit=500)
        target_finding = next(
            item
            for item in findings
            if item.candidate_id
            and workflow_contract_signature(candidates[item.candidate_id].content)
            == target_signature
        )

        assert len(target_observations) >= 2
        assert target_finding.observation_count == len(target_observations)
        assert target_finding.source_scope_count == len(
            {f"{item.scope_type}:{item.scope_id}" for item in target_observations}
        )
        assert target_finding.title == rule.title
        assert target_finding.summary.startswith(rule.description)
        assert target_finding.title != candidate.title
        assert target_finding.summary != candidate.content["description"]
        variant_finding = next(
            item for item in findings if item.candidate_id == variant_candidate_id
        )
        assert variant_finding.observation_count == 1
        assert variant_finding.candidate_id == variant_candidate_id
        assert variant_finding.title == source.title
        assert variant_finding.summary == source.summary
        assert variant_observation_id not in service.finding_observation_ids(source.id)
        assert variant_observation_id not in service.repository.workflow_evidence_ledger(
            candidate.id
        ).observation_ids
        assert variant_candidate_id in {
            item.id for item in service.workflows.list(limit=500)
        }
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
        assert service.repository.workflow_evidence_ledger(candidate.id).observation_ids
        active = service.workflows.show(candidate.id)
        assert active.status == WorkflowStatus.APPROVED
        assert active.lifecycle.deployment.value == "active"
        assert conn.execute(
            "SELECT status FROM evaluations WHERE workflow_version_id = (SELECT workflow_version_id FROM interventions WHERE id = ?)",
            (applied["intervention_id"],),
        ).fetchone()[0] == "passed"

        rolled_back = service.workflows.rollback(candidate.id)
        assert rolled_back["status"] == "rolled_back"
        assert not target.exists()
        rolled_back_candidate = service.workflows.show(candidate.id)
        assert rolled_back_candidate.status == WorkflowStatus.APPROVED
        assert rolled_back_candidate.lifecycle.deployment.value == "rolled_back"
    finally:
        conn.close()


def test_observation_session_ledger_without_workflow_candidate(tmp_path):
    service, conn = _service(tmp_path)
    try:
        service.repository.sync_rule_definitions(
            [
                RuleDefinition(
                    id="test_rule_no_candidate",
                    version=1,
                    category="reliability",
                    title="Test rule with no candidate",
                    description="Synthetic rule used to test the no-candidate session ledger path.",
                )
            ],
            now=NOW,
        )
        draft = ObservationDraft(
            rule_id="test_rule_no_candidate",
            rule_version=1,
            scope_type="user",
            scope_id="local",
            fingerprint="test-fingerprint-no-candidate",
            category="reliability",
            title="Example finding with no proposed workflow",
            summary="Example summary",
            metric_name="identical_retry_calls",
            metric_value=3,
            metric_unit="calls",
            metric_direction="lower_is_better",
            impact_score=90,
            severity=Severity.HIGH,
            confidence=0.9,
            affected_session_count=2,
            evidence=[
                EvidenceRef(
                    entity_type="session",
                    entity_id="session-1",
                    session_id="session-1",
                    summary_redacted="Session shows repeated retries",
                ),
                EvidenceRef(
                    entity_type="session",
                    entity_id="session-2",
                    session_id="session-2",
                    summary_redacted="Session shows repeated retries",
                ),
            ],
        )
        observation_id = service.repository.upsert_observation(draft, now=NOW)
        conn.commit()

        observation = service.repository.get_observation(observation_id)
        assert observation.candidate_id is None

        ledger = service.finding_evidence_ledger(observation_id)
        assert ledger.candidate_id is None
        assert ledger.observation_id == observation_id
        assert ledger.provenance_session_count == 2
        assert {row.session_id for row in ledger.provenance_sessions} == {"session-1", "session-2"}
        assert ledger.support_execution_unit_count == 0
        assert ledger.support_execution_units == []
    finally:
        conn.close()


def test_observation_session_ledger_missing_observation_raises(tmp_path):
    service, conn = _service(tmp_path)
    try:
        with pytest.raises(KeyError):
            service.finding_evidence_ledger("missing-observation")
    finally:
        conn.close()


def test_observation_session_ledger_reflects_grouped_finding(tmp_path):
    """A candidate-less finding groups same rule_id/title observations across scopes;
    the session ledger fetched from any one member must reflect the whole group, not
    just that member's own evidence."""
    service, conn = _service(tmp_path)
    try:
        service.repository.sync_rule_definitions(
            [
                RuleDefinition(
                    id="test_rule_grouped",
                    version=1,
                    category="reliability",
                    title="Test rule grouped across scopes",
                    description="Synthetic rule used to test multi-scope finding grouping.",
                )
            ],
            now=NOW,
        )

        def _draft(scope_id: str, session_id: str) -> ObservationDraft:
            return ObservationDraft(
                rule_id="test_rule_grouped",
                rule_version=1,
                scope_type="project",
                scope_id=scope_id,
                fingerprint=f"test-fingerprint-grouped-{scope_id}",
                category="reliability",
                title="Grouped finding with no proposed workflow",
                summary="Example summary",
                metric_name="identical_retry_calls",
                metric_value=3,
                metric_unit="calls",
                metric_direction="lower_is_better",
                impact_score=90,
                severity=Severity.HIGH,
                confidence=0.9,
                affected_session_count=1,
                evidence=[
                    EvidenceRef(
                        entity_type="session",
                        entity_id=session_id,
                        session_id=session_id,
                        summary_redacted="Session shows repeated retries",
                    ),
                ],
            )

        first_id = service.repository.upsert_observation(_draft("scope-a", "session-1"), now=NOW)
        second_id = service.repository.upsert_observation(_draft("scope-b", "session-2"), now=NOW)
        conn.commit()

        findings = service.list_findings()
        # A 2+ member finding surfaces the rule title, not the individual observation title.
        finding = next(item for item in findings if item.title == "Test rule grouped across scopes")
        assert finding.observation_count == 2
        assert finding.affected_session_count == 2
        assert finding.candidate_id is None

        resolved_ids = service.resolve_finding_observation_ids(first_id)
        assert set(resolved_ids) == {first_id, second_id}

        ledger = service.finding_evidence_ledger(first_id)
        assert ledger.provenance_session_count == 2
        assert {row.session_id for row in ledger.provenance_sessions} == {"session-1", "session-2"}
    finally:
        conn.close()


def test_inbox_omits_findings_without_retained_provenance_sessions(tmp_path):
    service, conn = _service(tmp_path)
    try:
        service.repository.sync_rule_definitions(
            [
                RuleDefinition(
                    id="test_rule_pruned_source",
                    version=1,
                    category="reliability",
                    title="Test pruned source rule",
                    description="Synthetic rule used to test retained evidence filtering.",
                )
            ],
            now=NOW,
        )
        draft = ObservationDraft(
            rule_id="test_rule_pruned_source",
            rule_version=1,
            scope_type="project",
            scope_id="repo-1",
            fingerprint="test-fingerprint-pruned-source",
            category="reliability",
            title="Finding whose source session was pruned",
            summary="Example summary",
            metric_name="retries",
            metric_value=3,
            metric_unit="calls",
            metric_direction="lower_is_better",
            impact_score=70,
            severity=Severity.MEDIUM,
            confidence=0.9,
            affected_session_count=1,
            evidence=[
                EvidenceRef(
                    entity_type="session",
                    entity_id="session-1",
                    session_id="session-1",
                    summary_redacted="Source evidence",
                )
            ],
        )
        observation_id = service.repository.upsert_observation(draft, now=NOW)
        conn.commit()
        assert any(item.id == observation_id for item in service.list_findings())

        conn.execute("DELETE FROM sessions WHERE id = 'session-1'")
        conn.commit()

        assert all(item.id != observation_id for item in service.list_findings())
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
        assert preview["suggested_unique_slug"] == f"{first.content['slug']}-2"
        with pytest.raises(RuntimeError, match="same procedure is already active"):
            service.workflows.apply(second.id, project_root=project_root)

        renamed = service.workflows.edit(
            second.id,
            content={**second.content, "slug": preview["suggested_unique_slug"]},
        )
        renamed_preview = service.workflows.preview(renamed.id, project_root=project_root)
        assert renamed_preview["checks"]["apply_allowed"] is True
        assert renamed_preview["change_kind"] == "create"
    finally:
        conn.close()


def test_tool_failure_workflows_use_tool_specific_names_and_retry_impact(tmp_path):
    service, conn = _service(tmp_path)
    try:
        for index, session_id in enumerate(("session-1", "session-2"), start=1):
            step_id = f"read-failure-step-{index}"
            conn.execute(
                """
                INSERT INTO steps(
                  id, session_id, seq, type, started_at, status, raw_attrs_json,
                  created_at, updated_at
                ) VALUES (?, ?, ?, 'tool_call', ?, 'failed', '{}', ?, ?)
                """,
                (step_id, session_id, 90 + index, NOW, NOW, NOW),
            )
            conn.execute(
                """
                INSERT INTO tool_calls(
                  id, step_id, session_id, tool_name, status, input_hash, error_type,
                  raw_attrs_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'Read', 'failed', ?, 'missing_file', '{}', ?, ?)
                """,
                (f"read-failure-{index}", step_id, session_id, f"read-{index}", NOW, NOW),
            )
        conn.commit()

        service.refresh()
        candidates = [
            item
            for item in service.workflows.list()
            if item.content.get("source", {}).get("rule_id") == "repeated_tool_failure_chain"
        ]

        assert len(candidates) == 2
        assert {item.target_metric for item in candidates} == {"identical_retry_calls"}
        slugs = {str(item.content["slug"]) for item in candidates}
        assert len(slugs) == 2
        assert all(slug.startswith("tool-failure-recovery-") for slug in slugs)
        assert "tool-failure-recovery" not in slugs
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


def test_procedure_impact_uses_first_five_comparable_execution_units(tmp_path):
    service, conn = _service(tmp_path)
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    try:
        service.refresh()
        candidate = service.workflows.list()[0]
        applied = service.workflows.apply(candidate.id, project_root=project_root)
        contract = _workflow_contract(
            "observe-act-verify",
            applicability={
                "repo_id": "repo-1",
                "workspace_id": "workspace-1",
                "task_archetype_id": "implementation",
            },
            validation={"baseline_minimum": 3, "baseline_maximum": 20},
        )
        _configure_contract(
            conn,
            candidate_id=candidate.id,
            intervention_id=applied["intervention_id"],
            contract=contract,
            exposed_at="2026-07-10T00:00:00+00:00",
            target_metric="workflow_adherence",
            measurement_window=5,
            metric_direction="higher_is_better",
        )
        for index in range(8):
            task_id = f"procedure-task-{index}"
            started_at = (
                f"2026-07-0{index + 4}T10:00:00+00:00"
                if index < 3
                else f"2026-07-{index + 8:02d}T10:00:00+00:00"
            )
            _insert_contract_execution(
                conn,
                task_id,
                started_at=started_at,
                sequence_base=100 + index * 3,
                verification_passed=index != 7,
            )
            if index >= 3:
                state = "ignored" if index == 7 else "followed"
                conn.execute(
                    """
                    INSERT INTO workflow_exposures(
                      id, intervention_id, session_id, execution_unit_id,
                      state, evidence_json, created_at
                    ) VALUES (?, ?, 'session-1', ?, ?, '{}', ?)
                    """,
                    (
                        f"procedure-exposure-{index}",
                        applied["intervention_id"],
                        task_id,
                        state,
                        NOW,
                    ),
                )
        conn.commit()

        result = service.measurements.measure(candidate.id)
        ledger = service.measurements.sessions(result["id"])

        assert result["before_count"] == 3
        assert result["after_count"] == 5
        assert result["before_value"] == 1.0
        assert result["after_value"] == 0.8
        assert result["verdict"] == "regressed"
        assert result["cohort"]["unit"] == "execution_units"
        assert ledger["unit"] == "execution_units"
        assert len(ledger["before_execution_units"]) == 3
        assert len(ledger["after_execution_units"]) == 5
        assert all(item["evidence_count"] == 3 for item in ledger["before_execution_units"])
        assert all(item["metric_value"] == 1.0 for item in ledger["before_execution_units"])
        assert ledger["after_execution_units"][-1]["adherence_state"] == "not_followed"
        assert ledger["after_execution_units"][-1]["metric_value"] == 0.0
        assert ledger["after_execution_units"][0]["evidence_summaries"] == [
            "Observed roles: observe → act → verify",
            "100% required milestone coverage",
        ]
    finally:
        conn.close()


def test_reported_procedure_milestones_require_independent_tool_corroboration(tmp_path):
    service, conn = _service(tmp_path)
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    try:
        service.refresh()
        candidate = service.workflows.list()[0]
        applied = service.workflows.apply(candidate.id, project_root=project_root)
        contract = _workflow_contract("corroboration-sequence")
        _configure_contract(
            conn,
            candidate_id=candidate.id,
            intervention_id=applied["intervention_id"],
            contract=contract,
            exposed_at="2026-07-01T00:00:00+00:00",
        )
        service.skills.refresh()
        version_id = conn.execute(
            """
            SELECT sv.id
            FROM skill_versions sv
            JOIN skills s ON s.id = sv.skill_id
            WHERE json_extract(sv.workflow_json, '$.workflow_contract.signature_hash')
                  = 'corroboration-sequence'
            """
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO mcp_task_runs(
              id, runtime_session_id, runtime_agent, workspace_path, question_hash,
              selected_skills_json, status, started_at, completed_at, outcome,
              verification_passed, created_at, updated_at
                ) VALUES ('procedure-run', 'session-1', 'codex', ?, 'question', ?,
                          'started', ?, ?, 'success', 1, ?, ?)
            """,
            (
                str(project_root),
                json.dumps([{"skill_id": "unused", "version_id": version_id}]),
                "2026-07-03T10:00:00+00:00",
                "2026-07-03T10:10:00+00:00",
                NOW,
                NOW,
            ),
        )
        _insert_contract_execution(
            conn,
            "procedure-unit",
            started_at="2026-07-03T10:00:00+00:00",
            sequence_base=200,
            mcp_task_run_id="procedure-run",
            tool_count=1,
        )
        conn.execute(
            "UPDATE mcp_task_runs SET execution_unit_id = 'procedure-unit' WHERE id = 'procedure-run'"
        )
        conn.commit()
        milestones = WorkflowMilestoneService(conn)
        for role in ("observe", "act", "verify"):
            milestones.record(
                task_run_id="procedure-run",
                skill_version_id=version_id,
                milestone_id=role,
                state="completed",
                idempotency_key=f"{role}-done",
            )

        service.adherence.refresh()
        assert conn.execute(
            "SELECT state FROM workflow_exposures WHERE execution_unit_id = 'procedure-unit'"
        ).fetchone()[0] == "invoked"

        for offset, (tool_name, preview) in enumerate(
            CONTRACT_SIGNALS[1:],
            start=1,
        ):
            conn.execute(
                """
                INSERT INTO tool_calls(
                  id, step_id, session_id, tool_name, status,
                  input_preview_redacted, raw_attrs_json, created_at, updated_at
                ) VALUES (?, ?, 'session-1', ?, 'ok', ?, '{}', ?, ?)
                """,
                (
                    f"procedure-unit-tool-{offset}",
                    f"procedure-unit-step-{offset}",
                    tool_name,
                    preview,
                    NOW,
                    NOW,
                ),
            )
        conn.commit()
        service.adherence.refresh()

        state, evidence_json = conn.execute(
            """
            SELECT state, evidence_json FROM workflow_exposures
            WHERE execution_unit_id = 'procedure-unit'
            """
        ).fetchone()
        assert state == "followed"
        assert json.loads(evidence_json)["corroborated"] is True
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
                  id, agent_id, workspace_id, repo_id, started_at, ended_at, status,
                  created_at, updated_at
                ) VALUES (?, 'agent-1', 'workspace-1', 'repo-1', ?, ?, 'completed', ?, ?)
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
                              summary, created_at, updated_at
                            ) VALUES (?, ?, ?, 'tool_call', ?, 'ok', '{}',
                                      'Implement feature change', ?, ?)
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
                conn.execute(
                    """
                    INSERT INTO conversation_facts(
                      id, step_id, session_id, kind, role, content_hash,
                      content_length, raw_attrs_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'prompt', 'user', ?, 24, '{}', ?, ?)
                    """,
                    (
                        f"p0-prompt-{index}",
                        f"p0-step-{index}-1",
                        session_id,
                        f"p0-prompt-hash-{index}",
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
        ).fetchone()[0] == 1
        candidate_row = conn.execute(
            """
            SELECT wc.id, wc.measurement_window,
                   json_extract(wc.content_json, '$.workflow_contract.signature_hash')
            FROM workflow_candidates wc
            JOIN observations o ON o.id = wc.observation_id
            WHERE o.rule_id = 'high_performing_repeated_workflow'
            """
        ).fetchone()
        assert candidate_row[1] == 5
        assert candidate_row[2]

        project_root = tmp_path
        (project_root / ".git").mkdir(parents=True)
        service.workflows.apply(candidate_row[0], project_root=project_root)
        service.skills.refresh()
        service.refresh()

        assert conn.execute(
            """
            SELECT status FROM observations
            WHERE rule_id = 'high_performing_repeated_workflow'
            """
        ).fetchone()[0] == "resolved"
        assert conn.execute(
            """
            SELECT COUNT(*) FROM workflow_candidates wc
            JOIN observations o ON o.id = wc.observation_id
            WHERE o.rule_id = 'high_performing_repeated_workflow'
            """
        ).fetchone()[0] == 1
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
            if item.content.get("source", {}).get("rule_id") == "repeated_tool_failure_chain"
        )
        applied = service.workflows.apply(candidate.id, project_root=project_root)
        conn.execute(
            "UPDATE interventions SET exposure_started_at = '2026-07-10T00:00:00+00:00' WHERE id = ?",
            (applied["intervention_id"],),
        )
        for index in range(1, 16):
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
            for repeat in range(1 if before else 3):
                repeated_step_id = f"{step_id}-{repeat}"
                conn.execute(
                    """
                    INSERT INTO steps(
                      id, session_id, seq, type, started_at, status, summary, raw_attrs_json,
                      created_at, updated_at
                    ) VALUES (?, ?, ?, 'tool_call', ?, ?, 'Fix failing error', ?, ?, ?)
                    """,
                    (
                        repeated_step_id,
                        session_id,
                        repeat + 1,
                        started_at,
                        status,
                        json.dumps({"skill": candidate.content["slug"]}) if not before else "{}",
                        NOW,
                        NOW,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO tool_calls(
                      id, step_id, session_id, tool_name, status, input_hash,
                      input_preview_redacted, error_type, raw_attrs_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'exec', ?, ?, ?, ?, '{}', ?, ?)
                    """,
                    (
                        f"measure-tool-{index}-{repeat}",
                        repeated_step_id,
                        session_id,
                        status,
                        f"measure-input-{index}",
                        "poetry run pytest -q" if not before else "safe command",
                        error_type,
                        NOW,
                        NOW,
                    ),
                )
            conn.execute(
                """
                INSERT INTO conversation_facts(
                  id, step_id, session_id, kind, role, content_hash,
                  content_length, raw_attrs_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'prompt', 'user', ?, 17, '{}', ?, ?)
                """,
                (
                    f"measure-prompt-{index}",
                    f"{step_id}-0",
                    session_id,
                    f"measure-prompt-hash-{index}",
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
        assert measurement["cohort"]["measurement_state"] == "measured"
        assert measurement["cohort"]["quality"]["after_signal_coverage"] == 1
        cohorts = service.measurements.sessions(measurement["id"])
        assert cohorts["candidate_id"] == candidate.id
        assert cohorts["snapshot_exact"] is True
        assert cohorts["before_count"] == measurement["before_count"]
        assert cohorts["after_count"] == measurement["after_count"]
        assert len(cohorts["before_execution_units"]) == measurement["before_count"]
        assert len(cohorts["after_execution_units"]) == measurement["after_count"]
        assert {item["session_id"] for item in cohorts["after_execution_units"]} >= {
            "measure-session-6",
            "measure-session-10",
        }
        metric_values = {
            item["session_id"]: item["metric_value"]
            for item in [
                *cohorts["before_execution_units"],
                *cohorts["after_execution_units"],
            ]
        }
        assert metric_values["measure-session-3"] == 0.0
        assert metric_values["measure-session-6"] == 3.0
        assert all(
            item["metric_value"] is not None
            for item in cohorts["after_execution_units"]
        )
        skill = service.skills.skill_for_candidate(candidate.id)
        assert skill.measurement_count == 1
        assert service.skills.show(skill.id).measurements[0].verdict == "regressed"
        assert second["measurements_created"] == 0
        assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == count_after_first_refresh
    finally:
        conn.close()


def test_failure_rate_impact_is_not_measurable_with_unknown_status_baseline(tmp_path):
    service, conn = _service(tmp_path)
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    try:
        service.refresh()
        candidate = next(
            item
            for item in service.workflows.list()
            if item.content.get("source", {}).get("rule_id") == "repeated_tool_failure_chain"
        )
        conn.execute(
            """
            UPDATE workflow_candidates
            SET target_metric = 'tool_failure_rate', task_archetype_id = NULL
            WHERE id = ?
            """,
            (candidate.id,),
        )
        applied = service.workflows.apply(candidate.id, project_root=project_root)
        conn.execute(
            "UPDATE interventions SET exposure_started_at = '2026-07-10T00:00:00+00:00' WHERE id = ?",
            (applied["intervention_id"],),
        )
        for index in range(5):
            session_id = f"unknown-baseline-{index}"
            started_at = f"2026-07-0{index + 3}T12:00:00+00:00"
            conn.execute(
                """
                INSERT INTO sessions(
                  id, agent_id, repo_id, started_at, ended_at, status,
                  created_at, updated_at
                ) VALUES (?, 'agent-1', 'repo-1', ?, ?, 'completed', ?, ?)
                """,
                (session_id, started_at, started_at, NOW, NOW),
            )
            conn.execute(
                """
                INSERT INTO steps(
                  id, session_id, seq, type, started_at, status, summary, raw_attrs_json,
                  created_at, updated_at
                ) VALUES (?, ?, 1, 'tool_call', ?, 'unknown', 'Fix failing error', '{}', ?, ?)
                """,
                (f"unknown-step-{index}", session_id, started_at, NOW, NOW),
            )
            conn.execute(
                """
                INSERT INTO tool_calls(
                  id, step_id, session_id, tool_name, status, input_hash,
                  raw_attrs_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'Read', 'unknown', ?, '{}', ?, ?)
                """,
                (
                    f"unknown-tool-{index}",
                    f"unknown-step-{index}",
                    session_id,
                    f"unknown-input-{index}",
                    NOW,
                    NOW,
                ),
            )
            conn.execute(
                """
                INSERT INTO conversation_facts(
                  id, step_id, session_id, kind, role, content_hash,
                  content_length, raw_attrs_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'prompt', 'user', ?, 17, '{}', ?, ?)
                """,
                (
                    f"unknown-prompt-{index}",
                    f"unknown-step-{index}",
                    session_id,
                    f"unknown-prompt-hash-{index}",
                    NOW,
                    NOW,
                ),
            )
        conn.commit()

        service.prepare_workflow_evidence()
        result = service.measurements.measure(candidate.id)
        ledger = service.measurements.sessions(result["id"])

        assert result["verdict"] == "insufficient_data"
        assert result["before_value"] is None
        assert result["after_value"] is None
        assert result["cohort"]["measurement_state"] == "not_measurable"
        assert result["cohort"]["quality"]["before_signal_coverage"] < 0.9
        assert "Baseline execution-unit signal coverage" in result["cohort"]["measurement_reasons"][0]
        assert ledger["measurement_state"] == "not_measurable"
        assert ledger["measurement_reasons"] == result["cohort"]["measurement_reasons"]
    finally:
        conn.close()


def test_measurement_keeps_completed_fixed_size_cohort_stable(tmp_path):
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
              id, session_id, seq, type, started_at, status, summary, raw_attrs_json,
              created_at, updated_at
            ) VALUES (?, ?, 1, 'tool_call', ?, ?, 'Fix failing error', '{}', ?, ?)
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
        conn.execute(
            """
            INSERT INTO conversation_facts(
              id, step_id, session_id, kind, role, content_hash,
              content_length, raw_attrs_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'prompt', 'user', ?, 17, '{}', ?, ?)
            """,
            (
                f"prompt-{session_id}",
                f"step-{session_id}",
                session_id,
                f"prompt-hash-{session_id}",
                NOW,
                NOW,
            ),
        )

    try:
        service.refresh()
        candidate = next(
            item
            for item in service.workflows.list()
            if item.content.get("source", {}).get("rule_id") == "repeated_tool_failure_chain"
        )
        service.workflows.edit(
            candidate.id,
            content={**candidate.content, "behavior_type": "verification"},
        )
        conn.execute(
            "UPDATE workflow_candidates SET target_metric = 'tool_failure_rate' WHERE id = ?",
            (candidate.id,),
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

        service.prepare_workflow_evidence()
        first = service.measurements.measure(candidate.id)
        add_session("after-newest", "2026-07-11T01:00:00+00:00", failed=True)
        conn.commit()
        service.prepare_workflow_evidence(session_ids={"after-newest"})
        second = service.measurements.measure(candidate.id, skip_unchanged=True)
        second_sessions = service.measurements.sessions(second["id"])

        assert first["after_count"] == second["after_count"] == candidate.measurement_window
        assert second["created"] is False
        assert second["id"] == first["id"]
        assert second["after_value"] == first["after_value"]
        assert "after-newest" not in {
            item["session_id"] for item in second_sessions["after_execution_units"]
        }
    finally:
        conn.close()


def test_ask_labels_pending_guidance_as_unapproved(tmp_path):
    service, conn = _service(tmp_path)
    try:
        service.refresh()
        answer = service.ask("How should I stop retrying failed exec calls?", path=tmp_path)

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
            if item.content.get("source", {}).get("rule_id") == "repeated_tool_failure_chain"
        )
        service.workflows.apply(candidate.id, project_root=project_root)

        answer = service.ask(
            "How should I stop retrying an unchanged failed exec call?",
            path=tmp_path,
        )

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

    with patch("reflect.core.prepare_sql_report_db") as prepare:
        improve_result = runner.invoke(
            main,
            ["improve", "--global", "--period", "all", "--json", "--db-path", str(db_path)],
        )
        prepare.assert_not_called()
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
        prepare.assert_not_called()

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


def test_improve_labels_cli_table_as_observed_improvements(tmp_path):
    service, conn = _service(tmp_path)
    db_path = tmp_path / "reflect.db"
    try:
        service.refresh()
    finally:
        conn.close()

    result = CliRunner().invoke(
        main,
        ["improve", "--global", "--period", "all", "--no-refresh", "--db-path", str(db_path)],
    )

    assert result.exit_code == 0
    assert "Observed Improvements" in result.output
    assert "Improvement Inbox" not in result.output


def test_improve_reports_progress_on_stderr_without_corrupting_json(tmp_path):
    from reflect.preparation import PreparationProgress, PreparationStage

    service, conn = _service(tmp_path)
    db_path = tmp_path / "reflect.db"
    try:
        service.refresh()
    finally:
        conn.close()
    runner = CliRunner()

    def prepare(*_args, progress=None, **_kwargs):
        assert progress is not None
        progress(
            PreparationProgress(
                stage=PreparationStage.INGESTING_TRACES,
                message="Reading new OTLP traces...",
            )
        )
        return {}

    with patch("reflect.core.prepare_sql_report_db", side_effect=prepare):
        result = runner.invoke(
            main,
            [
                "improve",
                "--global",
                "--period",
                "all",
                "--refresh",
                "--json",
                "--db-path",
                str(db_path),
            ],
        )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["observations"]
    assert "Reading new OTLP traces" in result.stderr
    assert "\x1b" not in result.stderr


def test_improve_requires_an_explicit_global_period(tmp_path):
    service, conn = _service(tmp_path)
    db_path = tmp_path / "reflect.db"
    try:
        service.refresh()
    finally:
        conn.close()

    runner = CliRunner()
    missing_period = runner.invoke(
        main,
        ["improve", "--global", "--db-path", str(db_path)],
    )
    unscoped_period = runner.invoke(
        main,
        ["improve", "--period", "week", "--db-path", str(db_path)],
    )

    assert missing_period.exit_code == 2
    assert "--global requires" in missing_period.output
    assert unscoped_period.exit_code == 2
    assert "--period requires --global" in unscoped_period.output


def test_snapshot_commands_refresh_only_when_explicit(tmp_path):
    service, conn = _service(tmp_path)
    db_path = tmp_path / "reflect.db"
    try:
        service.refresh()
    finally:
        conn.close()

    runner = CliRunner()
    with patch("reflect.core.prepare_sql_report_db", return_value={}) as prepare:
        ask_result = runner.invoke(
            main,
            ["ask", "What should I do?", "--json", "--db-path", str(db_path)],
        )
        workflow_result = runner.invoke(
            main,
            ["workflows", "list", "--json", "--db-path", str(db_path)],
        )
        loops_result = runner.invoke(
            main,
            ["loops", "--json", "--db-path", str(db_path)],
        )
        prepare.assert_not_called()

        refreshed = runner.invoke(
            main,
            [
                "ask",
                "What should I do?",
                "--refresh",
                "--json",
                "--db-path",
                str(db_path),
            ],
        )

    assert ask_result.exit_code == 0
    assert workflow_result.exit_code == 0
    assert loops_result.exit_code == 0
    assert refreshed.exit_code == 0
    prepare.assert_called_once()


def test_feedback_never_refreshes_implicitly(tmp_path):
    service, conn = _service(tmp_path)
    db_path = tmp_path / "reflect.db"
    try:
        service.refresh()
    finally:
        conn.close()

    with patch("reflect.core.prepare_sql_report_db") as prepare:
        result = CliRunner().invoke(
            main,
            [
                "feedback",
                "missing-session",
                "--outcome",
                "good",
                "--db-path",
                str(db_path),
            ],
        )

    assert result.exit_code == 1
    assert "Re-run with --refresh" in result.output
    prepare.assert_not_called()


@pytest.mark.parametrize("refresh_flag", [[], ["--no-refresh"]])
def test_improve_never_prepares_a_missing_snapshot_without_refresh(
    tmp_path,
    refresh_flag,
):
    db_path = tmp_path / "missing.db"
    with patch("reflect.core.prepare_sql_report_db") as prepare:
        result = CliRunner().invoke(
            main,
            ["improve", *refresh_flag, "--db-path", str(db_path)],
        )

    assert result.exit_code == 1
    assert "reflect refresh" in result.output
    prepare.assert_not_called()


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
            SELECT json_extract(content_json, '$.source.kind'),
                   json_extract(content_json, '$.source.agent'),
                   json_extract(content_json, '$.source.workflow_id'),
                   json_extract(provenance_json, '$.source')
            FROM workflow_candidates
            WHERE json_extract(content_json, '$.slug') = 'bounded-debug-loop'
            """
        ).fetchone()
        assert tuple(row) == (
            "agent_authored",
            "codex",
            source.id,
            "agent_authored",
        )
        added = next(
            item
            for item in ImprovementService(conn).workflows.list(limit=500)
            if item.content.get("slug") == "bounded-debug-loop"
        )
        assert added.support_execution_unit_count == source.support_execution_unit_count
    finally:
        conn.close()
