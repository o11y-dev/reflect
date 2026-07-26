from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field

from reflect.improvements.models import WorkflowCandidateRecord
from reflect.improvements.skills import SkillRegistryService
from reflect.improvements.workflows import WorkflowService
from reflect.schema.base import ReflectModel

_CHANGE_ACTOR = "mcp_conversational_approval"


class ChangeAction(StrEnum):
    """Mutation actions that can be reviewed and explicitly approved."""

    APPROVE_WORKFLOW = "approve_workflow"
    APPLY_WORKFLOW = "apply_workflow"
    ROLLBACK_WORKFLOW = "rollback_workflow"


class ChangeReviewState(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    STALE = "stale"


class ChangeNextAction(ReflectModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    when: str


class ChangeReviewAnswer(ReflectModel):
    review_id: str
    action: ChangeAction
    state: ChangeReviewState
    candidate_id: str
    skill_id: str | None = None
    skill_slug: str | None = None
    title: str
    workflow_status: str
    project_root: str | None = None
    target_path: str
    change_kind: str
    diff: str
    previous_hash: str | None = None
    proposed_hash: str | None = None
    binding_hash: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    risks: list[str] = Field(default_factory=list)
    rollback_plan: list[str] = Field(default_factory=list)
    checks: dict[str, Any] = Field(default_factory=dict)
    expires_at: str
    revised_proposal_staged: bool = False
    explicit_user_approval_required: bool = True
    approval_token: str
    next_action: ChangeNextAction


class ChangeApplyAnswer(ReflectModel):
    review_id: str
    action: ChangeAction
    candidate_id: str
    state: ChangeReviewState
    result: dict[str, Any] = Field(default_factory=dict)
    idempotent: bool = False


class ChangeReviewService:
    """Bind conversational approval to one exact existing workflow mutation."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        workflows: WorkflowService | None = None,
        skills: SkillRegistryService | None = None,
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.conn = conn
        self.workflows = workflows or WorkflowService(conn)
        self.skills = skills or SkillRegistryService(conn)
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(32))

    def review(
        self,
        *,
        action: ChangeAction | str,
        entity_id: str,
        project_root: Path | None = None,
        revised_content: dict[str, Any] | None = None,
        expires_in_minutes: int = 30,
    ) -> ChangeReviewAnswer:
        """Stage an optional revision and return an exact short-lived approval."""

        resolved_action = ChangeAction(action)
        candidate_id = self._candidate_id(entity_id)
        with self.conn:
            return self._prepare_review(
                action=resolved_action,
                candidate_id=candidate_id,
                project_root=project_root,
                revised_content=revised_content,
                expires_in_minutes=expires_in_minutes,
            )

    def _prepare_review(
        self,
        *,
        action: ChangeAction,
        candidate_id: str,
        project_root: Path | None,
        revised_content: dict[str, Any] | None,
        expires_in_minutes: int,
    ) -> ChangeReviewAnswer:
        """Prepare one review inside the caller-owned SQLite transaction."""

        revised = revised_content is not None
        if revised:
            if action == ChangeAction.ROLLBACK_WORKFLOW:
                raise ValueError("A rollback review cannot stage revised workflow content")
            self.workflows.edit(
                candidate_id,
                content=revised_content,
                actor=_CHANGE_ACTOR,
                commit=False,
            )
            self.skills.sync_workflow_candidates([candidate_id])

        candidate = self.workflows.show(candidate_id)
        preview = self._preview(
            action,
            candidate,
            project_root=project_root,
        )
        if (
            action == ChangeAction.APPLY_WORKFLOW
            and not (preview.get("checks") or {}).get("apply_allowed")
        ):
            issues = (preview.get("checks") or {}).get("issues") or [
                "The target is not currently safe to change."
            ]
            raise RuntimeError("; ".join(str(item) for item in issues))
        binding = self._binding(action, candidate, preview)
        now = self._now()
        expires = now + timedelta(minutes=max(1, min(int(expires_in_minutes), 120)))
        token = f"reflect_approval_{self.token_factory()}"
        token_hash = self._hash(token)
        review_id = f"change_review_{token_hash[:24]}"
        skill_id, skill_slug, measurements = self._skill_context(candidate_id)
        evidence = self._evidence(candidate, measurements=measurements)
        risks = self._risks(candidate, preview)
        rollback_plan = self._rollback_plan(action, candidate_id)
        payload = {
            "candidate_content_hash": self._candidate_content_hash(candidate),
            "preview": preview,
            "evidence": evidence,
            "risks": risks,
            "rollback_plan": rollback_plan,
            "skill_id": skill_id,
            "skill_slug": skill_slug,
            "revised_proposal_staged": revised,
        }

        self.conn.execute(
            """
            UPDATE change_reviews
            SET status = 'superseded', updated_at = ?
            WHERE candidate_id = ? AND status = 'pending'
            """,
            (now.isoformat(), candidate_id),
        )
        self.conn.execute(
            """
            INSERT INTO change_reviews(
              id, action, candidate_id, project_root, target_path,
              previous_hash, proposed_hash, binding_hash, token_hash, status,
              payload_json, result_json, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, '{}', ?, ?, ?)
            """,
            (
                review_id,
                action.value,
                candidate_id,
                preview.get("project_root"),
                preview["target_path"],
                preview.get("previous_hash"),
                preview.get("proposed_hash"),
                binding,
                token_hash,
                self._json(payload),
                expires.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        self.workflows.repository.record_event(
            entity_type="change_review",
            entity_id=review_id,
            event_type="prepared",
            actor=_CHANGE_ACTOR,
            details={
                "action": action.value,
                "candidate_id": candidate_id,
                "binding_hash": binding,
                "expires_at": expires.isoformat(),
            },
            now=now.isoformat(),
        )
        return ChangeReviewAnswer(
            review_id=review_id,
            action=action,
            state=ChangeReviewState.PENDING,
            candidate_id=candidate_id,
            skill_id=skill_id,
            skill_slug=skill_slug,
            title=candidate.title,
            workflow_status=candidate.status.value,
            project_root=preview.get("project_root"),
            target_path=preview["target_path"],
            change_kind=preview["change_kind"],
            diff=preview["diff"],
            previous_hash=preview.get("previous_hash"),
            proposed_hash=preview.get("proposed_hash"),
            binding_hash=binding,
            evidence=evidence,
            risks=risks,
            rollback_plan=rollback_plan,
            checks=preview.get("checks") or {},
            expires_at=expires.isoformat(),
            revised_proposal_staged=revised,
            approval_token=token,
            next_action=ChangeNextAction(
                tool="reflect_apply_change",
                arguments={"approval_token": token},
                when=(
                    "Only after the user explicitly approves this exact target and diff "
                    "in the current conversation."
                ),
            ),
        )

    def apply(self, approval_token: str) -> ChangeApplyAnswer:
        """Apply only the exact unexpired change bound to the supplied token."""

        token_hash = self._hash(approval_token.strip())
        row = self.conn.execute(
            """
            SELECT id, action, candidate_id, binding_hash, status, payload_json,
                   result_json, expires_at, project_root
            FROM change_reviews
            WHERE token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if row is None:
            raise ValueError("Invalid approval token")
        review_id = str(row[0])
        action = ChangeAction(str(row[1]))
        candidate_id = str(row[2])
        state = ChangeReviewState(str(row[4]))
        if state == ChangeReviewState.APPLIED:
            return ChangeApplyAnswer(
                review_id=review_id,
                action=action,
                candidate_id=candidate_id,
                state=state,
                result=self._loads(row[6]),
                idempotent=True,
            )
        if state != ChangeReviewState.PENDING:
            raise RuntimeError(f"Change review {review_id} is {state.value}")

        now = self._now()
        if now >= self._parse_time(str(row[7])):
            self._set_state(review_id, ChangeReviewState.EXPIRED, now=now)
            raise RuntimeError(f"Change review {review_id} has expired")

        payload = self._loads(row[5])
        candidate = self.workflows.show(candidate_id)
        if self._effect_already_present(action, candidate_id, payload):
            result = self._execute(
                action,
                candidate_id,
                project_root=self._optional_path(row[8]),
            )
            return self._finish(
                review_id,
                action,
                candidate_id,
                result,
                now=now,
                idempotent=True,
            )

        preview = self._preview(
            action,
            candidate,
            project_root=self._optional_path(row[8]),
        )
        current_binding = self._binding(action, candidate, preview)
        if not secrets.compare_digest(current_binding, str(row[3])):
            self._set_state(review_id, ChangeReviewState.STALE, now=now)
            raise RuntimeError(
                "The reviewed workflow or target changed. Prepare and approve a new review."
            )

        result = self._execute(
            action,
            candidate_id,
            project_root=self._optional_path(row[8]),
        )
        return self._finish(
            review_id,
            action,
            candidate_id,
            result,
            now=now,
            idempotent=bool(result.get("idempotent")),
        )

    def inspect(self, review_id: str) -> dict[str, Any] | None:
        """Return an audit-safe review record without exposing its approval token."""

        row = self.conn.execute(
            """
            SELECT id, action, candidate_id, project_root, target_path,
                   previous_hash, proposed_hash, binding_hash, status,
                   payload_json, result_json, expires_at, applied_at,
                   created_at, updated_at
            FROM change_reviews WHERE id = ?
            """,
            (review_id,),
        ).fetchone()
        if row is None:
            return None
        state = ChangeReviewState(str(row[8]))
        if state == ChangeReviewState.PENDING and self._now() >= self._parse_time(str(row[11])):
            state = ChangeReviewState.EXPIRED
        payload = self._loads(row[9])
        return {
            "review_id": str(row[0]),
            "action": str(row[1]),
            "candidate_id": str(row[2]),
            "project_root": row[3],
            "target_path": row[4],
            "previous_hash": row[5],
            "proposed_hash": row[6],
            "binding_hash": str(row[7]),
            "state": state.value,
            "evidence": payload.get("evidence") or {},
            "risks": payload.get("risks") or [],
            "rollback_plan": payload.get("rollback_plan") or [],
            "result": self._loads(row[10]),
            "expires_at": str(row[11]),
            "applied_at": row[12],
            "created_at": str(row[13]),
            "updated_at": str(row[14]),
        }

    def _candidate_id(self, entity_id: str) -> str:
        try:
            return self.workflows.show(entity_id).id
        except KeyError:
            return self.skills.workflow_candidate_for(entity_id)

    def _preview(
        self,
        action: ChangeAction,
        candidate: WorkflowCandidateRecord,
        *,
        project_root: Path | None,
    ) -> dict[str, Any]:
        if action == ChangeAction.APPROVE_WORKFLOW:
            if candidate.status.value not in {"pending", "approved", "active"}:
                raise RuntimeError(
                    f"Workflow {candidate.id} is {candidate.status.value}; it cannot be approved"
                )
            content_hash = self._candidate_content_hash(candidate)
            return {
                "project_root": None,
                "target_path": f"workflow_candidate:{candidate.id}",
                "change_kind": "approve",
                "previous_hash": content_hash,
                "proposed_hash": content_hash,
                "diff": (
                    f"--- workflow/{candidate.id}/status\n"
                    f"+++ workflow/{candidate.id}/status\n"
                    f"-{candidate.status.value}\n"
                    "+approved\n"
                ),
                "checks": {
                    "approval_allowed": True,
                    "installation_changed": False,
                    "issues": [],
                },
            }
        if action == ChangeAction.APPLY_WORKFLOW:
            if project_root is None:
                raise ValueError("project_root is required to review a workflow application")
            return self.workflows.preview(candidate.id, project_root=project_root)
        return self.workflows.preview_rollback(candidate.id)

    def _execute(
        self,
        action: ChangeAction,
        candidate_id: str,
        *,
        project_root: Path | None,
    ) -> dict[str, Any]:
        if action == ChangeAction.APPROVE_WORKFLOW:
            result = self.workflows.approve(candidate_id, actor=_CHANGE_ACTOR)
        elif action == ChangeAction.APPLY_WORKFLOW:
            if project_root is None:
                raise RuntimeError("Reviewed project root is missing")
            result = self.workflows.apply(
                candidate_id,
                project_root=project_root,
                actor=_CHANGE_ACTOR,
            )
        else:
            result = self.workflows.rollback(
                candidate_id,
                reason="conversational_operator_approval",
                actor=_CHANGE_ACTOR,
            )
        self.skills.sync_workflow_candidates([candidate_id])
        self.conn.commit()
        return result

    def _finish(
        self,
        review_id: str,
        action: ChangeAction,
        candidate_id: str,
        result: dict[str, Any],
        *,
        now: datetime,
        idempotent: bool,
    ) -> ChangeApplyAnswer:
        self.conn.execute(
            """
            UPDATE change_reviews
            SET status = 'applied', result_json = ?, applied_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (self._json(result), now.isoformat(), now.isoformat(), review_id),
        )
        self.workflows.repository.record_event(
            entity_type="change_review",
            entity_id=review_id,
            event_type="applied",
            actor=_CHANGE_ACTOR,
            details={"action": action.value, "candidate_id": candidate_id},
            now=now.isoformat(),
        )
        self.conn.commit()
        return ChangeApplyAnswer(
            review_id=review_id,
            action=action,
            candidate_id=candidate_id,
            state=ChangeReviewState.APPLIED,
            result=result,
            idempotent=idempotent,
        )

    def _effect_already_present(
        self,
        action: ChangeAction,
        candidate_id: str,
        payload: dict[str, Any],
    ) -> bool:
        expected = payload.get("preview") or {}
        if action == ChangeAction.APPROVE_WORKFLOW:
            candidate = self.workflows.show(candidate_id)
            return (
                candidate.status.value in {"approved", "active"}
                and self._candidate_content_hash(candidate)
                == payload.get("candidate_content_hash")
            )
        status = "active" if action == ChangeAction.APPLY_WORKFLOW else "rolled_back"
        row = self.workflows.intervention_snapshot(candidate_id, statuses={status})
        if row is None:
            return False
        if action == ChangeAction.APPLY_WORKFLOW:
            return (
                row["target_path"] == expected.get("target_path")
                and row["applied_hash"] == expected.get("proposed_hash")
            )
        return (
            row["intervention_id"] == expected.get("intervention_id")
            and row["target_path"] == expected.get("target_path")
            and row["previous_hash"] == expected.get("proposed_hash")
        )

    def _binding(
        self,
        action: ChangeAction,
        candidate: WorkflowCandidateRecord,
        preview: dict[str, Any],
    ) -> str:
        return self._hash(
            self._json(
                {
                    "action": action.value,
                    "candidate_id": candidate.id,
                    "candidate_content_hash": self._candidate_content_hash(candidate),
                    "candidate_status": candidate.status.value,
                    "candidate_updated_at": candidate.updated_at,
                    "project_root": preview.get("project_root"),
                    "target_path": preview["target_path"],
                    "previous_hash": preview.get("previous_hash"),
                    "proposed_hash": preview.get("proposed_hash"),
                    "intervention_id": preview.get("intervention_id"),
                }
            )
        )

    def _skill_context(
        self,
        candidate_id: str,
    ) -> tuple[str | None, str | None, list[dict[str, Any]]]:
        self.skills.sync_workflow_candidates([candidate_id])
        try:
            skill = self.skills.skill_for_candidate(candidate_id)
            detail = self.skills.show(skill.id)
        except (KeyError, RuntimeError):
            return None, None, []
        return (
            detail.skill.id,
            detail.skill.slug,
            [item.model_dump(mode="json") for item in detail.measurements[:10]],
        )

    def _evidence(
        self,
        candidate: WorkflowCandidateRecord,
        *,
        measurements: list[dict[str, Any]],
    ) -> dict[str, Any]:
        ledger = self.workflows.repository.workflow_session_ledger(candidate.id, limit=10)
        return {
            "support_count": candidate.support_count,
            "confidence": candidate.confidence,
            "source_session_count": ledger.source_session_count,
            "source_sessions": [
                {
                    "session_id": item.session_id,
                    "agent": item.agent,
                    "started_at": item.started_at,
                    "status": item.status,
                    "evidence_summaries": item.evidence_summaries,
                }
                for item in ledger.source_sessions
            ],
            "measurements": measurements,
            "provenance": candidate.provenance,
        }

    @staticmethod
    def _risks(
        candidate: WorkflowCandidateRecord,
        preview: dict[str, Any],
    ) -> list[str]:
        risks = [f"Declared workflow risk: {candidate.risk}."]
        checks = preview.get("checks") or {}
        risks.extend(str(item) for item in checks.get("issues") or [])
        risks.extend(str(item) for item in checks.get("advisories") or [])
        return list(dict.fromkeys(risks))

    @staticmethod
    def _rollback_plan(
        action: ChangeAction,
        candidate_id: str,
    ) -> list[str]:
        if action == ChangeAction.APPROVE_WORKFLOW:
            return [
                "Leave the approved workflow uninstalled, or reject it before application.",
            ]
        if action == ChangeAction.APPLY_WORKFLOW:
            return [
                f"Review rollback_workflow for {candidate_id}.",
                "Explicitly approve the exact rollback diff before restoring the prior file.",
            ]
        return [
            f"Review apply_workflow for {candidate_id}.",
            "Explicitly approve the exact application diff before reinstalling it.",
        ]

    def _set_state(
        self,
        review_id: str,
        state: ChangeReviewState,
        *,
        now: datetime,
    ) -> None:
        self.conn.execute(
            "UPDATE change_reviews SET status = ?, updated_at = ? WHERE id = ?",
            (state.value, now.isoformat(), review_id),
        )
        self.conn.commit()

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _optional_path(value: Any) -> Path | None:
        return Path(str(value)) if value else None

    @staticmethod
    def _candidate_content_hash(candidate: WorkflowCandidateRecord) -> str:
        return ChangeReviewService._hash(ChangeReviewService._json(candidate.content))

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @staticmethod
    def _loads(value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
