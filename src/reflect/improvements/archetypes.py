from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from typing import Any

from reflect.improvements.contracts import (
    ContractEvaluationService,
    WorkflowContract,
)
from reflect.improvements.repository import utc_now

_ARCHETYPES: dict[str, dict[str, Any]] = {
    "debugging": {
        "description": "Diagnose and repair a failure or unexpected behavior.",
        "terms": ("fail", "error", "debug", "fix", "broken", "exception", "traceback"),
    },
    "testing": {
        "description": "Add, run, or repair tests and validation.",
        "terms": ("test", "pytest", "ruff", "lint", "coverage", "validate"),
    },
    "release": {
        "description": "Prepare, validate, publish, or recover a release.",
        "terms": ("release", "changelog", "version", "publish", "deploy", "tag"),
    },
    "review": {
        "description": "Review a change, pull request, merge request, or design.",
        "terms": ("review", "pull request", "merge request", " pr ", " mr ", "comment"),
    },
    "documentation": {
        "description": "Create or update documentation and guidance.",
        "terms": ("readme", "documentation", " docs ", "guide", "spec"),
    },
    "research": {
        "description": "Explore a codebase or gather evidence before deciding.",
        "terms": ("research", "investigate", "explore", "find", "search", "understand"),
    },
    "ticket_creation": {
        "description": "Create or refine a tracked ticket, issue, or work item.",
        "terms": ("ticket", "jira", "issue", "story", "acceptance criteria", "work item"),
    },
    "implementation": {
        "description": "Build or modify product behavior.",
        "terms": ("implement", "build", "add", "create", "change", "refactor", "feature"),
    },
}


@dataclass(frozen=True, slots=True)
class _ArchetypeMatch:
    archetype_id: str
    confidence: float
    matched_terms: list[str]
    runner_up_score: int
    mixed: bool


def _classify_text(text: str) -> _ArchetypeMatch:
    normalized = text.lower()
    scores = Counter(
        {
            archetype_id: sum(normalized.count(term) for term in definition["terms"])
            for archetype_id, definition in _ARCHETYPES.items()
        }
    )
    ranked = scores.most_common(2)
    archetype_id, score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0
    mixed = bool(score > 0 and runner_up_score >= max(2, score * 0.75))
    if score <= 0:
        return _ArchetypeMatch("implementation", 0.35, [], runner_up_score, False)
    matched_terms = [
        term.strip()
        for term in _ARCHETYPES[archetype_id]["terms"]
        if term in normalized
    ]
    return _ArchetypeMatch(
        archetype_id,
        min(0.95, 0.5 + score * 0.08),
        matched_terms,
        runner_up_score,
        mixed,
    )


class TaskArchetypeService:
    """Classify execution units into comparable work archetypes."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def refresh(self, *, session_ids: set[str] | None = None) -> dict[str, int]:
        scoped_ids = (
            None
            if session_ids is None
            else sorted({str(item) for item in session_ids if item})
        )
        if scoped_ids == []:
            return {
                "classified_execution_units": 0,
                "excluded_execution_units": 0,
                "archetypes": len(_ARCHETYPES),
            }
        now = utc_now()
        for archetype_id, definition in _ARCHETYPES.items():
            self.conn.execute(
                """
                INSERT INTO task_archetypes(
                  id, name, description, matching_features_json,
                  lifecycle_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name = excluded.name,
                  description = excluded.description,
                  matching_features_json = excluded.matching_features_json,
                  updated_at = excluded.updated_at
                """,
                (
                    archetype_id,
                    archetype_id,
                    definition["description"],
                    json.dumps({"terms": definition["terms"]}, sort_keys=True),
                    now,
                    now,
                ),
            )
        self._prepare_scope(scoped_ids)
        execution_result = self._refresh_execution_units(
            now=now,
            scoped=scoped_ids is not None,
        )
        self._drop_scope(scoped_ids)
        return {
            "classified_execution_units": execution_result["classified"],
            "excluded_execution_units": execution_result["excluded"],
            "archetypes": len(_ARCHETYPES),
        }

    def _prepare_scope(self, scoped_ids: list[str] | None) -> None:
        if scoped_ids is None:
            return
        self.conn.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS
              reflect_archetype_session_scope(session_id TEXT PRIMARY KEY)
            """
        )
        self.conn.execute("DELETE FROM reflect_archetype_session_scope")
        self.conn.executemany(
            "INSERT INTO reflect_archetype_session_scope(session_id) VALUES (?)",
            ((session_id,) for session_id in scoped_ids),
        )

    def _drop_scope(self, scoped_ids: list[str] | None) -> None:
        if scoped_ids is not None:
            self.conn.execute("DROP TABLE IF EXISTS reflect_archetype_session_scope")

    def _refresh_execution_units(self, *, now: str, scoped: bool) -> dict[str, int]:
        scope_join = (
            "JOIN reflect_archetype_session_scope scope ON scope.session_id = tu.session_id"
            if scoped
            else ""
        )
        freshness_predicate = (
            "1 = 1"
            if scoped
            else "tua.execution_unit_id IS NULL OR tua.updated_at < tu.updated_at"
        )
        rows = self.conn.execute(
            f"""
            SELECT tu.id,
                   lower(
                     COALESCE(
                       (
                         SELECT GROUP_CONCAT(step_excerpt, ' ')
                         FROM (
                           SELECT substr(COALESCE(st.summary, ''), 1, 1000) AS step_excerpt
                           FROM execution_unit_steps tus
                           JOIN steps st ON st.id = tus.step_id
                           WHERE tus.execution_unit_id = tu.id
                           ORDER BY st.seq
                           LIMIT 24
                         )
                       ),
                       ''
                     ) || ' ' ||
                     COALESCE(
                       (
                         SELECT GROUP_CONCAT(tool_name, ' ')
                         FROM (
                           SELECT tc.tool_name
                           FROM execution_unit_steps tus
                           JOIN tool_calls tc ON tc.step_id = tus.step_id
                           JOIN steps st ON st.id = tus.step_id
                           WHERE tus.execution_unit_id = tu.id
                           ORDER BY st.seq
                           LIMIT 24
                         )
                       ),
                       ''
                     )
                   ) AS execution_text,
                   tu.source_confidence
            FROM execution_units tu
            {scope_join}
            LEFT JOIN execution_unit_archetypes tua ON tua.execution_unit_id = tu.id
            WHERE ({freshness_predicate})
              AND EXISTS(
                SELECT 1 FROM execution_unit_steps linked
                WHERE linked.execution_unit_id = tu.id
              )
            """
        ).fetchall()
        classified = 0
        excluded = 0
        for execution_unit_id, text, source_confidence in rows:
            match = _classify_text(str(text))
            confidence = min(float(source_confidence or 0), match.confidence)
            eligible = confidence >= 0.65 and not match.mixed
            self.conn.execute(
                """
                INSERT INTO execution_unit_archetypes(
                  execution_unit_id, task_archetype_id, confidence, mixed,
                  features_json, classified_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_unit_id) DO UPDATE SET
                  task_archetype_id = excluded.task_archetype_id,
                  confidence = excluded.confidence,
                  mixed = excluded.mixed,
                  features_json = excluded.features_json,
                  classified_at = excluded.classified_at,
                  updated_at = excluded.updated_at
                """,
                (
                    execution_unit_id,
                    match.archetype_id,
                    confidence,
                    int(match.mixed),
                    json.dumps(
                        {
                            "matched_terms": sorted(set(match.matched_terms)),
                            "runner_up_score": match.runner_up_score,
                            "source_confidence": source_confidence,
                        },
                        sort_keys=True,
                    ),
                    now,
                    now,
                ),
            )
            self.conn.execute(
                "UPDATE execution_units SET eligible = ?, updated_at = ? WHERE id = ?",
                (int(eligible), now, execution_unit_id),
            )
            classified += 1
            excluded += int(not eligible)
        self.conn.commit()
        return {"classified": classified, "excluded": excluded}

    def dominant_for_observation(self, observation_id: str) -> str | None:
        row = self.conn.execute(
            """
            WITH evidence_units AS (
              SELECT explicit_unit.id AS execution_unit_id
              FROM observation_evidence oe
              JOIN execution_units explicit_unit
                ON oe.entity_type = 'execution_unit'
               AND explicit_unit.id = oe.entity_id
              WHERE oe.observation_id = ?
              UNION
              SELECT eus.execution_unit_id
              FROM observation_evidence oe
              JOIN execution_unit_steps eus ON eus.step_id = oe.step_id
              WHERE oe.observation_id = ? AND oe.entity_type <> 'execution_unit'
              UNION
              SELECT fallback.id
              FROM observation_evidence oe
              JOIN execution_units fallback
                ON fallback.session_id = oe.session_id
               AND fallback.source = 'session_fallback'
              WHERE oe.observation_id = ?
                AND oe.entity_type <> 'execution_unit'
                AND oe.step_id IS NULL
            )
            SELECT eua.task_archetype_id, COUNT(*) AS support
            FROM evidence_units evidence
            JOIN execution_unit_archetypes eua
              ON eua.execution_unit_id = evidence.execution_unit_id
            GROUP BY eua.task_archetype_id
            ORDER BY support DESC, eua.task_archetype_id
            LIMIT 1
            """,
            (observation_id, observation_id, observation_id),
        ).fetchone()
        return str(row[0]) if row else None


class WorkflowAdherenceService:
    """Evaluate installed workflow contracts against bounded execution evidence."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.contracts = ContractEvaluationService(conn)

    def refresh(self) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT i.id, i.exposure_started_at, wc.id, wc.content_json,
                   o.repo_id, wc.task_archetype_id, wc.measurement_window
            FROM interventions i
            JOIN workflow_versions wv ON wv.id = i.workflow_version_id
            JOIN workflow_candidates wc ON wc.id = wv.candidate_id
            JOIN observations o ON o.id = wc.observation_id
            WHERE i.status = 'active'
            """
        ).fetchall()
        upserted = 0
        now = utc_now()
        for (
            intervention_id,
            exposed_at,
            candidate_id,
            content_json,
            repo_id,
            archetype_id,
            measurement_window,
        ) in rows:
            content = json.loads(content_json)
            slug = str(content.get("slug") or "")
            contract = WorkflowContract.from_raw(content.get("workflow_contract"))
            limit = max(1, min(int(measurement_window), 100)) if contract else 100
            execution_units = self.conn.execute(
                """
                SELECT eu.id, eu.session_id, eu.mcp_task_run_id,
                       EXISTS(
                         SELECT 1
                         FROM execution_unit_steps eus
                         JOIN steps st ON st.id = eus.step_id
                         WHERE eus.execution_unit_id = eu.id
                           AND lower(st.raw_attrs_json) LIKE ?
                       ) AS invoked,
                       EXISTS(
                         SELECT 1
                         FROM execution_unit_steps eus
                         JOIN tool_calls tc ON tc.step_id = eus.step_id
                         WHERE eus.execution_unit_id = eu.id AND (
                           lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*pytest*'
                           OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*ruff*'
                           OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '* test*'
                           OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*build*'
                         )
                       ) AS verified,
                       eu.status, eu.outcome, eu.verification_passed
                FROM execution_units eu
                JOIN execution_unit_archetypes eua ON eua.execution_unit_id = eu.id
                WHERE eu.started_at >= ?
                  AND COALESCE(eu.repo_id, '') = COALESCE(?, '')
                  AND (? IS NULL OR eua.task_archetype_id = ?)
                  AND (? = 0 OR (
                    eu.eligible = 1 AND eua.mixed = 0
                    AND lower(COALESCE(eu.status, '')) IN ('completed', 'ok', 'success')
                  ))
                ORDER BY eu.started_at, eu.id
                LIMIT ?
                """,
                (
                    f"%{slug.lower()}%",
                    exposed_at,
                    repo_id,
                    archetype_id,
                    archetype_id,
                    int(contract is not None),
                    limit,
                ),
            ).fetchall()
            task_run_ids = [str(item[2]) for item in execution_units if item[2]]
            reported_by_run, milestone_evidence_by_run = self._reported_milestones(
                task_run_ids,
                signature_hash=contract.signature_hash if contract else "",
            )
            evaluated = (
                self.contracts.evaluate_units(
                    contract,
                    [
                        {
                            "execution_unit_id": item[0],
                            "reported_milestones": reported_by_run.get(
                                str(item[2]) if item[2] else "", {}
                            ),
                            "status": item[5],
                            "outcome": item[6],
                            "verification_passed": (
                                None if item[7] is None else bool(item[7])
                            ),
                            "workflow_slug_observed": bool(item[3]),
                        }
                        for item in execution_units
                    ],
                )
                if contract is not None
                else {}
            )
            for execution_unit in execution_units:
                execution_unit_id = str(execution_unit[0])
                session_id = str(execution_unit[1])
                task_run_id = str(execution_unit[2]) if execution_unit[2] else None
                slug_invoked = bool(execution_unit[3])
                legacy_verified = bool(execution_unit[4])
                reported = reported_by_run.get(task_run_id or "", {})
                structured_evidence = milestone_evidence_by_run.get(task_run_id or "", {})
                if contract is not None:
                    contract_evidence = evaluated[execution_unit_id]
                    evaluation = contract_evidence.evaluation
                    observed_roles = contract_evidence.observed_roles
                    signal_fingerprints = contract_evidence.signal_fingerprints
                    invoked = evaluation.invoked
                    followed = evaluation.followed
                    corroborated = evaluation.corroborated
                else:
                    observed_roles = []
                    signal_fingerprints = []
                    invoked = bool(slug_invoked)
                    followed = invoked and bool(legacy_verified)
                    corroborated = bool(legacy_verified)
                    evaluation = None
                state = "followed" if followed else "invoked" if invoked else "ignored"
                exposure_id = f"exposure_{intervention_id}_{execution_unit_id}"
                self.conn.execute(
                    """
                    INSERT INTO workflow_exposures(
                      id, intervention_id, session_id, execution_unit_id,
                      state, evidence_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(intervention_id, execution_unit_id) DO UPDATE SET
                      state = excluded.state,
                      evidence_json = excluded.evidence_json
                    """,
                    (
                        exposure_id,
                        intervention_id,
                        session_id,
                        execution_unit_id,
                        state,
                        json.dumps(
                            {
                                "candidate_id": candidate_id,
                                "execution_unit_id": execution_unit_id,
                                "slug_observed": bool(slug_invoked),
                                "reported_milestones": reported,
                                "milestone_evidence": structured_evidence,
                                "observed_roles": observed_roles,
                                "observed_signal_fingerprints": signal_fingerprints,
                                "required_milestones": (
                                    [item.id for item in contract.required_milestones]
                                    if contract
                                    else []
                                ),
                                "corroborated": corroborated,
                                "contract_evaluation": (
                                    evaluation.model_dump(mode="json") if evaluation else None
                                ),
                            },
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                upserted += 1
        self.conn.commit()
        return {"exposures": upserted}

    def _reported_milestones(
        self,
        task_run_ids: list[str],
        *,
        signature_hash: str,
    ) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, Any]]]:
        if not task_run_ids:
            return {}, {}
        placeholders = ", ".join("?" for _ in task_run_ids)
        rows = self.conn.execute(
            f"""
            SELECT ie.entity_id, ie.details_json
            FROM improvement_events ie
            JOIN skill_versions sv
              ON sv.id = json_extract(ie.details_json, '$.skill_version_id')
            WHERE ie.entity_type = 'mcp_task_run'
              AND ie.entity_id IN ({placeholders})
              AND ie.event_type = 'workflow_milestone'
              AND (? = '' OR json_extract(
                    sv.workflow_json, '$.workflow_contract.signature_hash'
                  ) = ?)
            ORDER BY ie.created_at, ie.id
            """,
            [*task_run_ids, signature_hash, signature_hash],
        ).fetchall()
        states: dict[str, dict[str, str]] = {}
        evidence: dict[str, dict[str, Any]] = {}
        for task_run_id, details_json in rows:
            details = json.loads(str(details_json))
            milestone_id = str(details.get("milestone_id") or "")
            if not milestone_id:
                continue
            run_id = str(task_run_id)
            states.setdefault(run_id, {})[milestone_id] = str(details.get("state") or "")
            evidence.setdefault(run_id, {})[milestone_id] = details.get("evidence") or {}
        return states, evidence
