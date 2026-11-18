from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from reflect.context_artifacts import TaskContractArtifact, task_contract_artifact
from reflect.execution_units import ExecutionUnitRepository
from reflect.improvements.repository import utc_now
from reflect.schema.base import ReflectModel
from reflect.usage import RuntimeSessionHint, UsageService


@dataclass(frozen=True)
class TaskReportRefreshInputs:
    session_ids: frozenset[str]
    requires_full_refresh: bool
    snapshot: dict[str, dict[str, object]]


class TaskReportRefreshState:
    """Checkpoint report inputs independently of derived linkage bookkeeping.

    Inspect before derivation and mark that same captured input after success.
    Reports arriving during derivation then remain visible to the next refresh.
    Stored session IDs retain the old scope when a report moves or disappears.
    """

    METADATA_KEY = "task_report_inputs_v1"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def inspect(self) -> TaskReportRefreshInputs:
        receipts = dict(self.conn.execute(
            "SELECT task_run_id, MAX(rowid) FROM task_event_receipts GROUP BY task_run_id"
        ))
        milestones: dict[str, list[tuple]] = {}
        for task_id, event_id, details, created_at in self.conn.execute(
            """SELECT entity_id, id, details_json, created_at FROM improvement_events
               WHERE entity_type = 'mcp_task_run' AND event_type = 'workflow_milestone'
               ORDER BY entity_id, id"""
        ):
            milestones.setdefault(str(task_id), []).append((event_id, details, created_at))
        linked_sessions: dict[str, set[str]] = {}
        for task_id, session_id in self.conn.execute(
            """SELECT tr.id, eu.session_id FROM mcp_task_runs tr
               JOIN execution_units eu ON eu.id = tr.execution_unit_id
               UNION SELECT mcp_task_run_id, session_id FROM execution_units
               WHERE mcp_task_run_id IS NOT NULL"""
        ):
            linked_sessions.setdefault(str(task_id), set()).add(str(session_id))
        snapshot: dict[str, dict[str, object]] = {}
        for row in self.conn.execute(
            """SELECT id, runtime_session_id, runtime_agent, workspace_path,
                      task_file_path, question_hash, workflow_id, selected_skills_json,
                      selected_memories_json, task_contract_id, task_contract_hash,
                      status, outcome, verification_passed, completion_summary_redacted,
                      started_at, completed_at, runtime_identity_source,
                      runtime_identity_evidence_id, runtime_identity_conflict
               FROM mcp_task_runs ORDER BY id"""
        ):
            task_id = str(row[0])
            sessions = linked_sessions.get(task_id, set())
            if row[1]:
                sessions.add(str(row[1]))
            payload = [list(row), receipts.get(task_id), milestones.get(task_id, [])]
            snapshot[task_id] = {
                "fingerprint": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                "session_ids": sorted(sessions),
            }
        previous = self._stored_snapshot()
        changed_sessions: set[str] = set()
        for task_id in snapshot.keys() | (previous or {}).keys():
            old = (previous or {}).get(task_id, {})
            current = snapshot.get(task_id, {})
            if previous is None or old.get("fingerprint") != current.get("fingerprint"):
                changed_sessions.update(old.get("session_ids", []))
                changed_sessions.update(current.get("session_ids", []))
        return TaskReportRefreshInputs(frozenset(changed_sessions), previous is None, snapshot)

    def mark_current(self, inputs: TaskReportRefreshInputs) -> None:
        """Persist the captured inputs in the caller's successful derive transaction."""
        self.conn.execute(
            """INSERT INTO store_metadata(key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (self.METADATA_KEY, json.dumps(inputs.snapshot, sort_keys=True), utc_now()),
        )

    def _stored_snapshot(self) -> dict[str, dict[str, object]] | None:
        row = self.conn.execute(
            "SELECT value FROM store_metadata WHERE key = ?", (self.METADATA_KEY,),
        ).fetchone()
        if row is None:
            return None
        try:
            snapshot = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        if not isinstance(snapshot, dict):
            return None
        for task_id, entry in snapshot.items():
            if not isinstance(task_id, str) or not isinstance(entry, dict):
                return None
            fingerprint, sessions = entry.get("fingerprint"), entry.get("session_ids")
            if (
                not isinstance(fingerprint, str) or len(fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in fingerprint)
                or not isinstance(sessions, list)
                or any(not isinstance(session, str) or not session for session in sessions)
            ):
                return None
        return snapshot


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


class MCPSelectedMemoryRef(ReflectModel):
    """Stable, content-free memory identity captured when context is delivered."""

    memory_id: str
    provider: str
    validation_status: str = ""
    content_hash: str = ""


class MCPTaskRunLinkState(StrEnum):
    """Observable relationship between a task run and normalized telemetry."""

    NO_RUNTIME_SESSION = "no_runtime_session"
    PENDING_INGESTION = "pending_ingestion"
    SESSION_AVAILABLE = "session_available"
    READY_TO_RECONCILE = "ready_to_reconcile"
    LINKED = "linked"
    CONFLICTING_IDENTITY = "conflicting_identity"


class MCPTaskRunStatus(ReflectModel):
    """Privacy-safe status for one MCP task lifecycle."""

    task_run_id: str
    status: str
    outcome: MCPTaskOutcome | None = None
    verification_passed: bool | None = None
    summary_redacted: str = ""
    workflow_id: str | None = None
    selected_skills: list[MCPSelectedSkillRef] = Field(default_factory=list)
    selected_memories: list[MCPSelectedMemoryRef] = Field(default_factory=list)
    workspace_path: str
    task_file_path: str | None = None
    task_contract_id: str | None = None
    task_contract_hash: str | None = None
    runtime_session_id: str | None = None
    runtime_agent: str | None = None
    runtime_identity_source: str | None = None
    runtime_identity_evidence_id: str | None = None
    runtime_identity_conflict: str | None = None
    execution_unit_id: str | None = None
    milestone_count: int = Field(default=0, ge=0)
    link_state: MCPTaskRunLinkState
    session_outcome_recorded: bool = False
    skill_usage_recorded_count: int = Field(default=0, ge=0)
    memory_exposure_recorded_count: int = Field(default=0, ge=0)
    started_at: str
    completed_at: str | None = None
    updated_at: str
    tracking_state: str = "applied"
    projection_error: str | None = None


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
    execution_unit_id: str | None = None
    linked_to_session: bool
    idempotent: bool
    tracking_state: str = "applied"
    link_state: MCPTaskRunLinkState = MCPTaskRunLinkState.NO_RUNTIME_SESSION


class TaskStart(ReflectModel):
    """Frozen, privacy-safe input to task persistence, including contract identity."""

    task_run_id: str
    question_hash: str
    workspace_path: str
    task_file_path: str | None
    workflow_id: str | None
    selected_skills: list[MCPSelectedSkillRef]
    selected_memories: list[MCPSelectedMemoryRef]
    runtime_session_id: str | None
    runtime_agent: str | None
    runtime_identity_source: str | None
    started_at: str
    contract: TaskContractArtifact | None = None

    @classmethod
    def capture(cls, *, question: str, workspace_path: Path, task_file_path: Path | None,
                workflow_id: str | None, selected_skills: list[MCPSelectedSkillRef],
                selected_memories: list[MCPSelectedMemoryRef],
                runtime_session_id: str | None = None, runtime_agent: str | None = None,
                session_hint: RuntimeSessionHint | None = None) -> TaskStart:
        explicit_id = (runtime_session_id or "").strip() or None
        session_id = explicit_id or (session_hint.session_id if session_hint else None)
        return cls(
            task_run_id=f"mcp_task_{uuid.uuid4().hex}",
            question_hash=hashlib.sha256(question.encode("utf-8")).hexdigest(),
            workspace_path=str(workspace_path), task_file_path=str(task_file_path) if task_file_path else None,
            workflow_id=workflow_id, selected_skills=selected_skills, selected_memories=selected_memories,
            runtime_session_id=session_id,
            runtime_agent=runtime_agent if explicit_id else (session_hint.agent if session_hint else None),
            runtime_identity_source="explicit_argument" if explicit_id else "environment" if session_id else None,
            started_at=utc_now(),
            contract=task_contract_artifact(task_file_path, workspace_root=workspace_path) if task_file_path else None,
        )

    def status(self) -> MCPTaskRunStatus:
        return MCPTaskRunStatus(
            **self.model_dump(exclude={"question_hash", "contract"}),
            status="started", updated_at=self.started_at, tracking_state="accepted_pending",
            task_contract_id=self.contract.artifact_id if self.contract else None,
            task_contract_hash=self.contract.content_hash if self.contract else None,
            link_state=MCPTaskRunLinkState.PENDING_INGESTION if self.runtime_session_id else MCPTaskRunLinkState.NO_RUNTIME_SESSION,
        )


def validate_completion(task: MCPTaskRunStatus, *, outcome: MCPTaskOutcome | str,
                        verification_passed: bool | None = None, summary_redacted: str = "",
                        runtime_session_id: str | None = None, runtime_agent: str | None = None
                        ) -> tuple[MCPTaskOutcome, str, str | None, bool]:
    normalized = MCPTaskOutcome(outcome)
    summary = summary_redacted.strip()[:1000]
    explicit_id = (runtime_session_id or "").strip() or None
    if explicit_id and ((task.runtime_session_id and task.runtime_session_id != explicit_id)
                        or (task.runtime_agent and runtime_agent and task.runtime_agent != runtime_agent)):
        raise ValueError("Runtime identity conflicts with the task's recorded identity")
    completed = task.status == "completed"
    if completed and (task.outcome != normalized or task.verification_passed != verification_passed
                      or task.summary_redacted != summary):
        raise RuntimeError(f"MCP task run {task.task_run_id} is already completed; start a new guidance run for changed work")
    return normalized, summary, explicit_id, completed


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
        self.execution_units = ExecutionUnitRepository(conn)
        self.reconciler = TaskRunReconciler(
            conn,
            execution_units=self.execution_units,
        )

    def start(
        self,
        *,
        question: str,
        workspace_path: Path,
        task_file_path: Path | None,
        workflow_id: str | None,
        selected_skills: list[MCPSelectedSkillRef],
        selected_memories: list[MCPSelectedMemoryRef],
        runtime_session_id: str | None = None,
        runtime_agent: str | None = None,
    ) -> str:
        return self.apply_start(TaskStart.capture(
            question=question, workspace_path=workspace_path, task_file_path=task_file_path,
            workflow_id=workflow_id, selected_skills=selected_skills, selected_memories=selected_memories,
            runtime_session_id=runtime_session_id, runtime_agent=runtime_agent,
            session_hint=self.usage.runtime_session_hint(),
        ))

    def apply_start(self, start: TaskStart, *, commit: bool = True) -> str:
        task_run_id, now = start.task_run_id, start.started_at
        session_id, agent = start.runtime_session_id, start.runtime_agent
        contract = start.contract
        if contract:
            self._register_task_contract(contract, owner=agent, now=now)
        self.conn.execute(
            """
            INSERT INTO mcp_task_runs(
              id, runtime_session_id, runtime_agent, workspace_path, task_file_path,
              task_contract_id, task_contract_hash, question_hash, workflow_id,
              selected_skills_json, selected_memories_json, status,
              started_at, created_at, updated_at, runtime_identity_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', ?, ?, ?, ?)
            """,
            (
                task_run_id,
                session_id,
                agent,
                start.workspace_path,
                start.task_file_path,
                contract.artifact_id if contract else None,
                contract.content_hash if contract else None,
                start.question_hash,
                start.workflow_id,
                json.dumps(
                    [skill.model_dump(mode="json") for skill in start.selected_skills],
                    sort_keys=True,
                ),
                json.dumps(
                    [memory.model_dump(mode="json") for memory in start.selected_memories],
                    sort_keys=True,
                ),
                now,
                now,
                now,
                start.runtime_identity_source,
            ),
        )
        if session_id:
            self.execution_units.sync_task_run(
                task_run_id,
                session_id,
                now=now,
            )
        if commit:
            self.conn.commit()
        return task_run_id

    def _register_task_contract(
        self,
        contract: TaskContractArtifact,
        *,
        owner: str | None,
        now: str,
    ) -> TaskContractArtifact:
        self.conn.execute(
            """
            INSERT INTO specs(
              id, title, status, owner, source_path, content_hash, created_at, updated_at
            ) VALUES (?, ?, 'tracked', ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              title = excluded.title,
              owner = COALESCE(excluded.owner, specs.owner),
              source_path = excluded.source_path,
              content_hash = excluded.content_hash,
              updated_at = excluded.updated_at
            """,
            (
                contract.artifact_id,
                contract.title,
                owner,
                contract.source_path,
                contract.content_hash,
                now,
                now,
            ),
        )
        return contract

    def complete(
        self,
        task_run_id: str,
        *,
        outcome: MCPTaskOutcome | str,
        verification_passed: bool | None = None,
        summary_redacted: str = "",
        runtime_session_id: str | None = None,
        runtime_agent: str | None = None,
        commit: bool = True,
        completed_at: str | None = None,
    ) -> MCPTaskRunResult:
        normalized_outcome, summary, explicit_id, idempotent = validate_completion(
            self.status(task_run_id), outcome=outcome, verification_passed=verification_passed,
            summary_redacted=summary_redacted, runtime_session_id=runtime_session_id, runtime_agent=runtime_agent,
        )
        if idempotent:
            self._complete_identity(task_run_id, explicit_id, runtime_agent)
            self.reconciler.reconcile(task_run_ids={task_run_id}, commit=False)
            if commit:
                self.conn.commit()
            return self._result(task_run_id, idempotent=True)

        now = completed_at or utc_now()
        self._complete_identity(task_run_id, explicit_id, runtime_agent)
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
        if commit:
            self.conn.commit()
        return self._result(
            task_run_id,
            idempotent=False,
            linked_to_session=bool(reconciliation.linked),
        )

    def _complete_identity(self, task_run_id: str, session_id: str | None, agent: str | None) -> None:
        if session_id:
            self.conn.execute(
                """UPDATE mcp_task_runs SET runtime_session_id = ?,
                   runtime_agent = COALESCE(runtime_agent, ?),
                   runtime_identity_source = CASE WHEN runtime_session_id IS NULL
                     THEN 'explicit_argument' ELSE runtime_identity_source END
                   WHERE id = ?""", (session_id, agent, task_run_id),
            )

    def status(self, task_run_id: str) -> MCPTaskRunStatus:
        """Return a read-only, privacy-safe lifecycle snapshot."""

        row = self.conn.execute(
            """
            SELECT id, runtime_session_id, runtime_agent, workspace_path,
                   task_file_path, workflow_id, selected_skills_json,
                   selected_memories_json, status,
                   outcome, verification_passed, completion_summary_redacted,
                   started_at, completed_at, updated_at, session_linked_at,
                   session_outcome_recorded, skill_usage_recorded_count,
                   memory_exposure_recorded_count,
                   execution_unit_id, task_contract_id, task_contract_hash,
                   (
                     SELECT COUNT(*) FROM improvement_events ie
                     WHERE ie.entity_type = 'mcp_task_run'
                       AND ie.entity_id = mcp_task_runs.id
                       AND ie.event_type = 'workflow_milestone'
                   ) AS milestone_count,
                   runtime_identity_source, runtime_identity_evidence_id, runtime_identity_conflict
            FROM mcp_task_runs WHERE id = ?
            """,
            (task_run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"MCP task run not found: {task_run_id}")
        selected_skills = _selected_skills(str(row[6] or "[]"))
        selected_memories = _selected_memories(str(row[7] or "[]"))
        runtime_session_id = str(row[1] or "") or None
        session_exists = bool(
            runtime_session_id
            and self.conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?",
                (runtime_session_id,),
            ).fetchone()
        )
        if row[25]:
            link_state = MCPTaskRunLinkState.CONFLICTING_IDENTITY
        elif row[15]:
            link_state = MCPTaskRunLinkState.LINKED
        elif session_exists and str(row[8]) == "completed":
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
            runtime_identity_source=row[23],
            runtime_identity_evidence_id=row[24],
            runtime_identity_conflict=row[25],
            execution_unit_id=str(row[19]) if row[19] else None,
            task_contract_id=str(row[20]) if row[20] else None,
            task_contract_hash=str(row[21]) if row[21] else None,
            milestone_count=int(row[22] or 0),
            workspace_path=str(row[3]),
            task_file_path=str(row[4]) if row[4] else None,
            workflow_id=str(row[5]) if row[5] else None,
            selected_skills=selected_skills,
            selected_memories=selected_memories,
            status=str(row[8]),
            outcome=MCPTaskOutcome(str(row[9])) if row[9] else None,
            verification_passed=None if row[10] is None else bool(row[10]),
            summary_redacted=str(row[11] or ""),
            started_at=str(row[12]),
            completed_at=str(row[13]) if row[13] else None,
            updated_at=str(row[14]),
            link_state=link_state,
            session_outcome_recorded=bool(row[16]),
            skill_usage_recorded_count=int(row[17] or 0),
            memory_exposure_recorded_count=int(row[18] or 0),
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
                   completed_at, session_linked_at, execution_unit_id, runtime_identity_conflict
            FROM mcp_task_runs WHERE id = ?
            """,
            (task_run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"MCP task run not found: {task_run_id}")
        session_id = str(row[0] or "")
        if linked_to_session is None:
            linked_to_session = bool(row[5])
        if row[7]:
            linked_to_session = False
        return MCPTaskRunResult(
            task_run_id=task_run_id,
            status=str(row[1]),
            outcome=MCPTaskOutcome(str(row[2])) if row[2] is not None else None,
            verification_passed=None if row[3] is None else bool(row[3]),
            completed_at=row[4],
            runtime_session_id=session_id or None,
            execution_unit_id=str(row[6]) if row[6] else None,
            linked_to_session=linked_to_session,
            idempotent=idempotent,
            link_state=self.status(task_run_id).link_state,
        )


class TaskRunReconciler:
    """Link completed MCP task runs after their runtime sessions are normalized."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        execution_units: ExecutionUnitRepository | None = None,
    ) -> None:
        self.conn = conn
        self.execution_units = execution_units or ExecutionUnitRepository(conn)

    def recover_identities(self, session_ids: set[str] | None = None) -> set[str]:
        """Recover only exact task IDs in structured Reflect calls, never temporal guesses."""
        from reflect.store.mcp import DEFAULT_MCP_CLASSIFIER

        if session_ids == set():
            return set()
        scope = ""
        params = sorted(session_ids or ())
        if session_ids is not None:
            scope = f"AND tc.session_id IN ({','.join('?' for _ in params)})"
        rows = self.conn.execute(
            f"""SELECT tc.id, tc.session_id, tc.tool_name, tc.input_preview_redacted,
                       tc.output_preview_redacted, a.name, w.root_path
                FROM tool_calls tc JOIN sessions s ON s.id = tc.session_id
                LEFT JOIN agents a ON a.id = s.agent_id
                LEFT JOIN workspaces w ON w.id = s.workspace_id
                WHERE (tc.tool_name LIKE '%reflect_complete'
                    OR tc.tool_name LIKE '%reflect_context') {scope}""", params,
        )
        candidates: dict[str, dict[str, tuple[str, str | None, str | None]]] = {}
        for evidence_id, session_id, tool, inputs, outputs, agent, workspace in rows:
            # These are encoded MCP identities, not arbitrary names containing a substring.
            suffix = DEFAULT_MCP_CLASSIFIER.identify({"gen_ai.client.tool_name": tool}).tool_name
            if suffix is None:
                suffix = tool.removeprefix("reflect-")
            if suffix not in {"reflect_complete", "reflect_context"}:
                continue
            raw = inputs if suffix == "reflect_complete" else outputs
            try:
                payload = json.loads(raw or "null")
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and isinstance(payload.get("structuredContent"), dict):
                payload = payload["structuredContent"]
            task_id = payload.get("task_run_id") if isinstance(payload, dict) else None
            if isinstance(task_id, str) and task_id.startswith("mcp_task_"):
                candidates.setdefault(task_id, {})[session_id] = (evidence_id, agent, workspace)
        recovered = set()
        for task_id, matches in candidates.items():
            run = self.conn.execute(
                """SELECT runtime_session_id, runtime_agent, workspace_path,
                          runtime_identity_conflict FROM mcp_task_runs WHERE id = ?""", (task_id,),
            ).fetchone()
            if run is None or run[3]:
                continue
            session_id = next(iter(matches))
            evidence_id, agent, workspace = matches[session_id]
            conflict = None
            if len(matches) != 1 or (run[0] and run[0] != session_id):
                conflict = "Exact task ID occurs in conflicting runtime sessions."
            elif run[1] and agent and run[1] != agent:
                conflict = "Telemetry agent conflicts with the recorded runtime agent."
            elif workspace and run[2] and not (
                Path(run[2]).is_relative_to(Path(workspace))
                or Path(workspace).is_relative_to(Path(run[2]))
            ):
                conflict = "Telemetry workspace conflicts with the task workspace."
            if conflict:
                self.conn.execute(
                    "UPDATE mcp_task_runs SET runtime_identity_conflict = ? WHERE id = ?",
                    (conflict, task_id),
                )
                self.conn.execute(
                    "UPDATE execution_units SET eligible = 0 WHERE mcp_task_run_id = ?", (task_id,),
                )
                if run[0]:
                    recovered.add(run[0])
            elif not run[0]:
                self.conn.execute(
                    """UPDATE mcp_task_runs SET runtime_session_id = ?, runtime_agent = ?,
                       runtime_identity_source = 'telemetry_correlation',
                       runtime_identity_evidence_id = ?, updated_at = ? WHERE id = ?""",
                    (session_id, agent, evidence_id, utc_now(), task_id),
                )
                recovered.add(session_id)
        return recovered

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

        clauses = ["tr.status = 'completed'", "tr.runtime_identity_conflict IS NULL"]
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
                   tr.selected_memories_json, tr.outcome, tr.verification_passed,
                   tr.completion_summary_redacted, tr.session_linked_at,
                   tr.session_outcome_recorded, tr.skill_usage_recorded_count,
                   tr.memory_exposure_recorded_count
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
                selected_memories = _selected_memories(str(row[3] or "[]"))
                if (
                    bool(row[7])
                    and bool(row[8])
                    and int(row[9] or 0) >= len(selected_skills)
                    and int(row[10] or 0) >= len(selected_memories)
                ):
                    self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    unchanged += 1
                    continue
                now = utc_now()
                outcome = MCPTaskOutcome(str(row[4]))
                verification_passed = None if row[5] is None else bool(row[5])
                execution_unit_id = self.execution_units.sync_task_run(
                    str(row[0]),
                    str(row[1]),
                    now=now,
                )
                if execution_unit_id is None:
                    raise KeyError(f"Session not found: {row[1]}")
                self._record_session_outcome(
                    str(row[0]),
                    str(row[1]),
                    outcome=outcome,
                    verification_passed=verification_passed,
                    has_summary=bool(str(row[6] or "")),
                    now=now,
                )
                usage_count = self._record_skill_outcomes(
                    str(row[1]),
                    execution_unit_id,
                    selected_skills,
                    task_run_id=str(row[0]),
                    outcome=outcome,
                    verification_passed=verification_passed,
                    now=now,
                )
                memory_count = self._record_memory_exposures(
                    str(row[1]),
                    selected_memories,
                    now=now,
                )
                self.conn.execute(
                    """
                    UPDATE mcp_task_runs
                    SET session_linked_at = COALESCE(session_linked_at, ?),
                        session_outcome_recorded = 1,
                        skill_usage_recorded_count = ?,
                        memory_exposure_recorded_count = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, usage_count, memory_count, now, row[0]),
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
        execution_unit_id: str,
        selected_skills: list[MCPSelectedSkillRef],
        *,
        task_run_id: str,
        outcome: MCPTaskOutcome,
        verification_passed: bool | None,
        now: str,
    ) -> int:
        recorded = 0
        observed_owner = self.conn.execute(
            "SELECT 1 FROM execution_units eu WHERE eu.id=? AND eu.source <> 'mcp_report' "
            "AND EXISTS (SELECT 1 FROM execution_unit_steps eus WHERE eus.execution_unit_id=eu.id)",
            (execution_unit_id,),
        ).fetchone()
        for skill in selected_skills:
            if not skill.skill_id:
                continue
            source_id = task_run_id
            usage_id = (
                "skill_usage_"
                + hashlib.sha256(f"{skill.skill_id}:task_report:{source_id}".encode()).hexdigest()[:24]
            )
            self.conn.execute(
                """
                INSERT INTO skill_usage(
                  id, skill_id, skill_version_id, session_id, execution_unit_id, source_kind, source_id, state, outcome,
                  confidence, evidence_json, observed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'task_report', ?, 'reported', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(skill_id, source_kind, source_id) DO UPDATE SET
                  skill_version_id = excluded.skill_version_id,
                  session_id = excluded.session_id,
                  execution_unit_id = excluded.execution_unit_id,
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
                    execution_unit_id if observed_owner else None,
                    source_id,
                    outcome.value,
                    0.9 if verification_passed is not None else 0.7,
                    json.dumps(
                        {
                            "source": "reflect_complete",
                            "task_run_id": task_run_id,
                            "relationship": "reported_use",
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

    def _record_memory_exposures(
        self,
        session_id: str,
        selected_memories: list[MCPSelectedMemoryRef],
        *,
        now: str,
    ) -> int:
        recorded = 0
        seen: set[str] = set()
        for memory in selected_memories:
            if not memory.memory_id or memory.memory_id in seen:
                continue
            seen.add(memory.memory_id)
            self.conn.execute(
                """
                INSERT INTO memory_exposures(
                  memory_id, session_id, step_id, content_hash, source,
                  observed_at, created_at, updated_at
                ) VALUES (?, ?, NULL, NULLIF(?, ''), 'reflect_context', ?, ?, ?)
                ON CONFLICT(memory_id, session_id) DO UPDATE SET
                  content_hash = COALESCE(
                    excluded.content_hash,
                    memory_exposures.content_hash
                  ),
                  source = 'reflect_context',
                  observed_at = excluded.observed_at,
                  updated_at = excluded.updated_at
                """,
                (
                    memory.memory_id,
                    session_id,
                    memory.content_hash,
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


def _selected_memories(raw: str) -> list[MCPSelectedMemoryRef]:
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise TypeError("selected_memories_json must contain a list")
    return [MCPSelectedMemoryRef.model_validate(item) for item in payload]
