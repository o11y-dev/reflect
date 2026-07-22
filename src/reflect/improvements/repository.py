from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from reflect.improvements.models import (
    EvidenceRef,
    ImprovementSummary,
    ObservationDraft,
    ObservationRecord,
    ObservationStatus,
    RuleDefinition,
    RuleSummary,
    WorkflowCandidateRecord,
    WorkflowProposal,
    WorkflowSessionLedger,
    WorkflowSessionRecord,
    WorkflowStatus,
)

_ACTIVE_OBSERVATION_STATUSES = {
    ObservationStatus.NEW.value,
    ObservationStatus.ACKNOWLEDGED.value,
    ObservationStatus.PROPOSAL_READY.value,
    ObservationStatus.APPROVED.value,
    ObservationStatus.ACTIVE.value,
    ObservationStatus.REGRESSED.value,
}

_CANDIDATE_SELECT_SQL = """
    SELECT wc.id, wc.observation_id, wc.action_type, wc.title, wc.hypothesis,
           wc.scope, wc.risk, wc.content_json, wc.support_count, wc.confidence,
           wc.target_metric, wc.target_value, wc.measurement_window, wc.status,
           wc.checks_json, wc.provenance_json, wc.created_at, wc.updated_at,
           wc.task_archetype_id,
           (SELECT i.id FROM interventions i
            JOIN workflow_versions wv ON wv.id = i.workflow_version_id
            WHERE wv.candidate_id = wc.id AND i.status = 'active'
            ORDER BY i.created_at DESC LIMIT 1)
    FROM workflow_candidates wc
"""

_CANDIDATE_ORDER_SQL = """
    ORDER BY CASE wc.status
      WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 WHEN 'active' THEN 2 ELSE 3 END,
      wc.confidence DESC, wc.updated_at DESC, wc.id ASC
"""


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _observation_id(draft: ObservationDraft) -> str:
    identity = ":".join(
        (
            draft.rule_id,
            str(draft.rule_version),
            draft.scope_type,
            draft.scope_id,
            draft.fingerprint,
        )
    )
    return f"obs_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"


class ImprovementRepository:
    """SQLite persistence for the versioned improvement ledger."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def sync_rule_definitions(self, definitions: Iterable[RuleDefinition], *, now: str) -> None:
        for definition in definitions:
            self.conn.execute(
                """
                INSERT INTO rule_definitions(
                  id, version, category, title, description, detector_config_json,
                  required_signals_json, lifecycle_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id, version) DO UPDATE SET
                  category = excluded.category,
                  title = excluded.title,
                  description = excluded.description,
                  detector_config_json = excluded.detector_config_json,
                  required_signals_json = excluded.required_signals_json,
                  lifecycle_state = excluded.lifecycle_state,
                  updated_at = excluded.updated_at
                """,
                (
                    definition.id,
                    definition.version,
                    definition.category,
                    definition.title,
                    definition.description,
                    _json(definition.detector_config),
                    _json(definition.required_signals),
                    definition.lifecycle_state,
                    now,
                    now,
                ),
            )

    def list_rule_summaries(self) -> list[RuleSummary]:
        rows = self.conn.execute(
            """
            SELECT rd.id, rd.version, rd.category, rd.title, rd.description,
                   rd.detector_config_json, rd.required_signals_json, rd.lifecycle_state,
                   COUNT(DISTINCT o.id) AS observation_count,
                   COUNT(DISTINCT CASE
                     WHEN o.status IN ('new', 'acknowledged', 'proposal_ready', 'approved', 'active', 'regressed')
                     THEN o.id END) AS open_observation_count,
                   COUNT(DISTINCT json_extract(wc.content_json, '$.slug')) AS candidate_count,
                   MAX(o.last_evaluated_at) AS last_evaluated_at
            FROM rule_definitions rd
            LEFT JOIN observations o ON o.rule_id = rd.id AND o.rule_version = rd.version
            LEFT JOIN workflow_candidates wc ON wc.observation_id = o.id
            GROUP BY rd.id, rd.version
            ORDER BY rd.category, rd.title, rd.version DESC
            """
        ).fetchall()
        return [
            RuleSummary(
                id=row[0],
                version=int(row[1]),
                category=row[2],
                title=row[3],
                description=row[4],
                detector_config=_loads(row[5], {}),
                required_signals=_loads(row[6], []),
                lifecycle_state=row[7],
                observation_count=int(row[8]),
                open_observation_count=int(row[9]),
                candidate_count=int(row[10]),
                last_evaluated_at=row[11],
            )
            for row in rows
        ]

    def upsert_observation(self, draft: ObservationDraft, *, now: str) -> str:
        observation_id = _observation_id(draft)
        self.conn.execute(
            """
            INSERT INTO observations(
              id, rule_id, rule_version, scope_type, scope_id, repo_id, category,
              title, summary, metric_name, metric_value, metric_unit, metric_direction,
              baseline_value, baseline_query_json, impact_score, severity, confidence,
              first_seen_at, last_seen_at, last_evaluated_at, occurrence_count,
              affected_session_count, status, actionability, fingerprint, created_at, updated_at
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              'new', ?, ?, ?, ?
            )
            ON CONFLICT(id) DO UPDATE SET
              repo_id = excluded.repo_id,
              category = excluded.category,
              title = excluded.title,
              summary = excluded.summary,
              metric_name = excluded.metric_name,
              metric_value = excluded.metric_value,
              metric_unit = excluded.metric_unit,
              metric_direction = excluded.metric_direction,
              baseline_value = excluded.baseline_value,
              baseline_query_json = excluded.baseline_query_json,
              impact_score = excluded.impact_score,
              severity = excluded.severity,
              confidence = excluded.confidence,
              last_seen_at = excluded.last_seen_at,
              last_evaluated_at = excluded.last_evaluated_at,
              occurrence_count = excluded.occurrence_count,
              affected_session_count = excluded.affected_session_count,
              status = CASE WHEN observations.status = 'resolved' THEN 'new' ELSE observations.status END,
              actionability = excluded.actionability,
              updated_at = excluded.updated_at
            """,
            (
                observation_id,
                draft.rule_id,
                draft.rule_version,
                draft.scope_type,
                draft.scope_id,
                draft.repo_id,
                draft.category,
                draft.title,
                draft.summary,
                draft.metric_name,
                draft.metric_value,
                draft.metric_unit,
                draft.metric_direction,
                draft.baseline_value,
                _json(draft.baseline_query),
                draft.impact_score,
                draft.severity.value,
                draft.confidence,
                now,
                now,
                now,
                draft.occurrence_count,
                draft.affected_session_count,
                draft.actionability,
                draft.fingerprint,
                now,
                now,
            ),
        )
        self._replace_evidence(observation_id, draft.evidence, now=now)
        return observation_id

    def resolve_missing(
        self,
        definition: RuleDefinition,
        seen_ids: set[str],
        *,
        now: str,
    ) -> int:
        placeholders = ",".join("?" for _ in seen_ids)
        query = """
            UPDATE observations
            SET status = 'resolved', last_evaluated_at = ?, updated_at = ?
            WHERE rule_id = ? AND rule_version = ?
              AND status IN ({statuses})
        """.format(statuses=",".join("?" for _ in _ACTIVE_OBSERVATION_STATUSES))
        params: list[Any] = [
            now,
            now,
            definition.id,
            definition.version,
            *_ACTIVE_OBSERVATION_STATUSES,
        ]
        if seen_ids:
            query += f" AND id NOT IN ({placeholders})"
            params.extend(sorted(seen_ids))
        resolved = self.conn.execute(query, params).rowcount
        self.conn.execute(
            """
            UPDATE workflow_candidates
            SET status = 'stale',
                checks_json = json_set(
                  COALESCE(NULLIF(checks_json, ''), '{}'),
                  '$.stale_reason',
                  'source_observation_resolved'
                ),
                updated_at = ?
            WHERE status = 'pending'
              AND observation_id IN (
                SELECT id FROM observations
                WHERE rule_id = ? AND rule_version = ? AND status = 'resolved'
              )
            """,
            (now, definition.id, definition.version),
        )
        return resolved

    def ensure_candidate(
        self,
        observation_id: str,
        *,
        proposal: WorkflowProposal,
        now: str,
    ) -> str:
        row = self.conn.execute(
            """
            SELECT rule_id, occurrence_count, affected_session_count, confidence,
                   scope_type, scope_id
            FROM observations WHERE id = ?
            """,
            (observation_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"Observation not found: {observation_id}")
        rule_id, occurrence_count, affected_session_count, confidence, scope_type, scope_id = row
        existing = self.conn.execute(
            """
            SELECT id, status, checks_json
            FROM workflow_candidates
            WHERE observation_id = ? AND action_type = ?
            """,
            (observation_id, proposal.action_type),
        ).fetchone()
        source_kind = str((proposal.content.get("source") or {}).get("kind") or "rule_blueprint")
        provenance = {
            "observation_id": observation_id,
            "rule_id": rule_id,
            "source": source_kind,
        }
        if existing:
            checks = _loads(existing[2], {})
            if existing[1] == WorkflowStatus.STALE.value and checks.get("stale_reason") == "source_observation_resolved":
                checks.pop("stale_reason", None)
                self.conn.execute(
                    """
                    UPDATE workflow_candidates
                    SET status = 'pending', checks_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (_json(checks), now, existing[0]),
                )
                self.conn.execute(
                    """
                    UPDATE observations
                    SET status = 'proposal_ready', updated_at = ?
                    WHERE id = ? AND status = 'new'
                    """,
                    (now, observation_id),
                )
            if existing[1] == WorkflowStatus.PENDING.value:
                self.conn.execute(
                    """
                    UPDATE workflow_candidates
                    SET title = ?, hypothesis = ?, risk = ?, content_json = ?,
                        support_count = ?, confidence = ?, target_metric = ?,
                        target_value = ?, measurement_window = ?, provenance_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        proposal.title,
                        proposal.hypothesis,
                        proposal.risk,
                        _json(proposal.content),
                        int(affected_session_count or occurrence_count or 0),
                        float(confidence or 0),
                        proposal.target_metric,
                        proposal.target_value,
                        proposal.measurement_window,
                        _json(provenance),
                        now,
                        existing[0],
                    ),
                )
            return str(existing[0])
        candidate_id = f"wf_{hashlib.sha256((observation_id + ':workflow').encode('utf-8')).hexdigest()[:24]}"
        self.conn.execute(
            """
            INSERT INTO workflow_candidates(
              id, observation_id, action_type, title, hypothesis, scope, risk,
              content_json, support_count, confidence, target_metric, target_value,
              measurement_window, status, checks_json, provenance_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                candidate_id,
                observation_id,
                proposal.action_type,
                proposal.title,
                proposal.hypothesis,
                f"{scope_type}:{scope_id}",
                proposal.risk,
                _json(proposal.content),
                int(affected_session_count or occurrence_count or 0),
                float(confidence or 0),
                proposal.target_metric,
                proposal.target_value,
                proposal.measurement_window,
                _json({"schema": "valid", "review_required": True, "applied": False}),
                _json(provenance),
                now,
                now,
            ),
        )
        self.conn.execute(
            """
            UPDATE observations
            SET status = CASE WHEN status = 'new' THEN 'proposal_ready' ELSE status END,
                updated_at = ?
            WHERE id = ?
            """,
            (now, observation_id),
        )
        self.record_event(
            entity_type="workflow_candidate",
            entity_id=candidate_id,
            event_type="created_pending",
            details={"observation_id": observation_id},
            now=now,
        )
        return candidate_id

    def stage_candidate(
        self,
        observation_id: str,
        *,
        title: str,
        hypothesis: str,
        content: dict[str, Any],
        support_count: int,
        confidence: float,
        target_metric: str,
        scope: str = "user:local",
        now: str,
    ) -> str:
        candidate_id = f"wf_{hashlib.sha256((observation_id + ':workflow').encode('utf-8')).hexdigest()[:24]}"
        self.conn.execute(
            """
            INSERT INTO workflow_candidates(
              id, observation_id, action_type, title, hypothesis, scope, risk,
              content_json, support_count, confidence, target_metric,
              measurement_window, status, checks_json, provenance_json, created_at, updated_at
            ) VALUES (?, ?, 'workflow', ?, ?, ?, 'low', ?, ?, ?, ?, 10, 'pending', ?, ?, ?, ?)
            ON CONFLICT(observation_id, action_type) DO UPDATE SET
              title = CASE WHEN workflow_candidates.status = 'pending' THEN excluded.title ELSE workflow_candidates.title END,
              hypothesis = CASE WHEN workflow_candidates.status = 'pending' THEN excluded.hypothesis ELSE workflow_candidates.hypothesis END,
              content_json = CASE WHEN workflow_candidates.status = 'pending' THEN excluded.content_json ELSE workflow_candidates.content_json END,
              support_count = MAX(workflow_candidates.support_count, excluded.support_count),
              confidence = MAX(workflow_candidates.confidence, excluded.confidence),
              updated_at = excluded.updated_at
            """,
            (
                candidate_id,
                observation_id,
                title,
                hypothesis,
                scope,
                _json(content),
                support_count,
                confidence,
                target_metric,
                _json({"schema": "valid", "review_required": True, "applied": False}),
                _json(
                    {
                        "observation_id": observation_id,
                        "source": (content.get("source") or {}).get("kind", "skill_extraction"),
                    }
                ),
                now,
                now,
            ),
        )
        self.conn.execute(
            """
            UPDATE observations
            SET status = CASE WHEN status = 'new' THEN 'proposal_ready' ELSE status END,
                updated_at = ?
            WHERE id = ?
            """,
            (now, observation_id),
        )
        return candidate_id

    def list_observations(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
        include_resolved: bool = False,
    ) -> list[ObservationRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("o.status = ?")
            params.append(status)
        elif not include_resolved:
            clauses.append("o.status NOT IN ('resolved', 'dismissed', 'rejected', 'rolled_back')")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT o.id, o.rule_id, o.rule_version, o.scope_type, o.scope_id, o.repo_id,
                   o.fingerprint, o.category, o.title, o.summary, o.metric_name,
                   o.metric_value, o.metric_unit, o.metric_direction, o.baseline_value,
                   o.baseline_query_json, o.impact_score, o.severity, o.confidence,
                   o.occurrence_count, o.affected_session_count, o.actionability,
                   o.status, o.first_seen_at, o.last_seen_at, o.last_evaluated_at,
                   o.suppression_reason, o.suppressed_until, wc.id, wc.status
            FROM observations o
            LEFT JOIN workflow_candidates wc ON wc.observation_id = o.id
            {where}
            ORDER BY o.impact_score DESC, o.last_seen_at DESC
            LIMIT ?
            """,
            (*params, max(1, min(limit, 500))),
        ).fetchall()
        return [self._observation_from_row(row) for row in rows]

    def get_observation(self, observation_id: str) -> ObservationRecord | None:
        rows = self.conn.execute(
            """
            SELECT o.id, o.rule_id, o.rule_version, o.scope_type, o.scope_id, o.repo_id,
                   o.fingerprint, o.category, o.title, o.summary, o.metric_name,
                   o.metric_value, o.metric_unit, o.metric_direction, o.baseline_value,
                   o.baseline_query_json, o.impact_score, o.severity, o.confidence,
                   o.occurrence_count, o.affected_session_count, o.actionability,
                   o.status, o.first_seen_at, o.last_seen_at, o.last_evaluated_at,
                   o.suppression_reason, o.suppressed_until, wc.id, wc.status
            FROM observations o
            LEFT JOIN workflow_candidates wc ON wc.observation_id = o.id
            WHERE o.id = ?
            """,
            (observation_id,),
        ).fetchall()
        return self._observation_from_row(rows[0]) if rows else None

    def observation_session_count(self, observation_ids: Iterable[str]) -> int:
        ids = sorted({str(item) for item in observation_ids if str(item)})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        return int(
            self.conn.execute(
                f"SELECT COUNT(DISTINCT session_id) FROM observation_evidence WHERE observation_id IN ({placeholders})",
                ids,
            ).fetchone()[0]
        )

    def list_candidates(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[WorkflowCandidateRecord]:
        rows = self.conn.execute(
            f"{_CANDIDATE_SELECT_SQL} {_CANDIDATE_ORDER_SQL} LIMIT ? OFFSET ?",
            (max(1, min(limit, 500)), max(0, offset)),
        ).fetchall()
        return [self._candidate_from_row(row) for row in rows]

    def iter_candidates(self, *, page_size: int = 500) -> Iterator[WorkflowCandidateRecord]:
        """Iterate through every candidate without an implicit ledger-size cutoff."""
        bounded_page_size = max(1, min(page_size, 500))
        offset = 0
        while True:
            page = self.list_candidates(limit=bounded_page_size, offset=offset)
            yield from page
            if len(page) < bounded_page_size:
                return
            offset += len(page)

    def list_candidates_by_slug(self, slug: str) -> list[WorkflowCandidateRecord]:
        rows = self.conn.execute(
            f"""
            {_CANDIDATE_SELECT_SQL}
            WHERE COALESCE(json_extract(wc.content_json, '$.slug'), wc.id) = ?
            {_CANDIDATE_ORDER_SQL}
            """,
            (slug,),
        ).fetchall()
        return [self._candidate_from_row(row) for row in rows]

    def get_candidate(self, candidate_id: str) -> WorkflowCandidateRecord | None:
        row = self.conn.execute(
            f"{_CANDIDATE_SELECT_SQL} WHERE wc.id = ?",
            (candidate_id,),
        ).fetchone()
        return self._candidate_from_row(row) if row else None

    def workflow_session_ledger(
        self,
        candidate_id: str,
        *,
        limit: int = 50,
    ) -> WorkflowSessionLedger:
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise KeyError(f"Workflow candidate not found: {candidate_id}")
        slug = str(candidate.content.get("slug") or candidate.id)
        grouped_candidates = self.conn.execute(
            """
            SELECT id, observation_id, status
            FROM workflow_candidates
            WHERE json_extract(content_json, '$.slug') = ?
              AND status NOT IN ('rejected', 'rolled_back')
            ORDER BY created_at, id
            """,
            (slug,),
        ).fetchall()
        current_rows = [row for row in grouped_candidates if str(row[2]) != "stale"]
        evidence_rows = current_rows or grouped_candidates or [
            (candidate.id, candidate.observation_id, candidate.status.value)
        ]
        candidate_ids = [str(row[0]) for row in evidence_rows]
        observation_ids = [str(row[1]) for row in evidence_rows]
        observation_placeholders = ", ".join("?" for _ in observation_ids)
        candidate_placeholders = ", ".join("?" for _ in candidate_ids)
        bounded_limit = max(1, min(limit, 200))
        source_rows = self.conn.execute(
            f"""
            SELECT s.id, s.title, a.name, s.started_at, s.status,
                   COUNT(oe.id) AS evidence_count,
                   MAX(oe.confidence) AS evidence_confidence
            FROM observation_evidence oe
            JOIN sessions s ON s.id = oe.session_id
            LEFT JOIN agents a ON a.id = s.agent_id
            WHERE oe.observation_id IN ({observation_placeholders})
            GROUP BY s.id, s.title, a.name, s.started_at, s.status
            ORDER BY evidence_count DESC, evidence_confidence DESC, s.started_at DESC
            LIMIT ?
            """,
            (*observation_ids, bounded_limit),
        ).fetchall()
        source_sessions = [
            WorkflowSessionRecord(
                session_id=str(row[0]),
                relationship="source",
                title=row[1],
                agent=row[2],
                started_at=row[3],
                status=row[4],
                workspace=self._session_workspace(str(row[0])),
                evidence_count=int(row[5]),
                evidence_summaries=self._session_evidence_summaries(
                    observation_ids,
                    str(row[0]),
                ),
                evidence_focus_id=self._session_evidence_focus_id(
                    observation_ids,
                    str(row[0]),
                ),
            )
            for row in source_rows
        ]
        exposure_rows = self.conn.execute(
            f"""
            SELECT s.id, s.title, a.name, s.started_at, s.status,
                   we.state, we.evidence_json
            FROM workflow_exposures we
            JOIN interventions i ON i.id = we.intervention_id
            JOIN workflow_versions wv ON wv.id = i.workflow_version_id
            JOIN sessions s ON s.id = we.session_id
            LEFT JOIN agents a ON a.id = s.agent_id
            WHERE wv.candidate_id IN ({candidate_placeholders})
            ORDER BY s.started_at DESC
            LIMIT ?
            """,
            (*candidate_ids, bounded_limit),
        ).fetchall()
        exposure_sessions = [
            WorkflowSessionRecord(
                session_id=str(row[0]),
                relationship="exposure",
                title=row[1],
                agent=row[2],
                started_at=row[3],
                status=row[4],
                workspace=self._session_workspace(str(row[0])),
                evidence_count=1,
                evidence_summaries=self._exposure_summaries(row[5], row[6]),
                exposure_state=row[5],
            )
            for row in exposure_rows
        ]
        source_count = int(
            self.conn.execute(
                f"SELECT COUNT(DISTINCT session_id) FROM observation_evidence WHERE observation_id IN ({observation_placeholders})",
                observation_ids,
            ).fetchone()[0]
        )
        exposure_count = int(
            self.conn.execute(
                f"""
                SELECT COUNT(DISTINCT we.session_id)
                FROM workflow_exposures we
                JOIN interventions i ON i.id = we.intervention_id
                JOIN workflow_versions wv ON wv.id = i.workflow_version_id
                WHERE wv.candidate_id IN ({candidate_placeholders})
                """,
                candidate_ids,
            ).fetchone()[0]
        )
        return WorkflowSessionLedger(
            candidate_id=candidate_id,
            observation_id=candidate.observation_id,
            observation_ids=observation_ids,
            skill_slug=slug,
            source_session_count=source_count,
            source_sessions=source_sessions,
            exposure_session_count=exposure_count,
            exposure_sessions=exposure_sessions,
        )

    def record_feedback(
        self,
        session_id: str,
        outcome: str,
        *,
        reason_redacted: str | None,
        actor: str = "local_operator",
        now: str | None = None,
    ) -> str:
        allowed_outcomes = {"good", "bad", "no-change-correct", "corrected"}
        if outcome not in allowed_outcomes:
            raise ValueError(f"Unsupported feedback outcome: {outcome}")
        if reason_redacted is not None:
            reason_redacted = reason_redacted.strip()[:500] or None
        timestamp = now or utc_now()
        exists = self.conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not exists:
            raise KeyError(f"Session not found: {session_id}")
        feedback_id = f"feedback_{uuid.uuid4().hex}"
        self.conn.execute(
            """
            INSERT INTO operator_feedback(id, session_id, outcome, reason_redacted, actor, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (feedback_id, session_id, outcome, reason_redacted, actor, timestamp),
        )
        self.conn.execute(
            """
            INSERT INTO session_outcomes(
              id, session_id, outcome, source, confidence, verification_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'operator_feedback', 1, ?, ?, ?)
            ON CONFLICT(session_id, source) DO UPDATE SET
              outcome = excluded.outcome,
              confidence = excluded.confidence,
              verification_json = excluded.verification_json,
              updated_at = excluded.updated_at
            """,
            (
                f"outcome_{hashlib.sha256((session_id + ':operator').encode()).hexdigest()[:24]}",
                session_id,
                outcome,
                _json({"feedback_id": feedback_id, "has_reason": bool(reason_redacted)}),
                timestamp,
                timestamp,
            ),
        )
        self.record_event(
            entity_type="session",
            entity_id=session_id,
            event_type="feedback_recorded",
            actor=actor,
            details={"outcome": outcome, "feedback_id": feedback_id},
            now=timestamp,
        )
        self.conn.commit()
        return feedback_id

    def summary(self, *, limit: int = 50) -> ImprovementSummary:
        observations = self.list_observations(limit=limit)
        counts = Counter(
            str(row[0]) for row in self.conn.execute("SELECT status FROM observations").fetchall()
        )
        pending = int(self.conn.execute(
            """
            SELECT COUNT(DISTINCT json_extract(pending.content_json, '$.slug'))
            FROM workflow_candidates pending
            WHERE pending.status = 'pending'
              AND NOT EXISTS (
                SELECT 1
                FROM workflow_candidates active
                WHERE active.status = 'active'
                  AND json_extract(active.content_json, '$.slug') = json_extract(pending.content_json, '$.slug')
              )
            """
        ).fetchone()[0])
        active = int(
            self.conn.execute("SELECT COUNT(*) FROM interventions WHERE status = 'active'").fetchone()[0]
        )
        measured = self.conn.execute(
            """
            SELECT SUM(CASE WHEN verdict = 'improved' THEN 1 ELSE 0 END), COUNT(*)
            FROM measurements WHERE verdict <> 'insufficient_data'
            """
        ).fetchone()
        rate = None if not measured or not measured[1] else float(measured[0] or 0) / int(measured[1])
        return ImprovementSummary(
            generated_at=utc_now(),
            observations=observations,
            counts_by_status=dict(counts),
            pending_workflows=pending,
            active_interventions=active,
            verified_improvement_rate=rate,
        )

    def record_event(
        self,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        details: dict[str, Any],
        now: str,
        actor: str = "reflect",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO improvement_events(id, entity_type, entity_id, event_type, actor, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (f"event_{uuid.uuid4().hex}", entity_type, entity_id, event_type, actor, _json(details), now),
        )

    def _replace_evidence(self, observation_id: str, evidence: list[EvidenceRef], *, now: str) -> None:
        self.conn.execute("DELETE FROM observation_evidence WHERE observation_id = ?", (observation_id,))
        for item in evidence:
            evidence_id = "evidence_" + hashlib.sha256(
                f"{observation_id}:{item.entity_type}:{item.entity_id}:{item.polarity}".encode()
            ).hexdigest()[:24]
            self.conn.execute(
                """
                INSERT INTO observation_evidence(
                  id, observation_id, polarity, entity_type, entity_id, session_id,
                  step_id, tool_call_id, llm_call_id, file_id, memory_id,
                  summary_redacted, confidence, attrs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    observation_id,
                    item.polarity,
                    item.entity_type,
                    item.entity_id,
                    item.session_id,
                    item.step_id,
                    item.tool_call_id,
                    item.llm_call_id,
                    item.file_id,
                    item.memory_id,
                    item.summary_redacted,
                    item.confidence,
                    _json(item.attrs),
                    now,
                ),
            )

    def _evidence(self, observation_id: str) -> list[EvidenceRef]:
        rows = self.conn.execute(
            """
            SELECT entity_type, entity_id, summary_redacted, polarity, session_id,
                   step_id, tool_call_id, llm_call_id, file_id, memory_id,
                   confidence, attrs_json
            FROM observation_evidence
            WHERE observation_id = ?
            ORDER BY confidence DESC, created_at DESC
            """,
            (observation_id,),
        ).fetchall()
        return [
            EvidenceRef(
                entity_type=row[0],
                entity_id=row[1],
                summary_redacted=row[2],
                polarity=row[3],
                session_id=row[4],
                step_id=row[5],
                tool_call_id=row[6],
                llm_call_id=row[7],
                file_id=row[8],
                memory_id=row[9],
                confidence=float(row[10]),
                attrs=_loads(row[11], {}),
            )
            for row in rows
        ]

    def _session_workspace(self, session_id: str) -> str | None:
        canonical = self.conn.execute(
            """
            SELECT w.root_path
            FROM sessions s
            JOIN workspaces w ON w.id = s.workspace_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if canonical and canonical[0]:
            return str(canonical[0])
        row = self.conn.execute(
            """
            SELECT workspace
            FROM (
              SELECT COALESCE(
                       NULLIF(json_extract(raw_attrs_json, '$."gen_ai.client.workspace"'), ''),
                       NULLIF(json_extract(raw_attrs_json, '$."gen_ai.client.cwd"'), '')
                     ) AS workspace,
                     COUNT(*) AS occurrences
              FROM steps
              WHERE session_id = ? AND json_valid(raw_attrs_json)
              GROUP BY workspace
            )
            WHERE workspace IS NOT NULL
            ORDER BY occurrences DESC, workspace
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    def _session_evidence_summaries(
        self,
        observation_ids: list[str],
        session_id: str,
    ) -> list[str]:
        placeholders = ", ".join("?" for _ in observation_ids)
        return [
            str(row[0])
            for row in self.conn.execute(
                f"""
                SELECT summary_redacted
                FROM observation_evidence
                WHERE observation_id IN ({placeholders}) AND session_id = ?
                ORDER BY confidence DESC, created_at DESC
                LIMIT 4
                """,
                (*observation_ids, session_id),
            ).fetchall()
        ]

    def _session_evidence_focus_id(
        self,
        observation_ids: list[str],
        session_id: str,
    ) -> str | None:
        placeholders = ", ".join("?" for _ in observation_ids)
        row = self.conn.execute(
            f"""
            SELECT COALESCE(step_id, tool_call_id, entity_id)
            FROM observation_evidence
            WHERE observation_id IN ({placeholders}) AND session_id = ?
            ORDER BY confidence DESC, created_at DESC
            LIMIT 1
            """,
            (*observation_ids, session_id),
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    @staticmethod
    def _exposure_summaries(state: str, evidence_json: str) -> list[str]:
        evidence = _loads(evidence_json, {})
        details = [f"Workflow exposure classified as {state}."]
        if evidence.get("slug_observed"):
            details.append("The workflow slug was observed in session telemetry.")
        if evidence.get("verification_observed"):
            details.append("Verification activity was observed after workflow use.")
        return details

    def _observation_from_row(self, row: tuple[Any, ...]) -> ObservationRecord:
        return ObservationRecord(
            id=row[0],
            rule_id=row[1],
            rule_version=int(row[2]),
            scope_type=row[3],
            scope_id=row[4],
            repo_id=row[5],
            fingerprint=row[6],
            category=row[7],
            title=row[8],
            summary=row[9],
            metric_name=row[10],
            metric_value=float(row[11]),
            metric_unit=row[12],
            metric_direction=row[13],
            baseline_value=None if row[14] is None else float(row[14]),
            baseline_query=_loads(row[15], {}),
            impact_score=float(row[16]),
            severity=row[17],
            confidence=float(row[18]),
            occurrence_count=int(row[19]),
            affected_session_count=int(row[20]),
            actionability=row[21],
            status=row[22],
            first_seen_at=row[23],
            last_seen_at=row[24],
            last_evaluated_at=row[25],
            suppression_reason=row[26],
            suppressed_until=row[27],
            candidate_id=row[28],
            candidate_status=row[29],
            evidence=self._evidence(str(row[0])),
        )

    def _candidate_from_row(self, row: tuple[Any, ...]) -> WorkflowCandidateRecord:
        exposure_counts = {
            str(state): int(count)
            for state, count in self.conn.execute(
                """
                SELECT we.state, COUNT(*)
                FROM workflow_exposures we
                JOIN interventions i ON i.id = we.intervention_id
                JOIN workflow_versions wv ON wv.id = i.workflow_version_id
                WHERE wv.candidate_id = ?
                GROUP BY we.state
                """,
                (row[0],),
            ).fetchall()
        }
        return WorkflowCandidateRecord(
            id=row[0],
            observation_id=row[1],
            action_type=row[2],
            title=row[3],
            hypothesis=row[4],
            scope=row[5],
            risk=row[6],
            content=_loads(row[7], {}),
            support_count=int(row[8]),
            confidence=float(row[9]),
            target_metric=row[10],
            target_value=None if row[11] is None else float(row[11]),
            measurement_window=int(row[12]),
            status=row[13],
            checks=_loads(row[14], {}),
            provenance=_loads(row[15], {}),
            created_at=row[16],
            updated_at=row[17],
            task_archetype_id=row[18],
            exposure_counts=exposure_counts,
            active_intervention_id=row[19],
        )
