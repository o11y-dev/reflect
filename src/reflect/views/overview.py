from __future__ import annotations

import sqlite3
from typing import Any

from reflect.schema.base import ReflectModel
from reflect.store.provenance import origin_label, origin_transport


class OverviewViewModel(ReflectModel):
    session_count: int
    agent_count: int
    model_count: int
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    failure_count: int
    recovered_failure_count: int
    source_provenance: list[dict[str, Any]]
    top_sessions: list[dict[str, Any]]
    top_models: list[dict[str, Any]]
    top_tools: list[dict[str, Any]]


def build_overview(conn: sqlite3.Connection, *, limit: int = 10) -> OverviewViewModel:
    """Build the SQL-backed Overview screen model from rollups and canonical rows."""
    top_limit = _clamp_limit(limit)
    totals = conn.execute(
        """
        SELECT
          COUNT(*) AS session_count,
          COUNT(DISTINCT NULLIF(agent, '')) AS agent_count,
          COALESCE(SUM(tool_call_count), 0) AS tool_call_count,
          COALESCE(SUM(input_tokens), 0) AS input_tokens,
          COALESCE(SUM(output_tokens), 0) AS output_tokens,
          COALESCE(SUM(total_cost), 0) AS estimated_cost_usd,
          COALESCE(SUM(error_count), 0) AS failure_count
        FROM session_rollups
        """
    ).fetchone()
    recovered_failure_count = conn.execute(
        "SELECT COALESCE(SUM(recovered_failure_count), 0) FROM sessions"
    ).fetchone()[0]
    model_count = conn.execute(
        """
        SELECT COUNT(DISTINCT COALESCE(NULLIF(response_model, ''), NULLIF(request_model, '')))
        FROM llm_calls
        WHERE COALESCE(NULLIF(response_model, ''), NULLIF(request_model, '')) IS NOT NULL
        """
    ).fetchone()[0]

    return OverviewViewModel(
        session_count=totals[0],
        agent_count=totals[1],
        model_count=model_count,
        tool_call_count=totals[2],
        input_tokens=totals[3],
        output_tokens=totals[4],
        estimated_cost_usd=totals[5],
        failure_count=totals[6],
        recovered_failure_count=recovered_failure_count,
        source_provenance=list_source_provenance(conn),
        top_sessions=_top_sessions(conn, limit=top_limit),
        top_models=_top_models(conn, limit=top_limit),
        top_tools=_top_tools(conn, limit=top_limit),
    )


def list_source_provenance(
    conn: sqlite3.Connection,
    *,
    session_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    if session_ids is not None and not session_ids:
        return []
    params: list[str] = []
    where = ""
    if session_ids is not None:
        placeholders = ", ".join("?" for _ in sorted(session_ids))
        where = f"WHERE session_id IN ({placeholders})"
        params.extend(sorted(session_ids))
    cursor = conn.execute(
        f"""
        SELECT
          COALESCE(NULLIF(origin_kind, ''), 'unknown') AS origin_kind,
          COUNT(*) AS event_count
        FROM raw_events
        {where}
        GROUP BY origin_kind
        ORDER BY event_count DESC, origin_kind ASC
        """,
        params,
    )
    return [
        {
            "origin_kind": row[0],
            "label": origin_label(row[0]),
            "transport": origin_transport(row[0]),
            "event_count": row[1],
        }
        for row in cursor.fetchall()
    ]


def _top_sessions(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT
          sr.session_id,
          sr.agent,
          s.status,
          s.title,
          sr.started_at,
          sr.ended_at,
          sr.prompt_count,
          sr.tool_call_count,
          sr.error_count,
          sr.input_tokens,
          sr.output_tokens,
          sr.total_cost
        FROM session_rollups sr
        JOIN sessions s ON s.id = sr.session_id
        ORDER BY sr.total_cost DESC, sr.tool_call_count DESC, sr.started_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return _cursor_dicts(cursor)


def _top_models(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT
          COALESCE(NULLIF(response_model, ''), NULLIF(request_model, '')) AS model,
          COUNT(*) AS call_count,
          COUNT(DISTINCT session_id) AS session_count,
          COALESCE(SUM(input_tokens), 0) AS input_tokens,
          COALESCE(SUM(output_tokens), 0) AS output_tokens,
          COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
        FROM llm_calls
        WHERE COALESCE(NULLIF(response_model, ''), NULLIF(request_model, '')) IS NOT NULL
        GROUP BY model
        ORDER BY estimated_cost_usd DESC, call_count DESC, model ASC
        LIMIT ?
        """,
        (limit,),
    )
    return _cursor_dicts(cursor)


def _top_tools(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT
          tool_name,
          agent,
          call_count,
          success_count,
          error_count,
          total_duration_ms
        FROM tool_rollups
        ORDER BY call_count DESC, error_count DESC, tool_name ASC
        LIMIT ?
        """,
        (limit,),
    )
    return _cursor_dicts(cursor)


def _cursor_dicts(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, 100))
