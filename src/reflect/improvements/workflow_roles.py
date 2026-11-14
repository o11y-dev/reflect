from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from reflect.execution_assessment import check_command, command_from_tool


@dataclass(frozen=True, slots=True)
class WorkflowSignal:
    kind: str
    name: str
    preview: str = ""
    fingerprint: str | None = None
    failed: bool = False
    successful_result: bool = False
    step_id: str = ""
    path: str = ""
    command: str = ""


class WorkflowEvidenceRepository:
    """Load ordered canonical signals for bounded execution units."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def signals_for_execution_units(
        self,
        execution_unit_ids: list[str],
        *,
        limit_per_execution: int | None = None,
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
                  SELECT eus.execution_unit_id,st.seq,tc.created_at,tc.id AS signal_id,
                         'tool' AS kind,st.id AS step_id
                  FROM execution_unit_steps eus
                  JOIN steps st ON st.id=eus.step_id
                  JOIN tool_calls tc ON tc.step_id=st.id
                  WHERE eus.execution_unit_id IN ({placeholders})
                  UNION ALL
                  SELECT eus.execution_unit_id,st.seq,cf.created_at,cf.id AS signal_id,
                         'conversation' AS kind,st.id AS step_id
                  FROM execution_unit_steps eus
                  JOIN steps st ON st.id=eus.step_id
                  JOIN conversation_facts cf ON cf.step_id=st.id
                  WHERE eus.execution_unit_id IN ({placeholders})
                ), ranked AS (
                  SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY execution_unit_id ORDER BY seq,created_at,signal_id
                  ) AS position FROM signals
                ), selected AS (
                  SELECT * FROM ranked WHERE (? IS NULL OR position <= ?)
                )
                SELECT selected.execution_unit_id,selected.kind,
                       COALESCE(tc.tool_name,cf.role,''),
                       COALESCE(NULLIF(tc.input_preview_redacted,''),cf.content_preview_redacted,''),
                       COALESCE(tc.input_hash,cf.content_hash),
                       CASE WHEN lower(COALESCE(tc.status,'')) IN ('error','failed','failure')
                         OR NULLIF(tc.error_type,'') IS NOT NULL THEN 1 ELSE 0 END,
                       CASE WHEN tc.status='ok' OR
                         json_extract(tc.raw_attrs_json,'$."process.exit.code"')=0 THEN 1 ELSE 0 END,
                       selected.step_id,
                       COALESCE(json_extract(tc.raw_attrs_json,'$."gen_ai.client.file_path"'),
                         json_extract(st.raw_attrs_json,'$."gen_ai.client.file_path"'),''),
                       substr(COALESCE(json_extract(tc.raw_attrs_json,'$."gen_ai.client.command"'),
                         json_extract(tc.raw_attrs_json,'$."ide.command"'),
                         json_extract(tc.raw_attrs_json,'$."command"'),''),1,32769)
                FROM selected
                JOIN steps st ON st.id=selected.step_id
                LEFT JOIN tool_calls tc ON selected.kind='tool' AND tc.id=selected.signal_id
                LEFT JOIN conversation_facts cf ON selected.kind='conversation' AND cf.id=selected.signal_id
                ORDER BY selected.execution_unit_id,selected.position
                """,
                [*batch, *batch, limit_per_execution, limit_per_execution],
            ).fetchall()
            for row in rows:
                result.setdefault(str(row[0]), []).append(
                    WorkflowSignal(
                        kind=str(row[1]),
                        name=str(row[2]),
                        preview=str(row[3]),
                        fingerprint=str(row[4]) if row[4] else None,
                        failed=bool(row[5]),
                        successful_result=bool(row[6]),
                        step_id=str(row[7]),
                        path=str(row[8]),
                        command=str(row[9]),
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
    from reflect.execution_assessment import attrs_dict
    review = review_operation(signal.name, attrs_dict(signal.preview))
    if review and signal.successful_result and not signal.failed:
        return review[1]
    name = signal.name.lower().rsplit("__", 1)[-1]
    if any(term in name for term in ("approve", "authorize", "confirm")):
        return "authorize"
    # Commands and file paths in previews are data, not the operation name.
    # An edit of tests/foo.py remains an edit; rg pytest remains exploration.
    if any(term in name for term in ("edit", "write", "patch", "apply", "create", "update", "delete", "post")):
        return "act"
    command = signal.command or command_from_tool({"tool_name": signal.name, "input_preview_redacted": signal.preview})
    if check_command(command):
        return "verify"
    if any(term in name for term in ("read", "view", "search", "find", "grep", "list", "get", "open")):
        return "verify" if acted else "observe"
    return None


def workflow_roles(signals: list[WorkflowSignal], *, limit: int = 8) -> list[str]:
    """Return ordered, adjacent-deduplicated roles for bounded canonical evidence."""

    roles: list[str] = []
    for signal in signals:
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


def review_operation(tool_name: str, arguments: dict) -> tuple[str, str] | None:
    """Map supported provider reads to an identified change and semantic role."""
    name = str(tool_name).rsplit('__', 1)[-1].rsplit('/', 1)[-1]
    operations = {
        'get_merge_request_details': ('gitlab','review_metadata'),
        'get_merge_request_changes': ('gitlab','review_diff'),
        'get_pull_request': ('github','review_metadata'),
        'get_pull_request_diff': ('github','review_diff'),
    }
    if name not in operations:
        return None
    provider, role = operations[name]
    project = arguments.get('project_id') if provider=='gitlab' else '/'.join(str(arguments.get(k) or '') for k in ('owner','repo'))
    number = arguments.get('merge_request_iid') or arguments.get('mr_iid') or arguments.get('pull_number') or arguments.get('iid')
    if not project or project=='/' or not number:
        return None
    revision = arguments.get('head_sha') or arguments.get('commit_sha')
    target = f'{provider}:{project}:{number}'
    return target + (f'@{revision}' if revision else ''), role
