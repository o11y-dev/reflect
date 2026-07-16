from __future__ import annotations

import json
import sqlite3
import statistics
import uuid
from typing import Any

from reflect.improvements.repository import utc_now


class MeasurementService:
    """Compute conservative before/after results for active interventions."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def measure(self, candidate_id: str, *, skip_unchanged: bool = False) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT i.id, i.exposure_started_at, o.repo_id, wc.target_metric,
                   wc.task_archetype_id, o.metric_direction
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
        intervention_id, exposed_at, repo_id, metric_name, archetype_id, metric_direction = row
        before = self._session_values(
            str(metric_name), repo_id, task_archetype_id=archetype_id, before=exposed_at
        )
        after = self._session_values(
            str(metric_name), repo_id, task_archetype_id=archetype_id, after=exposed_at
        )
        before_value = statistics.mean(before) if before else None
        after_value = statistics.mean(after) if after else None
        delta = None if before_value is None or after_value is None else after_value - before_value
        verdict = "insufficient_data"
        confidence = 0.0
        if len(before) >= 5 and len(after) >= 5 and before_value is not None and after_value is not None:
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
            "repo_id": repo_id,
            "task_archetype_id": archetype_id,
            "metric": metric_name,
            "before": f"sessions before {exposed_at}",
            "after": f"sessions on or after {exposed_at}",
            "minimum_after_sessions": 5,
            "minimum_before_sessions": 5,
            "metric_direction": metric_direction,
        }
        latest = self.conn.execute(
            """
            SELECT id, before_count, after_count, verdict, measured_at
            FROM measurements
            WHERE intervention_id = ? AND metric_name = ?
            ORDER BY measured_at DESC LIMIT 1
            """,
            (intervention_id, metric_name),
        ).fetchone()
        if skip_unchanged and latest and int(latest[1]) == len(before) and int(latest[2]) == len(after):
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
        """Measure active workflows once per distinct before/after cohort size."""
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
                "cohort": json.loads(row[3]),
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

    def _session_values(
        self,
        metric_name: str,
        repo_id: str | None,
        *,
        task_archetype_id: str | None,
        before: str | None = None,
        after: str | None = None,
    ) -> list[float]:
        time_clause = "AND s.started_at < ?" if before else "AND s.started_at >= ?"
        time_value = before or after
        archetype_clause = """
          AND (? IS NULL OR EXISTS (
            SELECT 1 FROM session_task_archetypes sta
            WHERE sta.session_id = s.id AND sta.task_archetype_id = ?
          ))
        """
        params = (repo_id, time_value, task_archetype_id, task_archetype_id)
        if metric_name == "tool_failure_rate":
            rows = self.conn.execute(
                f"""
                SELECT s.id,
                       1.0 * SUM(CASE WHEN lower(COALESCE(tc.status, '')) IN ('error','failed','failure')
                                          OR NULLIF(tc.error_type, '') IS NOT NULL THEN 1 ELSE 0 END)
                       / COUNT(*)
                FROM sessions s JOIN tool_calls tc ON tc.session_id = s.id
                WHERE COALESCE(s.repo_id, '') = COALESCE(?, '') {time_clause} {archetype_clause}
                GROUP BY s.id ORDER BY s.started_at DESC LIMIT 50
                """,
                params,
            ).fetchall()
            return [float(row[1]) for row in rows]
        if metric_name == "context_outlier_sessions":
            rows = self.conn.execute(
                f"""
                SELECT input_tokens + output_tokens
                FROM sessions s
                WHERE COALESCE(s.repo_id, '') = COALESCE(?, '') {time_clause} {archetype_clause}
                ORDER BY s.started_at DESC LIMIT 50
                """,
                params,
            ).fetchall()
            return [float(row[0]) for row in rows]
        if metric_name == "identical_retry_calls":
            rows = self.conn.execute(
                f"""
                SELECT s.id, COALESCE((
                  SELECT SUM(call_count) FROM (
                    SELECT COUNT(*) AS call_count
                    FROM tool_calls tc
                    WHERE tc.session_id = s.id AND NULLIF(tc.input_hash, '') IS NOT NULL
                    GROUP BY tc.tool_name, tc.input_hash HAVING COUNT(*) >= 3
                  )
                ), 0)
                FROM sessions s
                WHERE COALESCE(s.repo_id, '') = COALESCE(?, '') {time_clause} {archetype_clause}
                ORDER BY s.started_at DESC LIMIT 50
                """,
                params,
            ).fetchall()
            return [float(row[1]) for row in rows]
        if metric_name == "unverified_change_sessions":
            rows = self.conn.execute(
                f"""
                SELECT s.id,
                       CASE WHEN EXISTS(
                         SELECT 1 FROM tool_calls tc WHERE tc.session_id = s.id AND (
                           lower(tc.tool_name) GLOB '*write*' OR lower(tc.tool_name) GLOB '*edit*'
                           OR lower(tc.tool_name) GLOB '*patch*'
                         )
                       ) AND NOT EXISTS(
                         SELECT 1 FROM tool_calls tc WHERE tc.session_id = s.id AND (
                           lower(tc.tool_name) GLOB '*test*'
                           OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*pytest*'
                           OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*ruff*'
                           OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*build*'
                           OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*compile*'
                         )
                       ) THEN 1.0 ELSE 0.0 END
                FROM sessions s
                WHERE COALESCE(s.repo_id, '') = COALESCE(?, '') {time_clause} {archetype_clause}
                ORDER BY s.started_at DESC LIMIT 50
                """,
                params,
            ).fetchall()
            return [float(row[1]) for row in rows]
        if metric_name == "read_only_exploration_calls":
            rows = self.conn.execute(
                f"""
                SELECT s.id,
                       CASE WHEN SUM(CASE WHEN lower(tc.tool_name) GLOB '*read*'
                                              OR lower(tc.tool_name) GLOB '*find*'
                                              OR lower(tc.tool_name) GLOB '*search*'
                                              OR lower(tc.tool_name) GLOB '*grep*'
                                              OR lower(tc.tool_name) GLOB '*glob*'
                                          THEN 1 ELSE 0 END) >= 8
                                  AND SUM(CASE WHEN lower(tc.tool_name) GLOB '*write*'
                                                   OR lower(tc.tool_name) GLOB '*edit*'
                                                   OR lower(tc.tool_name) GLOB '*patch*'
                                              THEN 1 ELSE 0 END) = 0
                            THEN SUM(CASE WHEN lower(tc.tool_name) GLOB '*read*'
                                              OR lower(tc.tool_name) GLOB '*find*'
                                              OR lower(tc.tool_name) GLOB '*search*'
                                              OR lower(tc.tool_name) GLOB '*grep*'
                                              OR lower(tc.tool_name) GLOB '*glob*'
                                         THEN 1 ELSE 0 END)
                            ELSE 0 END
                FROM sessions s LEFT JOIN tool_calls tc ON tc.session_id = s.id
                WHERE COALESCE(s.repo_id, '') = COALESCE(?, '') {time_clause} {archetype_clause}
                GROUP BY s.id ORDER BY s.started_at DESC LIMIT 50
                """,
                params,
            ).fetchall()
            return [float(row[1]) for row in rows]
        if metric_name in {"operator_correction_rate", "correct_no_change_sessions"}:
            outcome = "corrected" if metric_name == "operator_correction_rate" else "no-change-correct"
            rows = self.conn.execute(
                f"""
                SELECT s.id, CASE WHEN EXISTS(
                  SELECT 1 FROM session_outcomes so
                  WHERE so.session_id = s.id AND so.outcome = ?
                ) THEN 1.0 ELSE 0.0 END
                FROM sessions s
                WHERE COALESCE(s.repo_id, '') = COALESCE(?, '') {time_clause} {archetype_clause}
                ORDER BY s.started_at DESC LIMIT 50
                """,
                (outcome, *params),
            ).fetchall()
            return [float(row[1]) for row in rows]
        if metric_name == "constraint_violation_rate":
            rows = self.conn.execute(
                f"""
                SELECT s.id, COUNT(tc.id)
                FROM sessions s LEFT JOIN tool_calls tc ON tc.session_id = s.id AND (
                  lower(COALESCE(tc.error_type, '')) GLOB '*permission*'
                  OR lower(COALESCE(tc.error_type, '')) GLOB '*sandbox*'
                  OR lower(COALESCE(tc.error_type, '')) GLOB '*policy*'
                  OR lower(COALESCE(tc.error_type, '')) GLOB '*approval*'
                  OR lower(COALESCE(tc.error_message_redacted, '')) GLOB '*permission denied*'
                  OR lower(COALESCE(tc.error_message_redacted, '')) GLOB '*not allowed*'
                )
                WHERE COALESCE(s.repo_id, '') = COALESCE(?, '') {time_clause} {archetype_clause}
                GROUP BY s.id ORDER BY s.started_at DESC LIMIT 50
                """,
                params,
            ).fetchall()
            return [float(row[1]) for row in rows]
        if metric_name == "recovered_failure_sessions":
            rows = self.conn.execute(
                f"""
                SELECT s.id, CASE WHEN s.recovered_failure_count > 0 THEN 1.0 ELSE 0.0 END
                FROM sessions s
                WHERE COALESCE(s.repo_id, '') = COALESCE(?, '') {time_clause} {archetype_clause}
                ORDER BY s.started_at DESC LIMIT 50
                """,
                params,
            ).fetchall()
            return [float(row[1]) for row in rows]
        if metric_name == "successful_workflow_sessions":
            rows = self.conn.execute(
                f"""
                SELECT s.id, CASE WHEN s.failure_count = 0
                  AND lower(COALESCE(s.status, '')) IN ('completed', 'ok', 'success')
                  THEN 1.0 ELSE 0.0 END
                FROM sessions s
                WHERE COALESCE(s.repo_id, '') = COALESCE(?, '') {time_clause} {archetype_clause}
                ORDER BY s.started_at DESC LIMIT 50
                """,
                params,
            ).fetchall()
            return [float(row[1]) for row in rows]
        rows = self.conn.execute(
            f"""
            SELECT s.id, s.failure_count
            FROM sessions s
            WHERE COALESCE(s.repo_id, '') = COALESCE(?, '') {time_clause} {archetype_clause}
            ORDER BY s.started_at DESC LIMIT 50
            """,
            params,
        ).fetchall()
        return [float(row[1]) for row in rows]
