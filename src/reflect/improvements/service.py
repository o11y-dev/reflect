from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from time import perf_counter

from reflect.execution_units import ExecutionUnitService
from reflect.improvements.analysis_scope import AnalysisScope
from reflect.improvements.archetypes import TaskArchetypeService, WorkflowAdherenceService
from reflect.improvements.base import BaseImprovementRule, RuleRegistry
from reflect.improvements.contracts import WorkflowContract
from reflect.improvements.loops import LoopService
from reflect.improvements.measurement import MeasurementService
from reflect.improvements.models import (
    AskAnswer,
    AskEvidence,
    EvidenceRef,
    FindingEvidenceLedger,
    FindingRecord,
    ImprovementScope,
    ImprovementSummary,
    ObservationDraft,
    ObservationRecord,
    RuleDefinition,
    Severity,
    WorkflowCandidateRecord,
    WorkflowProposal,
    WorkflowSourceKind,
)
from reflect.improvements.repository import ImprovementRepository, utc_now
from reflect.improvements.rules import DEFAULT_RULE_REGISTRY
from reflect.improvements.scope import ImprovementScopeResolver
from reflect.improvements.session_insights import SessionInsightRule, session_inputs
from reflect.improvements.skills import SkillRegistryService
from reflect.improvements.workflows import WorkflowService
from reflect.store.migrate import migrate
from reflect.utils import _json_dumps
from reflect.views.measurements import build_measurements


class FindingWorkflowConflict(RuntimeError):
    def __init__(self, candidate_id: str):
        super().__init__("This finding already has a different workflow. Your instructions are kept; open the linked workflow to review or edit it.")
        self.candidate_id = candidate_id


class ImprovementService:
    """Application service for detection, review, retrieval, and measurement."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        rules: Iterable[BaseImprovementRule] | RuleRegistry | None = None,
        initialize_schema: bool = True,
    ):
        self.conn = conn
        if initialize_schema:
            migrate(conn)
        source_registry = DEFAULT_RULE_REGISTRY if rules is None else rules
        self.rule_registry = (
            source_registry.copy()
            if isinstance(source_registry, RuleRegistry)
            else RuleRegistry(source_registry)
        )
        self.rules = self.rule_registry.rules
        self.repository = ImprovementRepository(conn)
        self.workflows = WorkflowService(conn)
        self.measurements = MeasurementService(conn)
        self.loops = LoopService(conn, initialize_schema=False)
        self.skills = SkillRegistryService(conn, initialize_schema=False)
        self.execution_units = ExecutionUnitService(conn)
        self.archetypes = TaskArchetypeService(conn)
        self.adherence = WorkflowAdherenceService(conn)
        self.timings: dict[str, float] = {}

    def refresh(
        self, *, session_ids: set[str] | None = None,
        analysis_session_ids: set[str] | None = None,
        measure: bool = True,
        dependencies_expanded: bool = False,
        step_scope: dict[str, set[str] | None] | None = None,
        reassess_unit_ids: dict[str, set[str]] | None = None,
    ) -> dict[str, int]:
        """Refresh evidence and evaluate a complete selected analysis cohort.

        Each scope uses None for full and an empty set for no work. Repository
        detectors need the complete selected cohort, not just changed sessions.
        Findings outside the successfully evaluated cohort are retained. A
        caller owning pending-work acknowledgements can defer measurement until
        its completed evidence checkpoint commits.
        """
        self.timings.clear()
        phase_started = perf_counter()
        now = utc_now()
        resolved = self.repository.retire_rule(
            "retry_loop_without_state_change",
            now=now,
        )
        affected = (
            self.execution_units.affected_session_ids(session_ids, step_scope=step_scope)
            if session_ids is not None and not dependencies_expanded else session_ids
        )
        workflow_evidence = self.prepare_workflow_evidence(
            session_ids=affected, assess_contracts=False, step_scope=step_scope,
            reassess_unit_ids=reassess_unit_ids if dependencies_expanded else None,
        )
        self.timings["evidence"] = perf_counter() - phase_started
        phase_started = perf_counter()
        # Artifact drift and approval/revocation can invalidate historical
        # outcomes even when no telemetry arrived in the analysis window.
        integrity_result = self.workflows.refresh_integrity()
        contract_inputs = self._contract_inputs_fingerprint()
        previous = self.conn.execute(
            "SELECT value FROM store_metadata WHERE key='assessed_contract_inputs_v1'"
        ).fetchone()
        contracts_changed = previous is None or previous[0] != contract_inputs
        # Only rederived evidence can change an unchanged contract assessment.
        # Complete repository peers remain detector context, not new work.
        self.execution_units.assess_contracts(
            session_ids=None if contracts_changed else affected,
            execution_unit_ids=None if contracts_changed else self.execution_units.changed_unit_ids,
        )
        from reflect.store.graph_execution import refresh_execution_graph
        refresh_execution_graph(
            self.conn, None if contracts_changed else affected, commit=False,
            execution_unit_ids=None if contracts_changed else self.execution_units.changed_unit_ids,
        )
        self.repository.sync_rule_definitions(
            (rule.definition for rule in self.rules),
            now=now,
        )
        self._backfill_workflow_metadata()
        self.timings["contracts"] = perf_counter() - phase_started
        with AnalysisScope(
            self.conn, analysis_session_ids,
            current_execution_ids=self.execution_units.changed_unit_ids,
        ) as analysis_scope:
            return self._refresh_findings(
                now, resolved, workflow_evidence, analysis_scope,
                integrity_result, contract_inputs, contracts_changed, measure,
                self.execution_units.rederived_session_ids, step_scope,
            )

    def _contract_inputs_fingerprint(self) -> str:
        """Small configuration tables, never event history or derived timestamps."""
        payload = [
            [tuple(row) for row in self.conn.execute(query)]
            for query in (
                """SELECT wv.id, wv.status, wv.content_hash, wv.content_json,
                          wc.task_archetype_id, o.repo_id
                   FROM workflow_versions wv
                   JOIN workflow_candidates wc ON wc.id=wv.candidate_id
                   JOIN observations o ON o.id=wc.observation_id ORDER BY wv.id""",
                """SELECT id, workflow_version_id, status, exposure_started_at
                   FROM interventions ORDER BY id""",
                """SELECT id, skill_version_id, status, target_kind, target_ref
                   FROM skill_installations ORDER BY id""",
            )
        ]
        return hashlib.sha256(_json_dumps(payload).encode()).hexdigest()

    def _refresh_findings(
        self, now: str, resolved: int, workflow_evidence: dict[str, int],
        analysis_scope: AnalysisScope,
        integrity_result: dict[str, int], contract_inputs: str, contracts_changed: bool,
        measure: bool,
        evidence_session_ids: set[str] | None,
        step_scope: dict[str, set[str] | None] | None,
    ) -> dict[str, int]:
        detected = candidates = 0
        evaluated_rules = self.rules if analysis_scope.session_ids != set() else ()
        try:
            phase_started = perf_counter()
            # All seven session rules share one bounded canonical projection.
            session_rules = [rule for rule in evaluated_rules if isinstance(rule, SessionInsightRule)]
            premium = next((rule.definition.detector_config["premium_model_patterns"] for rule in session_rules if rule.name == "premium_model"), ["opus", "fable"])
            insight_inputs = session_inputs(build_measurements(self.conn, session_ids=analysis_scope.session_ids, include_turns=True), premium_patterns=tuple(premium)) if session_rules else {}
            for rule in evaluated_rules:
                seen_ids: set[str] = set()
                drafts = rule.detect_inputs(insight_inputs) if isinstance(rule, SessionInsightRule) else rule.evaluate(self.conn, scope=analysis_scope)
                for draft in drafts:
                    observation_id = self.repository.upsert_observation(draft, now=now)
                    seen_ids.add(observation_id)
                    detected += 1
                    proposal = rule.propose(draft)
                    if proposal is None:
                        continue
                    before = self.conn.total_changes
                    candidate_id = self.repository.ensure_candidate(
                        observation_id,
                        proposal=proposal,
                        now=now,
                    )
                    contract = WorkflowContract.from_raw(
                        proposal.content.get("workflow_contract")
                    )
                    archetype_id = (
                        contract.applicability.task_archetype_id if contract else None
                    ) or self.archetypes.dominant_for_observation(observation_id)
                    if archetype_id:
                        self.conn.execute(
                            "UPDATE workflow_candidates SET task_archetype_id = ?, updated_at = ? WHERE id = ?",
                            (archetype_id, now, candidate_id),
                        )
                    if self.conn.total_changes > before:
                        candidates += 1
                resolved += self.repository.resolve_missing(rule.definition, seen_ids, now=now, scope=analysis_scope)
            self.timings["detectors"] = perf_counter() - phase_started
            phase_started = perf_counter()
            adherence_result = self.adherence.refresh(
                scope=None if contracts_changed else analysis_scope,
                execution_unit_ids=None if contracts_changed else self.execution_units.changed_unit_ids,
            )
            self.timings["adherence"] = perf_counter() - phase_started
            phase_started = perf_counter()
            measurement_result = self.measurements.measure_active() if measure else {"created": 0, "regressed": 0}
            self.timings["measurements"] = perf_counter() - phase_started
            phase_started = perf_counter()
            loop_result = self.loops.refresh(commit=False, scope=analysis_scope)
            self.timings["loops"] = perf_counter() - phase_started
            phase_started = perf_counter()
            precise_usage = (
                evidence_session_ids is not None and step_scope is not None
                and all(step_scope.get(sid) is not None for sid in evidence_session_ids)
            )
            usage_steps = None
            if precise_usage:
                usage_steps = set().union(*(step_scope[sid] for sid in evidence_session_ids))
                usage_steps.update(self.execution_units.changed_membership_step_ids)
            skill_result = self.skills.refresh(
                commit=False, scope=analysis_scope,
                usage_scope=AnalysisScope(session_ids=evidence_session_ids),
                usage_step_ids=usage_steps,
            )
            self.timings["skills"] = perf_counter() - phase_started
            if contracts_changed:
                self.conn.execute(
                    """INSERT INTO store_metadata(key,value,updated_at)
                       VALUES ('assessed_contract_inputs_v1',?,?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                    (contract_inputs, now),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        phase_started = perf_counter()
        candidates = len(self.workflows.list(statuses={"pending"}, limit=500))
        self.timings["candidate_summary"] = perf_counter() - phase_started
        return {
            "detected": detected,
            "resolved": resolved,
            "contracts_changed": int(contracts_changed),
            "candidates": candidates,
            "execution_units": workflow_evidence["execution_units"],
            "reassessed_execution_units": workflow_evidence["reassessed_execution_units"],
            "classified_execution_units": workflow_evidence["classified_execution_units"],
            "excluded_execution_units": workflow_evidence["excluded_execution_units"],
            "workflow_exposures": adherence_result["exposures"],
            "stale_workflows": integrity_result["stale"],
            "measurements_created": measurement_result["created"],
            "regressions": measurement_result["regressed"],
            "loops": loop_result["detected"],
            "skills": skill_result["workflow_skills"],
        }

    def prepare_workflow_evidence(
        self,
        *,
        session_ids: Iterable[str] | None = None,
        assess_contracts: bool = True,
        step_scope: dict[str, set[str] | None] | None = None,
        reassess_unit_ids: dict[str, set[str]] | None = None,
    ) -> dict[str, int]:
        """Refresh bounded execution and archetype evidence without running detectors."""

        scoped_ids = (
            None
            if session_ids is None
            else {str(item) for item in session_ids if item}
        )
        phase_started = perf_counter()
        execution_units = self.execution_units.refresh(
            session_ids=scoped_ids, step_scope=step_scope, reassess_unit_ids=reassess_unit_ids,
        )
        self.timings["execution_derivation"] = perf_counter() - phase_started
        phase_started = perf_counter()
        archetypes = self.archetypes.refresh(
            session_ids=self.execution_units.rederived_session_ids,
            execution_unit_ids=self.execution_units.changed_unit_ids - self.execution_units.reassessed_unit_ids,
        )
        self.timings["archetypes"] = perf_counter() - phase_started
        if assess_contracts:
            self.execution_units.assess_contracts(session_ids=scoped_ids)
        return {
            "execution_units": execution_units["execution_units"],
            "reassessed_execution_units": len(self.execution_units.reassessed_unit_ids),
            "assigned_steps": execution_units["assigned_steps"],
            "classified_execution_units": archetypes["classified_execution_units"],
            "excluded_execution_units": archetypes["excluded_execution_units"],
        }

    def _backfill_workflow_metadata(self) -> None:
        """Add rule-owned behavior and authorship metadata to candidates from older builds."""
        for rule in self.rules:
            if rule.workflow is None:
                continue
            self.conn.execute(
                """
                UPDATE workflow_candidates
                SET content_json = json_set(
                      content_json,
                      '$.behavior_type', ?,
                      '$.suggested_artifact', ?,
                      '$.source.kind', ?,
                      '$.source.rule_id', ?,
                      '$.source.rule_version', ?
                    ),
                    provenance_json = json_set(provenance_json, '$.source', ?)
                WHERE json_extract(provenance_json, '$.rule_id') = ?
                  AND (
                    json_extract(content_json, '$.behavior_type') IS NULL
                    OR json_extract(content_json, '$.suggested_artifact') IS NULL
                    OR json_extract(content_json, '$.source.kind') IS NULL
                    OR json_extract(content_json, '$.source.rule_id') IS NULL
                    OR json_extract(provenance_json, '$.source') IS NULL
                  )
                """,
                (
                    rule.workflow.behavior_type.value,
                    rule.workflow.suggested_artifact.value,
                    WorkflowSourceKind.RULE_BLUEPRINT.value,
                    rule.definition.id,
                    rule.definition.version,
                    WorkflowSourceKind.RULE_BLUEPRINT.value,
                    rule.definition.id,
                ),
            )
        self.conn.execute(
            """
            UPDATE workflow_candidates
            SET content_json = json_set(
                  content_json,
                  '$.suggested_artifact', 'skill',
                  '$.source.kind', 'agent_authored'
                ),
                provenance_json = json_set(provenance_json, '$.source', 'agent_authored')
            WHERE json_extract(content_json, '$.source.rule_id') = 'discovered_reusable_workflow'
              AND COALESCE(
                    json_extract(content_json, '$.source.kind'),
                    json_extract(provenance_json, '$.source')
                  ) IN ('skill_extraction', 'agent_authored')
            """
        )
        self.conn.execute(
            """
            UPDATE workflow_candidates
            SET content_json = json_set(content_json, '$.suggested_artifact', 'skill')
            WHERE json_extract(content_json, '$.suggested_artifact') IS NULL
            """
        )

    def improve(
        self,
        observation_id: str | None = None,
        *,
        refresh: bool = True,
        scope: ImprovementScope | None = None,
    ) -> ImprovementSummary | ObservationRecord:
        if refresh:
            self.refresh()
        if observation_id:
            observation = self.repository.get_observation(observation_id)
            if observation is None:
                raise KeyError(f"Observation not found: {observation_id}")
            return self.repository.with_scope_stats(observation, scope) if scope else observation
        return self.repository.summary(scope=scope)

    def _group_observations_by_finding(
        self,
        observations: list[ObservationRecord],
    ) -> tuple[
        dict[tuple[str, ...], list[ObservationRecord]],
        dict[str, WorkflowCandidateRecord],
    ]:
        """Group scope-specific observations by the durable finding key.

        Candidate-backed observations group by their canonical procedure contract.
        Everything else groups by (rule_id, title).
        """
        candidate_by_id = {
            candidate.id: candidate for candidate in self.repository.iter_candidates()
        }
        grouped: dict[tuple[str, ...], list[ObservationRecord]] = {}
        for observation in observations:
            candidate = candidate_by_id.get(observation.candidate_id or "")
            if candidate is not None:
                key = ("workflow", candidate.contract_signature)
            else:
                key = ("observation", observation.rule_id, observation.title)
            grouped.setdefault(key, []).append(observation)
        return grouped, candidate_by_id

    def resolve_finding_observation_ids(
        self,
        observation_id: str,
        *,
        status: str | None = None,
        include_resolved: bool = False,
    ) -> list[str]:
        """Resolve every observation ID grouped into the same finding as observation_id.

        Mirrors the grouping list_findings uses, so an evidence ledger fetched by a single
        member observation still reflects the full finding rather than just that one scope.
        Falls back to the observation alone when the current filters exclude it from the finding list.
        """
        if self.repository.get_observation(observation_id) is None:
            raise KeyError(f"Observation not found: {observation_id}")

        observations = self.repository.list_observations(
            limit=500,
            status=status,
            include_resolved=include_resolved,
        )
        grouped, _ = self._group_observations_by_finding(observations)
        for members in grouped.values():
            if any(item.id == observation_id for item in members):
                return [item.id for item in members]
        return [observation_id]

    def list_findings(
        self,
        *,
        limit: int = 100,
        status: str | None = None,
        include_resolved: bool = False,
        scope: ImprovementScope | None = None,
    ) -> list[FindingRecord]:
        """Group scope-specific observations into durable reviewable findings."""
        observations = self.repository.list_observations(
            limit=500,
            status=status,
            include_resolved=include_resolved,
            scope=scope,
        )
        if not observations:
            return []

        rule_by_id = {
            rule.id: rule
            for rule in self.repository.list_rule_summaries()
        }

        grouped, candidate_by_id = self._group_observations_by_finding(observations)

        findings: list[FindingRecord] = []
        status_priority = {
            "regressed": 0,
            "active": 1,
            "approved": 2,
            "proposal_ready": 3,
            "acknowledged": 4,
            "new": 5,
        }
        severity_priority = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        for _key, members in grouped.items():
            member_candidates = [
                candidate_by_id[item.candidate_id]
                for item in members
                if item.candidate_id in candidate_by_id
            ]
            workflow = (
                min(member_candidates, key=self.workflows._representative_sort_key)
                if member_candidates
                else None
            )
            representative = next(
                (
                    item
                    for item in members
                    if workflow is not None and item.candidate_id == workflow.id
                ),
                None,
            )
            if representative is None:
                representative = min(
                    members,
                    key=lambda item: (
                        status_priority.get(item.status.value, 9),
                        0 if item.candidate_id else 1,
                        -(item.affected_session_ratio or 0.0),
                        -self.repository._timestamp(item.latest_source_at),
                        -item.confidence,
                        -item.impact_score,
                        item.id,
                    ),
                )

            source_scopes = sorted(
                {f"{item.scope_type}:{item.scope_id}" for item in members}
            )
            rule = rule_by_id.get(representative.rule_id)
            distinct_titles = {item.title for item in members}
            member_ids = [item.id for item in members]
            evidence_ledger = self.repository.finding_evidence_ledger(
                representative.id,
                member_ids,
                candidate_id=(
                    workflow.id if workflow is not None else representative.candidate_id
                ),
                scope=scope,
                limit=1,
            )
            linked_sessions = evidence_ledger.provenance_session_count
            if linked_sessions == 0:
                continue
            latest_source_at = max(
                (
                    item.latest_source_at
                    for item in members
                    if item.latest_source_at is not None
                ),
                key=self.repository._timestamp,
                default=None,
            )
            data = representative.model_dump(mode="python")
            data.update(
                {
                    "title": (
                        rule.title
                        if rule is not None and (len(members) > 1 or len(distinct_titles) > 1)
                        else representative.title
                    ),
                    "summary": (
                        f"{rule.description} {len(members)} current evidence pattern(s) "
                        f"across {len(source_scopes)} scope(s) are grouped here."
                        if rule is not None and len(members) > 1
                        else representative.summary
                    ),
                    "impact_score": max(item.impact_score for item in members),
                    "severity": max(
                        (item.severity for item in members),
                        key=lambda value: severity_priority.get(value.value, -1),
                    ),
                    "confidence": max(item.confidence for item in members),
                    "affected_session_count": linked_sessions,
                    "scope_affected_session_count": linked_sessions if scope else None,
                    "eligible_session_count": (
                        scope.eligible_session_count if scope is not None else 0
                    ),
                    "affected_session_ratio": (
                        float(linked_sessions) / scope.eligible_session_count
                        if scope is not None and scope.eligible_session_count
                        else 0.0
                    ),
                    "latest_source_at": latest_source_at,
                    "resolved_scope": scope,
                    "candidate_id": workflow.id if workflow is not None else representative.candidate_id,
                    "candidate_status": (
                        workflow.status if workflow is not None else representative.candidate_status
                    ),
                    "observation_count": len(members),
                    "variant_count": len(distinct_titles),
                    "source_scope_count": len(source_scopes),
                    "source_scopes": source_scopes,
                }
            )
            findings.append(FindingRecord.model_validate(data))

        findings.sort(
            key=lambda item: (
                status_priority.get(item.status.value, 9),
                0 if item.candidate_id else 1,
                -(item.affected_session_ratio or 0.0),
                -self.repository._timestamp(item.latest_source_at),
                -item.confidence,
                -item.impact_score,
                item.title,
            )
        )
        return findings[: max(1, min(limit, 500))]

    def finding_observation_ids(
        self,
        observation_id: str,
        *,
        scope: ImprovementScope | None = None,
    ) -> list[str]:
        target = self.repository.get_observation(observation_id)
        if target is None:
            raise KeyError(f"Observation not found: {observation_id}")
        if target.candidate_id:
            candidate = self.repository.get_candidate(target.candidate_id)
            if candidate is not None:
                allowed = (
                    {
                        item.id
                        for item in self.repository.list_observations(
                            limit=500,
                            scope=scope,
                        )
                    }
                    if scope
                    else None
                )
                return [
                    item.observation_id
                    for item in self.repository.list_candidates_by_contract(
                        candidate.contract_signature
                    )
                    if item.status.value != "rejected"
                    and (allowed is None or item.observation_id in allowed)
                ]
        return [
            item.id
            for item in self.repository.list_observations(limit=500, scope=scope)
            if item.rule_id == target.rule_id and item.title == target.title
        ] or [observation_id]

    def finding_evidence_ledger(
        self,
        observation_id: str,
        *,
        scope: ImprovementScope | None = None,
        limit: int = 50,
    ) -> FindingEvidenceLedger:
        target = self.repository.get_observation(observation_id)
        if target is None:
            raise KeyError(f"Observation not found: {observation_id}")
        ids = self.finding_observation_ids(observation_id, scope=scope)
        return self.repository.finding_evidence_ledger(
            observation_id,
            ids,
            candidate_id=target.candidate_id,
            scope=scope,
            limit=limit,
        )

    def ask(
        self,
        question: str,
        *,
        task_file: Path | None = None,
        path: Path | None = None,
    ) -> AskAnswer:
        terms = {
            term.lower()
            for term in re.findall(r"[A-Za-z0-9_.-]{3,}", question)
            if term.lower() not in {"what", "which", "should", "this", "that", "with", "from", "have"}
        }
        context_parts = [question]
        limitations: list[str] = []
        if task_file is not None:
            context_parts.append(task_file.expanduser().read_text(encoding="utf-8")[:20_000])
        context = " ".join(context_parts).lower()

        scope = ImprovementScopeResolver(
            self.conn,
            cwd=path or Path.cwd(),
        ).path(path)
        observations = self.repository.list_observations(limit=200, scope=scope)
        allowed_observation_ids = {item.id for item in observations}
        candidates = [
            item
            for item in self.repository.list_candidates(limit=500)
            if item.observation_id in allowed_observation_ids
            or self._candidate_is_unscoped_guidance(item.observation_id)
            or self._candidate_is_installed_for_path(item.id, path)
        ][:200]
        if not scope.matched:
            limitations.append(
                f"No telemetry workspace matches {scope.label}; Reflect did not fall back to global findings."
            )
        ranked_candidates = sorted(
            candidates,
            key=lambda item: self._match_score(context, terms, item.title, item.hypothesis, str(item.content)),
            reverse=True,
        )
        ranked_observations = sorted(
            observations,
            key=lambda item: self._match_score(context, terms, item.title, item.summary, item.category),
            reverse=True,
        )
        selected_candidates = [
            item for item in ranked_candidates
            if item.status.value == "approved"
            and item.lifecycle.deployment.value == "active"
            and self._match_score(context, terms, item.title, item.hypothesis, str(item.content)) > 0
        ][:1]
        approved_not_deployed = [
            item
            for item in ranked_candidates
            if item.status.value == "approved"
            and item.lifecycle.deployment.value != "active"
            and self._match_score(
                context,
                terms,
                item.title,
                item.hypothesis,
                str(item.content),
            )
            > 0
        ][:1]
        selected_observations = [
            item for item in ranked_observations
            if self._match_score(context, terms, item.title, item.summary, item.category) > 0
        ][:3]

        guidance: list[str] = []
        constraints: list[str] = []
        verification: list[str] = []
        evidence: list[AskEvidence] = []
        for candidate in selected_candidates:
            guidance.extend(str(step) for step in candidate.content.get("steps", [])[:5])
            constraints.extend(str(item) for item in candidate.content.get("abstain_when", [])[:5])
            verification.extend(str(item) for item in candidate.content.get("verification", [])[:5])
            evidence.append(
                AskEvidence(
                    kind="workflow",
                    id=candidate.id,
                    summary=f"{candidate.title} ({candidate.lifecycle.display})",
                    confidence=candidate.confidence,
                )
            )
        for observation in selected_observations:
            evidence.append(
                AskEvidence(
                    kind="observation",
                    id=observation.id,
                    summary=observation.summary,
                    confidence=observation.confidence,
                )
            )
        if not guidance and selected_observations:
            for observation in selected_observations:
                candidate = self.repository.get_candidate(observation.candidate_id or "")
                if candidate:
                    guidance.extend(str(step) for step in candidate.content.get("steps", [])[:4])
            limitations.append(
                "A matching reviewed workflow is not installed; Reflect did not provide it as executable guidance."
                if approved_not_deployed
                else "Matching workflow candidates are pending review; guidance is not yet approved."
            )
        elif not selected_candidates and approved_not_deployed:
            limitations.append(
                "A matching reviewed workflow is not installed; Reflect did not provide it as executable guidance."
            )
        if not evidence:
            limitations.append("No sufficiently matching local observation or workflow was found.")
            answer = "Reflect does not yet have enough local evidence to answer this confidently."
            confidence = 0.0
        else:
            answer = (
                f"Reflect found {len(evidence)} local evidence item(s). "
                "Use the bounded workflow below and verify it against the current repository state."
            )
            confidence = min(0.95, sum(item.confidence for item in evidence) / len(evidence))
        selected_workflow = selected_candidates[0] if selected_candidates else None
        return AskAnswer(
            question=question,
            answer=answer,
            guidance=list(dict.fromkeys(guidance))[:8],
            evidence=evidence,
            confidence=confidence,
            workflow_id=selected_workflow.id if selected_workflow else None,
            freshness=selected_workflow.updated_at if selected_workflow else None,
            constraints=list(dict.fromkeys(constraints)),
            verification=list(dict.fromkeys(verification)),
            fallback=(
                "Stop and ask the operator when the workflow preconditions or repository evidence do not match."
                if selected_workflow
                else "Inspect the linked evidence and ask the operator before applying unapproved guidance."
            ),
            limitations=limitations,
        )

    def _candidate_is_unscoped_guidance(
        self,
        observation_id: str,
    ) -> bool:
        row = self.conn.execute(
            """
            SELECT o.scope_type, o.scope_id,
                   EXISTS (
                     SELECT 1 FROM observation_sessions os
                     WHERE os.observation_id = o.id
                   )
            FROM observations o
            WHERE o.id = ?
            """,
            (observation_id,),
        ).fetchone()
        if row is None:
            return False
        return str(row[0]) == "user" and str(row[1]) == "local" and not bool(row[2])

    def _candidate_is_installed_for_path(
        self,
        candidate_id: str,
        path: Path | None,
    ) -> bool:
        if path is None:
            return False
        requested = path.expanduser().resolve()
        rows = self.conn.execute(
            """
            SELECT i.target_path
            FROM interventions i
            JOIN workflow_versions wv ON wv.id = i.workflow_version_id
            WHERE wv.candidate_id = ?
              AND i.status = 'active'
            """,
            (candidate_id,),
        ).fetchall()
        for row in rows:
            target = Path(str(row[0])).expanduser().resolve()
            target_root = target if target.is_dir() else target.parent
            if requested == target_root or target_root in requested.parents or requested in target_root.parents:
                return True
        return False

    def finding_workflow_context(self, finding_id: str) -> dict:
        """Resolve a finding on the server; never accept client evidence or metrics."""
        ids = self.resolve_finding_observation_ids(finding_id)
        observation = self.repository.get_observation(finding_id)
        linked = [candidate for candidate in self.repository.iter_candidates()
                  if candidate.observation_id in ids
                  or finding_id in candidate.provenance.get("finding_observation_ids", [])]
        candidate = min(linked, key=self.workflows._representative_sort_key) if linked else None
        ledger = self.repository.finding_evidence_ledger(finding_id, [finding_id], limit=50)
        roots = sorted({item.workspace for item in ledger.provenance_sessions if item.workspace})
        if observation.repo_id:
            roots = sorted({str(row[0]) for row in self.conn.execute(
                "SELECT root_path FROM workspaces WHERE repo_id=? AND root_path IS NOT NULL",
                (observation.repo_id,),
            )})
        slug = re.sub(r"[^a-z0-9]+", "-", observation.title.lower()).strip("-")[:48] or "finding"
        return {
            "finding_id": finding_id, "observation_ids": ids,
            "candidate_id": candidate.id if candidate else None,
            "title": observation.title, "summary": observation.summary,
            "scope": f"{observation.scope_type}:{observation.scope_id}",
            "target_metric": observation.metric_name, "project_roots": roots,
            "content": {
                "slug": slug + "-" + hashlib.sha256(finding_id.encode()).hexdigest()[:8],
                "description": f"Address this finding: {observation.title}",
                "steps": ["Inspect the linked task evidence and confirm the affected project and task scope.",
                          "Choose and perform a corrective step supported by that evidence.",
                          "Run the relevant verification and record its actual result."],
                "verification": ["Report observed results separately from expected improvement."],
                "abstain_when": ["The task or project differs from the finding, or the evidence is insufficient."],
            },
        }

    def create_workflow(self, *, content: dict, finding_id: str | None = None) -> dict:
        """Stage user-authored instructions, without claiming observed support."""
        allowed = {"slug", "description", "steps", "verification", "abstain_when"}
        if set(content) - allowed:
            raise ValueError("Provide workflow instructions only; evidence is assigned by Reflect")
        for key in ("steps", "verification", "abstain_when"):
            values = content.get(key, [])
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError(f"Workflow {key} must be a list of instructions")
            if len(values) > 100 or any(len(value) > 4000 for value in values):
                raise ValueError("Workflow instructions exceed the draft size limit")
        normalized = self.workflows._validate_content(content)
        if not normalized["steps"]:
            raise ValueError("Workflow requires at least one nonempty instruction")
        description = normalized.get("description", "")
        if not isinstance(description, str) or len(description) > 2000:
            raise ValueError("Workflow description must be text up to 2000 characters")
        if finding_id is not None:
            context = self.finding_workflow_context(finding_id)
            if context["candidate_id"]:
                existing = self.workflows.show(context["candidate_id"])
                if any(existing.content.get(key, [] if key in {"steps", "verification", "abstain_when"} else "")
                       != normalized.get(key, [] if key in {"steps", "verification", "abstain_when"} else "")
                       for key in allowed):
                    raise FindingWorkflowConflict(existing.id)
                return existing.model_dump(mode="json")
            observation = self.repository.get_observation(finding_id)
            normalized["source"] = {"kind": WorkflowSourceKind.USER_AUTHORED.value,
                                    "rule_id": observation.rule_id, "observation_id": finding_id}
            normalized["evidence"] = {"status": "unverified_draft",
                                      "note": "Finding evidence supports the problem, not these proposed instructions."}
            candidate_id = self.repository.ensure_candidate(finding_id, proposal=WorkflowProposal(
                title=f"Workflow: {normalized['slug']}",
                hypothesis="A user-proposed response to the linked finding; improvement is unmeasured.",
                risk="low", content=normalized, target_metric=observation.metric_name,
            ), now=utc_now())
            candidate = self.workflows.show(candidate_id)
            provenance = {**candidate.provenance, "finding_observation_ids": context["observation_ids"],
                          "finding_project_roots": context["project_roots"], "finding_repo_id": observation.repo_id}
            if not observation.repo_id:
                workspaces = {str(row[0]) for root in context["project_roots"] for row in self.conn.execute(
                    "SELECT id FROM workspaces WHERE root_path=? AND repo_id IS NULL", (root,),
                )}
                unique_scope = len(context["project_roots"]) == 1 and len(workspaces) == 1
                provenance["finding_workspace_id"] = next(iter(workspaces)) if unique_scope else None
                provenance["finding_scope_unavailable"] = not unique_scope
            self.conn.execute("UPDATE workflow_candidates SET provenance_json=?, task_archetype_id=? WHERE id=?",
                              (_json_dumps(provenance), self.archetypes.dominant_for_observation(finding_id), candidate_id))
            self.conn.commit()
            self.skills.sync_workflow_candidates([candidate_id])
            self.conn.commit()
            return self.workflows.show(candidate_id).model_dump(mode="json")
        candidate_id = self.stage_extracted_skills([{
            "name": normalized["slug"], "description": description,
            "steps": normalized["steps"], "verification": normalized["verification"],
            "abstain_when": normalized["abstain_when"],
            "source_kind": WorkflowSourceKind.USER_AUTHORED.value,
        }], session_ids=[])[0]
        return self.workflows.show(candidate_id).model_dump(mode="json")

    def stage_extracted_skills(
        self,
        skill_defs: list[dict],
        *,
        session_ids: list[str],
        source_agent: str | None = None,
        evidence_bundle: dict | None = None,
    ) -> list[str]:
        from reflect.improvements.procedures import procedure_contract
        from reflect.improvements.workflow_identity import WorkflowIdentity
        from reflect.skill_extraction import _validate_skill_draft_evidence

        instruction_fields = (
            "schema_version", "slug", "behavior_type", "suggested_artifact", "description",
            "source_markdown", "steps", "preconditions", "checkpoints", "verification",
            "abstain_when", "workflow_contract",
        )

        def artifact_identity(title: str, content: dict) -> WorkflowIdentity:
            # Review identities describe authored instructions, independently of
            # fresh evidence counts or duplicate-suggestion metadata.
            return WorkflowIdentity.from_content(title, {
                key: content[key] for key in instruction_fields if key in content
            })

        if not skill_defs:
            return []
        # Validate the entire response before persisting any draft or rule row.
        prepared = []
        for skill in skill_defs:
            evidence, procedure = _validate_skill_draft_evidence(
                self.conn, skill, session_ids=session_ids, evidence_bundle=evidence_bundle,
            )
            behavior_type = str(skill.get("behavior_type") or "proven_pattern")
            if behavior_type not in {"loop", "recovery", "verification", "exploration", "proven_pattern"}:
                raise ValueError(f"Unsupported workflow behavior type: {behavior_type}")
            content = {
                "schema_version": 1,
                "slug": str(skill.get("name") or "").strip(),
                "behavior_type": behavior_type,
                "suggested_artifact": "skill",
                "description": str(skill.get("description") or "").strip(),
                "source_markdown": str(skill.get("content") or "").strip(),
                "evidence": evidence,
            }
            for key in ("steps", "preconditions", "checkpoints", "verification", "abstain_when"):
                values = skill.get(key) or []
                if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                    raise ValueError(f"Skill {key} must be a list of instructions")
                content[key] = list(values)
            if procedure:
                content["procedure"] = procedure
                content["workflow_contract"] = procedure_contract(procedure).model_dump(mode="json")
                # Keep the exact applicability guard in reviewed instructions.
                content["abstain_when"].append(
                    "The task differs from the observed procedure scope: "
                    + ", ".join(f"{key}={procedure.get(key)}" for key in
                                ("repo_id", "workspace_id", "task_archetype_id")) + "."
                )
            title = f"Workflow: {content['slug']}"
            identity = artifact_identity(title, content)
            matches = []
            exact = None
            for candidate in self.repository.list_candidates_by_contract(identity.contract_signature):
                same_artifact = artifact_identity(candidate.title, candidate.content).revision_hash == identity.revision_hash
                if same_artifact and (exact is None or candidate.status.value == "approved"):
                    exact = candidate
                elif not same_artifact and procedure:
                    matches.append({"id": candidate.id, "title": candidate.title,
                                    "status": candidate.status.value,
                                    "reason": "Same scoped procedure; review as a duplicate or update suggestion."})
            content["existing_workflow_matches"] = sorted(matches, key=lambda item: item["id"])
            fingerprint = f"{identity.contract_signature}:{identity.revision_hash}"
            if exact is not None:
                # Reuse pending drafts created by earlier fingerprints. Exact
                # approved/installed artifacts are returned without any writes.
                observation = self.repository.get_observation(exact.observation_id)
                fingerprint = observation.fingerprint
            prepared.append((skill, content, fingerprint, exact))
        if all(exact is not None and exact.status.value == "approved" for _, _, _, exact in prepared):
            return [exact.id for _, _, _, exact in prepared]

        rule = RuleDefinition(
            id="discovered_reusable_workflow", version=1, category="workflow",
            title="Discovered reusable workflow",
            description="Stages agent-extracted reusable behavior for explicit human review.",
            required_signals=["reviewed_session_evidence"],
        )
        now = utc_now()
        self.repository.sync_rule_definitions((rule,), now=now)
        candidate_ids: list[str] = []
        staged_ids: list[str] = []
        for skill, content, fingerprint, exact in prepared:
            if exact is not None and exact.status.value == "approved":
                candidate_ids.append(exact.id)
                continue
            evidence = content["evidence"]
            support = evidence["checked_execution_count"]
            sessions = evidence["session_ids"]
            confidence = min(0.85, 0.5 + support * 0.03) if support else 0.0
            refs = [EvidenceRef(
                entity_type="execution_unit", entity_id=example["execution_unit_id"],
                session_id=example["session_id"],
                polarity="supporting" if example.get("verification") == "passed" and not example.get("signals_truncated") else "counterexample",
                summary_redacted=f"Candidate-cited procedure example; checks {example.get('verification', 'unknown')}; outcome {example.get('outcome', 'unknown')}.",
                confidence=0.8,
            ) for example in evidence.get("examples", [])]
            if not refs:
                refs = [EvidenceRef(
                    entity_type="session", entity_id=sid,
                    session_id=sid if self.conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone() else None,
                    polarity="context",
                    summary_redacted="Cited session context for an unverified draft; no checked execution support.",
                    confidence=0.0,
                ) for sid in sessions]
            procedure = content.get("procedure") or {}
            scope_type = "repo" if procedure.get("repo_id") else "user"
            scope_id = str(procedure.get("repo_id") or "local")
            summary = (
                f"Draft cites {support} executions with observed checks among "
                f"{evidence['denominator_execution_count']} cited executions; outcomes remain separate."
                if support else "Unverified instruction draft; no checked execution support is claimed."
            )
            draft = ObservationDraft(
                rule_id=rule.id, rule_version=rule.version,
                scope_type=scope_type, scope_id=scope_id, repo_id=procedure.get("repo_id"),
                fingerprint=fingerprint, category="workflow",
                title=f"Reusable workflow candidate: {content['slug']}", summary=summary,
                metric_name="workflow_support_execution_units", metric_value=float(support),
                metric_unit="execution_units", metric_direction="higher_is_better",
                impact_score=min(75.0, support * 5.0),
                severity=Severity.MEDIUM if support >= 3 else Severity.LOW,
                confidence=confidence, occurrence_count=len(evidence["execution_ids"]),
                affected_session_count=len(sessions), evidence=refs,
            )
            observation_id = self.repository.upsert_observation(draft, now=now)
            content["source"] = {
                "rule_id": rule.id, "observation_id": observation_id,
                "kind": str(skill.get("source_kind") or WorkflowSourceKind.AGENT_AUTHORED.value),
                **({"agent": source_agent} if source_agent else {}),
                **({"workflow_id": str(skill["source_workflow_id"])} if skill.get("source_workflow_id") else {}),
                **({"loop_id": str(skill["source_loop_id"])} if skill.get("source_loop_id") else {}),
            }
            candidate_ids.append(self.repository.stage_candidate(
                observation_id, title=f"Workflow: {content['slug']}",
                hypothesis=("Review this instruction draft before use; outcome improvement remains unmeasured. " + summary),
                content=content, confidence=confidence, target_metric="workflow_adherence",
                scope=f"{scope_type}:{scope_id}", now=now,
            ))
            staged_ids.append(candidate_ids[-1])
        self.conn.commit()
        self.skills.sync_workflow_candidates(staged_ids)
        self.conn.commit()
        return candidate_ids

    @staticmethod
    def _match_score(context: str, terms: set[str], *values: str) -> int:
        haystack = " ".join(values).lower()
        score = sum(2 for term in terms if term in haystack)
        score += sum(1 for term in terms if term in context and term in haystack)
        return score
