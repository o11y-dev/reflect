from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from reflect.improvements.models import LoopKind, LoopStatus, SkillLifecycleState
from reflect.improvements.service import ImprovementService
from reflect.store.sqlite import connect_sqlite, connect_sqlite_read_only


@dataclass(frozen=True)
class DashboardImprovementAdapter:
    """Translate improvement services into dashboard-ready read and write models."""

    db_path: Path

    @contextmanager
    def _service(self, *, writable: bool = False) -> Iterator[ImprovementService]:
        connect = connect_sqlite if writable else connect_sqlite_read_only
        conn = connect(self.db_path)
        try:
            yield ImprovementService(conn, initialize_schema=False)
        finally:
            conn.close()

    def findings(
        self,
        *,
        status: str | None,
        include_resolved: bool,
        limit: int,
    ) -> dict[str, object]:
        with self._service() as service:
            findings = service.list_findings(
                limit=500,
                status=status,
                include_resolved=include_resolved,
            )
            summary = service.repository.summary(limit=0)
            if status:
                observation_record_count = summary.counts_by_status.get(status, 0)
            elif include_resolved:
                observation_record_count = sum(summary.counts_by_status.values())
            else:
                observation_record_count = sum(
                    summary.counts_by_status.get(item, 0)
                    for item in (
                        "new",
                        "acknowledged",
                        "proposal_ready",
                        "approved",
                        "active",
                        "regressed",
                    )
                )
            return {
                "generated_at": summary.generated_at,
                "findings": [item.model_dump(mode="json") for item in findings[:limit]],
                "finding_total_count": len(findings),
                "observation_record_count": observation_record_count,
                "counts_by_status": summary.counts_by_status,
                "pending_workflows": summary.pending_workflows,
                "active_interventions": summary.active_interventions,
                "verified_improvement_rate": summary.verified_improvement_rate,
            }

    def rules(self) -> dict[str, object]:
        with self._service() as service:
            rules = service.repository.list_rule_summaries()
        return {
            "rules": [rule.model_dump(mode="json") for rule in rules],
            "extension": {
                "kind": "code_backed",
                "module": "reflect.improvements",
                "base_class": "BaseImprovementRule",
                "registry": "DEFAULT_RULE_REGISTRY",
                "registration": "RuleRegistry.register",
            },
        }

    def finding(self, finding_id: str) -> dict[str, object] | None:
        with self._service() as service:
            observation = service.repository.get_observation(finding_id)
        return observation.model_dump(mode="json") if observation is not None else None

    def finding_evidence(self, observation_id: str, *, limit: int) -> dict[str, object]:
        with self._service() as service:
            ledger = service.finding_evidence_ledger(observation_id, limit=limit)
        return ledger.model_dump(mode="json")

    def workflows(
        self,
        *,
        behavior_type: str | None,
        status: str | None,
    ) -> dict[str, object]:
        with self._service() as service:
            candidates = service.workflows.list(
                behavior_types={behavior_type} if behavior_type else None,
                statuses={status} if status else None,
            )
            serialized = []
            for candidate in candidates:
                item = candidate.model_dump(mode="json")
                try:
                    item["skill_id"] = service.skills.skill_for_candidate(candidate.id).id
                except KeyError:
                    item["skill_id"] = None
                serialized.append(item)
        return {"workflows": serialized}

    def loops(
        self,
        *,
        kind: str | None,
        status: str | None,
        limit: int,
    ) -> dict[str, object]:
        with self._service() as service:
            records = service.loops.list(
                kind=LoopKind(kind) if kind else None,
                status=LoopStatus(status) if status else None,
                limit=limit,
            )
        return {"loops": [record.model_dump(mode="json") for record in records]}

    def loop(self, loop_id: str) -> dict[str, object]:
        with self._service() as service:
            record = service.loops.show(loop_id)
        return record.model_dump(mode="json")

    def skills(
        self,
        *,
        status: str | None,
        include_stale: bool,
        limit: int,
    ) -> dict[str, object]:
        lifecycle = SkillLifecycleState(status) if status else None
        with self._service() as service:
            counts_by_lifecycle = service.skills.counts_by_lifecycle()
            records = service.skills.list(
                lifecycle=lifecycle,
                include_stale=include_stale,
                limit=limit,
            )
        if lifecycle:
            total_count = counts_by_lifecycle.get(lifecycle.value, 0)
        elif include_stale:
            total_count = sum(counts_by_lifecycle.values())
        else:
            total_count = sum(
                counts_by_lifecycle.get(item.value, 0)
                for item in (SkillLifecycleState.ACTIVE, SkillLifecycleState.PENDING)
            )
        return {
            "skills": [record.model_dump(mode="json") for record in records],
            "total_count": total_count,
            "archived_count": counts_by_lifecycle.get(SkillLifecycleState.STALE.value, 0),
            "counts_by_lifecycle": counts_by_lifecycle,
        }

    def skill(self, skill_id: str) -> dict[str, object]:
        with self._service() as service:
            record = service.skills.show(skill_id)
        return record.model_dump(mode="json")

    def workflow_evidence(self, candidate_id: str, *, limit: int) -> dict[str, object]:
        with self._service() as service:
            ledger = service.repository.workflow_evidence_ledger(candidate_id, limit=limit)
        return ledger.model_dump(mode="json")

    def workflow_preview(self, candidate_id: str, *, project_root: Path) -> dict[str, object]:
        with self._service() as service:
            return service.workflows.preview(candidate_id, project_root=project_root)

    def edit_workflow(self, candidate_id: str, *, content: dict) -> dict[str, object]:
        with self._service(writable=True) as service:
            candidate = service.workflows.edit(candidate_id, content=content)
        return candidate.model_dump(mode="json")

    def apply_workflow(self, candidate_id: str, *, project_root: Path) -> dict[str, object]:
        with self._service(writable=True) as service:
            return service.workflows.apply(candidate_id, project_root=project_root)

    def rollback_workflow(self, candidate_id: str) -> dict[str, object]:
        with self._service(writable=True) as service:
            return service.workflows.rollback(candidate_id)

    def reject_workflow(self, candidate_id: str, *, reason: str) -> dict[str, object]:
        with self._service(writable=True) as service:
            candidate = service.workflows.reject(candidate_id, reason=reason[:200])
        return candidate.model_dump(mode="json")

    def record_feedback(
        self,
        session_id: str,
        *,
        outcome: str,
        reason: object,
    ) -> dict[str, object]:
        with self._service(writable=True) as service:
            feedback_id = service.repository.record_feedback(
                session_id,
                outcome,
                reason_redacted=str(reason) if reason is not None else None,
            )
        return {"id": feedback_id, "session_id": session_id, "outcome": outcome}

    def impact(self) -> dict[str, object]:
        with self._service() as service:
            return {"impact_checks": service.measurements.list()}

    def impact_evidence(self, impact_id: str) -> dict[str, object]:
        with self._service() as service:
            return service.measurements.sessions(impact_id)
