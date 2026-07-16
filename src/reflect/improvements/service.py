from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from reflect.improvements.archetypes import TaskArchetypeService, WorkflowAdherenceService
from reflect.improvements.base import BaseImprovementRule, RuleRegistry
from reflect.improvements.loops import LoopService
from reflect.improvements.measurement import MeasurementService
from reflect.improvements.models import (
    AskAnswer,
    AskEvidence,
    EvidenceRef,
    ImprovementSummary,
    InboxFindingRecord,
    ObservationDraft,
    ObservationRecord,
    RuleDefinition,
    Severity,
    WorkflowSourceKind,
)
from reflect.improvements.repository import ImprovementRepository, utc_now
from reflect.improvements.rules import DEFAULT_RULE_REGISTRY
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
    ):
        self.conn = conn
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
        self.loops = LoopService(conn)
        self.skills = SkillRegistryService(conn)
        self.archetypes = TaskArchetypeService(conn)
        self.adherence = WorkflowAdherenceService(conn)

    def refresh(self) -> dict[str, int]:
        now = utc_now()
        detected = 0
        resolved = 0
        candidates = 0
        archetype_result = self.archetypes.refresh()
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
                    archetype_id = self.archetypes.dominant_for_observation(observation_id)
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
            "classified_sessions": archetype_result["classified"],
            "workflow_exposures": adherence_result["exposures"],
            "stale_workflows": integrity_result["stale"],
            "measurements_created": measurement_result["created"],
            "regressions": measurement_result["regressed"],
            "loops": loop_result["detected"],
            "skills": skill_result["workflow_skills"],
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

    def improve(self, observation_id: str | None = None, *, refresh: bool = True) -> ImprovementSummary | ObservationRecord:
        if refresh:
            self.refresh()
        if observation_id:
            observation = self.repository.get_observation(observation_id)
            if observation is None:
                raise KeyError(f"Observation not found: {observation_id}")
            return observation
        return self.repository.summary()

    def list_inbox_findings(
        self,
        *,
        limit: int = 100,
        status: str | None = None,
        include_resolved: bool = False,
    ) -> list[InboxFindingRecord]:
        """Group scope-specific observations into durable reviewable findings."""
        observations = self.repository.list_observations(
            limit=500,
            status=status,
            include_resolved=include_resolved,
        )
        if not observations:
            return []

        candidates = self.repository.list_candidates(limit=500)
        candidate_by_id = {candidate.id: candidate for candidate in candidates}
        workflows = self.workflows.list(limit=500)
        workflow_by_slug = {
            str(workflow.content.get("slug") or workflow.id): workflow
            for workflow in workflows
        }
        rule_by_id = {
            rule.id: rule
            for rule in self.repository.list_rule_summaries()
        }

        grouped: dict[tuple[str, ...], list[ObservationRecord]] = {}
        group_slug: dict[tuple[str, ...], str] = {}
        for observation in observations:
            candidate = candidate_by_id.get(observation.candidate_id or "")
            slug = str(candidate.content.get("slug") or "") if candidate else ""
            if slug:
                key = ("workflow", slug)
                group_slug[key] = slug
            else:
                key = ("observation", observation.rule_id, observation.title)
            grouped.setdefault(key, []).append(observation)

        findings: list[InboxFindingRecord] = []
        status_priority = {
            "regressed": 0,
            "active": 1,
            "approved": 2,
            "proposal_ready": 3,
            "acknowledged": 4,
            "new": 5,
        }
        severity_priority = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        for key, members in grouped.items():
            slug = group_slug.get(key)
            workflow = workflow_by_slug.get(slug or "")
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
                        -item.impact_score,
                        -item.confidence,
                        item.id,
                    ),
                )

            source_scopes = sorted(
                {f"{item.scope_type}:{item.scope_id}" for item in members}
            )
            rule = rule_by_id.get(representative.rule_id)
            distinct_titles = {item.title for item in members}
            linked_sessions = (
                workflow.support_count
                if workflow is not None
                else self.repository.observation_session_count(item.id for item in members)
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
            findings.append(InboxFindingRecord.model_validate(data))

        findings.sort(
            key=lambda item: (
                status_priority.get(item.status.value, 9),
                -item.impact_score,
                -item.confidence,
                item.title,
            )
        )
        return findings[: max(1, min(limit, 500))]

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
        if path is not None:
            context_parts.append(str(path.expanduser().resolve()))
        context = " ".join(context_parts).lower()

        candidates = self.repository.list_candidates(limit=200)
        observations = self.repository.list_observations(limit=200)
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
            if item.status.value in {"approved", "active"}
            and self._match_score(context, terms, item.title, item.hypothesis, str(item.content)) > 0
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
                    summary=f"{candidate.title} ({candidate.status.value})",
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
            limitations.append("Matching workflow candidates are pending review; guidance is not yet approved.")
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
                metric_name="workflow_support_sessions",
                metric_value=float(support),
                metric_unit="sessions",
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
                    support_count=support,
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
