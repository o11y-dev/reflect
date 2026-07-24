from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from reflect.improvements.repository import utc_now
from reflect.schema.base import ReflectModel
from reflect.usage import UsageService


class MCPTaskOutcome(StrEnum):
    """Agent-reported completion state for one Reflect task run."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"
    ABANDONED = "abandoned"


class MCPSelectedSkillRef(ReflectModel):
    """Stable skill identity captured when guidance starts."""

    skill_id: str
    version_id: str
    slug: str


class MCPTaskRunLinkState(StrEnum):
    """Observable relationship between a task run and normalized telemetry."""

    NO_RUNTIME_SESSION = "no_runtime_session"
    PENDING_INGESTION = "pending_ingestion"
    SESSION_AVAILABLE = "session_available"
    READY_TO_RECONCILE = "ready_to_reconcile"
    LINKED = "linked"


class MCPTaskRunStatus(ReflectModel):
    """Privacy-safe status for one MCP task lifecycle."""

    task_run_id: str
    status: str
    outcome: MCPTaskOutcome | None = None
    verification_passed: bool | None = None
    summary_redacted: str = ""
    workflow_id: str | None = None
    selected_skills: list[MCPSelectedSkillRef] = Field(default_factory=list)
    workspace_path: str
    task_file_path: str | None = None
    runtime_session_id: str | None = None
    runtime_agent: str | None = None
    link_state: MCPTaskRunLinkState
    session_outcome_recorded: bool = False
    skill_usage_recorded_count: int = Field(default=0, ge=0)
    started_at: str
    completed_at: str | None = None
    updated_at: str


class TaskRunReconciliationResult(ReflectModel):
    """Idempotent late-ingestion reconciliation counts."""

    scanned: int = Field(default=0, ge=0)
    linked: int = Field(default=0, ge=0)
    unchanged: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)


class MCPTaskRunResult(ReflectModel):
    """Typed completion result returned to agent-facing orchestration."""

    task_run_id: str
    status: str
    outcome: MCPTaskOutcome | None = None
    verification_passed: bool | None = None
    completed_at: str | None = None
    runtime_session_id: str | None = None
    linked_to_session: bool
    idempotent: bool


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
        self.reconciler = TaskRunReconciler(conn)

    def start(
        self,
        *,
        question: str,
        workspace_path: Path,
        task_file_path: Path | None,
        workflow_id: str | None,
        selected_skills: list[MCPSelectedSkillRef],
    ) -> str:
        session_hint = self.usage.runtime_session_hint()
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
                session_hint.session_id if session_hint else None,
                session_hint.agent if session_hint else None,
                str(workspace_path),
                str(task_file_path) if task_file_path else None,
                hashlib.sha256(question.encode("utf-8")).hexdigest(),
                workflow_id,
                json.dumps(
                    [skill.model_dump(mode="json") for skill in selected_skills],
                    sort_keys=True,
                ),
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
        outcome: MCPTaskOutcome | str,
        verification_passed: bool | None = None,
        summary_redacted: str = "",
    ) -> MCPTaskRunResult:
        try:
            normalized_outcome = MCPTaskOutcome(outcome)
        except ValueError as exc:
            raise ValueError(f"Unsupported task outcome: {outcome}") from exc
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
                str(row[3]) == normalized_outcome.value
                and existing_verification == verification_passed
                and str(row[5] or "") == summary
            ):
                self.reconciler.reconcile(task_run_ids={task_run_id}, commit=False)
                self.conn.commit()
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
                normalized_outcome.value,
                None if verification_passed is None else int(verification_passed),
                summary or None,
                now,
                now,
                task_run_id,
            ),
        )
        reconciliation = self.reconciler.reconcile(
            task_run_ids={task_run_id},
            commit=False,
        )
        self.conn.commit()
        return self._result(
            task_run_id,
            idempotent=False,
            linked_to_session=bool(reconciliation.linked),
        )

    def status(self, task_run_id: str) -> MCPTaskRunStatus:
        """Return a read-only, privacy-safe lifecycle snapshot."""

        row = self.conn.execute(
            """
            SELECT id, runtime_session_id, runtime_agent, workspace_path,
                   task_file_path, workflow_id, selected_skills_json, status,
                   outcome, verification_passed, completion_summary_redacted,
                   started_at, completed_at, updated_at, session_linked_at,
                   session_outcome_recorded, skill_usage_recorded_count
            FROM mcp_task_runs WHERE id = ?
            """,
            (task_run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"MCP task run not found: {task_run_id}")
        selected_skills = _selected_skills(str(row[6] or "[]"))
        runtime_session_id = str(row[1] or "") or None
        session_exists = bool(
            runtime_session_id
            and self.conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?",
                (runtime_session_id,),
            ).fetchone()
        )
        if row[14]:
            link_state = MCPTaskRunLinkState.LINKED
        elif session_exists and str(row[7]) == "completed":
            link_state = MCPTaskRunLinkState.READY_TO_RECONCILE
        elif session_exists:
            link_state = MCPTaskRunLinkState.SESSION_AVAILABLE
        elif runtime_session_id:
            link_state = MCPTaskRunLinkState.PENDING_INGESTION
        else:
            link_state = MCPTaskRunLinkState.NO_RUNTIME_SESSION
        return MCPTaskRunStatus(
            task_run_id=str(row[0]),
            runtime_session_id=runtime_session_id,
            runtime_agent=str(row[2]) if row[2] else None,
            workspace_path=str(row[3]),
            task_file_path=str(row[4]) if row[4] else None,
            workflow_id=str(row[5]) if row[5] else None,
            selected_skills=selected_skills,
            status=str(row[7]),
            outcome=MCPTaskOutcome(str(row[8])) if row[8] else None,
            verification_passed=None if row[9] is None else bool(row[9]),
            summary_redacted=str(row[10] or ""),
            started_at=str(row[11]),
            completed_at=str(row[12]) if row[12] else None,
            updated_at=str(row[13]),
            link_state=link_state,
            session_outcome_recorded=bool(row[15]),
            skill_usage_recorded_count=int(row[16] or 0),
        )

    def _result(
        self,
        task_run_id: str,
        *,
        idempotent: bool,
        linked_to_session: bool | None = None,
    ) -> MCPTaskRunResult:
        row = self.conn.execute(
            """
            SELECT runtime_session_id, status, outcome, verification_passed,
                   completed_at, session_linked_at
            FROM mcp_task_runs WHERE id = ?
            """,
            (task_run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"MCP task run not found: {task_run_id}")
        session_id = str(row[0] or "")
        if linked_to_session is None:
            linked_to_session = bool(row[5])
        return MCPTaskRunResult(
            task_run_id=task_run_id,
            status=str(row[1]),
            outcome=MCPTaskOutcome(str(row[2])) if row[2] is not None else None,
            verification_passed=None if row[3] is None else bool(row[3]),
            completed_at=row[4],
            runtime_session_id=session_id or None,
            linked_to_session=linked_to_session,
            idempotent=idempotent,
        )


class TaskRunReconciler:
    """Link completed MCP task runs after their runtime sessions are normalized."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def reconcile(
        self,
        *,
        session_ids: set[str] | None = None,
        task_run_ids: set[str] | None = None,
        commit: bool = True,
    ) -> TaskRunReconciliationResult:
        normalized_sessions = {item for item in session_ids or set() if item}
        normalized_runs = {item for item in task_run_ids or set() if item}
        if session_ids is not None and not normalized_sessions:
            return TaskRunReconciliationResult()
        if task_run_ids is not None and not normalized_runs:
            return TaskRunReconciliationResult()

        clauses = ["tr.status = 'completed'"]
        params: list[str] = []
        if session_ids is not None:
            placeholders = ",".join("?" for _ in normalized_sessions)
            clauses.append(f"tr.runtime_session_id IN ({placeholders})")
            params.extend(sorted(normalized_sessions))
        if task_run_ids is not None:
            placeholders = ",".join("?" for _ in normalized_runs)
            clauses.append(f"tr.id IN ({placeholders})")
            params.extend(sorted(normalized_runs))
        rows = self.conn.execute(
            f"""
            SELECT tr.id, tr.runtime_session_id, tr.selected_skills_json,
                   tr.outcome, tr.verification_passed,
                   tr.completion_summary_redacted, tr.session_linked_at,
                   tr.session_outcome_recorded, tr.skill_usage_recorded_count
            FROM mcp_task_runs tr
            JOIN sessions s ON s.id = tr.runtime_session_id
            WHERE {' AND '.join(clauses)}
            ORDER BY tr.started_at, tr.id
            """,
            params,
        ).fetchall()
        linked = 0
        unchanged = 0
        failed = 0
        for index, row in enumerate(rows):
            savepoint = f"reconcile_mcp_task_run_{index}"
            try:
                self.conn.execute(f"SAVEPOINT {savepoint}")
                selected_skills = _selected_skills(str(row[2] or "[]"))
                if bool(row[6]) and bool(row[7]) and int(row[8] or 0) >= len(selected_skills):
                    self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    unchanged += 1
                    continue
                now = utc_now()
                outcome = MCPTaskOutcome(str(row[3]))
                verification_passed = None if row[4] is None else bool(row[4])
                self._record_session_outcome(
                    str(row[0]),
                    str(row[1]),
                    outcome=outcome,
                    verification_passed=verification_passed,
                    has_summary=bool(str(row[5] or "")),
                    now=now,
                )
                usage_count = self._record_skill_outcomes(
                    str(row[1]),
                    selected_skills,
                    outcome=outcome,
                    verification_passed=verification_passed,
                    now=now,
                )
                self.conn.execute(
                    """
                    UPDATE mcp_task_runs
                    SET session_linked_at = COALESCE(session_linked_at, ?),
                        session_outcome_recorded = 1,
                        skill_usage_recorded_count = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, usage_count, now, row[0]),
                )
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                linked += 1
            except (sqlite3.Error, TypeError, ValueError):
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                failed += 1
        if commit:
            self.conn.commit()
        return TaskRunReconciliationResult(
            scanned=len(rows),
            linked=linked,
            unchanged=unchanged,
            failed=failed,
        )

    def _record_session_outcome(
        self,
        task_run_id: str,
        session_id: str,
        *,
        outcome: MCPTaskOutcome,
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
                outcome.value,
                0.9 if verification_passed is not None else 0.7,
                json.dumps(verification, sort_keys=True),
                now,
                now,
            ),
        )

    def _record_skill_outcomes(
        self,
        session_id: str,
        selected_skills: list[MCPSelectedSkillRef],
        *,
        outcome: MCPTaskOutcome,
        verification_passed: bool | None,
        now: str,
    ) -> int:
        recorded = 0
        for skill in selected_skills:
            if not skill.skill_id:
                continue
            usage_id = (
                "skill_usage_"
                + hashlib.sha256(f"{skill.skill_id}:{session_id}".encode()).hexdigest()[:24]
            )
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
                    skill.skill_id,
                    skill.version_id or None,
                    session_id,
                    outcome.value,
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
            recorded += 1
        return recorded


def _selected_skills(raw: str) -> list[MCPSelectedSkillRef]:
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise TypeError("selected_skills_json must contain a list")
    return [MCPSelectedSkillRef.model_validate(item) for item in payload]
