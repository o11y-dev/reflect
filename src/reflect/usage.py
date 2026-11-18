from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reflect.schema.base import ReflectModel
from reflect.store.pending_refresh import analysis_coverage, derived_analysis_state
from reflect.store.sqlite import snapshot_incomplete
from reflect.views.measurements import MeasurementView, build_measurements


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


class TokenProvenance(ReflectModel):
    exact_sessions: int = 0
    estimated_sessions: int = 0
    unavailable_sessions: int = 0
    sources: list[str]
    proxy_order: list[str]


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
    token_provenance: TokenProvenance
    measurements: MeasurementView
    git_activity: dict
    insights: dict
    analysis_state: str
    analysis_coverage: dict[str, dict[str, int]]


class RuntimeSessionHint(ReflectModel):
    """One runtime-provided session identity candidate."""

    session_id: str
    agent: str | None = None
    environment_key: str


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

        ids = {str(row[0]) for row in self.conn.execute(self._scope_cte(where_sql) + "SELECT id FROM scoped_sessions", params)}
        days = {"day": 1, "week": 7, "month": 30}.get(period) if global_scope else None
        date_from = (self.now-timedelta(days=days)).isoformat() if days else None
        date_to = self.now.isoformat() if days else None
        totals = self._totals(where_sql, params, date_from=date_from, date_to=date_to)
        if days:
            limitations.append("Activity counts include observed events in the selected UTC window. Duration is summed session wall time clipped to that window; recovered failures count matching observed failure-to-success tool sequences.")
        measurements = build_measurements(self.conn, session_ids=ids,
            date_from=date_from, date_to=date_to)
        coverage = analysis_coverage(self.conn, ids)
        analysis_state = derived_analysis_state(coverage, snapshot_ready=not snapshot_incomplete(self.conn))
        if analysis_state == "historical_pending":
            limitations.append("Derived analysis is pending for retained history; explicitly refresh the selected session or all history. Observed usage remains available.")
        elif analysis_state == "preparing":
            limitations.append("Derived analysis is refreshing; observed usage remains available and pending insights are withheld.")
        from reflect.improvements.session_insights import read_session_insights
        from reflect.store.git_activity import git_activity
        for key, value in measurements.totals.items():
            if key in UsageTotals.model_fields:
                setattr(totals, key, value)
        token_provenance = self._token_provenance(where_sql, params)
        if token_provenance.estimated_sessions:
            limitations.append(
                f"{token_provenance.estimated_sessions} session(s) use transcript-derived "
                "token estimates, not exact provider counts."
            )
            limitations.append(
                "Transcript-derived estimates retain an unknown model attribution; "
                "agent and overall observed totals retain their usage."
            )
        if token_provenance.unavailable_sessions:
            limitations.append(
                "Exact or adapter-estimated token counts are unavailable for "
                f"{token_provenance.unavailable_sessions} session(s) with LLM activity. "
                "Use output size when captured, then LLM calls, tool calls, and duration "
                "as inference-only proxies."
            )
        if measurements.coverage["cost_status"] != "complete":
            limitations.append(
                "Cost coverage is " + str(measurements.coverage["cost_status"])
                + "; observed cost is a known subtotal. Missing rates or models do not imply free usage."
            )
        return UsageReport(
            scope=scope,
            period=period if global_scope else None,
            generated_at=self.now.isoformat(),
            resolution=resolution,
            session=selected_session,
            totals=totals,
            agents=self._agent_breakdown(measurements),
            models=[UsageBreakdown(name=row["model"] or "unknown", count=row["calls"],
                input_tokens=row["input_tokens"],output_tokens=row["output_tokens"],
                estimated_cost_usd=row["cost_usd"]) for row in measurements.model_mix],
            tools=self._tool_breakdown(where_sql, params, date_from=date_from, date_to=date_to),
            limitations=limitations,
            token_provenance=token_provenance,
            measurements=measurements,
            git_activity=git_activity(self.conn, session_ids=ids,
                date_from=measurements.scope["date_from"], date_to=measurements.scope["date_to"]),
            insights=read_session_insights(self.conn, session_ids=ids) if analysis_state == "ready" else {},
            analysis_state=analysis_state,
            analysis_coverage=coverage,
        )

    def _resolve_session(self, explicit_session_id: str | None) -> tuple[str, str, str | None]:
        if explicit_session_id:
            if self._session_exists(explicit_session_id):
                return explicit_session_id, "explicit_session", None
            raise LookupError(f"Session not found: {explicit_session_id}")

        agent_hint: str | None = None
        for hint in self.runtime_session_hints():
            agent_hint = hint.agent or agent_hint
            if self._session_exists(hint.session_id):
                return hint.session_id, f"environment:{hint.environment_key}", None

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

    def runtime_session_hints(self) -> list[RuntimeSessionHint]:
        """Return runtime identities in precedence order, including unflushed sessions."""

        return self.environment_session_hints(self.environ)

    @classmethod
    def environment_session_hints(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> list[RuntimeSessionHint]:
        """Resolve runtime identities without requiring an open telemetry store."""

        source = environ if environ is not None else os.environ
        return [
            RuntimeSessionHint(
                session_id=runtime_id,
                agent=candidate_agent,
                environment_key=env_key,
            )
            for env_key, candidate_agent in cls._SESSION_ENV_KEYS
            if (runtime_id := str(source.get(env_key, "")).strip())
        ]

    def runtime_session_hint(self) -> RuntimeSessionHint | None:
        """Return the agent runtime identity without requiring ingestion to have completed."""

        return next(iter(self.runtime_session_hints()), None)

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
            conditions.append("EXISTS(SELECT 1 FROM steps st WHERE st.session_id=s.id AND julianday(st.started_at)>=julianday(?) AND julianday(st.started_at)<julianday(?))")
            params.extend([since.isoformat(), self.now.isoformat()])
        if agent:
            conditions.append("(lower(a.name) = lower(?) OR lower(a.id) = lower(?))")
            params.extend([agent, agent])
        return " AND ".join(conditions) or "1 = 1", params

    @staticmethod
    def _scope_cte(where_sql: str, date_from: str | None = None, date_to: str | None = None,
                   *, materialize: bool = False) -> str:
        step_filter = ""
        step_source = "steps st JOIN scoped_sessions scoped ON scoped.id=st.session_id"
        if date_from and date_to:
            step_filter = """AND julianday(st.started_at) >= julianday(?)
                AND julianday(st.started_at) < julianday(?)
                AND COALESCE(json_extract(st.raw_attrs_json, '$."reflect.time.provenance"'), 'observed') != 'inferred'"""
            # This covering index filters times/provenance before fetching the
            # few matching fact rows. Keep selected sessions outside the join;
            # otherwise SQLite may scan every retained event or its wide blobs.
            step_source = """scoped_sessions scoped CROSS JOIN steps st
                INDEXED BY idx_steps_measurement_times ON scoped.id=st.session_id"""
        materialized = "MATERIALIZED " if materialize else ""
        return f"""
        WITH scoped_sessions AS {materialized}(
          SELECT s.id
          FROM sessions s
          LEFT JOIN agents a ON a.id = s.agent_id
          WHERE {where_sql}
        ), scoped_steps AS {materialized}(
          SELECT st.id, st.session_id, st.started_at, st.type, st.summary,
            json_object('gen_ai.client.turn_id',json_extract(st.raw_attrs_json, '$."gen_ai.client.turn_id"'),
              'gen_ai.client.generation_id',json_extract(st.raw_attrs_json, '$."gen_ai.client.generation_id"'),
              'gen_ai.client.hook.event',json_extract(st.raw_attrs_json, '$."gen_ai.client.hook.event"')) AS raw_attrs_json
          FROM {step_source}
          WHERE 1=1 {step_filter}
        )
        """

    def activity_totals(self, *, session_ids: set[str], date_from: str, date_to: str) -> UsageTotals:
        """Share windowed activity across CLI/MCP and dashboard overview."""
        ids = sorted(session_ids)
        where = f"s.id IN ({','.join('?' for _ in ids)})" if ids else "0=1"
        return self._totals(where, ids, date_from=date_from, date_to=date_to)

    def _totals(self, where_sql: str, params: list[object], *, date_from: str | None = None,
                date_to: str | None = None) -> UsageTotals:
        if date_from and date_to:
            cte = self._scope_cte(where_sql, date_from, date_to, materialize=True)
            row = self.conn.execute(cte + """
              SELECT (SELECT COUNT(*) FROM scoped_sessions),
                (SELECT COUNT(*) FROM (
                  SELECT st.session_id, COALESCE(
                    json_extract(st.raw_attrs_json, '$."gen_ai.client.turn_id"'),
                    json_extract(st.raw_attrs_json, '$."gen_ai.client.generation_id"'), st.id)
                  FROM scoped_steps st WHERE
                    lower(COALESCE(json_extract(st.raw_attrs_json, '$."gen_ai.client.hook.event"'), st.summary))='userpromptsubmit'
                    OR EXISTS(SELECT 1 FROM conversation_facts cf WHERE cf.step_id=st.id AND cf.kind='prompt' AND cf.role='user')
                  GROUP BY 1,2)),
                (SELECT COUNT(*) FROM tool_calls tc JOIN scoped_steps st ON st.id=tc.step_id),
                (SELECT COUNT(*) FROM tool_calls tc JOIN scoped_steps st ON st.id=tc.step_id WHERE tc.status='error'),
                (SELECT COUNT(*) FROM (
                  SELECT tc.status, LEAD(tc.status) OVER (
                    PARTITION BY tc.session_id, tc.tool_name, tc.input_hash ORDER BY st.started_at,tc.id) AS next_status
                  FROM tool_calls tc JOIN scoped_steps st ON st.id=tc.step_id
                  WHERE NULLIF(tc.input_hash,'') IS NOT NULL) WHERE status='error' AND next_status='ok'),
                (SELECT COALESCE(SUM(MAX(0, MIN(julianday(COALESCE(s.ended_at,?)),julianday(?))
                    - MAX(julianday(s.started_at),julianday(?))))*86400000,0)
                 FROM sessions s JOIN scoped_sessions scoped ON scoped.id=s.id),
                (SELECT COUNT(*) FROM llm_calls lc JOIN scoped_steps st ON st.id=lc.step_id),
                (SELECT COUNT(*) FROM mcp_calls mc JOIN tool_calls tc ON tc.id=mc.tool_call_id
                    JOIN scoped_steps st ON st.id=tc.step_id),
                (SELECT COUNT(*) FROM (
                  SELECT ae.session_id,COALESCE(NULLIF(ae.agent_id,''),ae.event_id,ae.step_id)
                  FROM agent_events ae JOIN scoped_steps st ON st.id=ae.step_id
                  WHERE ae.event_name='SubagentStart' GROUP BY 1,2))
            """, [*params, date_from, date_to, date_to, date_to, date_from]).fetchone()
            return UsageTotals(sessions=int(row[0]), prompts=int(row[1]), tool_calls=int(row[2]),
                failures=int(row[3]), recovered_failures=int(row[4]), duration_ms=round(row[5]),
                llm_calls=int(row[6]), mcp_calls=int(row[7]), subagent_launches=int(row[8]))
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
        source = (
            "llm_calls item JOIN scoped_steps st ON st.id = item.step_id"
            if table == "llm_calls"
            else "mcp_calls item "
            "JOIN tool_calls tc ON tc.id = item.tool_call_id "
            "JOIN scoped_steps st ON st.id = tc.step_id"
        )
        row = self.conn.execute(
            self._scope_cte(where_sql)
            + f"SELECT COUNT(*) FROM {source}",
            params,
        ).fetchone()
        return int(row[0])

    def _token_provenance(
        self,
        where_sql: str,
        params: list[object],
    ) -> TokenProvenance:
        rows = self.conn.execute(
            self._scope_cte(where_sql)
            + """
            SELECT
              s.id,
              s.token_provenance,
              lower(COALESCE(a.name, '')) AS agent,
              s.input_tokens + s.output_tokens + s.cache_creation_tokens
                + s.cache_read_tokens + s.reasoning_tokens AS total_tokens,
              EXISTS (
                SELECT 1 FROM llm_calls lc WHERE lc.session_id = s.id
              ) AS has_llm_calls
            FROM sessions s
            JOIN scoped_sessions scoped ON scoped.id = s.id
            LEFT JOIN agents a ON a.id = s.agent_id
            """,
            params,
        ).fetchall()
        exact = 0
        estimated = 0
        unavailable = 0
        sources: set[str] = set()
        for _session_id, provenance, agent, total_tokens, has_llm_calls in rows:
            if provenance == "local_telemetry":
                exact += 1
                sources.add("local_telemetry")
            elif provenance == "estimated_cursor_transcript" and agent == "cursor":
                estimated += 1
                sources.add("estimated_cursor_transcript")
            elif int(total_tokens or 0) > 0:
                exact += 1
                sources.add("local_telemetry")
            elif bool(has_llm_calls):
                unavailable += 1
                sources.add("unavailable")
        return TokenProvenance(
            exact_sessions=exact,
            estimated_sessions=estimated,
            unavailable_sessions=unavailable,
            sources=sorted(sources),
            proxy_order=[
                "captured_output_size",
                "llm_call_count",
                "tool_call_count",
                "session_duration",
            ],
        )

    def _subagent_count(self, where_sql: str, params: list[object]) -> int:
        row = self.conn.execute(
            self._scope_cte(where_sql)
            + """
            SELECT COUNT(*)
            FROM (
              SELECT ae.session_id,COALESCE(NULLIF(ae.agent_id,''),ae.event_id,ae.step_id) AS identity
              FROM agent_events ae JOIN scoped_steps st ON st.id=ae.step_id
              WHERE ae.event_name='SubagentStart' GROUP BY 1,2
            )
            """,
            params,
        ).fetchone()
        return int(row[0])

    def _agent_breakdown(self, measurements: MeasurementView) -> list[UsageBreakdown]:
        agents = {str(row[0]): str(row[1] or "unknown") for row in self.conn.execute(
            "SELECT s.id,a.name FROM sessions s LEFT JOIN agents a ON a.id=s.agent_id")}
        result: dict[str, UsageBreakdown] = {}
        for sid, values in measurements.sessions.items():
            name = agents.get(sid, "unknown")
            item = result.setdefault(name, UsageBreakdown(name=name, count=0))
            item.count += 1
            item.input_tokens += int(values.get("input_tokens") or 0)
            item.output_tokens += int(values.get("output_tokens") or 0)
            item.estimated_cost_usd += float(values.get("known_cost_usd") or 0)
        return sorted(result.values(), key=lambda row: (-row.count, row.name))

    def _tool_breakdown(self, where_sql: str, params: list[object], *,
                        date_from: str | None = None, date_to: str | None = None) -> list[UsageBreakdown]:
        rows = self.conn.execute(
            self._scope_cte(where_sql, date_from, date_to)
            + """
            SELECT tc.tool_name, COUNT(*), 0, 0, 0
            FROM tool_calls tc
            JOIN scoped_steps st ON st.id = tc.step_id
            GROUP BY tc.tool_name
            ORDER BY COUNT(*) DESC, tc.tool_name
            LIMIT 10
            """,
            [*params, date_from, date_to] if date_from and date_to else params,
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
