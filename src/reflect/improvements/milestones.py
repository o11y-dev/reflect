from __future__ import annotations

import hashlib
import json
import sqlite3
from enum import StrEnum
from typing import Any

from pydantic import Field

from reflect.improvements.contracts import (
    WorkflowContract,
    WorkflowMilestoneEvidence,
)
from reflect.improvements.repository import utc_now
from reflect.schema.base import ReflectModel


class WorkflowMilestoneState(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class WorkflowMilestoneResult(ReflectModel):
    event_id: str
    task_run_id: str
    execution_unit_id: str | None = None
    skill_version_id: str
    milestone_id: str
    state: WorkflowMilestoneState
    evidence_requirement: str
    evidence: WorkflowMilestoneEvidence = Field(default_factory=WorkflowMilestoneEvidence)
    corroborated: bool = False
    created: bool
    recorded_at: str


class WorkflowMilestoneService:
    """Record typed, idempotent workflow checkpoints against active task runs."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def record(
        self,
        *,
        task_run_id: str,
        skill_version_id: str,
        milestone_id: str,
        state: WorkflowMilestoneState | str,
        idempotency_key: str,
        evidence: WorkflowMilestoneEvidence | None = None,
    ) -> WorkflowMilestoneResult:
        normalized_state = WorkflowMilestoneState(state)
        key = idempotency_key.strip()
        if not key or len(key) > 200:
            raise ValueError("idempotency_key must contain 1-200 characters")
        task_row = self.conn.execute(
            """
            SELECT status, selected_skills_json, execution_unit_id
            FROM mcp_task_runs WHERE id = ?
            """,
            (task_run_id,),
        ).fetchone()
        if task_row is None:
            raise KeyError(f"MCP task run not found: {task_run_id}")
        if str(task_row[0]) not in {"started", "completed"}:
            raise ValueError(f"Task run is not recordable: {task_row[0]}")
        selected = json.loads(str(task_row[1] or "[]"))
        selected_versions = {
            str(item.get("version_id") or "")
            for item in selected
            if isinstance(item, dict)
        }
        if skill_version_id not in selected_versions:
            raise ValueError("skill_version_id was not selected for this task run")

        version_row = self.conn.execute(
            "SELECT workflow_json FROM skill_versions WHERE id = ?",
            (skill_version_id,),
        ).fetchone()
        if version_row is None:
            raise KeyError(f"Skill version not found: {skill_version_id}")
        workflow = json.loads(str(version_row[0] or "{}"))
        contract = WorkflowContract.from_raw(workflow.get("workflow_contract"))
        if contract is None:
            raise ValueError("Selected skill has no workflow contract")
        milestones = {item.id: item for item in contract.milestones}
        milestone = milestones.get(milestone_id)
        if milestone is None:
            raise ValueError(f"Unknown workflow milestone: {milestone_id}")
        evidence_requirement = milestone.evidence
        normalized_evidence = evidence or WorkflowMilestoneEvidence()
        binding = {
            "task_run_id": task_run_id,
            "execution_unit_id": str(task_row[2]) if task_row[2] else None,
            "skill_version_id": skill_version_id,
            "milestone_id": milestone_id,
            "state": normalized_state.value,
            "evidence_requirement": evidence_requirement,
            "evidence": normalized_evidence.model_dump(mode="json", exclude_none=True),
            "corroborated": False,
        }
        binding_hash = hashlib.sha256(
            json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        event_id = "event_milestone_" + hashlib.sha256(
            f"{task_run_id}:{key}".encode()
        ).hexdigest()[:24]
        existing = self.conn.execute(
            "SELECT details_json, created_at FROM improvement_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if existing:
            details = json.loads(str(existing[0]))
            if details.get("binding_hash") != binding_hash:
                raise ValueError("idempotency_key is already bound to a different milestone")
            return WorkflowMilestoneResult(
                event_id=event_id,
                created=False,
                recorded_at=str(existing[1]),
                **binding,
            )
        if str(task_row[0]) != "started":
            raise ValueError(
                "Completed task runs accept only an idempotent replay of an existing milestone"
            )

        now = utc_now()
        details: dict[str, Any] = {**binding, "binding_hash": binding_hash}
        self.conn.execute(
            """
            INSERT INTO improvement_events(
              id, entity_type, entity_id, event_type, actor, details_json, created_at
            ) VALUES (?, 'mcp_task_run', ?, 'workflow_milestone',
                      'agent_milestone', ?, ?)
            """,
            (event_id, task_run_id, json.dumps(details, sort_keys=True), now),
        )
        self.conn.commit()
        return WorkflowMilestoneResult(
            event_id=event_id,
            created=True,
            recorded_at=now,
            **binding,
        )


__all__ = [
    "WorkflowMilestoneResult",
    "WorkflowMilestoneService",
    "WorkflowMilestoneState",
]
