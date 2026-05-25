from __future__ import annotations

import sqlite3
from typing import Any

from reflect.schema.base import ReflectModel


class SessionRow(ReflectModel):
    session_id: str
    agent: str | None = None
    repo: str | None = None
    status: str
    title: str | None = None
    started_at: str
    ended_at: str | None = None
    duration_ms: int
    prompt_count: int
    tool_call_count: int
    failure_count: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    estimated_cost_usd: float


class SessionPage(ReflectModel):
    rows: list[SessionRow]
    total: int
    limit: int
    offset: int


def list_sessions(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    offset: int = 0,
    agent: str | None = None,
    repo: str | None = None,
    model: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_cost: float | None = None,
    max_cost: float | None = None,
    min_failures: int | None = None,
) -> SessionPage:
    """Return a paginated SQL-backed Sessions screen model."""
    page_limit = _clamp_limit(limit)
    page_offset = max(0, offset)
    where, params = _session_filters(
        agent=agent,
        repo=repo,
        model=model,
        status=status,
        date_from=date_from,
        date_to=date_to,
        min_cost=min_cost,
        max_cost=max_cost,
        min_failures=min_failures,
    )
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM sessions s
        LEFT JOIN agents a ON a.id = s.agent_id
        LEFT JOIN repos r ON r.id = s.repo_id
        LEFT JOIN session_rollups sr ON sr.session_id = s.id
        {where_sql}
        """,
        params,
    ).fetchone()[0]
    cursor = conn.execute(
        f"""
        SELECT
          s.id AS session_id,
          COALESCE(a.name, sr.agent) AS agent,
          r.full_name AS repo,
          s.status,
          s.title,
          s.started_at,
          s.ended_at,
          COALESCE(
            sr.duration_ms,
            CASE
              WHEN s.started_at IS NOT NULL AND s.ended_at IS NOT NULL
              THEN CAST((julianday(s.ended_at) - julianday(s.started_at)) * 86400000 AS INTEGER)
              ELSE 0
            END,
            0
          ) AS duration_ms,
          COALESCE(sr.prompt_count, 0) AS prompt_count,
          COALESCE(sr.tool_call_count, 0) AS tool_call_count,
          COALESCE(sr.error_count, s.failure_count, 0) AS failure_count,
          COALESCE(sr.input_tokens, s.input_tokens, 0) AS input_tokens,
          COALESCE(sr.output_tokens, s.output_tokens, 0) AS output_tokens,
          COALESCE(sr.cache_write_tokens, s.cache_creation_tokens, 0) AS cache_creation_tokens,
          COALESCE(sr.cache_read_tokens, s.cache_read_tokens, 0) AS cache_read_tokens,
          COALESCE(sr.total_cost, s.estimated_cost_usd, 0) AS estimated_cost_usd
        FROM sessions s
        LEFT JOIN agents a ON a.id = s.agent_id
        LEFT JOIN repos r ON r.id = s.repo_id
        LEFT JOIN session_rollups sr ON sr.session_id = s.id
        {where_sql}
        ORDER BY s.started_at DESC, s.id ASC
        LIMIT ? OFFSET ?
        """,
        [*params, page_limit, page_offset],
    )
    return SessionPage(
        rows=[SessionRow(**row) for row in _cursor_dicts(cursor)],
        total=total,
        limit=page_limit,
        offset=page_offset,
    )


def _session_filters(
    *,
    agent: str | None,
    repo: str | None,
    model: str | None,
    status: str | None,
    date_from: str | None,
    date_to: str | None,
    min_cost: float | None,
    max_cost: float | None,
    min_failures: int | None,
) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if agent:
        where.append("COALESCE(a.name, sr.agent, '') = ?")
        params.append(agent)
    if repo:
        where.append("r.full_name = ?")
        params.append(repo)
    if model:
        where.append(
            """
            EXISTS (
              SELECT 1
              FROM llm_calls lc
              WHERE lc.session_id = s.id
                AND COALESCE(NULLIF(lc.response_model, ''), NULLIF(lc.request_model, '')) = ?
            )
            """
        )
        params.append(model)
    if status:
        where.append("s.status = ?")
        params.append(status)
    if date_from:
        where.append("s.started_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("s.started_at <= ?")
        params.append(date_to)
    if min_cost is not None:
        where.append("COALESCE(sr.total_cost, s.estimated_cost_usd, 0) >= ?")
        params.append(min_cost)
    if max_cost is not None:
        where.append("COALESCE(sr.total_cost, s.estimated_cost_usd, 0) <= ?")
        params.append(max_cost)
    if min_failures is not None:
        where.append("COALESCE(sr.error_count, s.failure_count, 0) >= ?")
        params.append(min_failures)
    return where, params


def _cursor_dicts(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, 500))
