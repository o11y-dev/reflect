"""Read-only validation of the exact execution units behind a measurement."""
from __future__ import annotations

import sqlite3
from typing import Any

_ELIGIBLE = """
    COALESCE(eu.repo_id, '') = COALESCE(:repo_id, '')
    AND (:workspace_id IS NULL OR eu.workspace_id = :workspace_id)
    AND (:task_archetype_id IS NULL OR ea.task_archetype_id = :task_archetype_id)
    AND eu.eligible = 1 AND ea.mixed = 0
    AND lower(COALESCE(eu.status, '')) IN ('completed', 'ok', 'success')
    AND CASE WHEN :before THEN eu.started_at < :cutoff ELSE eu.started_at >= :cutoff END
"""


class MeasurementValidity:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def select_ids(
        self, cohort: dict[str, Any], cutoff: str, *, before: bool,
        limit: int, newest_first: bool = False,
    ) -> list[str]:
        order = "DESC" if newest_first else "ASC"
        rows = self.conn.execute(
            f"""SELECT eu.id FROM execution_units eu
                JOIN execution_unit_archetypes ea ON ea.execution_unit_id = eu.id
                WHERE {_ELIGIBLE}
                ORDER BY eu.started_at {order}, eu.id {order} LIMIT :limit""",
            {**cohort, "cutoff": cutoff, "before": before, "limit": max(1, min(limit, 100))},
        )
        ids = [row[0] for row in rows]
        return list(reversed(ids)) if newest_first else ids

    def invalid_members(self, cohort: dict[str, Any], exposed_at: str) -> dict[str, list[str]]:
        before = cohort.get("before_execution_unit_ids") or []
        after = cohort.get("after_execution_unit_ids") or []
        ids = list(dict.fromkeys([*before, *after]))
        if not ids:
            return {}
        params = {key: cohort.get(key) for key in ("repo_id", "workspace_id", "task_archetype_id")}
        params.update({f"unit{i}": unit_id for i, unit_id in enumerate(ids)})
        params.update(cutoff=exposed_at, before=True)
        placeholders = ','.join(f':unit{i}' for i in range(len(ids)))
        rows = self.conn.execute(
            f"""SELECT eu.id, eu.eligible, ea.mixed, eu.repo_id, eu.workspace_id,
                       ea.task_archetype_id, eu.status, eu.started_at,
                       ({_ELIGIBLE}) AS eligible_before,
                       ({_ELIGIBLE.replace(':before', '0')}) AS eligible_after
                FROM execution_units eu
                LEFT JOIN execution_unit_archetypes ea ON ea.execution_unit_id = eu.id
                WHERE eu.id IN ({placeholders})""", params,
        )
        by_id = {row[0]: row for row in rows}
        invalid = {}
        for unit_id in ids:
            row = by_id.get(unit_id)
            if row is not None and row[8 if unit_id in before else 9]:
                continue
            reasons = []
            if row is None:
                reasons.append("Execution unit is missing.")
            else:
                if not row[1]:
                    reasons.append("Execution unit is no longer eligible.")
                if row[2] is None or row[2] != 0:
                    reasons.append("Archetype is missing or mixed.")
                if (row[3] or "") != (cohort.get("repo_id") or ""):
                    reasons.append("Repository no longer matches.")
                for key, value in (("workspace_id", row[4]), ("task_archetype_id", row[5])):
                    if cohort.get(key) is not None and cohort[key] != value:
                        reasons.append(f"{key} no longer matches.")
                if str(row[6] or "").lower() not in {"completed", "ok", "success"}:
                    reasons.append("Execution unit is not completed.")
                if not exposed_at or not row[7] or (
                    unit_id in before and row[7] >= exposed_at
                ) or (unit_id in after and row[7] < exposed_at):
                    reasons.append("Execution unit does not match the exposure-time boundary.")
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
