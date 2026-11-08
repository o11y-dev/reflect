from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

REFRESH_PHASES = ("canonical", "cost", "rollup", "graph", "improvements")


def analysis_coverage(
    conn: sqlite3.Connection, session_ids: set[str] | None = None,
) -> dict[str, dict[str, int]]:
    """Report pending derived work without treating window completion as history completion."""
    total = len(session_ids) if session_ids is not None else int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
    scope = ""
    params = []
    if session_ids is not None:
        scope = f" AND session_id IN ({','.join('?' for _ in session_ids)})" if session_ids else " AND 0"
        params = sorted(session_ids)
    counts = {phase: (pending, deferred) for phase, pending, deferred in conn.execute(
        "SELECT phase, COUNT(*), SUM(deferred) FROM pending_refresh WHERE phase IN ('graph','improvements')"
        + scope + " GROUP BY phase", params,
    )}
    return {phase: {"session_count": total, "pending_sessions": counts.get(phase, (0, 0))[0],
                    "deferred_sessions": counts.get(phase, (0, 0))[1],
                    "ready_sessions": max(total - counts.get(phase, (0, 0))[0], 0)}
            for phase in ("graph", "improvements")}


def derived_analysis_state(coverage: dict[str, dict[str, int]], *, snapshot_ready: bool) -> str:
    if not snapshot_ready:
        return "preparing"
    pending = sum(phase["pending_sessions"] for phase in coverage.values())
    if not pending:
        return "ready"
    deferred = sum(phase["deferred_sessions"] for phase in coverage.values())
    return "historical_pending" if pending == deferred else "preparing"

@dataclass(frozen=True)
class PendingRefreshSnapshot:
    """Captured phase/session/revision/deferred rows, safe to acknowledge after work."""

    entries: tuple[tuple[str, str, str, bool], ...]
    scoped_entries: frozenset[tuple[str, str]] = frozenset()
    step_changes: tuple[tuple[str, str, str, str | None, str | None], ...] = ()

    def pending_for(self, phase: str | None = None, *, include_deferred: bool = True) -> set[str]:
        return {session_id for item_phase, session_id, _, deferred in self.entries
                if (phase is None or item_phase == phase) and (include_deferred or not deferred)}

    def step_scope(self, phase: str) -> dict[str, set[str] | None]:
        """None requires full repair; a set names changed canonical evidence."""
        result: dict[str, set[str] | None] = {
            session: set() if (phase, session) in self.scoped_entries else None
            for item_phase, session, _, _ in self.entries if item_phase == phase
        }
        for item_phase, session, step, _, _ in self.step_changes:
            scoped = result.get(session)
            if item_phase == phase and scoped is not None:
                scoped.add(step)
        return result


class PendingRefreshState:
    """Durable dirty scope; mutations share the caller's canonical transaction.

    Capture after recording dependency expansion. Acknowledgements only remove
    the captured revisions, so concurrent arrivals and repeated acknowledgements
    cannot discard newer work. This class never commits or rolls back.
    """

    METADATA_KEY = "pending_refresh_initialized_v1"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def initialized(self) -> bool:
        row = self.conn.execute(
            "SELECT value FROM store_metadata WHERE key = ?", (self.METADATA_KEY,),
        ).fetchone()
        return row is not None and row[0] == "ready"

    def initialize(self, session_ids: Iterable[str] = ()) -> None:
        """Seed an upgrade/recovery once, preserving any already recorded changes."""
        if self.initialized():
            return
        self.mark(session_ids)
        self.conn.execute(
            "INSERT INTO store_metadata(key,value,updated_at) VALUES (?, 'ready', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (self.METADATA_KEY, datetime.now(UTC).isoformat()),
        )

    def mark(
        self, session_ids: Iterable[str], *, phases: Iterable[str] = REFRESH_PHASES,
    ) -> None:
        phases = tuple(phases)
        if unknown := set(phases) - set(REFRESH_PHASES):
            raise ValueError(f"Unknown refresh phase(s): {sorted(unknown)}")
        session_ids = sorted({str(item) for item in session_ids if item})
        if not session_ids or not phases:
            return
        revision = uuid4().hex
        self.conn.executemany(
            "INSERT INTO pending_refresh(phase, session_id, revision) VALUES (?, ?, ?) "
            "ON CONFLICT(phase, session_id) DO UPDATE SET revision=excluded.revision, deferred=0, full_scope=1",
            ((phase, session_id, revision) for phase in phases for session_id in session_ids),
        )

    def mark_steps(
        self, session_id: str, step_ids: Iterable[str], *, phases: Iterable[str] = REFRESH_PHASES,
    ) -> None:
        """Record changed and previously paired steps in the canonical transaction."""
        phases, steps = tuple(phases), sorted(set(step_ids))
        if set(phases) - set(REFRESH_PHASES):
            raise ValueError("Unknown refresh phase")
        if not session_id or not steps or not phases:
            return
        revision = uuid4().hex
        self.conn.executemany(
            "INSERT INTO pending_refresh(phase,session_id,revision,full_scope) VALUES (?,?,?,0) "
            "ON CONFLICT(phase,session_id) DO UPDATE SET revision=excluded.revision,deferred=0",
            ((phase, session_id, revision) for phase in phases),
        )
        for step in steps:
            owner = self.conn.execute(
                "SELECT eus.execution_unit_id, st.started_at FROM steps st "
                "LEFT JOIN execution_unit_steps eus ON eus.step_id=st.id WHERE st.id=?", (step,),
            ).fetchone()
            self.conn.executemany(
                "INSERT OR IGNORE INTO pending_step_changes "
                "(phase,session_id,step_id,old_execution_unit_id,old_started_at) VALUES (?,?,?,?,?)",
                ((phase, session_id, step, owner[0] if owner else None, owner[1] if owner else None)
                 for phase in phases),
            )

    def defer(self, session_ids: Iterable[str], *, phases: Iterable[str]) -> None:
        """Defer first-import historical analytics; later changes make them due."""
        phases = tuple(phases)
        if set(phases) - {"graph", "improvements"}:
            raise ValueError("Only historical graph and improvement work may be deferred")
        revision = uuid4().hex
        self.conn.executemany(
            "UPDATE pending_refresh SET deferred=1, revision=? "
            "WHERE phase=? AND session_id=? AND deferred=0",
            ((revision, phase, session_id) for session_id in sorted(set(session_ids)) for phase in phases),
        )

    def capture(self) -> PendingRefreshSnapshot:
        rows = self.conn.execute(
            "SELECT phase,session_id,revision,deferred,full_scope FROM pending_refresh ORDER BY phase,session_id"
        ).fetchall()
        return PendingRefreshSnapshot(
            tuple((str(row[0]), str(row[1]), str(row[2]), bool(row[3])) for row in rows),
            frozenset((str(row[0]), str(row[1])) for row in rows if not row[4]),
            tuple(tuple(row) for row in self.conn.execute(
                "SELECT phase,session_id,step_id,old_execution_unit_id,old_started_at FROM pending_step_changes"
            )),
        )

    def acknowledge(
        self, snapshot: PendingRefreshSnapshot, phase: str, *, session_ids: set[str] | None = None,
    ) -> None:
        if phase not in REFRESH_PHASES:
            raise ValueError(f"Unknown refresh phase: {phase}")
        self.conn.executemany(
            "DELETE FROM pending_refresh WHERE phase = ? AND session_id = ? AND revision = ?",
            ((item_phase, session_id, revision)
             for item_phase, session_id, revision, _ in snapshot.entries
             if item_phase == phase and (session_ids is None or session_id in session_ids)),
        )
