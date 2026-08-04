from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkflowSignal:
    kind: str
    name: str
    preview: str = ""
    fingerprint: str | None = None
    failed: bool = False


class WorkflowEvidenceRepository:
    """Load ordered canonical signals for bounded execution units."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def signals_for_execution_units(
        self,
        execution_unit_ids: list[str],
    ) -> dict[str, list[WorkflowSignal]]:
        if not execution_unit_ids:
            return {}
        result: dict[str, list[WorkflowSignal]] = {}
        for offset in range(0, len(execution_unit_ids), 400):
            batch = execution_unit_ids[offset : offset + 400]
            placeholders = ", ".join("?" for _ in batch)
            rows = self.conn.execute(
                f"""
                WITH signals AS (
                  SELECT eus.execution_unit_id, st.seq, tc.created_at, tc.id AS signal_id,
                         'tool' AS kind, tc.tool_name AS name,
                         COALESCE(tc.input_preview_redacted, '') AS preview,
                         tc.input_hash AS fingerprint,
                         CASE
                           WHEN lower(COALESCE(tc.status, ''))
                                  IN ('error', 'failed', 'failure')
                             OR NULLIF(tc.error_type, '') IS NOT NULL THEN 1 ELSE 0
                         END AS failed
                  FROM execution_unit_steps eus
                  JOIN steps st ON st.id = eus.step_id
                  JOIN tool_calls tc ON tc.step_id = st.id
                  WHERE eus.execution_unit_id IN ({placeholders})
                  UNION ALL
                  SELECT eus.execution_unit_id, st.seq, cf.created_at,
                         cf.id AS signal_id, 'conversation' AS kind,
                         cf.role AS name,
                         COALESCE(cf.content_preview_redacted, '') AS preview,
                         cf.content_hash AS fingerprint, 0 AS failed
                  FROM execution_unit_steps eus
                  JOIN steps st ON st.id = eus.step_id
                  JOIN conversation_facts cf ON cf.step_id = st.id
                  WHERE eus.execution_unit_id IN ({placeholders})
                )
                SELECT execution_unit_id, kind, name, preview, fingerprint, failed
                FROM signals
                ORDER BY execution_unit_id, seq, created_at, signal_id
                """,
                [*batch, *batch],
            ).fetchall()
            for row in rows:
                result.setdefault(str(row[0]), []).append(
                    WorkflowSignal(
                        kind=str(row[1]),
                        name=str(row[2]),
                        preview=str(row[3]),
                        fingerprint=str(row[4]) if row[4] else None,
                        failed=bool(row[5]),
                    )
                )
        return result


def workflow_role(signal: WorkflowSignal, *, acted: bool) -> str | None:
    """Map canonical evidence onto a provider-agnostic workflow vocabulary."""

    value = f"{signal.name} {signal.preview}".lower()
    if signal.kind == "conversation" and signal.name.lower() == "user":
        approval_terms = (
            "approve",
            "approved",
            "go ahead",
            "please proceed",
            "do it",
            "do that",
            "confirmed",
            "looks good",
        )
        if any(term in value for term in approval_terms):
            return "authorize"
    if any(term in value for term in ("approve", "permission", "authorize", "confirm")):
        return "authorize"
    if any(
        term in value
        for term in ("pytest", "test", "ruff", "lint", "build", "compile", "validate")
    ):
        return "verify"
    if any(
        term in value
        for term in ("edit", "write", "patch", "apply", "create", "update", "delete", "post")
    ):
        return "act"
    if any(
        term in value
        for term in ("read", "view", "search", "find", "grep", "list", "get", "open")
    ):
        return "verify" if acted else "observe"
    return None


def workflow_roles(signals: list[WorkflowSignal], *, limit: int = 8) -> list[str]:
    """Return ordered, adjacent-deduplicated roles for bounded canonical evidence."""

    roles: list[str] = []
    for signal in signals[:24]:
        role = workflow_role(signal, acted="act" in roles)
        if role and (not roles or roles[-1] != role):
            roles.append(role)
        if len(roles) >= limit:
            break
    return roles


__all__ = [
    "WorkflowEvidenceRepository",
    "WorkflowSignal",
    "workflow_role",
    "workflow_roles",
]
