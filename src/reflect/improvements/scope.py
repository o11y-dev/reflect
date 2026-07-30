from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reflect.improvements.models import ImprovementScope, ImprovementScopeKind
from reflect.usage import UsageService


class ImprovementScopeResolver:
    """Resolve CLI and MCP improvement scopes against the canonical session store."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        cwd: Path | None = None,
        environ: Mapping[str, str] | None = None,
        now: datetime | None = None,
    ) -> None:
        self.conn = conn
        self.cwd = (cwd or Path.cwd()).expanduser().resolve()
        self.environ = environ if environ is not None else os.environ
        self.now = (now or datetime.now(tz=UTC)).astimezone(UTC)

    def path(self, value: Path | None = None) -> ImprovementScope:
        requested = (value or self.cwd).expanduser().resolve()
        matches: list[tuple[int, sqlite3.Row | tuple]] = []
        for row in self.conn.execute(
            """
            SELECT w.id, w.root_path, w.repo_id, r.full_name
            FROM workspaces w
            LEFT JOIN repos r ON r.id = w.repo_id
            WHERE NULLIF(w.root_path, '') IS NOT NULL
            """
        ).fetchall():
            root = Path(str(row[1])).expanduser().resolve()
            if requested == root or root in requested.parents:
                matches.append((len(root.parts), row))
        if not matches:
            return ImprovementScope(
                kind=ImprovementScopeKind.PATH,
                label=str(requested),
                path=str(requested),
                matched=False,
            )
        row = max(matches, key=lambda item: item[0])[1]
        workspace_id = str(row[0])
        repo_id = str(row[2]) if row[2] else None
        predicate, params = self._path_predicate(workspace_id, repo_id)
        count = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM sessions s WHERE {predicate}",
                params,
            ).fetchone()[0]
        )
        return ImprovementScope(
            kind=ImprovementScopeKind.PATH,
            label=str(row[3] or row[1]),
            path=str(requested),
            workspace_id=workspace_id,
            repo_id=repo_id,
            eligible_session_count=count,
            matched=True,
        )

    def session(self, value: str) -> ImprovementScope:
        session_id = value.strip()
        if session_id == "current":
            usage = UsageService(
                self.conn,
                environ=self.environ,
                cwd=self.cwd,
                now=self.now,
            )
            session_id = next(
                (
                    hint.session_id
                    for hint in usage.runtime_session_hints()
                    if self.conn.execute(
                        "SELECT 1 FROM sessions WHERE id = ?",
                        (hint.session_id,),
                    ).fetchone()
                ),
                "",
            )
            if not session_id:
                raise LookupError(
                    "The current runtime session is not present in the local store yet. "
                    "Use --path for prior project evidence or refresh after ingestion."
                )
        row = self.conn.execute(
            """
            SELECT s.id, s.title, a.name
            FROM sessions s
            LEFT JOIN agents a ON a.id = s.agent_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Session not found: {session_id}")
        count = int(
            self.conn.execute(
                """
                WITH RECURSIVE scoped_sessions(id) AS (
                  SELECT ?
                  UNION
                  SELECT s.id
                  FROM sessions s
                  JOIN scoped_sessions parent ON s.parent_session_id = parent.id
                )
                SELECT COUNT(*) FROM scoped_sessions
                """,
                (session_id,),
            ).fetchone()[0]
        )
        return ImprovementScope(
            kind=ImprovementScopeKind.SESSION,
            label=str(row[1] or row[2] or session_id),
            root_session_id=session_id,
            eligible_session_count=count,
            matched=True,
        )

    def global_period(self, period: str) -> ImprovementScope:
        if period not in {"day", "week", "month", "all"}:
            raise ValueError("Global improvements require day, week, month, or all")
        days = {"day": 1, "week": 7, "month": 30}.get(period)
        since = (self.now - timedelta(days=days)).isoformat() if days else None
        predicate = "1 = 1"
        params: list[object] = []
        if since:
            predicate = "julianday(s.started_at) >= julianday(?)"
            params.append(since)
        count = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM sessions s WHERE {predicate}",
                params,
            ).fetchone()[0]
        )
        return ImprovementScope(
            kind=ImprovementScopeKind.GLOBAL,
            label=f"all local sessions · {period}",
            period=period,
            since=since,
            eligible_session_count=count,
            matched=True,
        )

    @staticmethod
    def _path_predicate(workspace_id: str, repo_id: str | None) -> tuple[str, list[object]]:
        if repo_id:
            return "(s.repo_id = ? OR s.workspace_id = ?)", [repo_id, workspace_id]
        return "s.workspace_id = ?", [workspace_id]


def session_scope_predicate(
    scope: ImprovementScope,
    *,
    alias: str = "s",
) -> tuple[str, list[object]]:
    """Return a bounded SQL predicate for sessions in one resolved scope."""

    if not scope.matched:
        return "0 = 1", []
    if scope.kind == ImprovementScopeKind.PATH:
        if scope.repo_id:
            return (
                f"({alias}.repo_id = ? OR {alias}.workspace_id = ?)",
                [scope.repo_id, scope.workspace_id],
            )
        return f"{alias}.workspace_id = ?", [scope.workspace_id]
    if scope.kind == ImprovementScopeKind.SESSION:
        return (
            f"""
            {alias}.id IN (
              WITH RECURSIVE scoped_sessions(id) AS (
                SELECT ?
                UNION
                SELECT child.id
                FROM sessions child
                JOIN scoped_sessions parent ON child.parent_session_id = parent.id
              )
              SELECT id FROM scoped_sessions
            )
            """,
            [scope.root_session_id],
        )
    if scope.since:
        return f"julianday({alias}.started_at) >= julianday(?)", [scope.since]
    return "1 = 1", []
