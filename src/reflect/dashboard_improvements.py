from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from reflect.improvements.models import LoopKind, LoopStatus, SkillLifecycleState
from reflect.improvements.service import ImprovementService
from reflect.preparation import PreparationLock
from reflect.store.sqlite import connect_sqlite, connect_sqlite_read_only, snapshot_incomplete


def _recorded_evidence(payload: dict, *, recorded_at: str | None = None) -> dict:
    """Recorded references are inspectable without claiming current validation."""
    return {
        **payload, "analysis_state": "pending", "evidence_state": "recorded",
        "publication": {
            "state": "recorded", "reason": "preparing",
            "loaded_at": datetime.now(UTC).isoformat(),
            "snapshot_completed_at": None, "recorded_at": recorded_at,
        },
    }


@dataclass(frozen=True)
class DashboardImprovementAdapter:
    """Translate improvement services into dashboard-ready read and write models."""

    db_path: Path

    @contextmanager
    def _service(
        self, *, writable: bool = False, profile: str | None = "snapshot",
    ) -> Iterator[ImprovementService]:
        with (
            PreparationLock(self.db_path) if writable else nullcontext(),
            closing(
                connect_sqlite(self.db_path) if writable else
                connect_sqlite_read_only(self.db_path, profile=profile)
            ) as conn,
        ):
            yield ImprovementService(conn, initialize_schema=False)

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
                "analysis_state": summary.analysis_state,
                "analysis_coverage": summary.analysis_coverage,
                "limitations": summary.limitations,
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
        with self._service(profile=None) as service:
            pending = snapshot_incomplete(service.conn)
            candidates = service.workflows.list(
                behavior_types={behavior_type} if behavior_type else None,
                statuses={status} if status else None,
                recorded_only=pending,
            )
            serialized = []
            for candidate in candidates:
                item = candidate.model_dump(mode="json")
                item["suggestion_assessment"] = service.workflows.suggestion_assessment(candidate)
                try:
                    item["skill_id"] = service.skills.skill_for_candidate(candidate.id).id
                except KeyError:
                    item["skill_id"] = None
                serialized.append(item)
        payload = {"workflows": serialized}
        return _recorded_evidence(payload) if pending else payload

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
        with self._service(profile=None) as service:
            # The read-only connection pins readiness and registry rows in one
            # transaction. Package history remains browsable during derivation.
            pending = snapshot_incomplete(service.conn)
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
        skills = [record.model_dump(mode="json") for record in records]
        payload = {
            "analysis_state": "pending" if pending else "ready",
            "skills": skills,
            "total_count": total_count,
            "archived_count": counts_by_lifecycle.get(SkillLifecycleState.STALE.value, 0),
            "counts_by_lifecycle": counts_by_lifecycle,
        }
        return _recorded_evidence(payload) if pending else payload

    def skill(self, skill_id: str) -> dict[str, object]:
        with self._service(profile=None) as service:
            pending = snapshot_incomplete(service.conn)
            record = service.skills.show(skill_id, recorded_only=pending)
        detail = record.model_dump(mode="json")
        detail["analysis_state"] = "pending" if pending else "ready"
        if pending:
            for usage in detail["usage_occurrences"]:
                usage["outcome"] = None
            for measurement in detail["measurements"]:
                measurement["recorded_result"] = {
                    field: measurement[field] for field in ("before_value", "after_value", "verdict", "confidence")
                }
                measurement.update(before_value=None, after_value=None, verdict="validation_pending", confidence=0.0)
            return _recorded_evidence(detail)
        return detail

    def workflow_evidence(self, candidate_id: str, *, limit: int) -> dict[str, object]:
        with self._service(profile=None) as service:
            pending = snapshot_incomplete(service.conn)
            ledger = service.repository.workflow_evidence_ledger(candidate_id, limit=limit, recorded_only=pending)
            candidate = service.workflows.show(candidate_id, recorded_only=pending, evidence_ledger=ledger)
        payload = ledger.model_dump(mode="json")
        payload["candidate"] = candidate.model_dump(mode="json")
        return _recorded_evidence(payload) if pending else payload

    def skill_usage_graph(self, skill_id: str, **filters) -> dict[str, object]:
        from reflect.dashboard_skill_graph import build_skill_usage_graph

        with self._service(profile=None) as service:
            return build_skill_usage_graph(service.conn, skill_id, **filters)

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

    def dismiss_suggestion(self, candidate_id: str, *, restore: bool = False) -> dict[str, object]:
        # Review metadata needs no analysis snapshot or filesystem preparation lock.
        # Reserve SQLite's writer before reading state; busy errors stay retryable.
        with closing(connect_sqlite(self.db_path, timeout_ms=1000)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            service = ImprovementService(conn, initialize_schema=False)
            return (service.workflows.restore_suggestion(candidate_id) if restore
                    else service.workflows.dismiss_suggestion(candidate_id))

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
        with self._service(profile=None) as service:
            pending = snapshot_incomplete(service.conn)
            checks = service.measurements.list(recorded_only=pending)
        payload = {"impact_checks": checks}
        return _recorded_evidence(payload, recorded_at=checks[0]["measured_at"] if checks else None) if pending else payload

    def impact_evidence(self, impact_id: str) -> dict[str, object]:
        with self._service(profile=None) as service:
            pending = snapshot_incomplete(service.conn)
            payload = service.measurements.sessions(impact_id, recorded_only=pending)
        return _recorded_evidence(payload, recorded_at=payload.get("measured_at")) if pending else payload
