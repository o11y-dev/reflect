from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reflect.schema.base import ReflectModel


class UsageTotals(ReflectModel):
    sessions: int = 0
    prompts: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    mcp_calls: int = 0
    subagent_launches: int = 0
    failures: int = 0
    recovered_failures: int = 0
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_cost_usd: float = 0.0


class UsageBreakdown(ReflectModel):
    name: str
    count: int
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


class UsageSession(ReflectModel):
    id: str
    agent: str = "unknown"
    workspace: str | None = None
    repository: str | None = None
    title: str | None = None
    status: str
    started_at: str
    ended_at: str | None = None


class UsageReport(ReflectModel):
    scope: str
    period: str | None = None
    generated_at: str
    resolution: str
    session: UsageSession | None = None
    totals: UsageTotals
    agents: list[UsageBreakdown]
    models: list[UsageBreakdown]
    tools: list[UsageBreakdown]
    limitations: list[str]


class UsageService:
    """Query exact usage from Reflect's canonical SQLite store."""

    _SESSION_ENV_KEYS = (
        ("REFLECT_SESSION_ID", None),
        ("CODEX_THREAD_ID", "codex"),
        ("CLAUDE_SESSION_ID", "claude"),
        ("CURSOR_SESSION_ID", "cursor"),
        ("GEMINI_SESSION_ID", "gemini"),
        ("COPILOT_SESSION_ID", "copilot"),
        ("OPENCODE_SESSION_ID", "opencode"),
        ("PI_SESSION_ID", "pi"),
    )

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        environ: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        now: datetime | None = None,
    ) -> None:
        self.conn = conn
        self.environ = environ if environ is not None else os.environ
        self.cwd = (cwd or Path.cwd()).resolve()
        self.now = (now or datetime.now(tz=UTC)).astimezone(UTC)

    def report(
        self,
        *,
        session_id: str | None = None,
        global_scope: bool = False,
        period: str = "week",
        agent: str | None = None,
    ) -> UsageReport:
        if session_id and global_scope:
            raise ValueError("--session and --global cannot be used together")
        if agent and not global_scope:
            raise ValueError("--agent is only valid with --global")
        if period not in {"day", "week", "month", "all"}:
            raise ValueError(f"Unsupported usage period: {period}")

        limitations: list[str] = []
        selected_session: UsageSession | None = None
        if global_scope:
            resolution = "global"
            where_sql, params = self._global_scope(period=period, agent=agent)
            scope = "global"
        else:
            resolved_id, resolution, warning = self._resolve_session(session_id)
            if warning:
                limitations.append(warning)
            selected_session = self._session_context(resolved_id)
            where_sql, params = "s.id = ?", [resolved_id]
            scope = "session"

        totals = self._totals(where_sql, params)
        return UsageReport(
            scope=scope,
            period=period if global_scope else None,
            generated_at=self.now.isoformat(),
            resolution=resolution,
            session=selected_session,
            totals=totals,
            agents=self._agent_breakdown(where_sql, params),
            models=self._model_breakdown(where_sql, params),
            tools=self._tool_breakdown(where_sql, params),
            limitations=limitations,
        )

    def _resolve_session(self, explicit_session_id: str | None) -> tuple[str, str, str | None]:
        if explicit_session_id:
            if self._session_exists(explicit_session_id):
                return explicit_session_id, "explicit_session", None
            raise LookupError(f"Session not found: {explicit_session_id}")

        runtime_id, agent_hint, env_key = self.runtime_session_hint()
        if runtime_id and self._session_exists(runtime_id):
            return runtime_id, f"environment:{env_key}", None

        workspace_match = self._latest_workspace_session(agent_hint)
        if workspace_match:
            return (
                workspace_match,
                "inferred_workspace",
                "The active runtime session was not present in the local store; showing the newest session in this workspace.",
            )

        agent_match = self._latest_session(agent_hint)
        if agent_match:
            return (
                agent_match,
                "inferred_agent" if agent_hint else "latest_session",
                "The active runtime session was not present in the local store; showing the newest matching local session.",
            )
        raise LookupError("No local sessions found. Run `reflect setup` and capture a session first.")

    def runtime_session_hint(self) -> tuple[str | None, str | None, str | None]:
        """Return the agent runtime identity without requiring ingestion to have completed."""

        for env_key, candidate_agent in self._SESSION_ENV_KEYS:
            runtime_id = str(self.environ.get(env_key, "")).strip()
            if runtime_id:
                return runtime_id, candidate_agent, env_key
        return None, None, None

    def _session_exists(self, session_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is not None

    def _latest_workspace_session(self, agent: str | None) -> str | None:
        conditions = [
            "(? = w.root_path OR ? LIKE rtrim(w.root_path, '/') || '/%')",
        ]
        params: list[object] = [str(self.cwd), str(self.cwd)]
        if agent:
            conditions.append("(lower(a.name) = lower(?) OR lower(a.id) = lower(?))")
            params.extend([agent, agent])
        row = self.conn.execute(
            f"""
            SELECT s.id
            FROM sessions s
            JOIN workspaces w ON w.id = s.workspace_id
            LEFT JOIN agents a ON a.id = s.agent_id
            WHERE {' AND '.join(conditions)}
            ORDER BY length(w.root_path) DESC, julianday(s.started_at) DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        return str(row[0]) if row else None

    def _latest_session(self, agent: str | None) -> str | None:
        where = ""
        params: list[object] = []
        if agent:
            where = "WHERE lower(a.name) = lower(?) OR lower(a.id) = lower(?)"
            params = [agent, agent]
        row = self.conn.execute(
            f"""
            SELECT s.id
            FROM sessions s
            LEFT JOIN agents a ON a.id = s.agent_id
            {where}
            ORDER BY julianday(s.started_at) DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        return str(row[0]) if row else None

    def _global_scope(self, *, period: str, agent: str | None) -> tuple[str, list[object]]:
        conditions: list[str] = []
        params: list[object] = []
        days = {"day": 1, "week": 7, "month": 30}.get(period)
        if days is not None:
            since = self.now - timedelta(days=days)
            conditions.append("julianday(s.started_at) >= julianday(?)")
            params.append(since.isoformat())
        if agent:
            conditions.append("(lower(a.name) = lower(?) OR lower(a.id) = lower(?))")
            params.extend([agent, agent])
        return " AND ".join(conditions) or "1 = 1", params

    @staticmethod
    def _scope_cte(where_sql: str) -> str:
        return f"""
        WITH scoped_sessions AS (
          SELECT s.id
          FROM sessions s
          LEFT JOIN agents a ON a.id = s.agent_id
          WHERE {where_sql}
        )
        """

    def _totals(self, where_sql: str, params: list[object]) -> UsageTotals:
        cte = self._scope_cte(where_sql)
        row = self.conn.execute(
            cte
            + """
            SELECT
              COUNT(*),
              COALESCE(SUM(sr.prompt_count), 0),
              COALESCE(SUM(sr.tool_call_count), 0),
              COALESCE(SUM(MAX(COALESCE(sr.error_count, 0), s.failure_count)), 0),
              COALESCE(SUM(s.recovered_failure_count), 0),
              COALESCE(SUM(sr.duration_ms), 0),
              COALESCE(SUM(s.input_tokens), 0),
              COALESCE(SUM(s.output_tokens), 0),
              COALESCE(SUM(s.cache_creation_tokens), 0),
              COALESCE(SUM(s.cache_read_tokens), 0),
              COALESCE(SUM(s.reasoning_tokens), 0),
              COALESCE(SUM(s.estimated_cost_usd), 0)
            FROM scoped_sessions scoped
            JOIN sessions s ON s.id = scoped.id
            LEFT JOIN session_rollups sr ON sr.session_id = s.id
            """,
            params,
        ).fetchone()
        llm_calls = self._scoped_count("llm_calls", where_sql, params)
        mcp_calls = self._scoped_count("mcp_calls", where_sql, params)
        subagents = self._subagent_count(where_sql, params)
        return UsageTotals(
            sessions=int(row[0]),
            prompts=int(row[1]),
            llm_calls=llm_calls,
            tool_calls=int(row[2]),
            failures=int(row[3]),
            recovered_failures=int(row[4]),
            duration_ms=int(row[5]),
            input_tokens=int(row[6]),
            output_tokens=int(row[7]),
            cache_creation_tokens=int(row[8]),
            cache_read_tokens=int(row[9]),
            reasoning_tokens=int(row[10]),
            estimated_cost_usd=float(row[11]),
            mcp_calls=mcp_calls,
            subagent_launches=subagents,
        )

    def _scoped_count(self, table: str, where_sql: str, params: list[object]) -> int:
        if table not in {"llm_calls", "mcp_calls"}:
            raise ValueError(f"Unsupported usage table: {table}")
        row = self.conn.execute(
            self._scope_cte(where_sql)
            + f"SELECT COUNT(*) FROM {table} item JOIN scoped_sessions scoped ON scoped.id = item.session_id",
            params,
        ).fetchone()
        return int(row[0])

    def _subagent_count(self, where_sql: str, params: list[object]) -> int:
        row = self.conn.execute(
            self._scope_cte(where_sql)
            + """
            SELECT COUNT(*)
            FROM (
              SELECT st.session_id, st.id AS identity
              FROM steps st
              JOIN scoped_sessions scoped ON scoped.id = st.session_id
              WHERE lower(st.type) IN ('subagent', 'subagent_start')
                 OR lower(COALESCE(json_extract(st.raw_attrs_json, '$."gen_ai.client.hook.event"'), '')) = 'subagentstart'
              UNION
              SELECT tc.session_id, tc.step_id AS identity
              FROM tool_calls tc
              JOIN scoped_sessions scoped ON scoped.id = tc.session_id
              WHERE lower(tc.tool_name) IN ('agent', 'subagent', 'task', 'spawn_agent', 'delegate')
            )
            """,
            params,
        ).fetchone()
        return int(row[0])

    def _agent_breakdown(self, where_sql: str, params: list[object]) -> list[UsageBreakdown]:
        rows = self.conn.execute(
            self._scope_cte(where_sql)
            + """
            SELECT
              COALESCE(NULLIF(a.name, ''), 'unknown'), COUNT(*),
              COALESCE(SUM(s.input_tokens), 0), COALESCE(SUM(s.output_tokens), 0),
              COALESCE(SUM(s.estimated_cost_usd), 0)
            FROM scoped_sessions scoped
            JOIN sessions s ON s.id = scoped.id
            LEFT JOIN agents a ON a.id = s.agent_id
            GROUP BY COALESCE(NULLIF(a.name, ''), 'unknown')
            ORDER BY COUNT(*) DESC, 1
            LIMIT 10
            """,
            params,
        ).fetchall()
        return [self._breakdown(row) for row in rows]

    def _model_breakdown(self, where_sql: str, params: list[object]) -> list[UsageBreakdown]:
        rows = self.conn.execute(
            self._scope_cte(where_sql)
            + """
            SELECT
              COALESCE(NULLIF(l.response_model, ''), NULLIF(l.request_model, ''), 'unknown'), COUNT(*),
              COALESCE(SUM(l.input_tokens), 0), COALESCE(SUM(l.output_tokens), 0),
              COALESCE(SUM(l.estimated_cost_usd), 0)
            FROM llm_calls l
            JOIN scoped_sessions scoped ON scoped.id = l.session_id
            GROUP BY COALESCE(NULLIF(l.response_model, ''), NULLIF(l.request_model, ''), 'unknown')
            ORDER BY COUNT(*) DESC, 1
            LIMIT 10
            """,
            params,
        ).fetchall()
        return [self._breakdown(row) for row in rows]

    def _tool_breakdown(self, where_sql: str, params: list[object]) -> list[UsageBreakdown]:
        rows = self.conn.execute(
            self._scope_cte(where_sql)
            + """
            SELECT tc.tool_name, COUNT(*), 0, 0, 0
            FROM tool_calls tc
            JOIN scoped_sessions scoped ON scoped.id = tc.session_id
            GROUP BY tc.tool_name
            ORDER BY COUNT(*) DESC, tc.tool_name
            LIMIT 10
            """,
            params,
        ).fetchall()
        return [self._breakdown(row) for row in rows]

    @staticmethod
    def _breakdown(row: tuple[object, ...]) -> UsageBreakdown:
        return UsageBreakdown(
            name=str(row[0]),
            count=int(row[1]),
            input_tokens=int(row[2]),
            output_tokens=int(row[3]),
            estimated_cost_usd=float(row[4]),
        )

    def _session_context(self, session_id: str) -> UsageSession:
        row = self.conn.execute(
            """
            SELECT
              s.id, COALESCE(NULLIF(a.name, ''), 'unknown'), w.root_path, r.full_name,
              s.title, s.status, s.started_at, s.ended_at
            FROM sessions s
            LEFT JOIN agents a ON a.id = s.agent_id
            LEFT JOIN workspaces w ON w.id = s.workspace_id
            LEFT JOIN repos r ON r.id = s.repo_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Session not found: {session_id}")
        return UsageSession(
            id=str(row[0]),
            agent=str(row[1]),
            workspace=str(row[2]) if row[2] else None,
            repository=str(row[3]) if row[3] else None,
            title=str(row[4]) if row[4] else None,
            status=str(row[5]),
            started_at=str(row[6]),
            ended_at=str(row[7]) if row[7] else None,
        )
