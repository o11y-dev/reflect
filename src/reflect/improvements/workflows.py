from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import Any

from reflect.improvements.models import WorkflowCandidateRecord, WorkflowEvidenceLedger
from reflect.improvements.repository import ImprovementRepository, utc_now
from reflect.improvements.workflow_identity import WorkflowIdentity
from reflect.store.user_state import StoredWorkflowDraft, UserStateStore

_SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class WorkflowService:
    """Review, render, apply, and safely roll back workflow candidates."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.repository = ImprovementRepository(conn)

    @staticmethod
    def create_draft(
        state: UserStateStore, *, title: str, content: dict[str, Any],
    ) -> StoredWorkflowDraft:
        """Save authored instructions without requiring an analysis writer.

        Manual instructions carry no claim of observed support or measured
        benefit. The same content validator is used before preview and apply.
        """
        normalized = WorkflowService._validate_content(content)
        return state.save_draft(
            f"draft_{uuid.uuid4().hex}", title=title, content=normalized,
            evidence={"origin": "manual", "verification": "unverified"},
            expected_revision=0,
        )

    @staticmethod
    def suggestion_assessment(candidate: WorkflowCandidateRecord) -> dict:
        """Assess stored procedure evidence without re-running discovery in a request."""
        from pydantic import ValidationError

        from reflect.improvements.models import ProcedureDefinition

        content = candidate.content
        reasons = []
        try:
            definition = ProcedureDefinition.model_validate(content.get('procedure_definition'))
        except (ValidationError, TypeError):
            definition = None
            reasons.append('No source-backed task procedure; inspect the finding in Inbox.')
        procedure = content.get('procedure') if isinstance(content.get('procedure'), dict) else {}
        roots = procedure.get('checked_independent_sessions') or []
        independent = content.get('independent_session_count', len(roots) if isinstance(roots, list) else 0)
        if not isinstance(independent, int) or isinstance(independent, bool) or independent < 0:
            independent = 0
        if definition:
            if definition.task_intent.state != 'identified' or not definition.task_intent.source_refs:
                reasons.append('Task intent is missing or mixed.')
            observed = [step for step in definition.steps if step.source_kind == 'observed']
            if len(observed) < 2 or any(not step.source_refs for step in observed):
                reasons.append('Fewer than two source-backed operations.')
            if not definition.inputs or not definition.expected_output or any(not step.decision for step in definition.steps):
                reasons.append('Inputs, decisions, or expected output are incomplete.')
        if independent < 3:
            reasons.append('Fewer than three independent source sessions.')
        return {'eligible':not reasons, 'reasons':reasons,
                'group_key':candidate.contract_signature,
                'independent_session_count':independent,
                'rank_reasons':(['Explicit task, ordered procedure, and decision points',
                                  f'{independent} independent source sessions'] if not reasons else []),
                'label':'Observed procedure — benefit unmeasured' if not reasons else 'Evidence incomplete'}

    def list(
        self,
        *,
        limit: int = 100,
        behavior_types: set[str] | None = None,
        statuses: set[str] | None = None,
        recorded_only: bool = False,
    ) -> list[WorkflowCandidateRecord]:
        candidates = list(self.repository.iter_candidates())
        if behavior_types:
            candidates = [
                candidate
                for candidate in candidates
                if str(candidate.content.get("behavior_type") or "proven_pattern") in behavior_types
            ]
        grouped: dict[str, list[WorkflowCandidateRecord]] = {}
        for candidate in candidates:
            grouped.setdefault(candidate.contract_signature, []).append(candidate)

        reviewable: list[WorkflowCandidateRecord] = []
        for members in grouped.values():
            eligible = members
            if statuses:
                if statuses == {"pending"} and any(
                    item.lifecycle.deployment.value == "active" for item in members
                ):
                    continue
                eligible = [item for item in members if item.status.value in statuses]
            if not eligible:
                continue
            representative = min(eligible, key=self._representative_sort_key)
            ledger = self.repository.workflow_evidence_ledger(
                representative.id, limit=1, recorded_only=recorded_only,
            )
            ledger_observation_ids = set(ledger.observation_ids)
            evidence_members = [
                item for item in members if item.observation_id in ledger_observation_ids
            ] or [representative]
            reviewable.append(
                representative.model_copy(
                    update={
                        "support_execution_unit_count": (
                            ledger.support_execution_unit_count
                        ),
                        "confidence": max(item.confidence for item in evidence_members),
                        "variant_count": len(evidence_members),
                        "supporting_observation_count": len(ledger_observation_ids),
                        "source_scopes": sorted({item.scope for item in evidence_members}),
                    }
                )
            )
        reviewable.sort(key=self._representative_sort_key)
        return reviewable[:limit]

    def show(
        self, candidate_id: str, *, recorded_only: bool = False,
        evidence_ledger: WorkflowEvidenceLedger | None = None,
    ) -> WorkflowCandidateRecord:
        candidate = self.repository.get_candidate(candidate_id)
        if candidate is None:
            raise KeyError(f"Workflow candidate not found: {candidate_id}")
        members = list(
            self.repository.list_candidates_by_contract(candidate.contract_signature)
        )
        ledger = evidence_ledger or self.repository.workflow_evidence_ledger(
            candidate.id, limit=1, recorded_only=recorded_only,
        )
        ledger_observation_ids = set(ledger.observation_ids)
        evidence_members = [
            item for item in members if item.observation_id in ledger_observation_ids
        ] or [candidate]
        return candidate.model_copy(
            update={
                "support_execution_unit_count": ledger.support_execution_unit_count,
                "confidence": max(item.confidence for item in evidence_members),
                "variant_count": len(evidence_members),
                "supporting_observation_count": max(1, len(ledger_observation_ids)),
                "source_scopes": sorted({item.scope for item in evidence_members}),
            }
        )

    @staticmethod
    def _representative_sort_key(candidate: WorkflowCandidateRecord) -> tuple[int, int, int, int, str]:
        status_priority = {
            "approved": 0,
            "pending": 1,
            "stale": 2,
            "rejected": 3,
        }
        return (
            (4 if candidate.status.value == "rejected" and not candidate.checks.get("dismissal")
             else status_priority.get(candidate.status.value, 9)),
            0 if candidate.content.get("procedure") or candidate.content.get("observed_steps") else 1,
            0 if WorkflowService.suggestion_assessment(candidate)['eligible'] else 1,
            -WorkflowService.suggestion_assessment(candidate)['independent_session_count'],
            candidate.id,
        )

    def preview(self, candidate_id: str, *, project_root: Path) -> dict[str, Any]:
        """Render the exact repository-local change without mutating the filesystem."""
        candidate = self.show(candidate_id)
        content = self._render_skill(candidate)
        root, target = self._target_for(candidate, project_root)
        previous_content = target.read_text(encoding="utf-8") if target.is_file() else ""
        diff = "".join(
            difflib.unified_diff(
                previous_content.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=str(target),
                tofile=str(target),
            )
        )
        ledger = self.repository.workflow_evidence_ledger(candidate_id, limit=50)
        workspace_counts = Counter(
            item.workspace for item in ledger.provenance_sessions if item.workspace
        )
        suggested_root_counts: Counter[tuple[str, bool]] = Counter()
        for workspace, count in workspace_counts.items():
            suggested_root, is_repository = self._normalize_project_root(Path(workspace))
            suggested_root_counts[(str(suggested_root), is_repository)] += count
        suggested_roots = [
            {
                "path": path,
                "provenance_sessions": count,
                "is_repository": is_repository,
                "is_directory": Path(path).is_dir(),
            }
            for (path, is_repository), count in suggested_root_counts.most_common()
        ]
        evidence_project_paths = {
            str(Path(item["path"]).expanduser().resolve())
            for item in suggested_roots
            if item["is_directory"]
        }
        checks = self._target_checks(candidate, root=root, target=target)
        suggested_unique_slug = (
            self._suggest_unique_slug(candidate, root=root)
            if checks["target_conflicts"] and not checks["contract_conflicts"]
            else None
        )
        checks["suggested_unique_slug"] = suggested_unique_slug
        evidence_scope_match = (
            str(root) in evidence_project_paths if evidence_project_paths else None
        )
        checks["evidence_scope_match"] = evidence_scope_match
        checks["advisories"] = []
        if evidence_scope_match is False:
            checks["advisories"].append(
                "This application folder differs from the project folders visible in the source evidence."
            )
        return {
            "candidate_id": candidate_id,
            "contract_signature": candidate.contract_signature,
            "candidate_updated_at": candidate.updated_at,
            "revision_hash": candidate.revision_hash,
            "project_root": str(root),
            "application_root": str(root),
            "application_repository": str(root),
            "is_git_repository": checks["repository_root"],
            "target_path": str(target),
            "target_relative_path": str(target.relative_to(root)),
            "change_kind": (
                "blocked"
                if target.exists() and not target.is_file()
                else "no_change"
                if target.is_file() and previous_content == content
                else "update"
                if target.is_file()
                else "create"
            ),
            "previous_hash": _content_hash(previous_content) if target.is_file() else None,
            "proposed_hash": _content_hash(content),
            "would_change": previous_content != content,
            "diff": diff,
            "content": content,
            "checks": checks,
            "suggested_unique_slug": suggested_unique_slug,
            "suggested_project_roots": suggested_roots,
            "evidence_project_paths": sorted(evidence_project_paths),
            "evidence_repository_paths": sorted(evidence_project_paths),
            "support_execution_unit_count": ledger.support_execution_unit_count,
            "provenance_session_count": ledger.provenance_session_count,
            "exposed_execution_unit_count": ledger.exposed_execution_unit_count,
        }

    def approve(
        self,
        candidate_id: str,
        *,
        actor: str = "local_operator",
    ) -> dict[str, Any]:
        """Approve a reviewed workflow without installing its rendered artifact."""

        candidate = self.show(candidate_id)
        if candidate.status.value == "approved":
            return {
                "candidate_id": candidate_id,
                "status": "approved",
                "idempotent": True,
            }
        if candidate.status.value in {"stale", "rejected"}:
            raise RuntimeError(
                f"Workflow {candidate_id} is {candidate.status.value}; review it before approving"
            )
        now = utc_now()
        self.conn.execute(
            """
            UPDATE workflow_candidates
            SET status = 'approved', reviewer = ?, reviewed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (actor, now, now, candidate_id),
        )
        self.conn.execute(
            "UPDATE observations SET status = 'approved', updated_at = ? WHERE id = ?",
            (now, candidate.observation_id),
        )
        self.repository.record_event(
            entity_type="workflow_candidate",
            entity_id=candidate_id,
            event_type="approved",
            actor=actor,
            details={"installation_changed": False},
            now=now,
        )
        self.conn.commit()
        return {
            "candidate_id": candidate_id,
            "status": "approved",
            "idempotent": False,
        }

    def apply(
        self,
        candidate_id: str,
        *,
        project_root: Path,
        actor: str = "local_operator",
    ) -> dict[str, Any]:
        candidate = self.show(candidate_id)
        if candidate.status.value in {"stale", "rejected"}:
            raise RuntimeError(f"Workflow {candidate_id} is {candidate.status.value}; review it before applying")
        content = self._render_skill(candidate)
        root, target = self._target_for(candidate, project_root)

        active = self.conn.execute(
            """
            SELECT i.id, i.target_path, i.previous_hash, i.applied_hash
            FROM interventions i
            JOIN workflow_versions wv ON wv.id = i.workflow_version_id
            WHERE wv.candidate_id = ? AND i.status = 'active'
            ORDER BY i.created_at DESC LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
        if active:
            intervention_id, active_path, previous_hash, active_hash = active
            if Path(str(active_path)).resolve() != target.resolve():
                raise RuntimeError(
                    f"Workflow {candidate_id} is already active at {active_path}; roll it back before changing scope"
                )
            current_content = target.read_text(encoding="utf-8") if target.exists() else None
            current_hash = _content_hash(current_content) if current_content is not None else None
            proposed_hash = _content_hash(content)
            if current_hash != active_hash:
                self._mark_stale(candidate_id, str(intervention_id), now=utc_now())
                self.conn.commit()
                raise RuntimeError(
                    "The active workflow file differs from Reflect's applied version; review or roll back manually"
                )
            if proposed_hash != active_hash:
                raise RuntimeError(
                    "The active workflow candidate changed after application; roll it back before applying the new version"
                )
            return {
                "candidate_id": candidate_id,
                "intervention_id": str(intervention_id),
                "target_path": str(target),
                "previous_hash": previous_hash,
                "applied_hash": active_hash,
                "status": "active",
                "idempotent": True,
            }

        checks = self._target_checks(candidate, root=root, target=target)
        if not checks["apply_allowed"]:
            raise RuntimeError("; ".join(checks["issues"]))

        previous_content = target.read_text(encoding="utf-8") if target.exists() else None
        previous_hash = _content_hash(previous_content) if previous_content is not None else None
        applied_hash = _content_hash(content)
        now = utc_now()
        version_row = self.conn.execute(
            """
            SELECT id FROM workflow_versions
            WHERE candidate_id = ? AND content_hash = ?
            """,
            (candidate_id, applied_hash),
        ).fetchone()
        if version_row:
            version_id = str(version_row[0])
        else:
            version = int(
                self.conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM workflow_versions WHERE candidate_id = ?",
                    (candidate_id,),
                ).fetchone()[0]
            )
            version_id = f"workflow_version_{uuid.uuid4().hex}"
            self.conn.execute(
                """
                INSERT INTO workflow_versions(
                  id, candidate_id, version, content_json, content_hash,
                  render_targets_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, '["skill"]', 'approved', ?)
                """,
                (version_id, candidate_id, version, json.dumps(candidate.content, sort_keys=True), applied_hash, now),
            )

        intervention_id = f"intervention_{uuid.uuid4().hex}"
        from reflect.workflow_journal import WorkflowFileJournal
        journal = WorkflowFileJournal.for_connection(self.conn)
        record = journal.begin(target=target, before=previous_content, after=content,
                               intervention_id=intervention_id, operation='apply') if journal else None
        self._atomic_write(target, content)
        try:
            evaluation_id = f"evaluation_{hashlib.sha256((version_id + ':artifact-integrity').encode()).hexdigest()[:24]}"
            self.conn.execute(
                """
                INSERT INTO evaluations(
                  id, workflow_version_id, name, kind, input_json, expected_json,
                  status, result_json, last_run_at, created_at, updated_at
                ) VALUES (?, ?, 'rendered_artifact_integrity', 'deterministic', ?, ?, 'passed', ?, ?, ?, ?)
                ON CONFLICT(workflow_version_id, name) DO UPDATE SET
                  input_json = excluded.input_json,
                  expected_json = excluded.expected_json,
                  status = excluded.status,
                  result_json = excluded.result_json,
                  last_run_at = excluded.last_run_at,
                  updated_at = excluded.updated_at
                """,
                (
                    evaluation_id,
                    version_id,
                    json.dumps({"target_path": str(target)}, sort_keys=True),
                    json.dumps({"content_hash": applied_hash}, sort_keys=True),
                    json.dumps({"content_hash": _content_hash(target.read_text(encoding="utf-8"))}, sort_keys=True),
                    now,
                    now,
                    now,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO interventions(
                  id, workflow_version_id, scope_type, scope_id, target_path,
                  previous_hash, applied_hash, previous_content, applied_content,
                  status, exposure_started_at, created_at, updated_at
                ) VALUES (?, ?, 'repository', ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    intervention_id,
                    version_id,
                    str(root),
                    str(target),
                    previous_hash,
                    applied_hash,
                    previous_content,
                    content,
                    now,
                    now,
                    now,
                ),
            )
            self.conn.execute(
                "UPDATE observations SET status = 'active', updated_at = ? WHERE id = ?",
                (now, candidate.observation_id),
            )
            self.conn.execute(
                """
                UPDATE workflow_candidates
                SET status = 'approved', reviewer = ?, reviewed_at = COALESCE(reviewed_at, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (actor, now, now, candidate_id),
            )
            self.repository.record_event(
                entity_type="intervention",
                entity_id=intervention_id,
                event_type="applied",
                actor=actor,
                details={"candidate_id": candidate_id, "target_path": str(target)},
                now=now,
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            self._restore_file(target, previous_content)
            if journal:
                journal.finish(record)
            raise
        if journal:
            journal.finish(record)
        return {
            "candidate_id": candidate_id,
            "intervention_id": intervention_id,
            "target_path": str(target),
            "previous_hash": previous_hash,
            "applied_hash": applied_hash,
            "status": "active",
            "idempotent": False,
        }

    def edit(
        self,
        candidate_id: str,
        *,
        content: dict[str, Any],
        actor: str = "local_operator",
        commit: bool = True,
    ) -> WorkflowCandidateRecord:
        """Replace a pending candidate's structured content and return it to review."""
        candidate = self.show(candidate_id)
        if candidate.lifecycle.deployment.value == "active":
            raise RuntimeError("Roll back an active workflow before editing it")
        normalized = self._validate_content(content)
        identity = WorkflowIdentity.from_content(candidate.title, normalized)
        now = utc_now()
        self.conn.execute(
            """
            UPDATE workflow_candidates
            SET content_json = ?, contract_signature = ?, revision_hash = ?,
                status = 'pending', reviewer = NULL, reviewed_at = NULL,
                checks_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(normalized, sort_keys=True),
                identity.contract_signature,
                identity.revision_hash,
                json.dumps({"schema": "valid", "review_required": True}),
                now,
                candidate_id,
            ),
        )
        self.conn.execute(
            "UPDATE observations SET status = 'proposal_ready', updated_at = ? WHERE id = ?",
            (now, candidate.observation_id),
        )
        self.repository.record_event(
            entity_type="workflow_candidate",
            entity_id=candidate_id,
            event_type="edited_pending",
            actor=actor,
            details={"content_hash": _content_hash(json.dumps(normalized, sort_keys=True))},
            now=now,
        )
        if commit:
            self.conn.commit()
        return self.show(candidate_id)

    def reject(
        self,
        candidate_id: str,
        *,
        reason: str = "operator_rejected",
        actor: str = "local_operator",
    ) -> WorkflowCandidateRecord:
        candidate = self.show(candidate_id)
        if candidate.lifecycle.deployment.value == "active":
            raise RuntimeError("Roll back an active workflow before rejecting it")
        now = utc_now()
        self.conn.execute(
            """
            UPDATE workflow_candidates
            SET status = 'rejected', reviewer = ?, reviewed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (actor, now, now, candidate_id),
        )
        self.conn.execute(
            "UPDATE observations SET status = 'rejected', updated_at = ? WHERE id = ?",
            (now, candidate.observation_id),
        )
        self.repository.record_event(
            entity_type="workflow_candidate",
            entity_id=candidate_id,
            event_type="rejected",
            actor=actor,
            details={"reason": reason},
            now=now,
        )
        self.conn.commit()
        return self.show(candidate_id)

    def dismiss_suggestion(self, candidate_id: str) -> dict[str, Any]:
        """Dismiss the uninstalled variants represented by one inventory row."""
        candidate = self.repository.get_candidate(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        if candidate.lifecycle.deployment.value == "active":
            raise RuntimeError("An active workflow must be rolled back, not dismissed")
        now, batch = utc_now(), uuid.uuid4().hex
        affected = []
        for member in self.repository.list_candidates_by_contract(candidate.contract_signature):
            if member.lifecycle.deployment.value == "active" or member.status.value not in {"pending", "stale"}:
                continue
            observation_status = self.conn.execute(
                "SELECT status FROM observations WHERE id=?", (member.observation_id,),
            ).fetchone()[0]
            self.conn.execute(
                """UPDATE workflow_candidates SET status='rejected', reviewer='local_operator',
                   reviewed_at=?, updated_at=?, checks_json=json_set(checks_json,
                   '$.dismissal', json(?)) WHERE id=?""",
                (now, now, json.dumps({"batch": batch, "previous_status": member.status.value,
                                      "observation_status": observation_status}), member.id),
            )
            self.conn.execute("UPDATE observations SET status='rejected', updated_at=? WHERE id=?",
                              (now, member.observation_id))
            self.repository.record_event(entity_type="workflow_candidate", entity_id=member.id,
                                         event_type="dismissed", actor="local_operator",
                                         details={"batch": batch}, now=now)
            affected.append(member.id)
        self.conn.commit()
        return {"candidate_id": candidate_id, "affected_ids": affected, "status": "rejected"}

    def restore_suggestion(self, candidate_id: str) -> dict[str, Any]:
        """Undo one dismissal batch; do not resurrect unrelated rejected work."""
        row = self.conn.execute(
            "SELECT json_extract(checks_json, '$.dismissal.batch') FROM workflow_candidates WHERE id=?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        if not row[0]:
            raise ValueError("This workflow has no reversible dismissal")
        now, affected = utc_now(), []
        members = self.conn.execute(
            """SELECT id, observation_id, json_extract(checks_json, '$.dismissal')
               FROM workflow_candidates WHERE status='rejected'
               AND json_extract(checks_json, '$.dismissal.batch')=?""", (row[0],),
        ).fetchall()
        for member_id, observation_id, raw in members:
            previous = json.loads(raw)
            self.conn.execute(
                """UPDATE workflow_candidates SET status=?, reviewer=NULL, reviewed_at=NULL,
                   updated_at=?, checks_json=json_remove(checks_json, '$.dismissal') WHERE id=?""",
                (previous["previous_status"], now, member_id),
            )
            self.conn.execute(
                "UPDATE observations SET status=?, updated_at=? WHERE id=? AND status='rejected'",
                (previous["observation_status"], now, observation_id),
            )
            self.repository.record_event(entity_type="workflow_candidate", entity_id=member_id,
                                         event_type="restored", actor="local_operator",
                                         details={"batch": row[0]}, now=now)
            affected.append(member_id)
        self.conn.commit()
        return {"candidate_id": candidate_id, "affected_ids": affected, "status": "restored"}

    def refresh_integrity(self) -> dict[str, int]:
        """Mark active workflows stale when their rendered artifact drifts or disappears."""
        rows = self.conn.execute(
            """
            SELECT i.id, i.target_path, i.applied_hash, wv.candidate_id
            FROM interventions i
            JOIN workflow_versions wv ON wv.id = i.workflow_version_id
            WHERE i.status = 'active'
            """
        ).fetchall()
        stale = 0
        for intervention_id, target_path, applied_hash, candidate_id in rows:
            target = Path(str(target_path))
            current = target.read_text(encoding="utf-8") if target.exists() else None
            current_hash = _content_hash(current) if current is not None else None
            if current_hash == applied_hash:
                continue
            self._mark_stale(str(candidate_id), str(intervention_id), now=utc_now())
            stale += 1
        if stale:
            self.conn.commit()
        return {"stale": stale, "checked": len(rows)}

    def preview_rollback(self, candidate_id: str) -> dict[str, Any]:
        """Render the exact inverse of an active workflow without changing it."""

        row = self.intervention_snapshot(candidate_id, statuses={"active"})
        if row is None:
            raise KeyError(f"No active intervention found for workflow: {candidate_id}")
        target = Path(row["target_path"])
        current_content = target.read_text(encoding="utf-8") if target.exists() else None
        current_hash = _content_hash(current_content) if current_content is not None else None
        if current_hash != row["applied_hash"]:
            raise RuntimeError(
                "The applied workflow file changed after Reflect wrote it; refusing to prepare a rollback"
            )
        previous_content = row["previous_content"] or ""
        diff = "".join(
            difflib.unified_diff(
                (current_content or "").splitlines(keepends=True),
                previous_content.splitlines(keepends=True),
                fromfile=str(target),
                tofile=str(target),
            )
        )
        return {
            "candidate_id": candidate_id,
            "intervention_id": row["intervention_id"],
            "target_path": str(target),
            "change_kind": "restore" if row["previous_content"] is not None else "delete",
            "previous_hash": row["applied_hash"],
            "proposed_hash": row["previous_hash"],
            "would_change": current_content != row["previous_content"],
            "diff": diff,
            "checks": {
                "artifact_unchanged": True,
                "rollback_allowed": True,
                "issues": [],
            },
        }

    def rollback(
        self,
        candidate_id: str,
        *,
        reason: str = "operator_requested",
        actor: str = "local_operator",
    ) -> dict[str, Any]:
        row = self.intervention_snapshot(candidate_id, statuses={"active"})
        if not row:
            completed = self.intervention_snapshot(
                candidate_id,
                statuses={"rolled_back"},
            )
            if completed is None:
                raise KeyError(f"No active intervention found for workflow: {candidate_id}")
            target = Path(completed["target_path"])
            current_content = target.read_text(encoding="utf-8") if target.exists() else None
            current_hash = (
                _content_hash(current_content) if current_content is not None else None
            )
            if current_hash != completed["previous_hash"]:
                raise RuntimeError(
                    "The restored workflow file changed after rollback; review it before continuing"
                )
            return {
                "candidate_id": candidate_id,
                "intervention_id": completed["intervention_id"],
                "target_path": completed["target_path"],
                "restored_hash": completed["previous_hash"],
                "status": "rolled_back",
                "idempotent": True,
            }
        target = Path(row["target_path"])
        current_content = target.read_text(encoding="utf-8") if target.exists() else None
        current_hash = _content_hash(current_content) if current_content is not None else None
        if current_hash != row["applied_hash"]:
            raise RuntimeError(
                "The applied workflow file changed after Reflect wrote it; refusing to overwrite those edits"
            )
        from reflect.workflow_journal import WorkflowFileJournal
        journal = WorkflowFileJournal.for_connection(self.conn)
        record = journal.begin(target=target, before=current_content, after=row['previous_content'],
                               intervention_id=row['intervention_id'], operation='rollback') if journal else None
        self._restore_file(target, row["previous_content"])
        now = utc_now()
        self.conn.execute(
            """
            UPDATE interventions
            SET status = 'rolled_back', rolled_back_at = ?, rollback_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, reason, now, row["intervention_id"]),
        )
        self.conn.execute(
            "UPDATE observations SET status = 'rolled_back', updated_at = ? WHERE id = ?",
            (now, row["observation_id"]),
        )
        self.repository.record_event(
            entity_type="intervention",
            entity_id=row["intervention_id"],
            event_type="rolled_back",
            actor=actor,
            details={"candidate_id": candidate_id, "reason": reason},
            now=now,
        )
        self.conn.commit()
        if journal:
            journal.finish(record)
        return {
            "candidate_id": candidate_id,
            "intervention_id": row["intervention_id"],
            "target_path": str(target),
            "restored_hash": row["previous_hash"],
            "status": "rolled_back",
            "idempotent": False,
        }

    def intervention_snapshot(
        self,
        candidate_id: str,
        *,
        statuses: set[str],
    ) -> dict[str, Any] | None:
        """Return the latest intervention state for exact-change orchestration."""

        placeholders = ",".join("?" for _ in statuses)
        row = self.conn.execute(
            f"""
            SELECT i.id, i.target_path, i.previous_content, i.previous_hash,
                   i.applied_hash, wc.observation_id
            FROM interventions i
            JOIN workflow_versions wv ON wv.id = i.workflow_version_id
            JOIN workflow_candidates wc ON wc.id = wv.candidate_id
            WHERE wc.id = ? AND i.status IN ({placeholders})
            ORDER BY i.created_at DESC
            LIMIT 1
            """,
            (candidate_id, *sorted(statuses)),
        ).fetchone()
        if row is None:
            return None
        return {
            "intervention_id": str(row[0]),
            "target_path": str(row[1]),
            "previous_content": row[2],
            "previous_hash": row[3],
            "applied_hash": str(row[4]),
            "observation_id": str(row[5]),
        }

    @staticmethod
    def _validate_content(content: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(content)
        slug = str(normalized.get("slug") or "")
        if not _SAFE_SLUG.fullmatch(slug):
            raise ValueError(f"Unsafe workflow slug: {slug!r}")
        behavior_type = str(normalized.get("behavior_type") or "proven_pattern")
        if behavior_type not in {"loop", "recovery", "verification", "exploration", "proven_pattern"}:
            raise ValueError(f"Unsupported workflow behavior type: {behavior_type!r}")
        normalized["behavior_type"] = behavior_type
        suggested_artifact = str(normalized.get("suggested_artifact") or "skill")
        if suggested_artifact != "skill":
            raise ValueError(f"Unsupported workflow artifact: {suggested_artifact!r}")
        normalized["suggested_artifact"] = suggested_artifact
        steps = normalized.get("steps")
        if not isinstance(steps, list):
            raise ValueError("Workflow steps must be a list")
        if not any(str(step).strip() for step in steps) and not str(
            normalized.get("source_markdown") or ""
        ).strip():
            raise ValueError("Workflow content requires steps or reviewed source markdown")
        normalized["steps"] = [str(step).strip() for step in steps if str(step).strip()]
        for key in ("abstain_when", "verification"):
            values = normalized.get(key, [])
            if not isinstance(values, list):
                raise ValueError(f"Workflow {key} must be a list")
            normalized[key] = [str(value).strip() for value in values if str(value).strip()]
        normalized["schema_version"] = 1
        return normalized

    @classmethod
    def _target_for(
        cls,
        candidate: WorkflowCandidateRecord,
        project_root: Path,
    ) -> tuple[Path, Path]:
        cls._validate_content(candidate.content)
        root, _ = cls._normalize_project_root(project_root)
        slug = str(candidate.content.get("slug") or "")
        skills_root = (root / ".agents" / "skills").resolve()
        target = skills_root / slug / "SKILL.md"
        resolved_target = target.resolve(strict=False)
        if not resolved_target.is_relative_to(skills_root):
            raise ValueError("Workflow target escapes the project skills directory")
        if target.is_symlink() or any(
            parent.is_symlink() for parent in target.parents if parent != root.parent
        ):
            raise ValueError("Refusing to apply a workflow through a symlink")
        return root, target

    @staticmethod
    def _normalize_project_root(project_root: Path) -> tuple[Path, bool]:
        """Resolve nested Git paths to their repository root and preserve ordinary folders."""
        root = project_root.expanduser().resolve()
        if not root.is_dir():
            return root, False
        for candidate in (root, *root.parents):
            if (candidate / ".git").exists():
                return candidate, True
        return root, False

    def _target_checks(
        self,
        candidate: WorkflowCandidateRecord,
        *,
        root: Path,
        target: Path,
    ) -> dict[str, Any]:
        project_directory = root.is_dir()
        repository_root = project_directory and (root / ".git").exists()
        unsafe_project_root = root in {Path(root.anchor), Path.home().expanduser().resolve()}
        invalid_parent = next(
            (
                parent
                for parent in target.parents
                if (parent == root or parent.is_relative_to(root))
                and parent.exists()
                and not parent.is_dir()
            ),
            None,
        )
        target_supported = not target.exists() or target.is_file()
        write_probe = target.parent
        while not write_probe.exists() and write_probe != root.parent:
            write_probe = write_probe.parent
        writable = (
            project_directory
            and invalid_parent is None
            and write_probe.is_dir()
            and os.access(write_probe, os.W_OK)
        )
        active_owners = [
            {"candidate_id": str(row[0]), "title": str(row[1])}
            for row in self.conn.execute(
                """
                SELECT wc.id, wc.title
                FROM interventions i
                JOIN workflow_versions wv ON wv.id = i.workflow_version_id
                JOIN workflow_candidates wc ON wc.id = wv.candidate_id
                WHERE i.status = 'active' AND i.target_path = ?
                ORDER BY i.created_at DESC
                """,
                (str(target),),
            ).fetchall()
        ]
        contract_owners = [
            {"candidate_id": str(row[0]), "title": str(row[1])}
            for row in self.conn.execute(
                """
                SELECT wc.id, wc.title
                FROM interventions i
                JOIN workflow_versions wv ON wv.id = i.workflow_version_id
                JOIN workflow_candidates wc ON wc.id = wv.candidate_id
                WHERE i.status = 'active' AND wc.contract_signature = ?
                ORDER BY i.created_at DESC
                """,
                (candidate.contract_signature,),
            ).fetchall()
        ]
        target_conflicts = [
            owner for owner in active_owners if owner["candidate_id"] != candidate.id
        ]
        contract_conflicts = [
            owner for owner in contract_owners if owner["candidate_id"] != candidate.id
        ]
        active_conflicts = {
            owner["candidate_id"]: owner
            for owner in [*target_conflicts, *contract_conflicts]
        }
        alternatives = [
            {"candidate_id": str(row[0]), "title": str(row[1]), "status": str(row[2])}
            for row in self.conn.execute(
                """
                SELECT id, title, status
                FROM workflow_candidates
                WHERE id <> ?
                  AND contract_signature = ?
                  AND status <> 'rejected'
                ORDER BY confidence DESC, updated_at DESC
                """,
                (candidate.id, candidate.contract_signature),
            ).fetchall()
        ]
        finding_roots = candidate.provenance.get("finding_project_roots", [])
        finding_scope_matches = (not finding_roots and not candidate.provenance.get("finding_repo_id")) or any(
            self._normalize_project_root(Path(path))[0] == root for path in finding_roots
        )
        issues: list[str] = []
        if not finding_scope_matches:
            issues.append("Choose a project folder from this finding's repository evidence; another project cannot use its impact baseline.")
        if not root.exists():
            issues.append(f"Target project folder does not exist: {root}")
        elif not root.is_dir():
            issues.append(f"Target project path is not a directory: {root}")
        elif unsafe_project_root:
            issues.append(f"Choose a project folder instead of a home or filesystem root: {root}")
        elif invalid_parent is not None:
            issues.append(f"Target path component is not a directory: {invalid_parent}")
        elif not target_supported:
            issues.append(f"Workflow target is not a file: {target}")
        elif not writable:
            issues.append(f"Target project folder is not writable: {root}")
        conflict_list = list(active_conflicts.values())
        if conflict_list:
            issues.append(
                "The same procedure is already active through "
                f"{conflict_list[0]['title']}"
            )
        return {
            "schema_valid": True,
            "project_directory": project_directory,
            "repository_root": repository_root,
            "writable": writable,
            "target_available": not conflict_list,
            "apply_allowed": (
                project_directory
                and finding_scope_matches
                and not unsafe_project_root
                and invalid_parent is None
                and target_supported
                and writable
                and not conflict_list
            ),
            "issues": issues,
            "target_owner": active_owners[0] if active_owners else None,
            "active_conflicts": conflict_list,
            "target_conflicts": target_conflicts,
            "contract_conflicts": contract_conflicts,
            "alternative_candidates": alternatives,
        }

    def _suggest_unique_slug(
        self,
        candidate: WorkflowCandidateRecord,
        *,
        root: Path,
    ) -> str:
        """Return the first deterministic project-local slug not already in use."""

        base = str(candidate.content.get("slug") or "workflow")
        occupied = {
            str(row[0])
            for row in self.conn.execute(
                """
                SELECT DISTINCT json_extract(content_json, '$.slug')
                FROM workflow_candidates
                WHERE id <> ? AND status <> 'rejected'
                """,
                (candidate.id,),
            ).fetchall()
            if row[0]
        }
        skills_root = root / ".agents" / "skills"
        if skills_root.is_dir():
            occupied.update(item.name for item in skills_root.iterdir() if item.is_dir())
        for index in range(2, 1000):
            suffix = f"-{index}"
            suggestion = f"{base[: 63 - len(suffix)].rstrip('-')}{suffix}"
            if suggestion not in occupied:
                return suggestion
        raise RuntimeError(f"Could not generate an available workflow name for {base!r}")

    def _mark_stale(self, candidate_id: str, intervention_id: str, *, now: str) -> None:
        self.conn.execute(
            "UPDATE interventions SET status = 'stale', updated_at = ? WHERE id = ?",
            (now, intervention_id),
        )
        self.repository.record_event(
            entity_type="intervention",
            entity_id=intervention_id,
            event_type="artifact_stale",
            details={"candidate_id": candidate_id},
            now=now,
        )

    @staticmethod
    def _render_skill(candidate: WorkflowCandidateRecord) -> str:
        description = str(candidate.content.get("description") or candidate.hypothesis).replace("\n", " ").strip()
        description_yaml = json.dumps(description, ensure_ascii=False)
        steps = [str(step).strip() for step in candidate.content.get("steps", []) if str(step).strip()]
        abstain = [str(step).strip() for step in candidate.content.get("abstain_when", []) if str(step).strip()]
        verification = [
            str(step).strip() for step in candidate.content.get("verification", []) if str(step).strip()
        ]
        source_markdown = str(candidate.content.get("source_markdown") or "").strip()
        lines = [
            "---",
            f"name: {candidate.content['slug']}",
            f"description: {description_yaml}",
            "---",
            "",
        ]
        if source_markdown:
            lines.append(source_markdown)
        else:
            lines.extend([f"# {candidate.title}", "", "## Workflow", "",
                          *[f"{index}. {step}" for index, step in enumerate(steps, start=1)]])
        for heading, values in (
            ("Preconditions", candidate.content.get("preconditions", [])),
            ("Checkpoints", candidate.content.get("checkpoints", [])),
            ("Abstain when", abstain),
            ("Verification", verification),
        ):
            instructions = [str(item).strip() for item in values if str(item).strip()]
            if instructions:
                lines.extend(["", f"## {heading}", "", *[f"- {item}" for item in instructions]])
        if not source_markdown:
            lines.extend(["", "Generated by Reflect."])
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=".reflect-workflow-",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    @classmethod
    def _restore_file(cls, path: Path, previous_content: str | None) -> None:
        if previous_content is None:
            if path.exists():
                path.unlink()
            with suppress(OSError):
                path.parent.rmdir()
            return
        cls._atomic_write(path, previous_content)
