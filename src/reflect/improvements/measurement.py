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
        "before_ids": cohort.get("before_execution_unit_ids", []),
        "after_ids": cohort.get("after_execution_unit_ids", []),
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
        workspace_id = contract.applicability.workspace_id if contract is not None else None
        minimum_before = (
            contract.validation.baseline_minimum if contract is not None else 5
        )
        baseline_limit = (
            contract.validation.baseline_maximum if contract is not None else 20
        )
        minimum_after = max(1, min(int(measurement_window), 100))
        frozen_before_ids = [
            str(item) for item in latest_cohort.get("before_execution_unit_ids") or []
        ]
        before_units = (
            self._execution_units_by_ids(frozen_before_ids)
            if frozen_before_ids
            else self._cohort_execution_units(
                repo_id,
                workspace_id=workspace_id,
                task_archetype_id=archetype_id,
                before=exposed_at,
                limit=baseline_limit,
                newest_first=True,
            )
        )
        after_units = self._cohort_execution_units(
            repo_id,
            workspace_id=workspace_id,
            task_archetype_id=archetype_id,
            after=exposed_at,
            limit=minimum_after,
        )
        if contract_mode and contract is not None:
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
            before_values = self._execution_unit_metric_values(
                str(metric_name),
                [str(item["execution_unit_id"]) for item in before_units],
            )
            after_values = self._execution_unit_metric_values(
                str(metric_name),
                [str(item["execution_unit_id"]) for item in after_units],
            )
            before = [
                before_values[str(item["execution_unit_id"])]
                for item in before_units
                if str(item["execution_unit_id"]) in before_values
            ]
            after = [
                after_values[str(item["execution_unit_id"])]
                for item in after_units
                if str(item["execution_unit_id"]) in after_values
            ]
            if recovery_mode:
                before_coverage = len(before) / len(before_units) if before_units else 0.0
                after_coverage = len(after) / len(after_units) if after_units else 0.0
                reasons = []
                if before_coverage < _MINIMUM_SIGNAL_COVERAGE:
                    reasons.append("Baseline execution-unit signal coverage is incomplete.")
                if after_coverage < _MINIMUM_SIGNAL_COVERAGE:
                    reasons.append("Post-installation execution-unit signal coverage is incomplete.")
                quality = {
                    "state": (
                        "not_measurable"
                        if before_coverage < _MINIMUM_SIGNAL_COVERAGE
                        else "collecting"
                        if reasons
                        else "measured"
                    ),
                    "reasons": reasons,
                    "before_signal_coverage": before_coverage,
                    "after_signal_coverage": after_coverage,
                }
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
        cohort = {
            "unit": "execution_units",
            "repo_id": repo_id,
            "workspace_id": workspace_id,
            "task_archetype_id": archetype_id,
            "metric": metric_name,
            "before": f"comparable tasks before {exposed_at}",
            "after": f"first {minimum_after} comparable tasks on or after {exposed_at}",
            "minimum_after_execution_units": minimum_after,
            "minimum_before_execution_units": minimum_before,
            "metric_direction": metric_direction,
            "before_execution_unit_ids": [item["execution_unit_id"] for item in before_units],
            "after_execution_unit_ids": [item["execution_unit_id"] for item in after_units],
            "contract_signature": (
                contract.signature_hash if contract is not None else None
            ),
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
        """Return the bounded execution-unit cohorts in a measurement snapshot."""
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
        if cohort.get("unit") != "execution_units":
            raise ValueError(f"Measurement {measurement_id} does not use execution units")
        before_execution_units = self._execution_units_by_ids(before_execution_ids)
        after_execution_units = self._execution_units_by_ids(after_execution_ids)
        workflow_content = json.loads(str(workflow_content_json or "{}"))
        contract = WorkflowContract.from_raw(workflow_content.get("workflow_contract"))
        if contract is not None and str(metric_name) == "workflow_adherence":
            enriched = self._execution_units_with_contract_evidence(
                contract,
                [*before_execution_units, *after_execution_units],
            )
        else:
            units = [*before_execution_units, *after_execution_units]
            values = self._execution_unit_metric_values(
                str(metric_name),
                [str(item["execution_unit_id"]) for item in units],
            )
            enriched = [
                {
                    **item,
                    "metric_value": values.get(str(item["execution_unit_id"])),
                    "evidence_count": int(
                        str(item["execution_unit_id"]) in values
                    ),
                    "evidence_summaries": (
                        [f"{metric_name}: {values[str(item['execution_unit_id'])]:.3g}"]
                        if str(item["execution_unit_id"]) in values
                        else ["Required execution-unit telemetry is unavailable"]
                    ),
                }
                for item in units
            ]
        by_id = {str(item["execution_unit_id"]): item for item in enriched}
        before_execution_units = [
            by_id[str(item["execution_unit_id"])] for item in before_execution_units
        ]
        after_execution_units = [
            by_id[str(item["execution_unit_id"])] for item in after_execution_units
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

    def _execution_unit_metric_values(
        self,
        metric_name: str,
        execution_unit_ids: list[str],
    ) -> dict[str, float]:
        """Calculate a metric only from signals bounded to each execution unit."""

        if not execution_unit_ids:
            return {}
        placeholders = ", ".join("?" for _ in execution_unit_ids)
        rows = self.conn.execute(
            f"""
            WITH tool_summary AS (
              SELECT eus.execution_unit_id,
                     SUM(
                       CASE
                         WHEN lower(COALESCE(tc.status, ''))
                                IN ('ok','success','completed','error','failed','failure')
                           OR NULLIF(tc.error_type, '') IS NOT NULL
                         THEN 1 ELSE 0
                       END
                     ) AS tool_calls,
                     SUM(CASE WHEN lower(COALESCE(tc.status, ''))
                                      IN ('error','failed','failure')
                                   OR NULLIF(tc.error_type, '') IS NOT NULL
                              THEN 1 ELSE 0 END) AS failed_calls,
                     SUM(CASE WHEN lower(tc.tool_name) GLOB '*read*'
                                   OR lower(tc.tool_name) GLOB '*find*'
                                   OR lower(tc.tool_name) GLOB '*search*'
                                   OR lower(tc.tool_name) GLOB '*grep*'
                                   OR lower(tc.tool_name) GLOB '*glob*'
                              THEN 1 ELSE 0 END) AS read_calls,
                     SUM(CASE WHEN lower(tc.tool_name) GLOB '*write*'
                                   OR lower(tc.tool_name) GLOB '*edit*'
                                   OR lower(tc.tool_name) GLOB '*patch*'
                              THEN 1 ELSE 0 END) AS mutation_calls,
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
              FROM execution_unit_steps eus
              JOIN tool_calls tc ON tc.step_id = eus.step_id
              WHERE eus.execution_unit_id IN ({placeholders})
              GROUP BY eus.execution_unit_id
            ), retry_summary AS (
              SELECT execution_unit_id, SUM(call_count) AS identical_retry_calls
              FROM (
                SELECT eus.execution_unit_id, COUNT(*) AS call_count
                FROM execution_unit_steps eus
                JOIN tool_calls tc ON tc.step_id = eus.step_id
                WHERE eus.execution_unit_id IN ({placeholders})
                  AND NULLIF(tc.input_hash, '') IS NOT NULL
                GROUP BY eus.execution_unit_id, tc.tool_name, tc.input_hash
                HAVING COUNT(*) >= 3
              )
              GROUP BY execution_unit_id
            ), token_summary AS (
              SELECT eus.execution_unit_id,
                     SUM(COALESCE(llm.input_tokens, 0)
                         + COALESCE(llm.output_tokens, 0)
                         + COALESCE(llm.cache_read_input_tokens, 0)
                         + COALESCE(llm.cache_creation_input_tokens, 0)
                         + COALESCE(llm.reasoning_output_tokens, 0))
                       AS tokens
              FROM execution_unit_steps eus
              JOIN llm_calls llm ON llm.step_id = eus.step_id
              WHERE eus.execution_unit_id IN ({placeholders})
              GROUP BY eus.execution_unit_id
            )
            SELECT eu.id, lower(COALESCE(eu.status, '')), eu.outcome,
                   COALESCE(ts.tool_calls, 0), COALESCE(ts.failed_calls, 0),
                   COALESCE(ts.read_calls, 0), COALESCE(ts.mutation_calls, 0),
                   COALESCE(ts.verification_calls, 0),
                   COALESCE(ts.constraint_violations, 0),
                   COALESCE(rs.identical_retry_calls, 0), toks.tokens
            FROM execution_units eu
            LEFT JOIN tool_summary ts ON ts.execution_unit_id = eu.id
            LEFT JOIN retry_summary rs ON rs.execution_unit_id = eu.id
            LEFT JOIN token_summary toks ON toks.execution_unit_id = eu.id
            WHERE eu.id IN ({placeholders})
            """,
            [
                *execution_unit_ids,
                *execution_unit_ids,
                *execution_unit_ids,
                *execution_unit_ids,
            ],
        ).fetchall()
        values: dict[str, float] = {}
        for row in rows:
            execution_unit_id = str(row[0])
            tool_calls = int(row[3])
            outcome = str(row[2] or "").lower()
            if metric_name == "tool_failure_rate":
                if not tool_calls:
                    continue
                value = float(row[4]) / tool_calls
            elif metric_name == "context_outlier_sessions":
                if row[10] is None:
                    continue
                value = float(row[10])
            elif metric_name == "identical_retry_calls":
                value = float(row[9])
            elif metric_name == "unverified_change_sessions":
                value = float(bool(row[6]) and not bool(row[7]))
            elif metric_name == "read_only_exploration_calls":
                value = float(row[5]) if int(row[5]) >= 8 and not row[6] else 0.0
            elif metric_name == "operator_correction_rate":
                value = float(outcome == "corrected")
            elif metric_name == "correct_no_change_sessions":
                value = float(outcome == "no-change-correct")
            elif metric_name == "constraint_violation_rate":
                value = float(row[8])
            elif metric_name == "recovered_failure_sessions":
                value = float(int(row[4]) > 0 and row[1] in {"completed", "ok", "success"})
            elif metric_name == "successful_workflow_sessions":
                value = float(int(row[4]) == 0 and row[1] in {"completed", "ok", "success"})
            else:
                value = float(row[4])
            values[execution_unit_id] = value
        return values

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
