from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from reflect.execution_units import ExecutionUnitService
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
    WorkflowSourceKind,
)
from reflect.improvements.repository import ImprovementRepository, utc_now
from reflect.improvements.rules import DEFAULT_RULE_REGISTRY
from reflect.improvements.scope import ImprovementScopeResolver
from reflect.improvements.skills import SkillRegistryService
from reflect.improvements.workflows import WorkflowService
from reflect.store.migrate import migrate


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

    def refresh(self) -> dict[str, int]:
        now = utc_now()
        detected = 0
        resolved = self.repository.retire_rule(
            "retry_loop_without_state_change",
            now=now,
        )
        candidates = 0
        workflow_evidence = self.prepare_workflow_evidence()
        self.repository.sync_rule_definitions(
            (rule.definition for rule in self.rules),
            now=now,
        )
        self._backfill_workflow_metadata()
        try:
            for rule in self.rules:
                seen_ids: set[str] = set()
                for draft in rule.evaluate(self.conn):
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
                resolved += self.repository.resolve_missing(rule.definition, seen_ids, now=now)
            integrity_result = self.workflows.refresh_integrity()
            adherence_result = self.adherence.refresh()
            measurement_result = self.measurements.measure_active()
            loop_result = self.loops.refresh(commit=False)
            skill_result = self.skills.refresh(commit=False)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        candidates = len(self.workflows.list(statuses={"pending"}, limit=500))
        return {
            "detected": detected,
            "resolved": resolved,
            "candidates": candidates,
            "execution_units": workflow_evidence["execution_units"],
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
    ) -> dict[str, int]:
        """Refresh bounded execution and archetype evidence without running detectors."""

        scoped_ids = (
            None
            if session_ids is None
            else {str(item) for item in session_ids if item}
        )
        execution_units = self.execution_units.refresh(session_ids=scoped_ids)
        archetypes = self.archetypes.refresh(session_ids=scoped_ids)
        return {
            "execution_units": execution_units["execution_units"],
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
                        workflow.title
                        if workflow is not None
                        else rule.title
                        if rule is not None and (len(members) > 1 or len(distinct_titles) > 1)
                        else representative.title
                    ),
                    "summary": (
                        str(workflow.content.get("description") or workflow.hypothesis)
                        if workflow is not None
                        else f"{rule.description} {len(members)} current evidence pattern(s) "
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

    def stage_extracted_skills(
        self,
        skill_defs: list[dict],
        *,
        session_ids: list[str],
        source_agent: str | None = None,
    ) -> list[str]:
        rule = RuleDefinition(
            id="discovered_reusable_workflow",
            version=1,
            category="workflow",
            title="Discovered reusable workflow",
            description="Stages agent-extracted reusable behavior for explicit human review.",
            required_signals=["reviewed_session_evidence"],
        )
        now = utc_now()
        self.repository.sync_rule_definitions((rule,), now=now)
        candidate_ids: list[str] = []
        support = max(1, len(session_ids))
        confidence = min(0.85, 0.5 + support * 0.03)
        valid_session_ids = [
            session_id
            for session_id in session_ids[:20]
            if self.conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
        ]
        for skill in skill_defs:
            slug = str(skill.get("name") or "").strip()
            description = str(skill.get("description") or "").strip()
            source_markdown = str(skill.get("content") or "").strip()
            behavior_type = str(skill.get("behavior_type") or "proven_pattern")
            if behavior_type not in {"loop", "recovery", "verification", "exploration", "proven_pattern"}:
                raise ValueError(f"Unsupported workflow behavior type: {behavior_type}")
            source_kind = str(
                skill.get("source_kind") or WorkflowSourceKind.AGENT_AUTHORED.value
            )
            source_workflow_id = str(skill.get("source_workflow_id") or "").strip()
            source_loop_id = str(skill.get("source_loop_id") or "").strip()
            draft = ObservationDraft(
                rule_id=rule.id,
                rule_version=rule.version,
                scope_type="user",
                scope_id="local",
                fingerprint=(
                    f"{slug}:{hashlib.sha256(source_markdown.encode('utf-8')).hexdigest()[:16]}"
                ),
                category="workflow",
                title=f"Reusable workflow candidate: {slug}",
                summary=f"Session evidence produced a reusable {slug} workflow for operator review.",
                metric_name="workflow_support_execution_units",
                metric_value=float(support),
                metric_unit="execution_units",
                metric_direction="higher_is_better",
                impact_score=min(75.0, 25.0 + support * 5.0),
                severity=Severity.MEDIUM if support >= 3 else Severity.LOW,
                confidence=confidence,
                occurrence_count=support,
                affected_session_count=support,
                evidence=[
                    EvidenceRef(
                        entity_type="session",
                        entity_id=session_id,
                        session_id=session_id,
                        summary_redacted="Session included in the bounded workflow extraction evidence packet",
                        confidence=0.7,
                    )
                    for session_id in valid_session_ids
                ],
            )
            observation_id = self.repository.upsert_observation(draft, now=now)
            content = {
                "schema_version": 1,
                "slug": slug,
                "behavior_type": behavior_type,
                "suggested_artifact": "skill",
                "description": description,
                "steps": [],
                "source_markdown": source_markdown,
                "source": {
                    "rule_id": rule.id,
                    "observation_id": observation_id,
                    "kind": source_kind,
                    **({"agent": source_agent} if source_agent else {}),
                    **({"workflow_id": source_workflow_id} if source_workflow_id else {}),
                    **({"loop_id": source_loop_id} if source_loop_id else {}),
                },
            }
            candidate_ids.append(
                self.repository.stage_candidate(
                    observation_id,
                    title=f"Workflow: {slug}",
                    hypothesis=f"Reviewing and applying {slug} will make the observed behavior reusable.",
                    content=content,
                    confidence=confidence,
                    target_metric="workflow_adherence",
                    now=now,
                )
            )
        self.conn.commit()
        self.skills.sync_workflow_candidates(candidate_ids)
        self.conn.commit()
        return candidate_ids

    @staticmethod
    def _match_score(context: str, terms: set[str], *values: str) -> int:
        haystack = " ".join(values).lower()
        score = sum(2 for term in terms if term in haystack)
        score += sum(1 for term in terms if term in context and term in haystack)
        return score
