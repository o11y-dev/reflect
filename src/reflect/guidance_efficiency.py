from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any

from reflect.improvements.measurement_validity import EXECUTION_EVIDENCE_CURRENT_SQL


class GuidanceEfficiencyService:
    """Describe comparable, verified executions without claiming causal impact."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def compare(
        self, *, repo_id: str, task_archetype_id: str, model: str, limit: int = 100
    ) -> dict[str, Any]:
        if not all((repo_id.strip(), task_archetype_id.strip(), model.strip())):
            raise ValueError("repo_id, task_archetype_id, and model are required")
        usage_query = f"""
            WITH unit_usage AS (
            SELECT eu.id, eu.outcome, eu.verification_passed, tr.id AS task_run_id,
                   COALESCE(tr.skill_usage_recorded_count, 0),
                   COUNT(l.id) AS model_calls, COALESCE(SUM(CASE
                     WHEN json_extract(l.raw_attrs_json, '$."reflect.usage.input_semantics"') = 'inclusive'
                     THEN MAX(0, l.input_tokens - l.cache_read_input_tokens - l.cache_creation_input_tokens)
                     ELSE l.input_tokens END), 0),
                   COALESCE(SUM(l.output_tokens), 0),
                   COALESCE(SUM(l.cache_read_input_tokens), 0),
                   COALESCE(SUM(l.cache_creation_input_tokens), 0),
                   COALESCE(SUM(l.estimated_cost_usd), 0),
                   COALESCE(SUM(CASE WHEN json_extract(l.raw_attrs_json, '$."reflect.cost.source"')
                       IN ('live', 'cache', 'fallback') THEN 1 ELSE 0 END), 0),
                   COUNT(DISTINCT COALESCE(NULLIF(TRIM(l.response_model), ''),
                                           NULLIF(TRIM(l.request_model), ''))) AS model_count,
                   MIN(COALESCE(NULLIF(TRIM(l.response_model), ''),
                                NULLIF(TRIM(l.request_model), ''))) AS model_name,
                   (SELECT COUNT(*) FROM tool_calls tc
                    JOIN execution_unit_steps eus ON eus.step_id = tc.step_id
                    WHERE eus.execution_unit_id = eu.id),
                   COALESCE(SUM(l.reasoning_output_tokens), 0),
                   COALESCE(SUM(CASE WHEN l.id IS NOT NULL AND
                     COALESCE(NULLIF(TRIM(l.response_model), ''),
                              NULLIF(TRIM(l.request_model), '')) IS NULL
                     OR json_extract(l.raw_attrs_json, '$."gen_ai.request.model_source"') = 'codex_config_default'
                     THEN 1 ELSE 0 END), 0) AS missing_model_calls,
                   eu.started_at, eu.boundary_json,
                   SUM(CASE WHEN json_type(l.raw_attrs_json, '$."gen_ai.usage.input_tokens"') IN ('integer','real')
                     AND json_type(l.raw_attrs_json, '$."gen_ai.usage.output_tokens"') IN ('integer','real')
                     AND COALESCE(json_type(l.raw_attrs_json, '$."gen_ai.usage.cache_read.input_tokens"'),
                                  json_type(l.raw_attrs_json, '$."hook.cache_read_tokens"')) IN ('integer','real')
                     AND COALESCE(json_type(l.raw_attrs_json, '$."gen_ai.usage.cache_creation.input_tokens"'),
                                  json_type(l.raw_attrs_json, '$."hook.cache_write_tokens"')) IN ('integer','real')
                     AND COALESCE(json_extract(l.raw_attrs_json, '$."reflect.token.source"'), '') NOT LIKE 'estimated%'
                     AND (COALESCE(json_extract(l.raw_attrs_json, '$."reflect.usage.input_semantics"'), '') != 'inclusive'
                          OR l.input_tokens >= l.cache_read_input_tokens + l.cache_creation_input_tokens)
                     THEN 1 ELSE 0 END) AS complete_usage_calls,
                   ({EXECUTION_EVIDENCE_CURRENT_SQL}) AS evidence_current
            FROM execution_units eu
            JOIN execution_unit_archetypes eua ON eua.execution_unit_id = eu.id
            LEFT JOIN mcp_task_runs tr ON tr.id = eu.mcp_task_run_id
            LEFT JOIN execution_unit_steps eus ON eus.execution_unit_id = eu.id
            LEFT JOIN llm_calls l ON l.step_id = eus.step_id
            WHERE eu.repo_id = ? AND eua.task_archetype_id = ?
              AND eua.mixed = 0 AND eua.confidence >= 0.65
              AND eu.eligible = 1
            GROUP BY eu.id
            )
            """
        scope = (repo_id.strip(), task_archetype_id.strip())
        excluded = self.conn.execute(
            usage_query
            + """
            SELECT COALESCE(SUM(CASE WHEN model_calls = 0 THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN model_calls > 0 AND
                     (missing_model_calls > 0 OR model_count != 1 OR model_name != ?)
                     THEN 1 ELSE 0 END), 0),
                   COUNT(*)
            FROM unit_usage
            """,
            (*scope, model.strip()),
        ).fetchone()
        rows = self.conn.execute(
            usage_query
            + """
            SELECT * FROM unit_usage
            WHERE model_calls > 0 AND missing_model_calls = 0
              AND model_count = 1 AND model_name = ?
            ORDER BY started_at DESC, id DESC
            LIMIT ?
            """,
            (*scope, model.strip(), min(max(limit, 1), 500)),
        ).fetchall()
        cohorts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            metadata = json.loads(row[18] or "{}")
            current = bool(row[20])
            assessment = (metadata.get("assessment") or {}) if current else {}
            group = {"observed": "guided", "observed_absent": "unguided"}.get(
                assessment.get("exposure"), "unknown_exposure"
            )
            assessed = bool(assessment.get("version")) and row[1] in {"success", "failure"} and row[2] is not None
            cohorts[group].append(
                {
                    "execution_unit_id": row[0],
                    "evidence_current": current,
                    "verified_success": assessed and row[1] == "success" and bool(row[2]),
                    "assessed_outcome": assessed,
                    "selected_skill_outcome_recorded": bool(row[4]),
                    "model_calls": row[5],
                    "input_tokens": row[6],
                    "output_tokens": row[7],
                    "cache_read_tokens": row[8],
                    "cache_creation_tokens": row[9],
                    "reasoning_tokens": row[15],
                    "estimated_cost_usd": row[10],
                    "priced_calls": row[11],
                    "complete_usage_calls": row[19],
                    "tool_calls": row[14],
                }
            )

        def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
            count = len(items)
            successes = sum(item["verified_success"] for item in items)
            assessed = [item for item in items if item["assessed_outcome"]]
            assessed_cost = sum(item["estimated_cost_usd"] for item in assessed)
            assessed_priced = bool(assessed) and all(
                item["priced_calls"] == item["model_calls"] == item["complete_usage_calls"] for item in assessed
            )
            model_calls = sum(item["model_calls"] for item in items)
            input_tokens = sum(item["input_tokens"] for item in items)
            output_tokens = sum(item["output_tokens"] for item in items)
            cache_read_tokens = sum(item["cache_read_tokens"] for item in items)
            cache_creation_tokens = sum(item["cache_creation_tokens"] for item in items)
            reasoning_tokens = sum(item["reasoning_tokens"] for item in items)
            input_total = input_tokens + cache_read_tokens + cache_creation_tokens
            priced = count > 0 and all(
                item["evidence_current"] and item["priced_calls"] == item["model_calls"] == item["complete_usage_calls"] for item in items
            )
            total_cost = sum(item["estimated_cost_usd"] for item in items)
            return {
                "count": count,
                "verified_success_count": successes,
                "verified_success_rate": successes / len(assessed) if assessed else None,
                "assessed_outcome_count": len(assessed),
                "unknown_outcome_count": count - len(assessed),
                "evidence_coverage": {
                    "current_execution_units": sum(item["evidence_current"] for item in items),
                    "pending_execution_units": sum(not item["evidence_current"] for item in items),
                },
                "observed_estimated_cost_usd": total_cost,
                "cost_coverage": {"priced_calls": sum(item["priced_calls"] for item in items),
                                  "observed_calls": model_calls,
                                  "complete_usage_calls": sum(item["complete_usage_calls"] for item in items)},
                "reported_selected_skill_count": sum(
                    item["selected_skill_outcome_recorded"] for item in items
                ),
                "mean_model_calls": model_calls / count if count else None,
                "mean_tool_calls": sum(item["tool_calls"] for item in items) / count
                if count
                else None,
                "mean_observed_tokens": (
                    input_total + output_tokens
                ) / count
                if count
                else None,
                "token_mix": {
                    "uncached_input": input_tokens,
                    "cache_read_input": cache_read_tokens,
                    "cache_creation_input": cache_creation_tokens,
                    "output": output_tokens,
                    "reasoning_output": reasoning_tokens,
                    "observed_cache_read_share_of_input": (
                        cache_read_tokens / input_total if input_total else None
                    ),
                },
                "mean_input_tokens_per_model_call": input_total / model_calls
                if model_calls
                else None,
                "mean_estimated_cost_usd": total_cost / count if priced else None,
                "estimated_cost_per_verified_success_usd": (
                    assessed_cost / successes if assessed_priced and successes else None
                ),
                "unpriced_model_calls": model_calls - sum(
                    item["priced_calls"] for item in items
                ),
                "execution_unit_ids": [item["execution_unit_id"] for item in items],
            }

        return {
            "repo_id": scope[0],
            "task_archetype_id": scope[1],
            "model": model.strip(),
            "guided": summarize(cohorts["guided"]),
            "unguided": summarize(cohorts["unguided"]),
            "unknown_exposure": summarize(cohorts["unknown_exposure"]),
            "excluded": {
                "no_llm_calls": excluded[0],
                "mixed_or_other_model": excluded[1],
            },
            "scanned": excluded[2],
            "returned": len(rows),
            "limit": min(max(limit, 1), 500),
            "truncated": excluded[2] - excluded[0] - excluded[1] > len(rows),
            "limitations": [
                "Supported success means observed acceptance of an approved scoped workflow contract, not proof of arbitrary task semantics.",
                "Guided requires observed guidance delivery; a missing MCP call leaves exposure unknown.",
                "Selected skill outcomes are recorded at task completion; they do not prove skill use or adherence.",
                "Only eligible, unmixed task archetypes with at least 0.65 classification confidence are compared.",
                "Only observed calls mapped to execution-unit steps are counted; missing usage and pricing are not imputed.",
                "Pending execution evidence retains last-projected observed usage; exposure and outcomes stay unknown until refreshed.",
                "Observed cache-read share is a token mix, not a provider cache-hit rate or a measured savings estimate.",
                "Cost per verified success includes only assessed outcomes with complete pricing; unknown-outcome usage remains in observed totals.",
                "This is a descriptive comparison, not a randomized or causal impact estimate.",
            ],
        }
