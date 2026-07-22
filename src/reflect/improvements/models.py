from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from reflect.schema.base import ReflectModel


class ObservationStatus(StrEnum):
    NEW = "new"
    ACKNOWLEDGED = "acknowledged"
    PROPOSAL_READY = "proposal_ready"
    APPROVED = "approved"
    ACTIVE = "active"
    MEASURED = "measured"
    REJECTED = "rejected"
    DISMISSED = "dismissed"
    REGRESSED = "regressed"
    ROLLED_BACK = "rolled_back"
    RESOLVED = "resolved"


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    ACTIVE = "active"
    STALE = "stale"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class WorkflowBehaviorType(StrEnum):
    LOOP = "loop"
    RECOVERY = "recovery"
    VERIFICATION = "verification"
    EXPLORATION = "exploration"
    PROVEN_PATTERN = "proven_pattern"


class WorkflowSourceKind(StrEnum):
    """Authorship boundary for a workflow candidate."""

    RULE_BLUEPRINT = "rule_blueprint"
    AGENT_AUTHORED = "agent_authored"
    MANUAL_SKILL_FILE = "manual_skill_file"


class WorkflowArtifactKind(StrEnum):
    """Artifact renderer suggested by a workflow definition."""

    SKILL = "skill"


class SkillOrigin(StrEnum):
    RULE_BLUEPRINT = "rule_blueprint"
    AGENT_AUTHORED = "agent_authored"
    IMPORTED = "imported"


class SkillLifecycleState(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    STALE = "stale"
    RETIRED = "retired"
    REJECTED = "rejected"


class SkillVersionStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    STALE = "stale"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class LoopKind(StrEnum):
    STALLED = "stalled"
    PRODUCTIVE = "productive"


class LoopStatus(StrEnum):
    DETECTED = "detected"
    ACKNOWLEDGED = "acknowledged"
    PROMOTED = "promoted"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RuleDefinition(ReflectModel):
    id: str
    version: int = Field(ge=1)
    category: str
    title: str
    description: str
    detector_config: dict[str, Any] = Field(default_factory=dict)
    required_signals: list[str] = Field(default_factory=list)
    lifecycle_state: str = "active"


class RuleSummary(RuleDefinition):
    observation_count: int = Field(default=0, ge=0)
    open_observation_count: int = Field(default=0, ge=0)
    candidate_count: int = Field(default=0, ge=0)
    last_evaluated_at: str | None = None


class EvidenceRef(ReflectModel):
    entity_type: str
    entity_id: str
    summary_redacted: str
    polarity: str = "supporting"
    session_id: str | None = None
    step_id: str | None = None
    tool_call_id: str | None = None
    llm_call_id: str | None = None
    file_id: str | None = None
    memory_id: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    attrs: dict[str, Any] = Field(default_factory=dict)


class ObservationDraft(ReflectModel):
    rule_id: str
    rule_version: int = Field(ge=1)
    scope_type: str
    scope_id: str
    fingerprint: str
    category: str
    title: str
    summary: str
    metric_name: str
    metric_value: float
    metric_unit: str
    metric_direction: str
    baseline_value: float | None = None
    baseline_query: dict[str, Any] = Field(default_factory=dict)
    impact_score: float = Field(ge=0, le=100)
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    occurrence_count: int = Field(default=1, ge=1)
    affected_session_count: int = Field(default=1, ge=1)
    actionability: str = "review"
    repo_id: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)


class WorkflowDefinition(ReflectModel):
    """Rule-owned, renderer-agnostic definition of an actionable workflow."""

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    behavior_type: WorkflowBehaviorType
    steps: list[str] = Field(min_length=1)
    description: str | None = None
    abstain_when: list[str] = Field(
        default_factory=lambda: [
            "The linked evidence does not match the current task.",
            "The operator has explicitly accepted the observed behavior.",
        ]
    )
    verification: list[str] = Field(
        default_factory=lambda: [
            "Confirm the intended artifact or behavior exists.",
            "Record the exact focused validation command and result.",
        ]
    )
    risk: str = "low"
    measurement_window: int = Field(default=10, ge=1)
    suggested_artifact: WorkflowArtifactKind = WorkflowArtifactKind.SKILL


class WorkflowProposal(ReflectModel):
    action_type: str = "workflow"
    title: str
    hypothesis: str
    risk: str
    content: dict[str, Any]
    target_metric: str
    target_value: float | None = None
    measurement_window: int = Field(default=10, ge=1)


class ObservationRecord(ObservationDraft):
    id: str
    status: ObservationStatus
    first_seen_at: str
    last_seen_at: str
    last_evaluated_at: str
    suppression_reason: str | None = None
    suppressed_until: str | None = None
    candidate_id: str | None = None
    candidate_status: WorkflowStatus | None = None


class InboxFindingRecord(ObservationRecord):
    """Presentation-safe group of equivalent open observations."""

    observation_count: int = Field(default=1, ge=1)
    variant_count: int = Field(default=1, ge=1)
    source_scope_count: int = Field(default=1, ge=1)
    source_scopes: list[str] = Field(default_factory=list)


class WorkflowCandidateRecord(ReflectModel):
    id: str
    observation_id: str
    action_type: str
    title: str
    hypothesis: str
    scope: str
    risk: str
    content: dict[str, Any]
    support_count: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    target_metric: str
    target_value: float | None = None
    measurement_window: int = Field(default=10, ge=1)
    status: WorkflowStatus
    checks: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    task_archetype_id: str | None = None
    exposure_counts: dict[str, int] = Field(default_factory=dict)
    active_intervention_id: str | None = None
    variant_count: int = Field(default=1, ge=1)
    supporting_observation_count: int = Field(default=1, ge=1)
    source_scopes: list[str] = Field(default_factory=list)


class WorkflowSessionRecord(ReflectModel):
    session_id: str
    relationship: str
    title: str | None = None
    agent: str | None = None
    started_at: str
    status: str
    workspace: str | None = None
    evidence_count: int = Field(default=0, ge=0)
    evidence_summaries: list[str] = Field(default_factory=list)
    evidence_focus_id: str | None = None
    exposure_state: str | None = None


class WorkflowSessionLedger(ReflectModel):
    candidate_id: str
    observation_id: str
    observation_ids: list[str] = Field(default_factory=list)
    skill_slug: str = ""
    source_session_count: int = Field(default=0, ge=0)
    source_sessions: list[WorkflowSessionRecord] = Field(default_factory=list)
    exposure_session_count: int = Field(default=0, ge=0)
    exposure_sessions: list[WorkflowSessionRecord] = Field(default_factory=list)


class SkillRecord(ReflectModel):
    id: str
    slug: str
    name: str
    description: str
    origin: SkillOrigin
    lifecycle_state: SkillLifecycleState
    current_version_id: str | None = None
    current_version: int | None = None
    current_version_status: SkillVersionStatus | None = None
    source_agent: str | None = None
    version_count: int = Field(default=0, ge=0)
    evidence_count: int = Field(default=0, ge=0)
    installation_count: int = Field(default=0, ge=0)
    installation_targets: list[str] = Field(default_factory=list)
    usage_count: int = Field(default=0, ge=0)
    measurement_count: int = Field(default=0, ge=0)
    first_seen_at: str
    last_seen_at: str
    updated_at: str


class SkillVersionRecord(ReflectModel):
    id: str
    skill_id: str
    version: int = Field(ge=1)
    content_markdown: str
    content_hash: str
    workflow: dict[str, Any] = Field(default_factory=dict)
    source_kind: str
    source_agent: str | None = None
    source_loop_id: str | None = None
    source_workflow_id: str | None = None
    workflow_candidate_id: str | None = None
    status: SkillVersionStatus
    created_at: str
    updated_at: str


class SkillInstallationRecord(ReflectModel):
    id: str
    skill_id: str
    skill_version_id: str | None = None
    target_kind: str
    target_ref: str
    path: str
    installed_hash: str | None = None
    status: str
    first_seen_at: str
    last_seen_at: str


class SkillMeasurementRecord(ReflectModel):
    id: str
    skill_id: str
    skill_version_id: str | None = None
    metric_name: str
    before_value: float | None = None
    after_value: float | None = None
    verdict: str
    confidence: float = Field(default=0, ge=0, le=1)
    measured_at: str
    details: dict[str, Any] = Field(default_factory=dict)


class SkillUsageSessionRecord(ReflectModel):
    session_id: str
    skill_version_id: str | None = None
    title: str | None = None
    agent: str | None = None
    started_at: str
    status: str
    workspace: str | None = None
    state: str
    outcome: str | None = None
    confidence: float = Field(default=0, ge=0, le=1)
    observed_at: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class SkillDetail(ReflectModel):
    skill: SkillRecord
    versions: list[SkillVersionRecord] = Field(default_factory=list)
    installations: list[SkillInstallationRecord] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    usage_sessions: list[SkillUsageSessionRecord] = Field(default_factory=list)
    measurements: list[SkillMeasurementRecord] = Field(default_factory=list)


class LoopRecord(ReflectModel):
    id: str
    fingerprint: str
    kind: LoopKind
    title: str
    summary: str
    scope_type: str
    scope_id: str
    repo_id: str | None = None
    tool_name: str | None = None
    occurrence_count: int = Field(default=0, ge=0)
    affected_session_count: int = Field(default=0, ge=0)
    state_change_count: int = Field(default=0, ge=0)
    confidence: float = Field(default=0, ge=0, le=1)
    status: LoopStatus
    evidence: dict[str, Any] = Field(default_factory=dict)
    first_seen_at: str
    last_seen_at: str
    updated_at: str


class LoopOccurrenceRecord(ReflectModel):
    id: str
    loop_id: str
    session_id: str
    tool_name: str
    input_hash: str | None = None
    repeat_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    state_changed: bool = False
    outcome: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class LoopDetail(ReflectModel):
    loop: LoopRecord
    occurrences: list[LoopOccurrenceRecord] = Field(default_factory=list)


class ImprovementSummary(ReflectModel):
    generated_at: str
    observations: list[ObservationRecord]
    counts_by_status: dict[str, int]
    pending_workflows: int
    active_interventions: int
    verified_improvement_rate: float | None = None


class AskEvidence(ReflectModel):
    kind: str
    id: str
    summary: str
    confidence: float = Field(ge=0, le=1)


class AskAnswer(ReflectModel):
    question: str
    answer: str
    guidance: list[str]
    evidence: list[AskEvidence]
    confidence: float = Field(ge=0, le=1)
    workflow_id: str | None = None
    freshness: str | None = None
    constraints: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    fallback: str | None = None
    limitations: list[str] = Field(default_factory=list)
