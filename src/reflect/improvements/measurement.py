from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import uuid
from typing import Any

from reflect.improvements.contracts import ContractEvaluationService, WorkflowContract
from reflect.improvements.repository import utc_now

_RECOVERY_METRICS = {"tool_failure_rate", "identical_retry_calls"}
_MINIMUM_SIGNAL_COVERAGE = 0.9


def _public_cohort(raw: str) -> dict[str, Any]:
    cohort = json.loads(raw)
    cohort.pop("before_session_ids", None)
    cohort.pop("after_session_ids", None)
    cohort.pop("before_execution_unit_ids", None)
    cohort.pop("after_execution_unit_ids", None)
    cohort.pop("fingerprint", None)
    return cohort


def _cohort_fingerprint(
    cohort: dict[str, Any],
    *,
    before_values: list[float],
    after_values: list[float],
) -> str:
    payload = {
        "before_ids": cohort.get("before_execution_unit_ids", cohort.get("before_session_ids", [])),
        "after_ids": cohort.get("after_execution_unit_ids", cohort.get("after_session_ids", [])),
        "before_values": before_values,
        "after_values": after_values,
        "measurement_state": cohort.get("measurement_state"),
        "quality": cohort.get("quality"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MeasurementService:
    """Compute conservative before/after results for active interventions."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.contracts = ContractEvaluationService(conn)

    def measure(self, candidate_id: str, *, skip_unchanged: bool = False) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT i.id, i.exposure_started_at, o.repo_id, wc.target_metric,
                   wc.task_archetype_id, o.metric_direction, wc.content_json,
                   wc.measurement_window
            FROM interventions i
            JOIN workflow_versions wv ON wv.id = i.workflow_version_id
            JOIN workflow_candidates wc ON wc.id = wv.candidate_id
            JOIN observations o ON o.id = wc.observation_id
            WHERE wc.id = ? AND i.status = 'active'
            ORDER BY i.created_at DESC LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"No active intervention found for workflow: {candidate_id}")
        (
            intervention_id,
            exposed_at,
            repo_id,
            metric_name,
            archetype_id,
            metric_direction,
            content_json,
            measurement_window,
        ) = row
        content = json.loads(str(content_json or "{}"))
        contract = WorkflowContract.from_raw(content.get("workflow_contract"))
        contract_mode = contract is not None and str(metric_name) == "workflow_adherence"
        recovery_mode = (
            str(content.get("behavior_type") or "") == "recovery"
            and str(metric_name) in _RECOVERY_METRICS
        )
        latest = self.conn.execute(
            """
            SELECT id, before_count, after_count, verdict, measured_at, cohort_json
            FROM measurements
            WHERE intervention_id = ? AND metric_name = ?
            ORDER BY measured_at DESC LIMIT 1
            """,
            (intervention_id, metric_name),
        ).fetchone()
        latest_cohort = json.loads(latest[5]) if latest else {}
        quality: dict[str, Any] | None = None
        if contract_mode and contract is not None:
            minimum_before = contract.validation.baseline_minimum
            baseline_limit = contract.validation.baseline_maximum
            minimum_after = max(1, min(int(measurement_window), 100))
            frozen_before_ids = [
                str(item) for item in latest_cohort.get("before_execution_unit_ids") or []
            ]
            before_units = (
                self._execution_units_by_ids(frozen_before_ids)
                if frozen_before_ids
                else self._cohort_execution_units(
                    repo_id,
                    workspace_id=contract.applicability.workspace_id,
                    task_archetype_id=archetype_id,
                    before=exposed_at,
                    limit=baseline_limit,
                    newest_first=True,
                )
            )
            after_units = self._cohort_execution_units(
                repo_id,
                workspace_id=contract.applicability.workspace_id,
                task_archetype_id=archetype_id,
                after=exposed_at,
                limit=minimum_after,
            )
            evaluated = self.contracts.evaluate_units(
                contract,
                [*before_units, *after_units],
            )
            before = [
                1.0
                if evaluated[str(item["execution_unit_id"])].evaluation.followed
                else 0.0
                for item in before_units
            ]
            after = [
                1.0
                if evaluated[str(item["execution_unit_id"])].evaluation.followed
                else 0.0
                for item in after_units
            ]
        else:
            minimum_before = 5
            minimum_after = 5
            before_sessions = self._cohort_sessions(
                str(metric_name),
                repo_id,
                task_archetype_id=archetype_id,
                before=exposed_at,
            )
            after_sessions = self._cohort_sessions(
                str(metric_name),
                repo_id,
                task_archetype_id=archetype_id,
                after=exposed_at,
            )
            if recovery_mode:
                before_sessions, after_sessions, quality = self._recovery_session_cohorts(
                    str(intervention_id),
                    str(metric_name),
                    before_sessions=before_sessions,
                    after_sessions=after_sessions,
                    latest_cohort=latest_cohort,
                    minimum_before=minimum_before,
                    minimum_after=minimum_after,
                )
            before_values = self._session_metric_values(
                str(metric_name),
                [str(item["session_id"]) for item in before_sessions],
            )
            after_values = self._session_metric_values(
                str(metric_name),
                [str(item["session_id"]) for item in after_sessions],
            )
            before = [
                before_values[str(item["session_id"])]
                for item in before_sessions
                if str(item["session_id"]) in before_values
            ]
            after = [
                after_values[str(item["session_id"])]
                for item in after_sessions
                if str(item["session_id"]) in after_values
            ]
        raw_before_value = statistics.mean(before) if before else None
        raw_after_value = statistics.mean(after) if after else None
        sample_ready = (
            len(before) >= minimum_before
            and len(after) >= minimum_after
            and raw_before_value is not None
            and raw_after_value is not None
        )
        measurement_state = (
            str(quality["state"])
            if quality is not None
            else "measured"
            if sample_ready
            else "collecting"
        )
        before_value = (
            raw_before_value
            if not recovery_mode or measurement_state == "measured"
            else None
        )
        after_value = (
            raw_after_value
            if not recovery_mode or measurement_state == "measured"
            else None
        )
        delta = None if before_value is None or after_value is None else after_value - before_value
        verdict = "insufficient_data"
        confidence = 0.0
        if sample_ready and measurement_state == "measured":
            confidence = 0.45 if len(after) < 10 else min(0.85, 0.6 + len(after) * 0.015)
            if metric_direction == "higher_is_better":
                if after_value > before_value * 1.1 or (before_value == 0 and after_value > 0):
                    verdict = "improved"
                elif after_value < before_value * 0.9:
                    verdict = "regressed"
                else:
                    verdict = "unchanged"
            else:
                if after_value < before_value * 0.9:
                    verdict = "improved"
                elif after_value > before_value * 1.1 or (before_value == 0 and after_value > 0):
                    verdict = "regressed"
                else:
                    verdict = "unchanged"
        now = utc_now()
        if contract_mode and contract is not None:
            cohort = {
                "unit": "execution_units",
                "repo_id": repo_id,
                "workspace_id": contract.applicability.workspace_id,
                "task_archetype_id": archetype_id,
                "metric": metric_name,
                "before": f"comparable tasks before {exposed_at}",
                "after": f"first {minimum_after} comparable tasks on or after {exposed_at}",
                "minimum_after_execution_units": minimum_after,
                "minimum_before_execution_units": minimum_before,
                "metric_direction": metric_direction,
                "before_execution_unit_ids": [item["execution_unit_id"] for item in before_units],
                "after_execution_unit_ids": [item["execution_unit_id"] for item in after_units],
                "signature_hash": contract.signature_hash,
                "measurement_state": measurement_state,
            }
        else:
            cohort = {
                "unit": "sessions",
                "repo_id": repo_id,
                "task_archetype_id": archetype_id,
                "metric": metric_name,
                "before": f"sessions before {exposed_at}",
                "after": f"sessions on or after {exposed_at}",
                "minimum_after_sessions": minimum_after,
                "minimum_before_sessions": minimum_before,
                "metric_direction": metric_direction,
                "before_session_ids": [item["session_id"] for item in before_sessions],
                "after_session_ids": [item["session_id"] for item in after_sessions],
                "measurement_state": measurement_state,
            }
            if quality is not None:
                cohort["measurement_reasons"] = quality["reasons"]
                cohort["quality"] = quality
        cohort["fingerprint"] = _cohort_fingerprint(
            cohort,
            before_values=before,
            after_values=after,
        )
        if (
            skip_unchanged
            and latest
            and latest_cohort.get("fingerprint") == cohort["fingerprint"]
        ):
            return {
                "id": latest[0],
                "candidate_id": candidate_id,
                "metric_name": metric_name,
                "before_value": before_value,
                "after_value": after_value,
                "before_count": len(before),
                "after_count": len(after),
                "delta": delta,
                "verdict": latest[3],
                "confidence": confidence,
                "cohort": cohort,
                "measured_at": latest[4],
                "created": False,
            }
        measurement_id = f"measurement_{uuid.uuid4().hex}"
        self.conn.execute(
            """
            INSERT INTO measurements(
              id, intervention_id, metric_name, cohort_json, before_value,
              after_value, before_count, after_count, delta, verdict, confidence,
              confounders_json, measured_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?)
            """,
            (
                measurement_id,
                intervention_id,
                metric_name,
                json.dumps(cohort, sort_keys=True),
                before_value,
                after_value,
                len(before),
                len(after),
                delta,
                verdict,
                confidence,
                now,
                now,
                now,
            ),
        )
        if verdict in {"improved", "regressed", "unchanged"}:
            observation_status = "measured" if verdict != "regressed" else "regressed"
            self.conn.execute(
                """
                UPDATE observations SET status = ?, updated_at = ?
                WHERE id = (SELECT observation_id FROM workflow_candidates WHERE id = ?)
                """,
                (observation_status, now, candidate_id),
            )
        self.conn.commit()
        return {
            "id": measurement_id,
            "candidate_id": candidate_id,
            "metric_name": metric_name,
            "before_value": before_value,
            "after_value": after_value,
            "before_count": len(before),
            "after_count": len(after),
            "delta": delta,
            "verdict": verdict,
            "confidence": confidence,
            "cohort": cohort,
            "measured_at": now,
            "created": True,
        }

    def measure_active(self) -> dict[str, int]:
        """Measure active workflows once per distinct bounded cohort snapshot."""
        candidate_ids = [
            str(row[0])
            for row in self.conn.execute(
                """
                SELECT DISTINCT wv.candidate_id
                FROM interventions i
                JOIN workflow_versions wv ON wv.id = i.workflow_version_id
                WHERE i.status = 'active'
                """
            ).fetchall()
        ]
        created = 0
        regressed = 0
        for candidate_id in candidate_ids:
            result = self.measure(candidate_id, skip_unchanged=True)
            created += int(bool(result["created"]))
            regressed += int(result["verdict"] == "regressed")
        return {"active": len(candidate_ids), "created": created, "regressed": regressed}

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT m.id, wv.candidate_id, m.metric_name, m.cohort_json,
                   m.before_value, m.after_value, m.before_count, m.after_count,
                   m.delta, m.verdict, m.confidence, m.measured_at
            FROM measurements m
            JOIN interventions i ON i.id = m.intervention_id
            JOIN workflow_versions wv ON wv.id = i.workflow_version_id
            ORDER BY m.measured_at DESC LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        ).fetchall()
        return [
            {
                "id": row[0],
                "candidate_id": row[1],
                "metric_name": row[2],
                "cohort": _public_cohort(row[3]),
                "before_value": row[4],
                "after_value": row[5],
                "before_count": row[6],
                "after_count": row[7],
                "delta": row[8],
                "verdict": row[9],
                "confidence": row[10],
                "measured_at": row[11],
            }
            for row in rows
        ]

    def sessions(self, measurement_id: str) -> dict[str, Any]:
        """Return the bounded execution or session cohorts in a measurement snapshot."""
        row = self.conn.execute(
            """
            SELECT m.id, wv.candidate_id, m.metric_name, m.cohort_json,
                   m.before_count, m.after_count, i.exposure_started_at,
                   o.repo_id, wc.task_archetype_id, wv.content_json
            FROM measurements m
            JOIN interventions i ON i.id = m.intervention_id
            JOIN workflow_versions wv ON wv.id = i.workflow_version_id
            JOIN workflow_candidates wc ON wc.id = wv.candidate_id
            JOIN observations o ON o.id = wc.observation_id
            WHERE m.id = ?
            """,
            (measurement_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"Measurement not found: {measurement_id}")
        (
            stored_id,
            candidate_id,
            metric_name,
            cohort_json,
            before_count,
            after_count,
            exposed_at,
            repo_id,
            archetype_id,
            workflow_content_json,
        ) = row
        cohort = json.loads(cohort_json)
        before_execution_ids = [
            str(item) for item in cohort.get("before_execution_unit_ids") or []
        ]
        after_execution_ids = [
            str(item) for item in cohort.get("after_execution_unit_ids") or []
        ]
        if (
            before_execution_ids
            or after_execution_ids
            or cohort.get("unit") == "execution_units"
        ):
            before_execution_units = self._execution_units_by_ids(before_execution_ids)
            after_execution_units = self._execution_units_by_ids(after_execution_ids)
            workflow_content = json.loads(str(workflow_content_json or "{}"))
            contract = WorkflowContract.from_raw(workflow_content.get("workflow_contract"))
            if contract is not None:
                enriched = self._execution_units_with_contract_evidence(
                    contract,
                    [*before_execution_units, *after_execution_units],
                )
                by_id = {
                    str(item["execution_unit_id"]): item
                    for item in enriched
                }
                before_execution_units = [
                    by_id[str(item["execution_unit_id"])]
                    for item in before_execution_units
                ]
                after_execution_units = [
                    by_id[str(item["execution_unit_id"])]
                    for item in after_execution_units
                ]
            return {
                "id": stored_id,
                "candidate_id": candidate_id,
                "metric_name": metric_name,
                "unit": "execution_units",
                "measurement_state": cohort.get("measurement_state", "collecting"),
                "measurement_reasons": cohort.get("measurement_reasons", []),
                "quality": cohort.get("quality", {}),
                "before_count": int(before_count),
                "after_count": int(after_count),
                "snapshot_exact": True,
                "before_execution_units": before_execution_units,
                "after_execution_units": after_execution_units,
                "before_sessions": before_execution_units,
                "after_sessions": after_execution_units,
            }
        before_ids = [str(item) for item in cohort.get("before_session_ids") or []]
        after_ids = [str(item) for item in cohort.get("after_session_ids") or []]
        snapshot_exact = "before_session_ids" in cohort and "after_session_ids" in cohort
        before_sessions = (
            self._sessions_by_ids(before_ids)
            if snapshot_exact
            else self._cohort_sessions(
                str(metric_name),
                repo_id,
                task_archetype_id=archetype_id,
                before=exposed_at,
            )[: int(before_count)]
        )
        after_sessions = (
            self._sessions_by_ids(after_ids)
            if snapshot_exact
            else self._cohort_sessions(
                str(metric_name),
                repo_id,
                task_archetype_id=archetype_id,
                after=exposed_at,
            )[: int(after_count)]
        )
        before_sessions = self._sessions_with_metric_values(
            str(metric_name), before_sessions
        )
        after_sessions = self._sessions_with_metric_values(
            str(metric_name), after_sessions
        )
        return {
            "id": stored_id,
            "candidate_id": candidate_id,
            "metric_name": metric_name,
            "measurement_state": cohort.get("measurement_state", "collecting"),
            "measurement_reasons": cohort.get("measurement_reasons", []),
            "quality": cohort.get("quality", {}),
            "before_count": int(before_count),
            "after_count": int(after_count),
            "snapshot_exact": snapshot_exact,
            "before_sessions": before_sessions,
            "after_sessions": after_sessions,
        }

    def _execution_units_by_ids(self, execution_unit_ids: list[str]) -> list[dict[str, Any]]:
        if not execution_unit_ids:
            return []
        placeholders = ", ".join("?" for _ in execution_unit_ids)
        rows = self.conn.execute(
            f"""
            SELECT eu.id, eu.session_id, s.title, a.name, eu.started_at,
                   eu.status, w.root_path, eua.task_archetype_id, eu.source,
                   eu.outcome, eu.verification_passed
            FROM execution_units eu
            JOIN sessions s ON s.id = eu.session_id
            LEFT JOIN agents a ON a.id = eu.agent_id
            LEFT JOIN workspaces w ON w.id = eu.workspace_id
            LEFT JOIN execution_unit_archetypes eua ON eua.execution_unit_id = eu.id
            WHERE eu.id IN ({placeholders})
            """,
            execution_unit_ids,
        ).fetchall()
        by_id = {
            str(item[0]): {
                "execution_unit_id": item[0],
                "session_id": item[1],
                "title": item[2],
                "agent": item[3],
                "started_at": item[4],
                "status": item[5],
                "workspace": item[6],
                "task_archetype_id": item[7],
                "boundary_source": item[8],
                "outcome": item[9],
                "verification_passed": None if item[10] is None else bool(item[10]),
            }
            for item in rows
        }
        return [by_id[item] for item in execution_unit_ids if item in by_id]

    def _execution_units_with_contract_evidence(
        self,
        contract: WorkflowContract,
        execution_units: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        evaluated = self.contracts.evaluate_units(contract, execution_units)
        required_count = len(contract.required_milestones)
        enriched: list[dict[str, Any]] = []
        for item in execution_units:
            evidence = evaluated[str(item["execution_unit_id"])]
            evaluation = evidence.evaluation
            observed = " → ".join(evidence.observed_roles)
            summaries = [
                f"{round(evaluation.evidence_coverage * 100)}% required milestone coverage"
            ]
            if observed:
                summaries.insert(0, f"Observed roles: {observed}")
            elif required_count:
                summaries.insert(0, "No required workflow roles were observed")
            enriched.append(
                {
                    **item,
                    "metric_value": 1.0 if evaluation.followed else 0.0,
                    "adherence_state": "followed" if evaluation.followed else "not_followed",
                    "evidence_count": evidence.signal_count,
                    "evidence_summaries": summaries,
                }
            )
        return enriched

    def _cohort_execution_units(
        self,
        repo_id: str | None,
        *,
        workspace_id: str | None,
        task_archetype_id: str | None,
        before: str | None = None,
        after: str | None = None,
        limit: int,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        time_clause = "AND tu.started_at < ?" if before else "AND tu.started_at >= ?"
        order = "DESC" if newest_first else "ASC"
        rows = self.conn.execute(
            f"""
            SELECT tu.id
            FROM execution_units tu
            JOIN execution_unit_archetypes tua ON tua.execution_unit_id = tu.id
            WHERE COALESCE(tu.repo_id, '') = COALESCE(?, '')
              AND (? IS NULL OR tu.workspace_id = ?)
              AND (? IS NULL OR tua.task_archetype_id = ?)
              AND tu.eligible = 1 AND tua.mixed = 0
              AND lower(COALESCE(tu.status, '')) IN ('completed', 'ok', 'success')
              {time_clause}
            ORDER BY tu.started_at {order}, tu.id {order}
            LIMIT ?
            """,
            (
                repo_id,
                workspace_id,
                workspace_id,
                task_archetype_id,
                task_archetype_id,
                before or after,
                max(1, min(limit, 100)),
            ),
        ).fetchall()
        execution_unit_ids = [str(row[0]) for row in rows]
        if newest_first:
            execution_unit_ids.reverse()
        return self._execution_units_by_ids(execution_unit_ids)

    def _sessions_by_ids(self, session_ids: list[str]) -> list[dict[str, Any]]:
        if not session_ids:
            return []
        placeholders = ", ".join("?" for _ in session_ids)
        rows = self.conn.execute(
            f"""
            SELECT s.id, s.title, a.name, s.started_at, s.status
            FROM sessions s
            LEFT JOIN agents a ON a.id = s.agent_id
            WHERE s.id IN ({placeholders})
            """,
            session_ids,
        ).fetchall()
        by_id = {
            str(item[0]): {
                "session_id": item[0],
                "title": item[1],
                "agent": item[2],
                "started_at": item[3],
                "status": item[4],
            }
            for item in rows
        }
        return [by_id[session_id] for session_id in session_ids if session_id in by_id]

    def _recovery_session_cohorts(
        self,
        intervention_id: str,
        metric_name: str,
        *,
        before_sessions: list[dict[str, Any]],
        after_sessions: list[dict[str, Any]],
        latest_cohort: dict[str, Any],
        minimum_before: int,
        minimum_after: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        """Qualify recovery impact using observed adherence and compatible telemetry."""

        exposure_rows = self.conn.execute(
            """
            SELECT state, COUNT(DISTINCT session_id)
            FROM workflow_exposures
            WHERE intervention_id = ?
            GROUP BY state
            """,
            (intervention_id,),
        ).fetchall()
        exposure_counts = {str(row[0]): int(row[1]) for row in exposure_rows}
        followed_ids = {
            str(row[0])
            for row in self.conn.execute(
                """
                SELECT DISTINCT session_id
                FROM workflow_exposures
                WHERE intervention_id = ? AND state = 'followed'
                """,
                (intervention_id,),
            ).fetchall()
        }
        after_sessions = [
            item for item in after_sessions if str(item["session_id"]) in followed_ids
        ]
        after_agents = {
            str(item.get("agent") or "unknown") for item in after_sessions
        }
        baseline_frozen = bool((latest_cohort.get("quality") or {}).get("baseline_frozen"))
        frozen_ids = [str(item) for item in latest_cohort.get("before_session_ids") or []]
        if baseline_frozen:
            before_sessions = self._sessions_by_ids(frozen_ids)
        elif after_agents:
            before_sessions = [
                item
                for item in before_sessions
                if str(item.get("agent") or "unknown") in after_agents
            ]
            baseline_frozen = True

        before_agents = self._agent_counts(before_sessions)
        after_agent_counts = self._agent_counts(after_sessions)
        before_signal = self._tool_signal_coverage(metric_name, before_sessions)
        after_signal = self._tool_signal_coverage(metric_name, after_sessions)
        hard_reasons: list[str] = []
        collecting_reasons: list[str] = []
        signal_label = (
            "tool-call statuses" if metric_name == "tool_failure_rate" else "tool input fingerprints"
        )
        for label, signal in (("baseline", before_signal), ("post-activation", after_signal)):
            coverage = signal["coverage"]
            if signal["total_calls"] and coverage < _MINIMUM_SIGNAL_COVERAGE:
                missing = 1.0 - coverage
                hard_reasons.append(
                    f"{missing:.1%} of {label} {signal_label} are unknown or missing."
                )
        if after_agent_counts and set(before_agents) != set(after_agent_counts):
            hard_reasons.append(
                "Baseline and post-activation sessions do not share the same agent/source cohort."
            )
        if not after_sessions:
            collecting_reasons.append(
                "No comparable post-activation sessions followed this workflow yet."
            )
        if len(before_sessions) < minimum_before:
            collecting_reasons.append(
                f"Reflect needs {minimum_before - len(before_sessions)} more comparable baseline sessions."
            )
        if len(after_sessions) < minimum_after:
            collecting_reasons.append(
                f"Reflect needs {minimum_after - len(after_sessions)} more followed post-activation sessions."
            )
        state = "not_measurable" if hard_reasons else "collecting" if collecting_reasons else "measured"
        return before_sessions, after_sessions, {
            "state": state,
            "reasons": [*hard_reasons, *collecting_reasons],
            "signal_requirement": (
                "known_tool_status" if metric_name == "tool_failure_rate" else "input_hash"
            ),
            "minimum_signal_coverage": _MINIMUM_SIGNAL_COVERAGE,
            "before_signal": before_signal,
            "after_signal": after_signal,
            "before_agents": before_agents,
            "after_agents": after_agent_counts,
            "exposure_counts": exposure_counts,
            "baseline_frozen": baseline_frozen,
        }

    @staticmethod
    def _agent_counts(sessions: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in sessions:
            agent = str(item.get("agent") or "unknown")
            counts[agent] = counts.get(agent, 0) + 1
        return counts

    def _tool_signal_coverage(
        self,
        metric_name: str,
        sessions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        session_ids = [str(item["session_id"]) for item in sessions]
        if not session_ids:
            return {"covered_calls": 0, "total_calls": 0, "coverage": 0.0}
        placeholders = ", ".join("?" for _ in session_ids)
        if metric_name == "tool_failure_rate":
            covered_sql = """
                lower(COALESCE(status, '')) NOT IN ('', 'unknown')
                OR NULLIF(error_type, '') IS NOT NULL
            """
        else:
            covered_sql = "NULLIF(input_hash, '') IS NOT NULL"
        row = self.conn.execute(
            f"""
            SELECT COUNT(*), SUM(CASE WHEN {covered_sql} THEN 1 ELSE 0 END)
            FROM tool_calls
            WHERE session_id IN ({placeholders})
            """,
            session_ids,
        ).fetchone()
        total_calls = int(row[0] or 0)
        covered_calls = int(row[1] or 0)
        return {
            "covered_calls": covered_calls,
            "total_calls": total_calls,
            "coverage": covered_calls / total_calls if total_calls else 0.0,
        }

    def _cohort_sessions(
        self,
        metric_name: str,
        repo_id: str | None,
        *,
        task_archetype_id: str | None,
        before: str | None = None,
        after: str | None = None,
        through: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = [repo_id]
        if before:
            clauses.append("s.started_at < ?")
            params.append(before)
        if after:
            clauses.append("s.started_at >= ?")
            params.append(after)
        if through:
            clauses.append("s.started_at <= ?")
            params.append(through)
        clauses.append(
            "(? IS NULL OR EXISTS (SELECT 1 FROM session_task_archetypes sta "
            "WHERE sta.session_id = s.id AND sta.task_archetype_id = ?))"
        )
        params.extend((task_archetype_id, task_archetype_id))
        if metric_name in _RECOVERY_METRICS:
            clauses.append("EXISTS (SELECT 1 FROM tool_calls tc WHERE tc.session_id = s.id)")
        rows = self.conn.execute(
            f"""
            SELECT s.id, s.title, a.name, s.started_at, s.status
            FROM sessions s
            LEFT JOIN agents a ON a.id = s.agent_id
            WHERE COALESCE(s.repo_id, '') = COALESCE(?, '')
              AND {' AND '.join(clauses)}
            ORDER BY s.started_at DESC LIMIT 50
            """,
            params,
        ).fetchall()
        return [
            {
                "session_id": item[0],
                "title": item[1],
                "agent": item[2],
                "started_at": item[3],
                "status": item[4],
            }
            for item in rows
        ]

    def _session_values(
        self,
        metric_name: str,
        repo_id: str | None,
        *,
        task_archetype_id: str | None,
        before: str | None = None,
        after: str | None = None,
    ) -> list[float]:
        sessions = self._cohort_sessions(
            metric_name,
            repo_id,
            task_archetype_id=task_archetype_id,
            before=before,
            after=after,
        )
        session_ids = [str(item["session_id"]) for item in sessions]
        values = self._session_metric_values(metric_name, session_ids)
        return [values[session_id] for session_id in session_ids if session_id in values]

    def _sessions_with_metric_values(
        self,
        metric_name: str,
        sessions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        values = self._session_metric_values(
            metric_name,
            [str(item["session_id"]) for item in sessions],
        )
        return [
            {**item, "metric_value": values.get(str(item["session_id"]))}
            for item in sessions
        ]

    def _session_metric_values(
        self,
        metric_name: str,
        session_ids: list[str],
    ) -> dict[str, float]:
        if not session_ids:
            return {}
        placeholders = ", ".join("?" for _ in session_ids)
        rows = self.conn.execute(
            f"""
            WITH tool_summary AS (
              SELECT tc.session_id,
                     COUNT(*) AS tool_calls,
                     SUM(CASE WHEN lower(COALESCE(tc.status, '')) IN ('error','failed','failure')
                                   OR NULLIF(tc.error_type, '') IS NOT NULL THEN 1 ELSE 0 END)
                       AS failed_calls,
                     SUM(CASE WHEN lower(tc.tool_name) GLOB '*read*'
                                   OR lower(tc.tool_name) GLOB '*find*'
                                   OR lower(tc.tool_name) GLOB '*search*'
                                   OR lower(tc.tool_name) GLOB '*grep*'
                                   OR lower(tc.tool_name) GLOB '*glob*' THEN 1 ELSE 0 END)
                       AS read_calls,
                     SUM(CASE WHEN lower(tc.tool_name) GLOB '*write*'
                                   OR lower(tc.tool_name) GLOB '*edit*'
                                   OR lower(tc.tool_name) GLOB '*patch*' THEN 1 ELSE 0 END)
                       AS mutation_calls,
                     SUM(CASE WHEN lower(tc.tool_name) GLOB '*test*'
                                   OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*pytest*'
                                   OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*ruff*'
                                   OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*build*'
                                   OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*compile*'
                              THEN 1 ELSE 0 END) AS verification_calls,
                     SUM(CASE WHEN lower(COALESCE(tc.error_type, '')) GLOB '*permission*'
                                   OR lower(COALESCE(tc.error_type, '')) GLOB '*sandbox*'
                                   OR lower(COALESCE(tc.error_type, '')) GLOB '*policy*'
                                   OR lower(COALESCE(tc.error_type, '')) GLOB '*approval*'
                                   OR lower(COALESCE(tc.error_message_redacted, '')) GLOB '*permission denied*'
                                   OR lower(COALESCE(tc.error_message_redacted, '')) GLOB '*not allowed*'
                              THEN 1 ELSE 0 END) AS constraint_violations
              FROM tool_calls tc
              WHERE tc.session_id IN ({placeholders})
              GROUP BY tc.session_id
            ), retry_summary AS (
              SELECT session_id, SUM(call_count) AS identical_retry_calls
              FROM (
                SELECT tc.session_id, COUNT(*) AS call_count
                FROM tool_calls tc
                WHERE tc.session_id IN ({placeholders})
                  AND NULLIF(tc.input_hash, '') IS NOT NULL
                GROUP BY tc.session_id, tc.tool_name, tc.input_hash
                HAVING COUNT(*) >= 3
              )
              GROUP BY session_id
            ), outcome_summary AS (
              SELECT so.session_id,
                     MAX(CASE WHEN so.outcome = 'corrected' THEN 1 ELSE 0 END) AS corrected,
                     MAX(CASE WHEN so.outcome = 'no-change-correct' THEN 1 ELSE 0 END)
                       AS no_change_correct
              FROM session_outcomes so
              WHERE so.session_id IN ({placeholders})
              GROUP BY so.session_id
            )
            SELECT s.id, s.input_tokens + s.output_tokens, s.failure_count,
                   s.recovered_failure_count, lower(COALESCE(s.status, '')),
                   COALESCE(ts.tool_calls, 0), COALESCE(ts.failed_calls, 0),
                   COALESCE(ts.read_calls, 0), COALESCE(ts.mutation_calls, 0),
                   COALESCE(ts.verification_calls, 0),
                   COALESCE(ts.constraint_violations, 0),
                   COALESCE(rs.identical_retry_calls, 0),
                   COALESCE(os.corrected, 0), COALESCE(os.no_change_correct, 0)
            FROM sessions s
            LEFT JOIN tool_summary ts ON ts.session_id = s.id
            LEFT JOIN retry_summary rs ON rs.session_id = s.id
            LEFT JOIN outcome_summary os ON os.session_id = s.id
            WHERE s.id IN ({placeholders})
            """,
            [*session_ids, *session_ids, *session_ids, *session_ids],
        ).fetchall()
        values: dict[str, float] = {}
        for row in rows:
            session_id = str(row[0])
            tool_calls = int(row[5])
            if metric_name == "tool_failure_rate":
                value = float(row[6]) / tool_calls if tool_calls else 0.0
            elif metric_name == "context_outlier_sessions":
                value = float(row[1])
            elif metric_name == "identical_retry_calls":
                value = float(row[11])
            elif metric_name == "unverified_change_sessions":
                value = float(bool(row[8]) and not bool(row[9]))
            elif metric_name == "read_only_exploration_calls":
                value = float(row[7]) if int(row[7]) >= 8 and not row[8] else 0.0
            elif metric_name == "operator_correction_rate":
                value = float(row[12])
            elif metric_name == "correct_no_change_sessions":
                value = float(row[13])
            elif metric_name == "constraint_violation_rate":
                value = float(row[10])
            elif metric_name == "recovered_failure_sessions":
                value = float(int(row[3]) > 0)
            elif metric_name == "successful_workflow_sessions":
                value = float(int(row[2]) == 0 and row[4] in {"completed", "ok", "success"})
            else:
                value = float(row[2])
            values[session_id] = value
        return values
