from __future__ import annotations

import hashlib
import json
import sqlite3
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from reflect.execution_assessment import EditObservation, assess_execution, attrs_dict, edit_paths
from reflect.telemetry_facts import event_time, observed_turn_terminal


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
    native_turn_id: str | None = None


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
        linked = self.conn.execute(
            "SELECT execution_unit_id FROM mcp_task_runs WHERE id = ?", (task_run_id,)
        ).fetchone()
        if linked and linked[0] and self.conn.execute(
            "SELECT 1 FROM execution_unit_steps WHERE execution_unit_id = ? LIMIT 1", (linked[0],)
        ).fetchone():
            return str(linked[0])
        execution_unit_id = execution_unit_id_for_run(task_run_id)
        self.upsert(
            ExecutionBoundary(
                id=execution_unit_id,
                session_id=session_id,
                source="mcp_report",
                confidence=0.0,
                started_at=str(run[0]),
                ended_at=None,
                status="unknown",
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
                        "native_turn_id": boundary.native_turn_id,
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
            ON CONFLICT(step_id) DO UPDATE SET
              execution_unit_id = excluded.execution_unit_id,
              session_id = excluded.session_id,
              created_at = excluded.created_at
            WHERE execution_unit_steps.execution_unit_id <> excluded.execution_unit_id
               OR execution_unit_steps.session_id <> excluded.session_id
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
        self._workspace_edits: dict[tuple[str, str], list[EditObservation]] = {}
        self._workspace_edit_starts: dict[tuple[str, str], str] = {}

    def affected_session_ids(
        self, session_ids: set[str], *,
        step_scope: dict[str, set[str] | None] | None = None,
        consumer_unit_ids: dict[str, set[str]] | None = None,
    ) -> set[str] | None:
        """Find consumers of changed evidence, retaining old ownership on correction.

        Precise changes use their observed intervals and prior owner intervals.
        Missing ownership or broad repairs fall back to the selected session's
        complete bounds. The optional collector contains only bounded consumer
        units outside the requested sessions; broad scopes leave it empty.
        Detector cohorts remain a separate caller-owned scope.
        """
        if consumer_unit_ids is not None:
            consumer_unit_ids.clear()
        selected = {str(item) for item in session_ids if item}
        if not selected:
            return set()
        intervals: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        full_sessions = set(selected)
        for session_id in sorted(selected):
            step_ids = (step_scope or {}).get(session_id)
            if not step_ids or len(step_ids) > 400:
                continue
            session = self.repository._session(session_id)
            if session is None:
                continue
            placeholders = ",".join("?" for _ in step_ids)
            steps = self.conn.execute(
                f"SELECT id,started_at,COALESCE(ended_at,started_at) FROM steps "
                f"WHERE session_id=? AND id IN ({placeholders})",
                (session_id, *sorted(step_ids)),
            ).fetchall()
            # Pending ownership survives a deleted/moved step; current ownership
            # also supports callers which did not enter through normalization.
            owners = self.conn.execute(
                f"""SELECT eus.step_id,eu.session_id,eu.workspace_id,eu.repo_id,eu.started_at,eu.ended_at
                    FROM execution_unit_steps eus JOIN execution_units eu ON eu.id=eus.execution_unit_id
                    WHERE eus.step_id IN ({placeholders})
                    UNION
                    SELECT changed.step_id,eu.session_id,eu.workspace_id,eu.repo_id,eu.started_at,eu.ended_at
                    FROM pending_step_changes changed
                    LEFT JOIN execution_units eu ON eu.id=changed.old_execution_unit_id
                    WHERE changed.phase='improvements' AND changed.session_id=?
                      AND changed.step_id IN ({placeholders}) AND changed.old_execution_unit_id IS NOT NULL""",
                (*sorted(step_ids), session_id, *sorted(step_ids)),
            ).fetchall()
            known_steps = {str(row[0]) for row in steps} | {str(row[0]) for row in owners}
            if (known_steps != step_ids or any(not row[1] or row[2] < row[1] for row in steps)
                    or any(row[1] != session_id or not row[4] or (row[5] and row[5] < row[4]) for row in owners)):
                continue
            full_sessions.remove(session_id)
            changed_intervals = [(session[1], session[2], row[1], row[2]) for row in steps]
            changed_intervals.extend((row[2], row[3], row[4], row[5]) for row in owners)
            for workspace_id, repo_id, start, end in changed_intervals:
                for kind, value in (("workspace_id", workspace_id), ("repo_id", repo_id)):
                    if value:
                        intervals[(kind, str(value))].add((str(start), str(end or start)))
        if full_sessions:
            self._prepare_scope(sorted(full_sessions))
            try:
                current = self.conn.execute(
                    """SELECT s.id,s.workspace_id,s.repo_id,
                              MIN(st.started_at),MAX(COALESCE(st.ended_at,st.started_at))
                       FROM reflect_execution_session_scope scope
                       JOIN sessions s ON s.id=scope.session_id
                       LEFT JOIN steps st ON st.session_id=s.id GROUP BY s.id"""
                ).fetchall()
                previous = self.conn.execute(
                    """SELECT eu.session_id,eu.workspace_id,eu.repo_id,
                              MIN(eu.started_at),MAX(COALESCE(eu.ended_at,eu.started_at))
                       FROM reflect_execution_session_scope scope
                       JOIN execution_units eu ON eu.session_id=scope.session_id
                       GROUP BY eu.session_id,eu.workspace_id,eu.repo_id"""
                ).fetchall()
            finally:
                self._drop_scope(sorted(full_sessions))
            if full_sessions - {str(row[0]) for row in [*current, *previous]}:
                return None  # Deleted source without retained scope cannot be bounded.
            for _, workspace_id, repo_id, start, end in [*current, *previous]:
                if start is None:
                    continue
                for kind, value in (("workspace_id", workspace_id), ("repo_id", repo_id)):
                    if value:
                        intervals[(kind, str(value))].add((str(start), str(end or start)))
        affected = set(selected)
        for (kind, value), spans in intervals.items():
            # Merge overlaps, not gaps between distinct changed tasks.
            merged: list[tuple[str, str]] = []
            for start, end in sorted(spans):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                else:
                    merged.append((start, end))
            index = "idx_sessions_workspace_started" if kind == "workspace_id" else "idx_sessions_repo_started"
            fallback = " AND (s.workspace_id IS NULL OR s.workspace_id='')" if kind == "repo_id" else ""
            for start, end in merged:
                for peer_session, unit_id in self.conn.execute(
                    f"""SELECT DISTINCT eu.session_id,eu.id FROM sessions s INDEXED BY {index}
                        CROSS JOIN execution_units eu INDEXED BY idx_execution_units_session_started
                          ON eu.session_id=s.id
                        WHERE s.{kind}=?{fallback} AND eu.eligible=1
                          AND eu.started_at<=? AND COALESCE(eu.ended_at,eu.started_at)>=?""",
                    (value, end, start),
                ):
                    affected.add(str(peer_session))
                    if consumer_unit_ids is not None and not full_sessions and peer_session not in selected:
                        consumer_unit_ids.setdefault(str(peer_session), set()).add(str(unit_id))
        return affected

    def refresh(
        self, *, session_ids: set[str] | None = None,
        step_scope: dict[str, set[str] | None] | None = None,
        reassess_unit_ids: dict[str, set[str]] | None = None,
    ) -> dict[str, int]:
        """Repair changed owners, then reassess bounded stable consumers in place.

        The caller must durably queue full peer-session repair before supplying
        reassessment scope. A missing/stale consumer falls back to full repair.
        """
        self.changed_unit_ids: set[str] = set()
        self.changed_membership_step_ids: set[str] = set()
        self.reassessed_unit_ids: set[str] = set()
        self.full_repaired_session_ids: set[str] | None = None if session_ids is None else set()
        self._workspace_edits.clear()
        self._workspace_edit_starts.clear()
        consumers = []
        derived_sessions = None if session_ids is None else set(session_ids)
        if derived_sessions is not None and step_scope is not None:
            for session_id, unit_ids in (reassess_unit_ids or {}).items():
                if session_id not in derived_sessions or not unit_ids:
                    continue
                session = self.repository._session(session_id)
                rows = [self.conn.execute(
                    """SELECT id,session_id,source,source_confidence,started_at,ended_at,status,
                              boundary_json,workspace_id,repo_id,agent_id FROM execution_units
                       WHERE id=? AND session_id=? AND eligible=1 AND EXISTS(
                         SELECT 1 FROM execution_unit_steps member WHERE member.execution_unit_id=execution_units.id)""",
                    (unit_id, session_id),
                ).fetchone() for unit_id in sorted(unit_ids)]
                if session is None or any(row is None or tuple(row[8:11]) != tuple(session[1:4]) for row in rows):
                    continue
                consumers.extend((row, session) for row in rows)
                derived_sessions.remove(session_id)
        self.rederived_session_ids = derived_sessions
        totals = {"execution_units": 0, "explicit": 0, "inferred": 0, "assigned_steps": 0}
        if derived_sessions is None or step_scope is None:
            self.full_repaired_session_ids = derived_sessions
            totals = self._refresh_sessions(session_ids=derived_sessions)
        else:
            remaining = set(derived_sessions)
            for session_id in sorted(derived_sessions):
                steps = step_scope.get(session_id)
                if not steps:
                    continue
                repaired = self._refresh_native_changes(session_id, steps)
                if repaired is None:
                    continue
                remaining.remove(session_id)
                for key in totals:
                    totals[key] += repaired[key]
            if remaining:
                self.full_repaired_session_ids = remaining
                repaired = self._refresh_sessions(session_ids=remaining)
                for key in totals:
                    totals[key] += repaired[key]
        self._reassess_consumers(consumers)
        if reassess_unit_ids is not None:
            totals["reassessed_execution_units"] = len(self.reassessed_unit_ids)
        self.conn.commit()
        return totals

    def _reassess_consumers(self, consumers) -> None:
        # Source repair may have cached edits from a later start. Seed all stable
        # consumer intervals before any assessment and read fresh canonical edits.
        self._workspace_edits.clear()
        self._workspace_edit_starts.clear()
        for row, session in consumers:
            if session[1] or session[2]:
                scope = ("workspace_id", str(session[1])) if session[1] else ("repo_id", str(session[2]))
                self._workspace_edit_starts[scope] = min(
                    self._workspace_edit_starts.get(scope, str(row[4])), str(row[4]),
                )
        for row, session in consumers:
            metadata = attrs_dict(row[7])
            boundary = ExecutionBoundary(
                id=str(row[0]), session_id=str(row[1]), source=str(row[2]), confidence=float(row[3]),
                started_at=str(row[4]), ended_at=row[5], status=str(row[6]),
                anchor_step_id=metadata.get("anchor_step_id"), native_turn_id=metadata.get("native_turn_id"),
            )
            self._assess(boundary, session)
            self.reassessed_unit_ids.add(boundary.id)
        self.changed_unit_ids.update(self.reassessed_unit_ids)

    def _refresh_native_changes(self, session_id: str, step_ids: set[str]) -> dict[str, int] | None:
        session = self.repository._session(session_id)
        if session is None or len(step_ids) > 400:
            return None
        placeholders = ','.join('?' for _ in step_ids)
        changed = self.conn.execute(
            f"SELECT id,raw_attrs_json FROM steps WHERE session_id=? AND id IN ({placeholders})",
            (session_id, *sorted(step_ids)),
        ).fetchall()
        if len(changed) != len(step_ids):
            return None  # Retractions without retained ownership require full repair.
        native_ids = set()
        for _, raw in changed:
            attrs = attrs_dict(raw)
            native_id = attrs.get('gen_ai.client.turn_id') or attrs.get('turn.id') or attrs.get('turn_id')
            if not native_id or attrs.get('reflect.turn.identity_provenance') == 'synthetic':
                return None
            native_ids.add(str(native_id))
        old_units = self.conn.execute(
            f"SELECT DISTINCT eu.id,eu.source,eu.boundary_json,eu.workspace_id,eu.repo_id,eu.agent_id "
            f"FROM execution_unit_steps eus JOIN execution_units eu ON eu.id=eus.execution_unit_id "
            f"WHERE eus.step_id IN ({placeholders})", sorted(step_ids),
        ).fetchall()
        # A late edit can invalidate another native turn in this same session.
        # Retained ownership covers edits moved to a different time or group.
        intervals = self.conn.execute(
            f"SELECT started_at,COALESCE(ended_at,started_at) FROM steps WHERE id IN ({placeholders})",
            sorted(step_ids),
        ).fetchall()
        if old_units:
            intervals += self.conn.execute(
                f"SELECT started_at,COALESCE(ended_at,started_at) FROM execution_units WHERE id IN ({','.join('?' for _ in old_units)})",
                [row[0] for row in old_units],
            ).fetchall()
        dependent = {str(row[0]): row for row in old_units}
        for start, end in intervals:
            for row in self.conn.execute(
                "SELECT eu.id,eu.source,eu.boundary_json,eu.workspace_id,eu.repo_id,eu.agent_id "
                "FROM execution_units eu WHERE eu.session_id=? AND eu.eligible=1 "
                "AND eu.started_at<=? AND COALESCE(eu.ended_at,eu.started_at)>=? "
                "AND EXISTS(SELECT 1 FROM execution_unit_steps member WHERE member.execution_unit_id=eu.id)",
                (session_id, end, start),
            ):
                dependent[str(row[0])] = row
        old_units = list(dependent.values())
        for _, source, metadata, workspace, repo, agent in old_units:
            native_id = attrs_dict(metadata).get('native_turn_id')
            if source != 'native_turn' or not native_id or (workspace, repo, agent) != tuple(session[1:4]):
                return None
            native_ids.add(str(native_id))
        native_expr = "CAST(COALESCE(NULLIF(json_extract(raw_attrs_json,'$.\"gen_ai.client.turn_id\"'),''),NULLIF(json_extract(raw_attrs_json,'$.\"turn.id\"'),''),json_extract(raw_attrs_json,'$.turn_id')) AS TEXT)"
        rows = self.conn.execute(
            "SELECT id,started_at,seq,raw_attrs_json,ended_at FROM steps "
            f"WHERE session_id=? AND {native_expr} IN ({','.join('?' for _ in native_ids)}) "
            "ORDER BY started_at,seq,id", (session_id, *sorted(native_ids)),
        ).fetchall()
        if any(attrs_dict(row[3]).get('reflect.turn.identity_provenance') == 'synthetic' for row in rows):
            return None
        boundaries = self._observed_boundaries(session, rows, set())
        affected = {str(row[0]) for row in old_units} | {boundary.id for boundary, _ in boundaries}
        previous = dict(self.conn.execute(
            f"SELECT step_id,execution_unit_id FROM execution_unit_steps WHERE execution_unit_id IN ({','.join('?' for _ in affected)})",
            sorted(affected),
        ))
        now = datetime.now(UTC).isoformat()
        for unit in affected:
            self.conn.execute('DELETE FROM execution_unit_steps WHERE execution_unit_id=?', (unit,))
            self.conn.execute('UPDATE execution_units SET eligible=0,updated_at=? WHERE id=?', (now, unit))
        if session[1] or session[2]:
            scope = ('workspace_id', str(session[1])) if session[1] else ('repo_id', str(session[2]))
            earliest = min(boundary.started_at for boundary, _ in boundaries)
            self._workspace_edit_starts[scope] = earliest
            self._workspace_edits.pop(scope, None)
        desired = {}
        for boundary, members in boundaries:
            self.repository.upsert(boundary, session, now=now)
            self.repository.assign_steps(boundary.id, session_id, members, now=now)
            desired.update(dict.fromkeys(members, boundary.id))
            self._assess(boundary, session)
            self.conn.execute('UPDATE execution_units SET eligible=1 WHERE id=?', (boundary.id,))
        self.changed_membership_step_ids.update(
            step for step in previous.keys() | desired.keys() if previous.get(step) != desired.get(step)
        )
        for retired in affected - {boundary.id for boundary, _ in boundaries}:
            replacements = sorted({desired[step] for step, owner in previous.items() if owner == retired and step in desired})
            self.conn.execute("UPDATE execution_units SET boundary_json=json_set(boundary_json,'$.superseded_by',json(?)) WHERE id=?",
                              (json.dumps(replacements), retired))
        # Reports can span unchanged groups. Relink against compact boundaries,
        # without loading their steps, tool payloads, or assessments again.
        all_boundaries = [(ExecutionBoundary(
            id=row[0], session_id=session_id, source=row[1], confidence=row[2],
            started_at=row[3], ended_at=row[4], status=row[5],
        ), []) for row in self.conn.execute(
            "SELECT id,source,source_confidence,started_at,ended_at,status FROM execution_units "
            "WHERE session_id=? AND EXISTS(SELECT 1 FROM execution_unit_steps eus WHERE eus.execution_unit_id=execution_units.id)",
            (session_id,),
        )]
        self.conn.execute('UPDATE execution_units SET mcp_task_run_id=NULL WHERE session_id=?', (session_id,))
        self._link_reports(session_id, all_boundaries, now=now)
        self.changed_unit_ids.update(affected)
        return {"execution_units": len(boundaries), "explicit": len(boundaries), "inferred": 0,
                "assigned_steps": sum(len(members) for _, members in boundaries)}

    def _refresh_sessions(self, *, session_ids: set[str] | None = None) -> dict[str, int]:
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
        self._workspace_edits.clear()
        self._workspace_edit_starts.clear()
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
        # Bound raw edit reads by selected observed work, not session creation
        # or a fixed history window. The covering index avoids old step JSON.
        for workspace_id, repo_id, started_at in self.conn.execute(
            f"""SELECT sessions.workspace_id, sessions.repo_id, MIN(st.started_at)
                FROM sessions {scope_join}
                CROSS JOIN steps st INDEXED BY idx_steps_measurement_times
                  ON st.session_id = sessions.id
                GROUP BY sessions.workspace_id, sessions.repo_id"""
        ):
            scope = ("workspace_id", str(workspace_id)) if workspace_id else ("repo_id", str(repo_id))
            if workspace_id or repo_id:
                previous = self._workspace_edit_starts.get(scope, str(started_at))
                self._workspace_edit_starts[scope] = min(previous, str(started_at))
        prompt_steps = self._prompt_steps(scoped=scoped_ids is not None)

        self.conn.execute("DROP TABLE IF EXISTS temp.reflect_previous_execution_steps")
        previous_scope = (
            "WHERE session_id IN (SELECT session_id FROM reflect_execution_session_scope)"
            if scoped_ids is not None else ""
        )
        self.changed_unit_ids.update(str(row[0]) for row in self.conn.execute(
            f"SELECT id FROM execution_units {previous_scope}"
        ))
        self.conn.execute(
            "CREATE TEMP TABLE reflect_previous_execution_steps AS "
            f"SELECT execution_unit_id, step_id FROM execution_unit_steps {previous_scope}"
        )

        if scoped_ids is None:
            self.conn.execute("UPDATE execution_units SET eligible = 0, updated_at = ?", (now,))
        else:
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
                SELECT id, started_at, seq, raw_attrs_json, ended_at
                FROM steps
                WHERE session_id = ?
                ORDER BY started_at, seq, id
                """,
                (session_id,),
            ).fetchall()
            boundaries = self._observed_boundaries(session, steps, prompt_steps.get(session_id, set()))
            desired_membership = {
                step_id: boundary.id for boundary, step_ids in boundaries for step_id in step_ids
            }
            previous_membership = self.conn.execute(
                "SELECT step_id, execution_unit_id FROM execution_unit_steps WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            previous_owners = dict(previous_membership)
            self.changed_membership_step_ids.update(
                step for step in previous_owners.keys() | desired_membership.keys()
                if previous_owners.get(step) != desired_membership.get(step)
            )
            # Assessments below must never read stale ownership from an earlier
            # boundary. Retain unchanged rows, including their original timestamp.
            self.conn.executemany(
                "DELETE FROM execution_unit_steps WHERE step_id = ?",
                [(step_id,) for step_id, unit_id in previous_membership
                 if desired_membership.get(step_id) != unit_id],
            )
            # Report links are annotations on observed units. Retired report-only
            # rows remain available to historical ledgers but cannot own steps.
            self.conn.execute(
                "UPDATE execution_units SET mcp_task_run_id = NULL WHERE session_id = ?",
                (session_id,),
            )
            for boundary, step_ids in boundaries:
                self.changed_unit_ids.add(boundary.id)
                self.repository.upsert(boundary, session, now=now)
                self.repository.assign_steps(boundary.id, session_id, step_ids, now=now)
                assigned_steps += len(step_ids)
                explicit_count += int(boundary.source == "native_turn")
                inferred_count += int(boundary.source != "native_turn")
                self._assess(boundary, session)
            self._link_reports(session_id, boundaries, now=now)

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
        replacements = self.conn.execute(
            """SELECT old_id, json_group_array(new_id) FROM (
                 SELECT DISTINCT old.execution_unit_id AS old_id, current.execution_unit_id AS new_id
                 FROM reflect_previous_execution_steps old
                 JOIN execution_unit_steps current ON current.step_id = old.step_id
                 WHERE old.execution_unit_id <> current.execution_unit_id
                   AND NOT EXISTS(SELECT 1 FROM execution_unit_steps remaining
                                  WHERE remaining.execution_unit_id = old.execution_unit_id)
                 ORDER BY old_id, new_id
               ) GROUP BY old_id"""
        ).fetchall()
        self.conn.executemany(
            "UPDATE execution_units SET boundary_json=json_set(boundary_json, '$.superseded_by', json(?)) WHERE id=?",
            [(new_ids, old_id) for old_id, new_ids in replacements],
        )
        self.conn.execute("DROP TABLE reflect_previous_execution_steps")
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

    def _observed_boundaries(self, session, steps, prompt_step_ids):
        grouped: dict[str, list] = defaultdict(list)
        fallback = []
        for step in steps:
            attrs = attrs_dict(step[3])
            native_id = attrs.get("gen_ai.client.turn_id") or attrs.get("turn.id") or attrs.get("turn_id")
            if native_id and attrs.get("reflect.turn.identity_provenance") != "synthetic":
                grouped[str(native_id)].append(step)
            else:
                fallback.append(step)
        boundaries = []
        for native_id, group in grouped.items():
            closed = self._closed_observation(group)
            boundaries.append((ExecutionBoundary(
                id=_execution_id("turn", session[0], native_id), session_id=str(session[0]),
                source="native_turn", confidence=1.0, started_at=str(group[0][1]),
                ended_at=max(str(step[4] or step[1]) for step in group), status="completed" if closed else "unknown",
                anchor_step_id=str(group[0][0]), native_turn_id=native_id,
            ), [str(step[0]) for step in group]))
        boundaries.extend(self._fallback_boundaries(session, fallback, prompt_step_ids, closure_ambiguous=bool(grouped)))
        return boundaries

    @staticmethod
    def _closed_observation(steps) -> bool:
        """A same-unit stop must cover the latest observed work, including late arrivals."""
        last_step = last_terminal = None
        for step in steps:
            attrs = attrs_dict(step[3])
            if attrs.get("reflect.time.provenance") in {"synthetic", "inferred", "unavailable"}:
                return False
            at = event_time(step[4] or step[1])
            if at is None:
                return False
            last_step = max(last_step, at) if last_step else at
            if observed_turn_terminal(attrs):
                last_terminal = max(last_terminal, at) if last_terminal else at
        return last_terminal is not None and last_terminal == last_step

    def _link_reports(self, session_id, boundaries, *, now):
        runs = self.conn.execute(
            """SELECT id, started_at FROM mcp_task_runs WHERE runtime_session_id = ?
               AND runtime_identity_conflict IS NULL ORDER BY started_at, id""", (session_id,)
        ).fetchall()
        for run_id, started_at in runs:
            matching = [boundary for boundary, _ in boundaries
                        if boundary.started_at <= str(started_at) <= (boundary.ended_at or str(started_at))]
            if not matching and len(boundaries) == 1:
                matching = [boundaries[0][0]]
            target = matching[0].id if len(matching) == 1 else None
            self.conn.execute("UPDATE mcp_task_runs SET execution_unit_id = ?, updated_at = ? WHERE id = ?",
                              (target, now, run_id))
            if target:
                self.conn.execute(
                    "UPDATE execution_units SET mcp_task_run_id = COALESCE(mcp_task_run_id, ?) WHERE id = ?",
                    (run_id, target),
                )

    def _assess(self, boundary, session):
        rows = self.conn.execute(
            """SELECT tc.*, st.started_at, st.ended_at FROM tool_calls tc
               JOIN steps st ON st.id = tc.step_id
               JOIN execution_unit_steps eus ON eus.step_id = tc.step_id
               WHERE eus.execution_unit_id = ? ORDER BY st.started_at, st.seq, tc.id""", (boundary.id,)
        )
        names = [column[0] for column in rows.description]
        tools = [dict(zip(names, row, strict=True)) for row in rows]
        # Include concurrent work in the same workspace, bounded by this unit's
        # observed interval. Later independent executions do not rewrite history.
        edits = []
        if session[1] or session[2]:
            scope = ("workspace_id", str(session[1])) if session[1] else ("repo_id", str(session[2]))
            if scope not in self._workspace_edits:
                # One indexed scan per workspace in a refresh, not one scan for
                # every turn in a long-lived session. Filter indexed times before
                # reading tool payloads or parsing hook JSON.
                earliest = self._workspace_edit_starts.get(scope, boundary.started_at)
                external = self.conn.execute(
                    f"""SELECT tc.tool_name, tc.input_preview_redacted, tc.raw_attrs_json, st.started_at, st.id
                       FROM sessions s CROSS JOIN steps st INDEXED BY idx_steps_measurement_times
                         ON st.session_id = s.id
                       CROSS JOIN tool_calls tc INDEXED BY idx_tool_calls_step
                         ON tc.step_id = st.id AND tc.session_id = s.id
                       WHERE s.{scope[0]} = ?
                         AND st.started_at >= ?
                         AND (lower(tc.tool_name) LIKE '%edit%' OR lower(tc.tool_name) LIKE '%patch%'
                              OR lower(tc.tool_name) LIKE '%write%')""", (scope[1], earliest),
                )
                observed = []
                for name, preview, attrs, at, step_id in external:
                    paths = edit_paths({"tool_name": name, "input_preview_redacted": preview, "raw_attrs_json": attrs})
                    if paths is not None:
                        observed.append(EditObservation(str(step_id), str(at), frozenset(paths)))
                file_events = self.conn.execute(
                    f"""SELECT st.raw_attrs_json, COALESCE(st.ended_at, st.started_at), st.id
                       FROM sessions s CROSS JOIN steps st INDEXED BY idx_steps_measurement_times
                         ON st.session_id = s.id
                       WHERE s.{scope[0]} = ?
                         AND COALESCE(st.ended_at, st.started_at) >= ?
                         AND lower(json_extract(st.raw_attrs_json, '$."gen_ai.client.hook.event"'))
                             IN ('afterfileedit', 'after_file_edit', 'postfileedit')""", (scope[1], earliest),
                )
                for raw, at, step_id in file_events:
                    path = attrs_dict(raw).get("gen_ai.client.file_path")
                    observed.append(EditObservation(str(step_id), str(at), frozenset({str(path)} if path else set())))
                self._workspace_edits[scope] = sorted(observed, key=lambda item: item.at)
            observed = self._workspace_edits[scope]
            start = bisect_left(observed, boundary.started_at, key=lambda item: item.at)
            end = bisect_right(observed, boundary.ended_at or boundary.started_at, key=lambda item: item.at)
            edits = observed[start:end]
        else:
            edits = [EditObservation(str(tool['step_id']), str(tool["started_at"]), frozenset(paths)) for tool in tools
                     if (paths := edit_paths(tool)) is not None]
        assessment = assess_execution(tools=tools, edits=edits, closed=boundary.status == "completed",
                                      input_identity=[boundary.id, boundary.started_at, boundary.ended_at, session[1], session[2],
                                          [(tool.get("id"), tool.get("raw_attrs_json"), tool.get("input_hash"),
                                            tool.get("output_hash"), tool.get("status")) for tool in tools]])
        assessment["scope_known"] = bool(session[1] or session[2])
        assessment["evidence_digest"] = assessment["input_digest"]
        if not assessment["scope_known"] and assessment["verification"] == "passed":
            assessment["verification"] = "unknown"
            assessment["reasons"].append("workspace_scope_unknown")
        metadata = {"source": boundary.source, "anchor_step_id": boundary.anchor_step_id,
                    "native_turn_id": boundary.native_turn_id, "assessment": assessment}
        verification = {"passed": 1, "failed": 0}.get(assessment["verification"])
        self.conn.execute(
            "UPDATE execution_units SET outcome = NULL, verification_passed = ?, boundary_json = ? WHERE id = ?",
            (verification, json.dumps(metadata, sort_keys=True), boundary.id),
        )

    def assess_contracts(
        self, *, session_ids: set[str] | None = None, execution_unit_ids: set[str] | None = None,
    ) -> dict[str, int]:
        """Evaluate approved acceptance criteria from observations, for both cohorts.

        Called after archetype classification. Reported milestones never satisfy
        acceptance, and an unscoped or unapproved contract cannot supply success.
        """
        from reflect.improvements.contracts import WorkflowContract, ordered_role_coverage
        from reflect.improvements.workflow_roles import (
            WorkflowEvidenceRepository,
            workflow_role,
            workflow_roles,
        )

        if session_ids == set() or execution_unit_ids == set():
            return {"assessed": 0, "supported": 0}
        scope = ""
        params: list[str] = []
        if session_ids is not None:
            params = sorted(session_ids)
            scope = f" AND eu.session_id IN ({','.join('?' for _ in params)})"
        if execution_unit_ids is not None:
            scope += f" AND eu.id IN ({','.join('?' for _ in execution_unit_ids)})"
            params.extend(sorted(execution_unit_ids))
        units = self.conn.execute(
            f"""SELECT eu.id, eu.repo_id, eu.workspace_id, eu.boundary_json,
                       eua.task_archetype_id, eua.confidence, eua.mixed
                FROM execution_units eu
                LEFT JOIN execution_unit_archetypes eua ON eua.execution_unit_id = eu.id
                WHERE eu.eligible = 1 {scope}""", params,
        ).fetchall()
        versions = self.conn.execute(
            """SELECT wv.id, wv.content_json, o.repo_id, wc.task_archetype_id
               FROM workflow_versions wv JOIN workflow_candidates wc ON wc.id = wv.candidate_id
               JOIN observations o ON o.id = wc.observation_id
               WHERE wv.status = 'approved' ORDER BY wv.id"""
        ).fetchall()
        contracts = []
        for version_id, content, contract_repo, contract_archetype in versions:
            try:
                contract = WorkflowContract.from_raw(attrs_dict(content).get("workflow_contract"))
            except ValueError:
                continue
            if contract is not None:
                contracts.append((version_id, content, contract_repo, contract_archetype, contract))
        applicable = {}
        for unit_id, repo_id, workspace_id, _raw, archetype, confidence, mixed in units:
            matches = []
            for item in contracts:
                _, _, contract_repo, contract_archetype, contract = item
                applicability = contract.applicability
                expected_repo = applicability.repo_id or contract_repo
                expected_workspace = applicability.workspace_id
                expected_archetype = applicability.task_archetype_id or contract_archetype
                if (expected_repo or expected_workspace) and not (
                    expected_repo and expected_repo != repo_id
                    or expected_workspace and expected_workspace != workspace_id
                    or expected_archetype and (expected_archetype != archetype or mixed or float(confidence or 0) < .65)
                ):
                    matches.append(item)
            if matches:
                applicable[str(unit_id)] = matches
        signals = (
            WorkflowEvidenceRepository(self.conn).signals_for_execution_units(list(applicable))
            if applicable else {}
        )
        procedure_signatures = {}
        if any(item[4].procedure_signature for matches in applicable.values() for item in matches):
            from reflect.improvements.procedures import ProcedureEvidenceRepository
            procedure_signatures = ProcedureEvidenceRepository(self.conn).signatures(list(applicable), signals)
        supported = 0
        for unit_id, _repo_id, _workspace_id, raw, _archetype, _confidence, _mixed in units:
            metadata = attrs_dict(raw)
            assessment = metadata.get("assessment")
            if not isinstance(assessment, dict):
                continue
            assessment["outcome"] = "unknown"
            assessment.pop("acceptance_contract_id", None)
            assessment.pop("outcome_scope", None)
            assessment.pop("contract_digest", None)
            assessment["reasons"] = ["task_acceptance_not_observed"]
            assessment["exposure"] = "unknown"
            outcome = None
            for version_id, content, _contract_repo, _contract_archetype, contract in applicable.get(str(unit_id), []):
                if (contract.procedure_signature and
                    procedure_signatures.get(str(unit_id)) != contract.procedure_signature):
                    continue
                required = [m.resolved_role for m in contract.required_milestones]
                # Discovery may classify attempts and conversational language.
                # Acceptance requires tool results; arbitrary user prose is not
                # an approval observation. Check status is assessed separately.
                accepted = [signal for signal in signals.get(str(unit_id), [])
                    if signal.kind == "tool" and (signal.successful_result or
                        workflow_role(signal, acted=False) == "verify")]
                observed = workflow_roles(accepted)
                if ("verify" not in required or
                    ordered_role_coverage(observed, required) < 1.0 or
                    assessment.get("boundary") != "closed" or
                    assessment.get("pending_check_step_ids") or
                    assessment.get("verification") not in {"passed", "failed"}):
                    continue
                outcome = "success" if assessment["verification"] == "passed" else "failure"
                assessment["outcome"] = outcome
                assessment["acceptance_contract_id"] = str(version_id)
                assessment["outcome_scope"] = "approved_workflow_contract"
                assessment["contract_digest"] = hashlib.sha256(str(content).encode()).hexdigest()
                assessment["reasons"] = ["approved_contract_observed", "scoped_terminal_checks"]
                supported += 1
                break
            exposure = self.conn.execute(
                """SELECT 1 FROM memory_exposures me
                   JOIN execution_unit_steps eus ON eus.step_id = me.step_id
                   WHERE eus.execution_unit_id = ? LIMIT 1""", (unit_id,),
            ).fetchone()
            if exposure:
                assessment["exposure"] = "observed"
            # A server-selected skill plus a paired context result is observed
            # delivery, unlike an empty reflect_context/start call by itself.
            delivered = self.conn.execute(
                """SELECT 1 FROM mcp_task_runs tr
                   WHERE tr.execution_unit_id = ? AND json_array_length(tr.selected_skills_json) > 0
                     AND EXISTS(SELECT 1 FROM tool_calls tc
                                JOIN execution_unit_steps eus ON eus.step_id = tc.step_id
                                WHERE eus.execution_unit_id = tr.execution_unit_id
                                  AND tc.tool_name LIKE '%reflect_context' AND tc.status='ok'
                                  AND NULLIF(tc.output_hash, '') IS NOT NULL) LIMIT 1""", (unit_id,),
            ).fetchone()
            if delivered:
                assessment["exposure"] = "observed"
            assessment["input_digest"] = hashlib.sha256(json.dumps([
                assessment.get("evidence_digest", assessment.get("input_digest")),
                assessment.get("contract_digest"), assessment["outcome"], assessment["exposure"],
            ], sort_keys=True).encode()).hexdigest()
            self.conn.execute(
                "UPDATE execution_units SET outcome = ?, boundary_json = ? WHERE id = ?",
                (outcome, json.dumps(metadata, sort_keys=True), unit_id),
            )
        self.conn.commit()
        return {"assessed": len(units), "supported": supported}

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
        *,
        closure_ambiguous: bool = False,
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
                        ended_at=max(str(step[4] or step[1]) for step in group),
                        status="completed" if not closure_ambiguous and self._closed_observation(group) else "unknown",
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
