from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import uuid
from collections.abc import Iterator
from typing import Any

from reflect.improvements.contracts import ContractEvaluationService, WorkflowContract
from reflect.improvements.measurement_validity import COMPARISON_POLICY_VERSION, MeasurementValidity
from reflect.improvements.repository import utc_now
from reflect.usage_facts import usage_facts

_RECOVERY_METRICS = {"tool_failure_rate", "identical_retry_calls"}
_MINIMUM_SIGNAL_COVERAGE = 0.9


def _public_cohort(raw: str) -> dict[str, Any]:
    cohort = json.loads(raw)
    cohort.pop("before_execution_unit_ids", None)
    cohort.pop("after_execution_unit_ids", None)
    cohort.pop("fingerprint", None)
    snapshots = cohort.pop("execution_unit_snapshots", None) or cohort.get("superseded_token_comparison", {}).get("snapshots") or {}
    cohort.pop("superseded_token_comparison", None)
    cohort["periods"] = {}
    cohort["metric_distribution"] = {}
    for side in ("before", "after"):
        units = snapshots.get(side, [])
        starts = [str(unit["started_at"]) for unit in units if unit.get("started_at")]
        ends = [str(unit.get("ended_at") or unit.get("started_at")) for unit in units
                if unit.get("ended_at") or unit.get("started_at")]
        cohort["periods"][side] = {"start": min(starts) if starts else None, "end": max(ends) if ends else None}
        values = [unit["metric_value"] for unit in units if unit.get("metric_value") is not None]
        cohort["metric_distribution"][side] = {
            "metric_count": len(values), "nonzero_count": sum(value != 0 for value in values),
        }
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
        "assessment_version": cohort.get("assessment_version"),
        "evidence_digests": cohort.get("evidence_digests"),
        "invalid_members": cohort.get("invalid_members", {}),
        "policy_version": cohort.get("policy_version"),
        "baseline_state": cohort.get("baseline_state"),
        "exposure": cohort.get("exposure"),
        "eligibility": cohort.get("eligibility"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MeasurementService:
    """Compute conservative before/after results for active interventions."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.contracts = ContractEvaluationService(conn)
        self.validity = MeasurementValidity(conn)

    def measure(self, candidate_id: str, *, skip_unchanged: bool = False,
                reassess: bool = False, commit: bool = True) -> dict[str, Any]:
        """Append a comparison; explicit reassessment renews evidence, never membership.

        Reassessment is used by numbered assessment migrations. Ordinary refreshes
        continue to validate the pinned evidence and cannot silently renew it.
        """
        row = self.conn.execute(
            """
            SELECT i.id, i.exposure_started_at, o.repo_id, wc.target_metric,
                   wc.task_archetype_id, o.metric_direction, wc.content_json,
                   wc.measurement_window, wc.provenance_json
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
            provenance_json,
        ) = row
        content = json.loads(str(content_json or "{}"))
        contract = WorkflowContract.from_raw(content.get("workflow_contract"))
        contract_mode = contract is not None and str(metric_name) == "workflow_adherence"
        recovery_mode = (
            str(content.get("behavior_type") or "") == "recovery"
            and str(metric_name) in _RECOVERY_METRICS
        )
        previous = self.conn.execute(
            """
            SELECT id, before_count, after_count, verdict, measured_at, cohort_json
            FROM measurements
            WHERE intervention_id = ? AND metric_name = ?
            ORDER BY measured_at DESC LIMIT 1
            """,
            (intervention_id, metric_name),
        ).fetchone()
        previous_cohort = json.loads(previous[5]) if previous else {}
        latest = previous if previous_cohort.get("policy_version") == COMPARISON_POLICY_VERSION else None
        latest_cohort = previous_cohort if latest else {}
        quality: dict[str, Any] | None = None
        provenance = json.loads(str(provenance_json or "{}"))
        workspace_id = (contract.applicability.workspace_id if contract is not None else None) or provenance.get("finding_workspace_id")
        minimum_before = (
            contract.validation.baseline_minimum if contract is not None else 5
        )
        baseline_limit = (
            contract.validation.baseline_maximum if contract is not None else 20
        )
        minimum_after = max(1, min(int(measurement_window), 100))
        cohort_scope = {
            "assessment_version": 1, "policy_version": COMPARISON_POLICY_VERSION,
            "repo_id": repo_id, "workspace_id": workspace_id,
            "task_archetype_id": archetype_id, "metric": str(metric_name),
            "scope_unavailable": bool(provenance.get("finding_scope_unavailable")),
        }
        eligibility = {
            side: self.validity.diagnostics(cohort_scope, exposed_at, before=side == "before")
            for side in ("before", "after")
        }
        frozen_before_ids = [
            str(item) for item in latest_cohort.get("before_execution_unit_ids") or []
        ] if latest_cohort.get("baseline_frozen") else []
        before_units = (
            self._execution_units_by_ids(frozen_before_ids)
            if frozen_before_ids
            else self._execution_units_by_ids(self.validity.select_ids(
                cohort_scope, exposed_at, before=True, limit=baseline_limit, newest_first=True,
            ))
        )
        frozen_after_ids = [str(item) for item in latest_cohort.get("after_execution_unit_ids") or []]
        if len(frozen_after_ids) < minimum_after:
            frozen_after_ids = []
        after_units = (
            self._execution_units_by_ids(frozen_after_ids)
            if frozen_after_ids
            else self._execution_units_by_ids(self.validity.select_ids(
                cohort_scope, exposed_at, before=False, limit=minimum_after,
            ))
        )
        eligibility_cohort = {
            **cohort_scope,
            "evidence_digests": {
                unit_id: digest for unit_id, digest in latest_cohort.get("evidence_digests", {}).items()
                if not reassess and unit_id in {*frozen_before_ids, *frozen_after_ids}
            },
            "before_execution_unit_ids": frozen_before_ids or [
                item["execution_unit_id"] for item in before_units
            ],
            "after_execution_unit_ids": frozen_after_ids or [item["execution_unit_id"] for item in after_units],
        }
        invalid_members = self.validity.invalid_members(eligibility_cohort, exposed_at)
        if contract_mode and contract is not None:
            evidence = self._execution_units_with_contract_evidence(contract, [*before_units, *after_units])
            metric_values = {item["execution_unit_id"]: item["metric_value"] for item in evidence}
            before = [metric_values[item["execution_unit_id"]] for item in before_units]
            after = [metric_values[item["execution_unit_id"]] for item in after_units]
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
            metric_values = {**before_values, **after_values}
            evidence = [
                {**item, "metric_value": metric_values.get(item["execution_unit_id"]),
                 "evidence_count": int(item["execution_unit_id"] in metric_values),
                 "evidence_summaries": [f"{metric_name}: {metric_values[item['execution_unit_id']]:.3g}"]
                 if item["execution_unit_id"] in metric_values else ["Required execution-unit telemetry is unavailable"]}
                for item in [*before_units, *after_units]
            ]
            if recovery_mode or metric_name == "context_outlier_sessions":
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
        for side, values in (("before", before), ("after", after)):
            selected_count = len(eligibility_cohort[f"{side}_execution_unit_ids"])
            eligibility[side].update(selected_count=selected_count, metric_count=len(values),
                                     missing_metric_count=selected_count - len(values))
        baseline_frozen = bool(frozen_before_ids) or (len(before) >= minimum_before and not invalid_members)
        if baseline_frozen:
            baseline_state = "frozen"
        elif eligibility["before"]["exclusion_reasons"].get("evidence_pending"):
            baseline_state = "analysis_pending"
        else:
            baseline_state = "historical_unavailable"
        exposure = {
            side: self._exposure_counts(intervention_id, eligibility_cohort[f"{side}_execution_unit_ids"])
            for side in ("before", "after")
        }
        if invalid_members:
            quality = {"state": "not_measurable", "reasons": [
                "Frozen comparison contains missing or ineligible execution units; no replacements were selected."
            ]}
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
        if measurement_state == "measured" and not sample_ready:
            measurement_state = "collecting"
        if baseline_state == "historical_unavailable":
            measurement_state = "not_measurable"
        reasons = list(quality["reasons"]) if quality is not None else []
        if cohort_scope["scope_unavailable"]:
            reasons.append("The finding has no resolved repository or unique workspace; impact cannot be measured across unrelated tasks.")
        if baseline_state == "historical_unavailable":
            reasons.append("Selected pre-activation evidence does not currently supply the required baseline; future post-activation tasks cannot fill it.")
        elif baseline_state == "analysis_pending":
            reasons.append("Pre-activation evidence is awaiting analysis; the baseline remains provisional.")
        if quality is not None:
            quality = {**quality, "state": measurement_state, "reasons": reasons}
        before_value = (
            raw_before_value
            if not invalid_members and (not recovery_mode or measurement_state == "measured")
            else None
        )
        after_value = (
            raw_after_value
            if not invalid_members and (not recovery_mode or measurement_state == "measured")
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
            **({"scope_unavailable": True} if cohort_scope["scope_unavailable"] else {}),
            "policy_version": COMPARISON_POLICY_VERSION,
            "previous_policy_measurement_id": latest_cohort.get("previous_policy_measurement_id")
            if latest else previous[0] if previous else None,
            "baseline_state": baseline_state,
            "baseline_frozen": baseline_frozen,
            "eligibility": eligibility,
            "exposure": exposure,
            "assessment_version": eligibility_cohort["assessment_version"],
            "evidence_digests": {
                **{item["execution_unit_id"]: item.get("assessment", {}).get("input_digest")
                   for item in [*before_units, *after_units]},
                **eligibility_cohort["evidence_digests"],
            },
            "repo_id": repo_id,
            "workspace_id": workspace_id,
            "task_archetype_id": archetype_id,
            "metric": metric_name,
            "before": f"comparable tasks before {exposed_at}",
            "after": f"first {minimum_after} comparable tasks on or after {exposed_at}",
            "minimum_after_execution_units": minimum_after,
            "minimum_before_execution_units": minimum_before,
            "metric_direction": metric_direction,
            "before_execution_unit_ids": eligibility_cohort["before_execution_unit_ids"],
            "after_execution_unit_ids": eligibility_cohort["after_execution_unit_ids"],
            "contract_signature": (
                contract.signature_hash if contract is not None else None
            ),
            "measurement_state": measurement_state,
            "exposure_started_at": exposed_at,
            "invalid_members": invalid_members,
            "execution_unit_snapshots": {
                side: [
                    {**item, "eligible_at_measurement": item["execution_unit_id"] not in invalid_members}
                    for item in evidence if item["execution_unit_id"] in eligibility_cohort[f"{side}_execution_unit_ids"]
                ] for side in ("before", "after")
            },
        }
        if quality is not None:
            cohort["quality"] = quality
        if reasons:
            cohort["measurement_reasons"] = reasons
        if reassess and latest:
            cohort["reassessed_from_measurement_id"] = latest[0]
        elif latest_cohort.get("reassessed_from_measurement_id"):
            cohort["reassessed_from_measurement_id"] = latest_cohort["reassessed_from_measurement_id"]
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
                "cohort": latest_cohort,
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
        if commit:
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

    def _exposure_counts(self, intervention_id: str, execution_unit_ids: list[str]) -> dict[str, int]:
        counts = dict.fromkeys(("followed", "invoked", "ignored", "unknown"), 0)
        if execution_unit_ids:
            placeholders = ",".join("?" for _ in execution_unit_ids)
            rows = self.conn.execute(
                f"""SELECT state, COUNT(*) FROM workflow_exposures
                    WHERE intervention_id = ? AND execution_unit_id IN ({placeholders}) GROUP BY state""",
                [intervention_id, *execution_unit_ids],
            )
            for state, count in rows:
                counts[state if state in counts else "unknown"] += count
        counts["unknown"] += len(execution_unit_ids) - sum(counts.values())
        return {"selected_count": len(execution_unit_ids), **counts}

    def list(
        self, *, limit: int | None = 100, candidate_id: str | None = None,
        measurement_id: str | None = None, recorded_only: bool = False,
    ) -> list[dict[str, Any]]:
        return list(self.iter_results(limit=limit, candidate_id=candidate_id,
                                      measurement_id=measurement_id, recorded_only=recorded_only))

    def iter_results(
        self, *, limit: int | None = None, candidate_id: str | None = None,
        measurement_id: str | None = None, recorded_only: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Read saved comparisons; recorded-only callers label validation pending."""
        rows = self.conn.execute(
            """
            SELECT m.id, wv.candidate_id, m.metric_name, m.cohort_json,
                   m.before_value, m.after_value, m.before_count, m.after_count,
                   m.delta, m.verdict, m.confidence, m.measured_at, i.exposure_started_at, wv.id, wv.version, i.id
            FROM measurements m
            JOIN interventions i ON i.id = m.intervention_id
            JOIN workflow_versions wv ON wv.id = i.workflow_version_id
            WHERE (? IS NULL OR wv.candidate_id = ?)
              AND (? IS NULL OR m.id = ?)
            ORDER BY m.measured_at DESC LIMIT ?
            """,
            (candidate_id, candidate_id, measurement_id, measurement_id,
             -1 if limit is None else max(1, min(limit, 500))),
        )
        for row in rows:
            result = {
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
                "workflow_version_id": row[13],
                "workflow_version": row[14],
                "intervention_id": row[15],
            }
            yield result if recorded_only else self.validity.apply(result, json.loads(row[3]), row[12])

    def sessions(self, measurement_id: str, *, recorded_only: bool = False) -> dict[str, Any]:
        """Return the bounded execution-unit cohorts in a measurement snapshot."""
        row = self.conn.execute(
            """
            SELECT m.id, wv.candidate_id, m.metric_name, m.cohort_json,
                   m.before_count, m.after_count, i.exposure_started_at,
                   o.repo_id, wc.task_archetype_id, wv.content_json, m.measured_at
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
            measured_at,
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
        snapshots = cohort.get("execution_unit_snapshots")
        if snapshots is not None:
            before_execution_units = snapshots["before"]
            after_execution_units = snapshots["after"]
        elif recorded_only:
            # Older measurements froze membership but not the member details.
            # Current unit rows cannot reconstruct their historical outcomes.
            before_execution_units, after_execution_units = [
                [{"execution_unit_id": unit_id, "snapshot_missing": True, "metric_value": None,
                  "evidence_summaries": ["Member details were not frozen in this measurement."]}
                 for unit_id in ids]
                for ids in (before_execution_ids, after_execution_ids)
            ]
        else:
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
        invalid = None if recorded_only else self.validity.invalid_members(cohort, exposed_at)
        if recorded_only:
            before_execution_units, after_execution_units = [
                [{**item, "outcome": None, "verification_passed": None,
                  "assessment": None, "adherence_state": None} for item in units]
                for units in (before_execution_units, after_execution_units)
            ]
        return {
            "id": stored_id,
            "candidate_id": candidate_id,
            "metric_name": metric_name,
            "unit": "execution_units",
            "measured_at": measured_at,
            "measurement_state": "validation_pending" if recorded_only else "not_measurable" if invalid else cohort.get("measurement_state", "collecting"),
            "measurement_reasons": ["Current validation is pending; only recorded cohort evidence is shown."]
            if recorded_only else ["Recorded comparison members are no longer eligible."]
            if invalid else cohort.get("measurement_reasons", []),
            "invalid_members": invalid,
            "quality": cohort.get("quality", {}),
            "policy_version": cohort.get("policy_version", 1),
            "baseline_state": cohort.get("baseline_state"),
            "minimum_before_execution_units": cohort.get("minimum_before_execution_units"),
            "minimum_after_execution_units": cohort.get("minimum_after_execution_units"),
            "eligibility": cohort.get("eligibility"),
            "exposure": cohort.get("exposure"),
            "before_count": int(before_count),
            "after_count": int(after_count),
            "snapshot_exact": snapshots is not None,
            "missing_snapshot_execution_unit_ids": [*before_execution_ids, *after_execution_ids]
            if recorded_only and snapshots is None else [],
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
                   eu.outcome, eu.verification_passed, eu.boundary_json
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
                "assessment": json.loads(item[11] or "{}").get("assessment", {}),
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

        # Adherence requires the contract evaluator; tool failures are not a proxy.
        if not execution_unit_ids or metric_name == "workflow_adherence":
            return {}
        placeholders = ", ".join("?" for _ in execution_unit_ids)
        if metric_name == "context_outlier_sessions":
            totals: dict[str, float] = {}
            missing: set[str] = set()
            seen: set[tuple[str, tuple[Any, ...]]] = set()
            cursor = self.conn.execute(
                f"""SELECT eus.execution_unit_id, llm.*
                    FROM execution_unit_steps eus
                    JOIN llm_calls llm ON llm.step_id = eus.step_id
                    WHERE eus.execution_unit_id IN ({placeholders})""",
                execution_unit_ids,
            )
            columns = [column[0] for column in cursor.description]
            for row in cursor:
                record = dict(zip(columns, row, strict=True))
                unit_id = str(record["execution_unit_id"])
                attrs = json.loads(record["raw_attrs_json"] or "{}")
                facts = usage_facts(record, attrs)
                # Legacy positive buckets are observed; default zero columns are not.
                # Inclusive input cannot fall back when its cache split is unknown.
                complete = all(
                    facts.buckets[bucket] is not None or (
                        attrs.get("reflect.usage.input_semantics") != "inclusive"
                        and (record[column] or 0) > 0
                    )
                    for bucket, column in (
                        ("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                        ("cache_read_tokens", "cache_read_input_tokens"),
                        ("cache_write_tokens", "cache_creation_input_tokens"),
                    )
                )
                if not complete:
                    missing.add(unit_id)
                    continue
                identity = (unit_id, facts.identity)
                if identity in seen:
                    continue
                seen.add(identity)
                totals[unit_id] = totals.get(unit_id, 0.0) + sum(
                    value for bucket, value in facts.tokens.items()
                    if bucket != "reasoning_output_tokens"
                )
            return {unit_id: value for unit_id, value in totals.items() if unit_id not in missing}
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
            )
            SELECT eu.id, lower(COALESCE(eu.status, '')), eu.outcome,
                   COALESCE(ts.tool_calls, 0), COALESCE(ts.failed_calls, 0),
                   COALESCE(ts.read_calls, 0), COALESCE(ts.mutation_calls, 0),
                   COALESCE(ts.verification_calls, 0),
                   COALESCE(ts.constraint_violations, 0),
                   COALESCE(rs.identical_retry_calls, 0), eu.verification_passed, eu.boundary_json
            FROM execution_units eu
            LEFT JOIN tool_summary ts ON ts.execution_unit_id = eu.id
            LEFT JOIN retry_summary rs ON rs.execution_unit_id = eu.id
            WHERE eu.id IN ({placeholders})
            """,
            [
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
            elif metric_name == "identical_retry_calls":
                value = float(row[9])
            elif metric_name == "unverified_change_sessions":
                value = float(bool(row[6]) and row[10] is not True and row[10] != 1)
            elif metric_name == "read_only_exploration_calls":
                value = float(row[5]) if int(row[5]) >= 8 and not row[6] else 0.0
            elif metric_name == "operator_correction_rate":
                value = float(outcome == "corrected")
            elif metric_name == "correct_no_change_sessions":
                value = float(outcome == "no-change-correct")
            elif metric_name == "constraint_violation_rate":
                value = float(row[8])
            elif metric_name == "recovered_failure_sessions":
                if outcome not in {"success", "failure"} or row[10] is None:
                    continue
                value = float(int(row[4]) > 0 and outcome == "success" and bool(row[10]))
            elif metric_name == "successful_workflow_sessions":
                if outcome not in {"success", "failure"} or row[10] is None:
                    continue
                value = float(outcome == "success" and bool(row[10]))
            else:
                # Unknown finding metrics have no execution-unit measurement.
                continue
            values[execution_unit_id] = value
        return values
