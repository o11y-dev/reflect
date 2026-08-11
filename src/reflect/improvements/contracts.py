from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from pydantic import Field, model_validator

from reflect.improvements.workflow_roles import WorkflowEvidenceRepository, workflow_roles
from reflect.schema.base import ReflectModel


class WorkflowContractApplicability(ReflectModel):
    repo_id: str | None = None
    workspace_id: str | None = None
    task_archetype_id: str | None = None


class WorkflowContractValidation(ReflectModel):
    baseline_minimum: int = Field(default=3, ge=1, le=100)
    baseline_maximum: int = Field(default=20, ge=1, le=100)
    minimum_evidence_coverage: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_baseline_window(self) -> WorkflowContractValidation:
        if self.baseline_maximum < self.baseline_minimum:
            raise ValueError("baseline_maximum must be at least baseline_minimum")
        return self


class WorkflowMilestoneContract(ReflectModel):
    id: str = Field(min_length=1, max_length=120)
    role: str | None = Field(default=None, max_length=120)
    required: bool = True
    evidence: str = "corroborated"
    evidence_fields: list[str] = Field(default_factory=list)

    @property
    def resolved_role(self) -> str:
        return self.role or self.id


class WorkflowContract(ReflectModel):
    """Versioned procedure definition evaluated against comparable executions."""

    signature_hash: str = Field(min_length=1, max_length=256)
    applicability: WorkflowContractApplicability = Field(
        default_factory=WorkflowContractApplicability
    )
    milestones: list[WorkflowMilestoneContract] = Field(min_length=1)
    validation: WorkflowContractValidation = Field(default_factory=WorkflowContractValidation)

    @classmethod
    def from_raw(cls, raw: Any) -> WorkflowContract | None:
        if not isinstance(raw, dict) or not raw.get("milestones"):
            return None
        return cls.model_validate(raw)

    @property
    def required_milestones(self) -> list[WorkflowMilestoneContract]:
        return [milestone for milestone in self.milestones if milestone.required]


class WorkflowMilestoneEvidence(ReflectModel):
    """Privacy-safe evidence attached to one reported contract milestone."""

    provider: str | None = Field(default=None, max_length=120)
    target_ref: str | None = Field(default=None, max_length=500)
    preview_fingerprint: str | None = Field(default=None, max_length=256)
    approval_fingerprint: str | None = Field(default=None, max_length=256)
    write_fingerprint: str | None = Field(default=None, max_length=256)
    readback_fingerprint: str | None = Field(default=None, max_length=256)
    attributes: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class WorkflowEvidence(ReflectModel):
    """Canonical evidence observed for one execution unit."""

    observed_roles: list[str] = Field(default_factory=list)
    reported_milestones: dict[str, str] = Field(default_factory=dict)
    status: str = "unknown"
    outcome: str | None = None
    verification_passed: bool | None = None
    workflow_slug_observed: bool = False


class ContractEvaluation(ReflectModel):
    invoked: bool
    followed: bool
    corroborated: bool
    reported_complete: bool
    successful_outcome: bool
    evidence_coverage: float = Field(ge=0, le=1)


@dataclass(frozen=True, slots=True)
class EvaluatedContractEvidence:
    evaluation: ContractEvaluation
    observed_roles: list[str]
    signal_count: int
    signal_fingerprints: list[str]


def ordered_role_coverage(observed: list[str], required: list[str]) -> float:
    if not required:
        return 0.0
    position = 0
    for role in observed:
        if position < len(required) and role == required[position]:
            position += 1
    return position / len(required)


def evaluate_contract(
    contract: WorkflowContract,
    evidence: WorkflowEvidence,
) -> ContractEvaluation:
    """Apply one symmetric adherence definition before and after installation."""

    required = contract.required_milestones
    required_roles = [milestone.resolved_role for milestone in required]
    required_ids = [milestone.id for milestone in required]
    coverage = ordered_role_coverage(evidence.observed_roles, required_roles)
    status_ok = evidence.status.lower() in {"completed", "ok", "success"}
    outcome_ok = (evidence.outcome or "success").lower() not in {
        "failure",
        "partial",
        "abandoned",
    }
    successful_outcome = status_ok and outcome_ok and evidence.verification_passed is not False
    corroborated = (
        coverage >= contract.validation.minimum_evidence_coverage and successful_outcome
    )
    reported_complete = bool(required_ids) and all(
        evidence.reported_milestones.get(milestone_id) == "completed"
        for milestone_id in required_ids
    )
    return ContractEvaluation(
        invoked=bool(
            evidence.workflow_slug_observed
            or evidence.reported_milestones
            or evidence.observed_roles
        ),
        followed=corroborated,
        corroborated=corroborated,
        reported_complete=reported_complete,
        successful_outcome=successful_outcome,
        evidence_coverage=coverage,
    )


class ContractEvaluationService:
    """Load canonical evidence and evaluate workflow contracts consistently."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        evidence: WorkflowEvidenceRepository | None = None,
    ):
        self.evidence = evidence or WorkflowEvidenceRepository(conn)

    def evaluate_units(
        self,
        contract: WorkflowContract,
        execution_units: list[dict[str, Any]],
    ) -> dict[str, EvaluatedContractEvidence]:
        execution_unit_ids = [str(unit["execution_unit_id"]) for unit in execution_units]
        signals_by_execution = self.evidence.signals_for_execution_units(execution_unit_ids)
        evaluated: dict[str, EvaluatedContractEvidence] = {}
        for unit in execution_units:
            execution_unit_id = str(unit["execution_unit_id"])
            signals = signals_by_execution.get(execution_unit_id, [])
            observed_roles = workflow_roles(signals)
            evaluation = evaluate_contract(
                contract,
                WorkflowEvidence(
                    observed_roles=observed_roles,
                    reported_milestones=dict(unit.get("reported_milestones") or {}),
                    status=str(unit.get("status") or "unknown"),
                    outcome=str(unit["outcome"]) if unit.get("outcome") else None,
                    verification_passed=unit.get("verification_passed"),
                    workflow_slug_observed=bool(unit.get("workflow_slug_observed")),
                ),
            )
            evaluated[execution_unit_id] = EvaluatedContractEvidence(
                evaluation=evaluation,
                observed_roles=observed_roles,
                signal_count=len(signals),
                signal_fingerprints=[
                    signal.fingerprint for signal in signals if signal.fingerprint
                ][:24],
            )
        return evaluated


__all__ = [
    "ContractEvaluation",
    "ContractEvaluationService",
    "EvaluatedContractEvidence",
    "WorkflowContract",
    "WorkflowContractApplicability",
    "WorkflowContractValidation",
    "WorkflowEvidence",
    "WorkflowMilestoneContract",
    "WorkflowMilestoneEvidence",
    "evaluate_contract",
    "ordered_role_coverage",
]
