from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any


class GuidanceEfficiencyService:
    """Describe comparable, verified executions without claiming causal impact."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def compare(
        self, *, repo_id: str, task_archetype_id: str, model: str, limit: int = 100
    ) -> dict[str, Any]:
        if not all((repo_id.strip(), task_archetype_id.strip(), model.strip())):
            raise ValueError("repo_id, task_archetype_id, and model are required")
        rows = self.conn.execute(
            """
            SELECT eu.id, eu.outcome, eu.verification_passed, tr.id,
                   COALESCE(tr.skill_usage_recorded_count, 0),
                   COUNT(l.id), COALESCE(SUM(l.input_tokens), 0),
                   COALESCE(SUM(l.output_tokens), 0),
                   COALESCE(SUM(l.cache_read_input_tokens), 0),
                   COALESCE(SUM(l.cache_creation_input_tokens), 0),
                   COALESCE(SUM(l.estimated_cost_usd), 0),
                   COALESCE(SUM(CASE WHEN l.estimated_cost_usd > 0 THEN 1 ELSE 0 END), 0),
                   COUNT(DISTINCT COALESCE(NULLIF(l.response_model, ''), l.request_model)),
                   MIN(COALESCE(NULLIF(l.response_model, ''), l.request_model)),
                   (SELECT COUNT(*) FROM tool_calls tc
                    JOIN execution_unit_steps eus ON eus.step_id = tc.step_id
                    WHERE eus.execution_unit_id = eu.id)
            FROM execution_units eu
            JOIN execution_unit_archetypes eua ON eua.execution_unit_id = eu.id
            LEFT JOIN mcp_task_runs tr ON tr.id = eu.mcp_task_run_id
            LEFT JOIN execution_unit_steps eus ON eus.execution_unit_id = eu.id
            LEFT JOIN llm_calls l ON l.step_id = eus.step_id
            WHERE eu.repo_id = ? AND eua.task_archetype_id = ?
              AND eu.eligible = 1 AND eu.status = 'completed'
              AND eu.verification_passed IS NOT NULL
              AND (tr.id IS NULL OR tr.status = 'completed')
            GROUP BY eu.id
            ORDER BY eu.started_at DESC, eu.id DESC
            LIMIT ?
            """,
            (repo_id, task_archetype_id, min(max(limit, 1), 500)),
        ).fetchall()
        cohorts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        excluded = {"no_llm_calls": 0, "mixed_or_other_model": 0}
        for row in rows:
            if not row[5]:
                excluded["no_llm_calls"] += 1
                continue
            if row[12] != 1 or row[13] != model:
                excluded["mixed_or_other_model"] += 1
                continue
            group = "unguided" if row[3] is None else "guided"
            cohorts[group].append(
                {
                    "execution_unit_id": row[0],
                    "verified_success": row[1] == "success" and bool(row[2]),
                    "skill_usage_recorded": bool(row[4]),
                    "model_calls": row[5],
                    "input_tokens": row[6],
                    "output_tokens": row[7],
                    "cache_read_tokens": row[8],
                    "cache_creation_tokens": row[9],
                    "estimated_cost_usd": row[10],
                    "priced_calls": row[11],
                    "tool_calls": row[14],
                }
            )

        def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
            count = len(items)
            return {
                "count": count,
                "verified_success_count": sum(item["verified_success"] for item in items),
                "recorded_skill_use_count": sum(item["skill_usage_recorded"] for item in items),
                "mean_model_calls": sum(item["model_calls"] for item in items) / count
                if count
                else None,
                "mean_tool_calls": sum(item["tool_calls"] for item in items) / count
                if count
                else None,
                "mean_billed_tokens": sum(
                    item["input_tokens"]
                    + item["output_tokens"]
                    + item["cache_read_tokens"]
                    + item["cache_creation_tokens"]
                    for item in items
                )
                / count
                if count
                else None,
                "mean_estimated_cost_usd": (
                    sum(item["estimated_cost_usd"] for item in items) / count
                    if count and all(item["priced_calls"] == item["model_calls"] for item in items)
                    else None
                ),
                "execution_unit_ids": [item["execution_unit_id"] for item in items],
            }

        return {
            "repo_id": repo_id,
            "task_archetype_id": task_archetype_id,
            "model": model,
            "guided": summarize(cohorts["guided"]),
            "unguided": summarize(cohorts["unguided"]),
            "excluded": excluded,
            "scanned": len(rows),
            "limitations": [
                "Guided means reflect_context created a linked task run; it does not prove guidance was followed.",
                "Recorded skill use is a separate exposure signal, not proof of adherence.",
                "Only observed calls mapped to execution-unit steps are counted; missing usage and pricing are not imputed.",
                "This is a descriptive comparison, not a randomized or causal impact estimate.",
            ],
        }
