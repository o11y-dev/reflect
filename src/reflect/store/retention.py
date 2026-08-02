from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

_INVALID_BEFORE_YEAR = 2000
_ACTIVE_STATUSES = {"active", "running", "in_progress", "in-progress"}
_PRESERVED_SESSION_TABLES = {
    "evidence",
    "memories",
    "observation_evidence",
    "operator_feedback",
    "session_outcomes",
}


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _valid_timestamp(value: str | None) -> bool:
    parsed = _parse_timestamp(value)
    return bool(parsed and parsed.year >= _INVALID_BEFORE_YEAR)


@dataclass(frozen=True)
class SessionRetentionCandidate:
    session_id: str
    agent: str
    repository: str
    workspace: str
    source_kind: str
    source_ref: str
    started_at: str
    last_observed_at: str | None
    status: str
    reason: str
    dependent_rows: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionRetentionResult:
    dry_run: bool
    candidates: tuple[SessionRetentionCandidate, ...]
    pruned_session_ids: tuple[str, ...] = ()
    foreign_key_violations: tuple[tuple[object, ...], ...] = ()
    graph: dict[str, int] = field(default_factory=dict)
    rollups: dict[str, int] = field(default_factory=dict)


class SessionRetentionPolicy:
    """Classify only stale sessions with missing or invalid start timestamps."""

    def __init__(
        self,
        *,
        older_than_days: int = 60,
        now: datetime | None = None,
    ) -> None:
        if older_than_days < 1:
            raise ValueError("older_than_days must be at least 1")
        self.older_than_days = older_than_days
        self.now = (now or datetime.now(tz=UTC)).astimezone(UTC)

    @property
    def cutoff(self) -> datetime:
        return self.now - timedelta(days=self.older_than_days)

    def eligible(self, *, started_at: str, last_observed_at: str | None, status: str) -> bool:
        if _valid_timestamp(started_at):
            return False
        if status.strip().lower() in _ACTIVE_STATUSES:
            return False
        observed = _parse_timestamp(last_observed_at)
        return observed is not None and observed < self.cutoff


class SessionPruner:
    """Preview and transactionally prune sessions selected by one retention policy."""

    def __init__(self, conn: sqlite3.Connection, policy: SessionRetentionPolicy) -> None:
        self.conn = conn
        self.policy = policy

    def preview(self) -> tuple[SessionRetentionCandidate, ...]:
        rows = self.conn.execute(
            """
            SELECT s.id, COALESCE(a.name, ''), COALESCE(r.full_name, ''),
                   COALESCE(w.root_path, ''), COALESCE(s.source_kind, ''),
                   COALESCE(s.source_ref, ''), COALESCE(s.started_at, ''),
                   s.last_observed_at, COALESCE(s.status, ''), s.parent_session_id
            FROM sessions s
            LEFT JOIN agents a ON a.id = s.agent_id
            LEFT JOIN repos r ON r.id = s.repo_id
            LEFT JOIN workspaces w ON w.id = s.workspace_id
            ORDER BY COALESCE(s.last_observed_at, s.started_at), s.id
            """
        ).fetchall()
        by_id = {str(row[0]): row for row in rows}
        eligible = {
            session_id
            for session_id, row in by_id.items()
            if self.policy.eligible(
                started_at=str(row[6] or ""),
                last_observed_at=str(row[7]) if row[7] else None,
                status=str(row[8] or ""),
            )
        }
        children: dict[str, set[str]] = {}
        for session_id, row in by_id.items():
            parent_id = str(row[9] or "")
            if parent_id:
                children.setdefault(parent_id, set()).add(session_id)
        changed = True
        while changed:
            changed = False
            for session_id in tuple(eligible):
                if any(child_id not in eligible for child_id in children.get(session_id, ())):
                    eligible.remove(session_id)
                    changed = True

        return tuple(
            SessionRetentionCandidate(
                session_id=session_id,
                agent=str(by_id[session_id][1] or ""),
                repository=str(by_id[session_id][2] or ""),
                workspace=str(by_id[session_id][3] or ""),
                source_kind=str(by_id[session_id][4] or ""),
                source_ref=str(by_id[session_id][5] or ""),
                started_at=str(by_id[session_id][6] or ""),
                last_observed_at=(
                    str(by_id[session_id][7]) if by_id[session_id][7] else None
                ),
                status=str(by_id[session_id][8] or ""),
                reason=(
                    f"invalid start timestamp and no trusted activity within "
                    f"{self.policy.older_than_days} days"
                ),
                dependent_rows=self._dependent_row_counts(session_id),
            )
            for session_id in sorted(eligible)
        )

    def run(self, *, apply: bool = False) -> SessionRetentionResult:
        if not apply:
            return SessionRetentionResult(dry_run=True, candidates=self.preview())

        timestamp = self.policy.now.isoformat()
        nested_transaction = self.conn.in_transaction
        savepoint = "reflect_session_prune"
        if nested_transaction:
            self.conn.execute(f"SAVEPOINT {savepoint}")
        else:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            candidates = self.preview()
            if not candidates:
                if nested_transaction:
                    self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self.conn.commit()
                return SessionRetentionResult(dry_run=False, candidates=())
            candidate_ids = {candidate.session_id for candidate in candidates}
            for candidate in candidates:
                row = self.conn.execute(
                    """
                    SELECT s.agent_id, s.repo_id, s.workspace_id, s.source_kind, s.source_ref,
                           s.started_at, s.last_observed_at, s.status
                    FROM sessions s
                    WHERE s.id = ?
                    """,
                    (candidate.session_id,),
                ).fetchone()
                if row is None:
                    raise RuntimeError(
                        f"Session changed after preview: {candidate.session_id} no longer exists"
                    )
                source_ref = str(row[4] or "")
                self.conn.execute(
                    """
                    INSERT INTO pruned_sessions(
                      id, agent_id, agent_name, repo_id, workspace_id, source_kind,
                      source_ref, source_hash, started_at, last_observed_at, status,
                      pruned_at, prune_reason, deleted_counts_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      agent_id = excluded.agent_id,
                      agent_name = excluded.agent_name,
                      repo_id = excluded.repo_id,
                      workspace_id = excluded.workspace_id,
                      source_kind = excluded.source_kind,
                      source_ref = excluded.source_ref,
                      source_hash = excluded.source_hash,
                      started_at = excluded.started_at,
                      last_observed_at = excluded.last_observed_at,
                      status = excluded.status,
                      pruned_at = excluded.pruned_at,
                      prune_reason = excluded.prune_reason,
                      deleted_counts_json = excluded.deleted_counts_json,
                      resurrected_at = NULL,
                      newer_observed_at = NULL
                    """,
                    (
                        candidate.session_id,
                        row[0],
                        candidate.agent,
                        row[1],
                        row[2],
                        row[3],
                        source_ref,
                        hashlib.sha256(source_ref.encode("utf-8")).hexdigest()
                        if source_ref
                        else None,
                        row[5],
                        row[6],
                        row[7],
                        timestamp,
                        candidate.reason,
                        json.dumps(candidate.dependent_rows, sort_keys=True),
                    ),
                )
                for table in sorted(_PRESERVED_SESSION_TABLES):
                    self.conn.execute(
                        f"""
                        UPDATE {table}
                        SET pruned_session_id = ?
                        WHERE session_id = ?
                        """,
                        (candidate.session_id, candidate.session_id),
                    )
                self.conn.execute(
                    "DELETE FROM raw_events WHERE session_id = ?",
                    (candidate.session_id,),
                )
                self.conn.execute(
                    "DELETE FROM graph_edges WHERE session_id = ?",
                    (candidate.session_id,),
                )
                self.conn.execute(
                    "DELETE FROM graph_nodes WHERE session_id = ?",
                    (candidate.session_id,),
                )
                self.conn.execute(
                    "DELETE FROM session_rollups WHERE session_id = ?",
                    (candidate.session_id,),
                )
                self.conn.execute(
                    "DELETE FROM sessions WHERE id = ?",
                    (candidate.session_id,),
                )

            remaining = {
                str(row[0])
                for row in self.conn.execute(
                    "SELECT id FROM sessions WHERE id IN ({})".format(
                        ",".join("?" for _ in candidate_ids)
                    ),
                    sorted(candidate_ids),
                ).fetchall()
            }
            if remaining:
                raise RuntimeError(
                    f"Failed to prune session(s): {', '.join(sorted(remaining))}"
                )
            from reflect.store.graph_normalize import rebuild_graph
            from reflect.store.rollups import rebuild_rollups

            graph = rebuild_graph(self.conn, commit=False)
            rollups = rebuild_rollups(self.conn, commit=False)
            violations = tuple(
                tuple(row) for row in self.conn.execute("PRAGMA foreign_key_check").fetchall()
            )
            if violations:
                raise RuntimeError(
                    f"Pruning introduced {len(violations)} foreign-key violation(s)"
                )
            if nested_transaction:
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.conn.commit()
        except Exception:
            if nested_transaction:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.conn.rollback()
            raise

        return SessionRetentionResult(
            dry_run=False,
            candidates=candidates,
            pruned_session_ids=tuple(sorted(candidate_ids)),
            foreign_key_violations=(),
            graph=graph,
            rollups=rollups,
        )

    def _dependent_row_counts(self, session_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        tables = [
            str(row[0])
            for row in self.conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
        ]
        for table in tables:
            columns = {
                str(row[1])
                for row in self.conn.execute(
                    f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")'
                ).fetchall()
            }
            if "session_id" not in columns:
                continue
            quoted = table.replace('"', '""')
            count = int(
                self.conn.execute(
                    f'SELECT COUNT(*) FROM "{quoted}" WHERE session_id = ?',
                    (session_id,),
                ).fetchone()[0]
            )
            if count:
                counts[table] = count
        return counts
