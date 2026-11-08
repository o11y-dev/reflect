"""Read-only validation of the exact execution units behind a measurement."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from reflect.telemetry_facts import event_time

# Shared by selection, frozen-ledger validation and guidance comparison. Deferred
# analytics are pending evidence too; a graph-only backlog does not affect this.
EXECUTION_INPUTS_CURRENT_SQL = """
    NOT EXISTS (
      SELECT 1 FROM pending_refresh pending
      WHERE pending.session_id = eu.session_id
        AND pending.phase IN ('canonical', 'cost')
    )
"""
EXECUTION_EVIDENCE_CURRENT_SQL = f"""
    ({EXECUTION_INPUTS_CURRENT_SQL}) AND NOT EXISTS (
      SELECT 1 FROM pending_refresh pending
      WHERE pending.session_id = eu.session_id AND pending.phase = 'improvements'
    )
"""

_ELIGIBLE = f"""
    COALESCE(eu.repo_id, '') = COALESCE(:repo_id, '')
    AND (:workspace_id IS NULL OR eu.workspace_id = :workspace_id)
    AND (:task_archetype_id IS NULL OR ea.task_archetype_id = :task_archetype_id)
    AND eu.eligible = 1 AND ea.mixed = 0 AND ea.confidence >= 0.65
    AND {EXECUTION_EVIDENCE_CURRENT_SQL}
    AND lower(COALESCE(eu.status, '')) IN ('completed', 'ok', 'success')
    AND CASE WHEN :before THEN eu.started_at < :cutoff ELSE eu.started_at >= :cutoff END
"""

COMPARISON_POLICY_VERSION = 2
_OBSERVED_METRICS = {
    "tool_failure_rate", "identical_retry_calls", "context_outlier_sessions",
    "read_only_exploration_calls", "unverified_change_sessions", "constraint_violation_rate",
}


def _time_comparison(left: str, operator: str, right: str) -> str:
    """Offset-aware SQL comparison, excluding ambiguous sub-ms boundaries.

    SQLite rounds fractional timestamps to milliseconds. A two-ms separation
    safely exceeds rounding/float error; whole-second or identically encoded
    instants can also establish equality without losing precision.
    """
    direction = "<" if operator in {"<", "<="} else ">"
    margin = "-" if direction == "<" else "+"
    exact_equal = (
        f"OR replace({left}, 'Z', '+00:00') = replace({right}, 'Z', '+00:00')"
        if operator in {"<=", ">="} else ""
    )
    return f"""(
        julianday({left}) {direction} julianday({right}) {margin} (0.002 / 86400.0)
        OR (substr({left}, 20, 1) != '.' AND substr({right}, 20, 1) != '.'
            AND julianday({left}) {operator} julianday({right}))
        {exact_equal}
    )"""


def _policy_gates(cohort: dict[str, Any]) -> list[tuple[str, str]]:
    """Ordered exclusions shared by selection, diagnostics and revalidation."""
    gates = [
        ("evidence_pending", EXECUTION_EVIDENCE_CURRENT_SQL),
        ("archetype_missing", "ea.execution_unit_id IS NOT NULL"),
        ("archetype_mismatch", "(:task_archetype_id IS NULL OR ea.task_archetype_id = :task_archetype_id)"),
        ("archetype_mixed", "ea.mixed = 0"),
        ("archetype_low_confidence", "ea.confidence >= 0.65"),
        ("boundary_unobserved", f"""json_extract(eu.boundary_json, '$.assessment.boundary') = 'closed'
            AND eu.ended_at IS NOT NULL AND {_time_comparison('eu.ended_at', '>=', 'eu.started_at')}
            AND lower(COALESCE(eu.status, '')) NOT IN ('running', 'pending', 'in_progress', 'started')"""),
        ("activation_time_ambiguous", f"""CASE WHEN :before
            THEN {_time_comparison('eu.started_at', '<', ':cutoff')}
            ELSE {_time_comparison('eu.started_at', '>=', ':cutoff')} END"""),
        ("activation_overlap", f"NOT :before OR {_time_comparison('eu.ended_at', '<=', ':cutoff')}"),
    ]
    if cohort.get("metric") not in _OBSERVED_METRICS:
        gates.append(("outcome_unobserved", """eu.outcome IN ('success', 'failure', 'corrected', 'no-change-correct')
            AND eu.verification_passed IS NOT NULL"""))
    return gates


def _eligibility_sql(cohort: dict[str, Any]) -> str:
    if cohort.get("policy_version", 1) < COMPARISON_POLICY_VERSION:
        return _ELIGIBLE
    return """
        COALESCE(eu.repo_id, '') = COALESCE(:repo_id, '')
        AND (:workspace_id IS NULL OR eu.workspace_id = :workspace_id)
        AND eu.eligible = 1
        AND CASE WHEN :before THEN julianday(eu.started_at) < julianday(:cutoff)
                 ELSE julianday(eu.started_at) >= julianday(:cutoff) END
        AND """ + " AND ".join(f"({predicate})" for _, predicate in _policy_gates(cohort))


class MeasurementValidity:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def select_ids(
        self, cohort: dict[str, Any], cutoff: str, *, before: bool,
        limit: int, newest_first: bool = False,
    ) -> list[str]:
        order = "DESC" if newest_first else "ASC"
        started = "julianday(eu.started_at)" if cohort.get("policy_version", 1) >= COMPARISON_POLICY_VERSION else "eu.started_at"
        rows = self.conn.execute(
            f"""SELECT eu.id FROM execution_units eu
                JOIN execution_unit_archetypes ea ON ea.execution_unit_id = eu.id
                WHERE {_eligibility_sql(cohort)}
                ORDER BY {started} {order}, eu.id {order} LIMIT :limit""",
            {**cohort, "cutoff": cutoff, "before": before, "limit": max(1, min(limit, 100))},
        )
        ids = [row[0] for row in rows]
        return list(reversed(ids)) if newest_first else ids

    def diagnostics(self, cohort: dict[str, Any], cutoff: str, *, before: bool) -> dict[str, Any]:
        """Count active canonical candidates without loading their telemetry JSON."""
        cases = " ".join(
            f"WHEN NOT COALESCE(({predicate}), 0) THEN '{reason}'"
            for reason, predicate in _policy_gates(cohort)
        )
        rows = self.conn.execute(
            f"""SELECT CASE {cases} ELSE 'eligible' END AS reason, COUNT(*)
                FROM execution_units eu
                LEFT JOIN execution_unit_archetypes ea ON ea.execution_unit_id = eu.id
                WHERE COALESCE(eu.repo_id, '') = COALESCE(:repo_id, '')
                  AND (:workspace_id IS NULL OR eu.workspace_id = :workspace_id)
                  AND eu.eligible = 1
                  AND CASE WHEN :before THEN julianday(eu.started_at) < julianday(:cutoff)
                           ELSE julianday(eu.started_at) >= julianday(:cutoff) END
                GROUP BY reason""",
            {**cohort, "cutoff": cutoff, "before": before},
        )
        counts = dict(rows)
        eligible = counts.pop("eligible", 0)
        excluded = sum(counts.values())
        return {"candidate_count": eligible + excluded, "eligible_count": eligible,
                "excluded_count": excluded, "exclusion_reasons": counts}

    def invalid_members(self, cohort: dict[str, Any], exposed_at: str) -> dict[str, list[str]]:
        before = cohort.get("before_execution_unit_ids") or []
        after = cohort.get("after_execution_unit_ids") or []
        ids = list(dict.fromkeys([*before, *after]))
        if not ids:
            return {}
        if cohort.get("assessment_version") == 0:
            return {unit_id: ["Recorded comparison predates independent execution assessment."] for unit_id in ids}
        params = {key: cohort.get(key) for key in ("repo_id", "workspace_id", "task_archetype_id")}
        params.update({f"unit{i}": unit_id for i, unit_id in enumerate(ids)})
        params.update(cutoff=exposed_at, before=True)
        placeholders = ','.join(f':unit{i}' for i in range(len(ids)))
        eligible_sql = _eligibility_sql(cohort)
        rows = self.conn.execute(
            f"""SELECT eu.id, eu.eligible, ea.mixed, eu.repo_id, eu.workspace_id,
                       ea.task_archetype_id, eu.status, eu.started_at,
                       ({eligible_sql}) AS eligible_before,
                       ({eligible_sql.replace(':before', '0')}) AS eligible_after, eu.boundary_json,
                       ({EXECUTION_EVIDENCE_CURRENT_SQL}) AS evidence_current,
                       eu.ended_at, eu.outcome, eu.verification_passed, ea.confidence
                FROM execution_units eu
                LEFT JOIN execution_unit_archetypes ea ON ea.execution_unit_id = eu.id
                WHERE eu.id IN ({placeholders})""", params,
        )
        by_id = {row[0]: row for row in rows}
        invalid = {}
        for unit_id in ids:
            row = by_id.get(unit_id)
            expected_digest = (cohort.get("evidence_digests") or {}).get(unit_id)
            current_digest = json.loads(row[10] or "{}").get("assessment", {}).get("input_digest") if row is not None else None
            digest_changed = bool(expected_digest) and expected_digest != current_digest
            if row is not None and row[8 if unit_id in before else 9] and not digest_changed:
                continue
            reasons = ["Observed evidence changed after this measurement."] if digest_changed else []
            if row is None:
                reasons.append("Execution unit is missing.")
            else:
                if not row[11]:
                    reasons.append("Execution evidence refresh is pending.")
                if not row[1]:
                    reasons.append("Execution unit is no longer eligible.")
                if row[2] is None or row[2] != 0:
                    reasons.append("Archetype is missing or mixed.")
                if row[15] is not None and row[15] < 0.65:
                    reasons.append("Archetype confidence is below the comparison threshold.")
                if (row[3] or "") != (cohort.get("repo_id") or ""):
                    reasons.append("Repository no longer matches.")
                for key, value in (("workspace_id", row[4]), ("task_archetype_id", row[5])):
                    if cohort.get(key) is not None and cohort[key] != value:
                        reasons.append(f"{key} no longer matches.")
                if cohort.get("policy_version", 1) < COMPARISON_POLICY_VERSION:
                    if str(row[6] or "").lower() not in {"completed", "ok", "success"}:
                        reasons.append("Execution unit is not completed.")
                else:
                    assessment = json.loads(row[10] or "{}").get("assessment", {})
                    started, ended, cutoff = event_time(row[7]), event_time(row[12]), event_time(exposed_at)
                    if (assessment.get("boundary") != "closed" or not ended or not started
                            or ended < started
                            or str(row[6]).lower() in {"running", "pending", "in_progress", "started"}):
                        reasons.append("Execution boundary is not observably closed.")
                    if unit_id in before and ended and cutoff and ended > cutoff:
                        reasons.append("Execution overlaps workflow activation.")
                    if cohort.get("metric") not in _OBSERVED_METRICS and (
                        row[13] not in {"success", "failure", "corrected", "no-change-correct"}
                        or row[14] is None
                    ):
                        reasons.append("Independent outcome and verification evidence are unavailable.")
                started, cutoff = (event_time(row[7]), event_time(exposed_at)) if cohort.get("policy_version", 1) >= COMPARISON_POLICY_VERSION else (row[7], exposed_at)
                if not cutoff or not started or (
                    unit_id in before and started >= cutoff
                ) or (unit_id in after and started < cutoff):
                    reasons.append("Execution unit does not match the exposure-time boundary.")
            if not reasons:
                reasons.append("Recorded times cannot be ordered safely at SQLite timestamp precision."
                               if cohort.get("policy_version", 1) >= COMPARISON_POLICY_VERSION
                               else "Archetype confidence is below the comparison threshold.")
            if reasons:
                invalid[unit_id] = reasons
        return invalid

    def apply(self, result: dict[str, Any], cohort: dict[str, Any], exposed_at: str) -> dict[str, Any]:
        invalid = self.invalid_members(cohort, exposed_at)
        if not invalid:
            return result
        return {
            **result,
            "recorded_result": {key: result.get(key) for key in (
                "before_value", "after_value", "delta", "verdict", "confidence",
            )},
            "before_value": None, "after_value": None, "delta": None,
            "verdict": "insufficient_data", "confidence": 0.0,
            "cohort": {
                **result.get("cohort", {}),
                "measurement_state": "not_measurable",
                "measurement_reasons": ["Recorded comparison members are no longer eligible."],
                "invalid_members": invalid,
            },
        }
