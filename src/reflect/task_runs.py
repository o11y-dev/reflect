from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from reflect.improvements.repository import utc_now
from reflect.usage import UsageService


class MCPTaskRunService:
    """Persist the non-destructive start and completion lifecycle for agent tasks."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        usage: UsageService | None = None,
    ) -> None:
        self.conn = conn
        self.usage = usage or UsageService(conn)

    def start(
        self,
        *,
        question: str,
        workspace_path: Path,
        task_file_path: Path | None,
        workflow_id: str | None,
        selected_skills: list[dict[str, str]],
    ) -> str:
        runtime_session_id, runtime_agent, _source = self.usage.runtime_session_hint()
        task_run_id = f"mcp_task_{uuid.uuid4().hex}"
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO mcp_task_runs(
              id, runtime_session_id, runtime_agent, workspace_path, task_file_path,
              question_hash, workflow_id, selected_skills_json, status,
              started_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'started', ?, ?, ?)
            """,
            (
                task_run_id,
                runtime_session_id,
                runtime_agent,
                str(workspace_path),
                str(task_file_path) if task_file_path else None,
                hashlib.sha256(question.encode("utf-8")).hexdigest(),
                workflow_id,
                json.dumps(selected_skills, sort_keys=True),
                now,
                now,
                now,
            ),
        )
        self.conn.commit()
        return task_run_id

    def complete(
        self,
        task_run_id: str,
        *,
        outcome: str,
        verification_passed: bool | None = None,
        summary_redacted: str = "",
    ) -> dict[str, Any]:
        allowed_outcomes = {"success", "partial", "failure", "abandoned"}
        if outcome not in allowed_outcomes:
            raise ValueError(f"Unsupported task outcome: {outcome}")
        summary = summary_redacted.strip()[:1000]
        row = self.conn.execute(
            """
            SELECT runtime_session_id, selected_skills_json, status, outcome,
                   verification_passed, completion_summary_redacted
            FROM mcp_task_runs WHERE id = ?
            """,
            (task_run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"MCP task run not found: {task_run_id}")
        existing_verification = None if row[4] is None else bool(row[4])
        if str(row[2]) == "completed":
            if (
                str(row[3]) == outcome
                and existing_verification == verification_passed
                and str(row[5] or "") == summary
            ):
                return self._result(task_run_id, idempotent=True)
            raise RuntimeError(
                f"MCP task run {task_run_id} is already completed; start a new guidance run for changed work"
            )

        now = utc_now()
        self.conn.execute(
            """
            UPDATE mcp_task_runs
            SET status = 'completed', outcome = ?, verification_passed = ?,
                completion_summary_redacted = ?, completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                outcome,
                None if verification_passed is None else int(verification_passed),
                summary or None,
                now,
                now,
                task_run_id,
            ),
        )
        runtime_session_id = str(row[0] or "")
        linked_to_session = bool(
            runtime_session_id
            and self.conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?",
                (runtime_session_id,),
            ).fetchone()
        )
        if linked_to_session:
            self._record_session_outcome(
                task_run_id,
                runtime_session_id,
                outcome=outcome,
                verification_passed=verification_passed,
                has_summary=bool(summary),
                now=now,
            )
            self._record_skill_outcomes(
                runtime_session_id,
                json.loads(str(row[1] or "[]")),
                outcome=outcome,
                verification_passed=verification_passed,
                now=now,
            )
        self.conn.commit()
        return self._result(
            task_run_id,
            idempotent=False,
            linked_to_session=linked_to_session,
        )

    def _record_session_outcome(
        self,
        task_run_id: str,
        session_id: str,
        *,
        outcome: str,
        verification_passed: bool | None,
        has_summary: bool,
        now: str,
    ) -> None:
        verification = {
            "task_run_id": task_run_id,
            "verification_passed": verification_passed,
            "has_summary": has_summary,
        }
        self.conn.execute(
            """
            INSERT INTO session_outcomes(
              id, session_id, outcome, source, confidence, verification_json,
              created_at, updated_at
            ) VALUES (?, ?, ?, 'agent_completion', ?, ?, ?, ?)
            ON CONFLICT(session_id, source) DO UPDATE SET
              outcome = excluded.outcome,
              confidence = excluded.confidence,
              verification_json = excluded.verification_json,
              updated_at = excluded.updated_at
            """,
            (
                f"outcome_{hashlib.sha256((task_run_id + ':agent').encode()).hexdigest()[:24]}",
                session_id,
                outcome,
                0.9 if verification_passed is not None else 0.7,
                json.dumps(verification, sort_keys=True),
                now,
                now,
            ),
        )

    def _record_skill_outcomes(
        self,
        session_id: str,
        selected_skills: list[dict[str, Any]],
        *,
        outcome: str,
        verification_passed: bool | None,
        now: str,
    ) -> None:
        for skill in selected_skills:
            skill_id = str(skill.get("skill_id") or "")
            version_id = str(skill.get("version_id") or "") or None
            if not skill_id:
                continue
            usage_id = f"skill_usage_{hashlib.sha256(f'{skill_id}:{session_id}'.encode()).hexdigest()[:24]}"
            self.conn.execute(
                """
                INSERT INTO skill_usage(
                  id, skill_id, skill_version_id, session_id, state, outcome,
                  confidence, evidence_json, observed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'reported', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(skill_id, session_id) DO UPDATE SET
                  skill_version_id = excluded.skill_version_id,
                  state = excluded.state,
                  outcome = excluded.outcome,
                  confidence = MAX(skill_usage.confidence, excluded.confidence),
                  evidence_json = excluded.evidence_json,
                  observed_at = excluded.observed_at,
                  updated_at = excluded.updated_at
                """,
                (
                    usage_id,
                    skill_id,
                    version_id,
                    session_id,
                    outcome,
                    0.9 if verification_passed is not None else 0.7,
                    json.dumps(
                        {
                            "source": "reflect_complete",
                            "verification_passed": verification_passed,
                        },
                        sort_keys=True,
                    ),
                    now,
                    now,
                    now,
                ),
            )

    def _result(
        self,
        task_run_id: str,
        *,
        idempotent: bool,
        linked_to_session: bool | None = None,
    ) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT runtime_session_id, status, outcome, verification_passed,
                   completed_at
            FROM mcp_task_runs WHERE id = ?
            """,
            (task_run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"MCP task run not found: {task_run_id}")
        session_id = str(row[0] or "")
        if linked_to_session is None:
            linked_to_session = bool(
                session_id
                and self.conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
            )
        return {
            "task_run_id": task_run_id,
            "status": str(row[1]),
            "outcome": row[2],
            "verification_passed": None if row[3] is None else bool(row[3]),
            "completed_at": row[4],
            "runtime_session_id": session_id or None,
            "linked_to_session": linked_to_session,
            "idempotent": idempotent,
        }
