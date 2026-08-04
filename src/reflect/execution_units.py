from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any


def _execution_id(prefix: str, *parts: object) -> str:
    identity = "\x1f".join(str(part or "") for part in parts)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"execution_{prefix}_{digest}"


def execution_unit_id_for_run(task_run_id: str) -> str:
    return _execution_id("mcp", task_run_id)


@dataclass(frozen=True, slots=True)
class ExecutionBoundary:
    id: str
    session_id: str
    source: str
    confidence: float
    started_at: str
    ended_at: str | None
    status: str
    outcome: str | None = None
    verification_passed: bool | None = None
    mcp_task_run_id: str | None = None
    anchor_step_id: str | None = None


class ExecutionUnitRepository:
    """Own persistence for execution-unit lifecycle and step membership."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def sync_task_run(
        self,
        task_run_id: str,
        session_id: str,
        *,
        now: str,
    ) -> str | None:
        session = self._session(session_id)
        run = self.conn.execute(
            """
            SELECT started_at, completed_at, status, outcome, verification_passed
            FROM mcp_task_runs WHERE id = ?
            """,
            (task_run_id,),
        ).fetchone()
        if run is None:
            raise KeyError(f"MCP task run not found: {task_run_id}")
        if session is None:
            return None
        execution_unit_id = execution_unit_id_for_run(task_run_id)
        self.upsert(
            ExecutionBoundary(
                id=execution_unit_id,
                session_id=session_id,
                source="mcp_task_run",
                confidence=1.0,
                started_at=str(run[0]),
                ended_at=str(run[1]) if run[1] else None,
                status=str(run[2]),
                outcome=str(run[3]) if run[3] else None,
                verification_passed=None if run[4] is None else bool(run[4]),
                mcp_task_run_id=task_run_id,
            ),
            session,
            now=now,
        )
        self.link_task_run(task_run_id, execution_unit_id, now=now)
        return execution_unit_id

    def ensure_session_fallback(self, session_id: str, *, now: str) -> str | None:
        session = self._session(session_id)
        if session is None:
            return None
        execution_unit_id = f"execution_session_{session_id}"
        self.upsert(
            ExecutionBoundary(
                id=execution_unit_id,
                session_id=session_id,
                source="session_fallback",
                confidence=0.5,
                started_at=str(session[4]),
                ended_at=str(session[5]) if session[5] else None,
                status=str(session[6]),
            ),
            session,
            now=now,
        )
        return execution_unit_id

    def upsert(
        self,
        boundary: ExecutionBoundary,
        session: sqlite3.Row | tuple[Any, ...],
        *,
        now: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO execution_units(
              id, session_id, mcp_task_run_id, source, source_confidence,
              workspace_id, repo_id, agent_id, started_at, ended_at, status,
              outcome, verification_passed, eligible, boundary_json,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              mcp_task_run_id = excluded.mcp_task_run_id,
              source = excluded.source,
              source_confidence = excluded.source_confidence,
              workspace_id = excluded.workspace_id,
              repo_id = excluded.repo_id,
              agent_id = excluded.agent_id,
              started_at = excluded.started_at,
              ended_at = excluded.ended_at,
              status = excluded.status,
              outcome = excluded.outcome,
              verification_passed = excluded.verification_passed,
              boundary_json = excluded.boundary_json,
              updated_at = excluded.updated_at
            """,
            (
                boundary.id,
                boundary.session_id,
                boundary.mcp_task_run_id,
                boundary.source,
                boundary.confidence,
                session[1],
                session[2],
                session[3],
                boundary.started_at,
                boundary.ended_at,
                boundary.status,
                boundary.outcome,
                None
                if boundary.verification_passed is None
                else int(boundary.verification_passed),
                json.dumps(
                    {
                        "anchor_step_id": boundary.anchor_step_id,
                        "source": boundary.source,
                    },
                    sort_keys=True,
                ),
                now,
                now,
            ),
        )

    def assign_steps(
        self,
        execution_unit_id: str,
        session_id: str,
        step_ids: list[str],
        *,
        now: str,
    ) -> None:
        self.conn.executemany(
            """
            INSERT INTO execution_unit_steps(execution_unit_id, step_id, session_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            [(execution_unit_id, step_id, session_id, now) for step_id in step_ids],
        )

    def link_task_run(self, task_run_id: str, execution_unit_id: str, *, now: str) -> None:
        self.conn.execute(
            "UPDATE mcp_task_runs SET execution_unit_id = ?, updated_at = ? WHERE id = ?",
            (execution_unit_id, now, task_run_id),
        )

    def _session(self, session_id: str) -> sqlite3.Row | tuple[Any, ...] | None:
        return self.conn.execute(
            """
            SELECT id, workspace_id, repo_id, agent_id, started_at, ended_at, status
            FROM sessions WHERE id = ?
            """,
            (session_id,),
        ).fetchone()


class ExecutionUnitService:
    """Derive bounded executions beneath long-lived provider sessions."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        repository: ExecutionUnitRepository | None = None,
    ):
        self.conn = conn
        self.repository = repository or ExecutionUnitRepository(conn)

    def refresh(self, *, session_ids: set[str] | None = None) -> dict[str, int]:
        scoped_ids = (
            None
            if session_ids is None
            else sorted({str(item) for item in session_ids if item})
        )
        if scoped_ids == []:
            return {
                "execution_units": 0,
                "explicit": 0,
                "inferred": 0,
                "assigned_steps": 0,
            }
        now = datetime.now(UTC).isoformat()
        self._prepare_scope(scoped_ids)
        scope_join = (
            "JOIN reflect_execution_session_scope scope "
            "ON scope.session_id = sessions.id"
            if scoped_ids is not None
            else ""
        )
        sessions = self.conn.execute(
            f"""
            SELECT sessions.id, sessions.workspace_id, sessions.repo_id,
                   sessions.agent_id, sessions.started_at, sessions.ended_at,
                   sessions.status
            FROM sessions
            {scope_join}
            ORDER BY started_at, id
            """
        ).fetchall()
        explicit_by_session = self._explicit_boundaries(scoped=scoped_ids is not None)
        prompt_steps = self._prompt_steps(scoped=scoped_ids is not None)

        if scoped_ids is None:
            self.conn.execute("DELETE FROM execution_unit_steps")
            self.conn.execute("UPDATE execution_units SET eligible = 0, updated_at = ?", (now,))
        else:
            self.conn.execute(
                """
                DELETE FROM execution_unit_steps
                WHERE session_id IN (SELECT session_id FROM reflect_execution_session_scope)
                """
            )
            self.conn.execute(
                """
                UPDATE execution_units
                SET eligible = 0, updated_at = ?
                WHERE session_id IN (SELECT session_id FROM reflect_execution_session_scope)
                """,
                (now,),
            )

        explicit_count = 0
        inferred_count = 0
        assigned_steps = 0
        for session in sessions:
            session_id = str(session[0])
            steps = self.conn.execute(
                """
                SELECT id, started_at, seq
                FROM steps
                WHERE session_id = ?
                ORDER BY started_at, seq, id
                """,
                (session_id,),
            ).fetchall()
            boundaries = explicit_by_session.get(session_id, [])
            claimed: set[str] = set()
            for boundary in boundaries:
                self.repository.upsert(boundary, session, now=now)
                matching = [
                    str(step[0])
                    for step in steps
                    if self._inside(str(step[1]), boundary.started_at, boundary.ended_at)
                    and str(step[0]) not in claimed
                ]
                self.repository.assign_steps(boundary.id, session_id, matching, now=now)
                claimed.update(matching)
                assigned_steps += len(matching)
                explicit_count += 1
                if boundary.mcp_task_run_id:
                    self.repository.link_task_run(
                        boundary.mcp_task_run_id,
                        boundary.id,
                        now=now,
                    )

            remaining = [step for step in steps if str(step[0]) not in claimed]
            for boundary, step_ids in self._fallback_boundaries(
                session,
                remaining,
                prompt_steps.get(session_id, set()),
            ):
                self.repository.upsert(boundary, session, now=now)
                self.repository.assign_steps(boundary.id, session_id, step_ids, now=now)
                assigned_steps += len(step_ids)
                inferred_count += 1

        eligibility_scope = (
            "WHERE session_id IN (SELECT session_id FROM reflect_execution_session_scope)"
            if scoped_ids is not None
            else ""
        )
        self.conn.execute(
            f"""
            UPDATE execution_units
            SET eligible = CASE
                  WHEN EXISTS(
                    SELECT 1 FROM execution_unit_steps eus
                    WHERE eus.execution_unit_id = execution_units.id
                  ) THEN 1 ELSE 0 END,
                updated_at = ?
            {eligibility_scope}
            """,
            (now,),
        )
        self.conn.commit()
        self._drop_scope(scoped_ids)
        return {
            "execution_units": explicit_count + inferred_count,
            "explicit": explicit_count,
            "inferred": inferred_count,
            "assigned_steps": assigned_steps,
        }

    def _prepare_scope(self, scoped_ids: list[str] | None) -> None:
        if scoped_ids is None:
            return
        self.conn.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS
              reflect_execution_session_scope(session_id TEXT PRIMARY KEY)
            """
        )
        self.conn.execute("DELETE FROM reflect_execution_session_scope")
        self.conn.executemany(
            "INSERT INTO reflect_execution_session_scope(session_id) VALUES (?)",
            ((session_id,) for session_id in scoped_ids),
        )

    def _drop_scope(self, scoped_ids: list[str] | None) -> None:
        if scoped_ids is not None:
            self.conn.execute("DROP TABLE IF EXISTS reflect_execution_session_scope")

    def _explicit_boundaries(self, *, scoped: bool) -> dict[str, list[ExecutionBoundary]]:
        scope_join = (
            "JOIN reflect_execution_session_scope scope "
            "ON scope.session_id = mcp_task_runs.runtime_session_id"
            if scoped
            else ""
        )
        rows = self.conn.execute(
            f"""
            SELECT id, runtime_session_id, started_at, completed_at, status,
                   outcome, verification_passed
            FROM mcp_task_runs
            {scope_join}
            WHERE runtime_session_id IS NOT NULL
            ORDER BY runtime_session_id, started_at, id
            """
        ).fetchall()
        grouped: dict[str, list[ExecutionBoundary]] = defaultdict(list)
        for row in rows:
            task_run_id = str(row[0])
            session_id = str(row[1])
            grouped[session_id].append(
                ExecutionBoundary(
                    id=execution_unit_id_for_run(task_run_id),
                    session_id=session_id,
                    source="mcp_task_run",
                    confidence=1.0,
                    started_at=str(row[2]),
                    ended_at=str(row[3]) if row[3] else None,
                    status=str(row[4]),
                    outcome=str(row[5]) if row[5] else None,
                    verification_passed=None if row[6] is None else bool(row[6]),
                    mcp_task_run_id=task_run_id,
                )
            )
        for boundaries in grouped.values():
            for index, boundary in enumerate(boundaries[:-1]):
                next_started_at = boundaries[index + 1].started_at
                if boundary.ended_at is None or boundary.ended_at > next_started_at:
                    boundaries[index] = replace(boundary, ended_at=next_started_at)
        return grouped

    def _prompt_steps(self, *, scoped: bool) -> dict[str, set[str]]:
        scope_join = (
            "JOIN reflect_execution_session_scope scope "
            "ON scope.session_id = conversation_facts.session_id"
            if scoped
            else ""
        )
        rows = self.conn.execute(
            f"""
            SELECT conversation_facts.session_id, conversation_facts.step_id
            FROM conversation_facts
            {scope_join}
            WHERE kind = 'prompt' AND role = 'user'
            """
        ).fetchall()
        result: dict[str, set[str]] = defaultdict(set)
        for session_id, step_id in rows:
            result[str(session_id)].add(str(step_id))
        return result

    def _fallback_boundaries(
        self,
        session: sqlite3.Row | tuple[Any, ...],
        steps: list[sqlite3.Row | tuple[Any, ...]],
        prompt_step_ids: set[str],
    ) -> list[tuple[ExecutionBoundary, list[str]]]:
        session_id = str(session[0])
        if not steps:
            return []
        groups: list[list[sqlite3.Row | tuple[Any, ...]]] = []
        current: list[sqlite3.Row | tuple[Any, ...]] = []
        for step in steps:
            step_id = str(step[0])
            if step_id in prompt_step_ids and current:
                groups.append(current)
                current = []
            current.append(step)
        if current:
            groups.append(current)

        boundaries: list[tuple[ExecutionBoundary, list[str]]] = []
        for group in groups:
            anchor = next(
                (str(step[0]) for step in group if str(step[0]) in prompt_step_ids),
                None,
            )
            source = "prompt_fallback" if anchor else "session_fallback"
            confidence = 0.65 if anchor else 0.5
            execution_unit_id = (
                _execution_id("prompt", session_id, anchor)
                if anchor
                else f"execution_session_{session_id}"
            )
            boundaries.append(
                (
                    ExecutionBoundary(
                        id=execution_unit_id,
                        session_id=session_id,
                        source=source,
                        confidence=confidence,
                        started_at=str(group[0][1]),
                        ended_at=str(group[-1][1]),
                        status=str(session[6]),
                        anchor_step_id=anchor,
                    ),
                    [str(step[0]) for step in group],
                )
            )
        return boundaries

    @staticmethod
    def _inside(value: str, started_at: str, ended_at: str | None) -> bool:
        return value >= started_at and (ended_at is None or value < ended_at)


__all__ = [
    "ExecutionBoundary",
    "ExecutionUnitRepository",
    "ExecutionUnitService",
    "execution_unit_id_for_run",
]
