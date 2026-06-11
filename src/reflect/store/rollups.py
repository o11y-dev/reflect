from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from reflect.store.normalize import refresh_all_session_statuses


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def rebuild_rollups(conn: sqlite3.Connection) -> dict[str, int]:
    timestamp = _now()
    refresh_all_session_statuses(conn)
    conn.execute("DELETE FROM session_rollups")
    conn.execute("DELETE FROM daily_rollups")
    conn.execute("DELETE FROM tool_rollups")

    conn.execute(
        """
        INSERT INTO session_rollups(
          session_id, agent, started_at, ended_at, duration_ms, prompt_count,
          tool_call_count, error_count, input_tokens, output_tokens,
          cache_read_tokens, cache_write_tokens, total_cost, updated_at
        )
        SELECT
          s.id,
          COALESCE(a.name, ''),
          CASE
            WHEN (s.started_at IS NULL OR s.started_at = '' OR substr(s.started_at, 1, 4) < '2000')
              AND s.ended_at IS NOT NULL AND s.ended_at <> '' AND substr(s.ended_at, 1, 4) >= '2000'
            THEN s.ended_at
            ELSE s.started_at
          END,
          s.ended_at,
          COALESCE(CAST((julianday(s.ended_at) - julianday(s.started_at)) * 86400000 AS INTEGER), 0),
          COALESCE(COUNT(DISTINCT CASE
            WHEN COALESCE(json_extract(st.raw_attrs_json, '$."gen_ai.client.hook.event"'), st.summary) = 'UserPromptSubmit'
              THEN COALESCE(
                json_extract(st.raw_attrs_json, '$."gen_ai.client.generation_id"'),
                json_extract(st.raw_attrs_json, '$."gen_ai.client.prompt.sha256"'),
                st.id
              )
          END), 0),
          COALESCE(SUM(CASE WHEN st.type IN ('tool_call', 'mcp_call', 'shell_command') THEN 1 ELSE 0 END), 0),
          COALESCE(COUNT(DISTINCT CASE
            WHEN st.status = 'error'
              THEN COALESCE(
                json_extract(st.raw_attrs_json, '$."gen_ai.client.tool_use_id"'),
                json_extract(st.raw_attrs_json, '$."tool.id"'),
                st.id
              )
          END), 0),
          s.input_tokens,
          s.output_tokens,
          s.cache_read_tokens,
          s.cache_creation_tokens,
          s.estimated_cost_usd,
          ?
        FROM sessions s
        LEFT JOIN agents a ON a.id = s.agent_id
        LEFT JOIN steps st ON st.session_id = s.id
        GROUP BY s.id
        """,
        (timestamp,),
    )

    conn.execute(
        """
        INSERT INTO daily_rollups(
          day, agent, session_count, prompt_count, tool_call_count, error_count,
          input_tokens, output_tokens, total_cost, updated_at
        )
        SELECT
          substr(
            CASE
              WHEN (s.started_at IS NULL OR s.started_at = '' OR substr(s.started_at, 1, 4) < '2000')
                AND s.ended_at IS NOT NULL AND s.ended_at <> '' AND substr(s.ended_at, 1, 4) >= '2000'
              THEN s.ended_at
              ELSE s.started_at
            END,
            1,
            10
          ),
          sr.agent,
          COUNT(DISTINCT s.id),
          COALESCE(SUM(sr.prompt_count), 0),
          COALESCE(SUM(sr.tool_call_count), 0),
          COALESCE(SUM(sr.error_count), 0),
          COALESCE(SUM(sr.input_tokens), 0),
          COALESCE(SUM(sr.output_tokens), 0),
          COALESCE(SUM(sr.total_cost), 0),
          ?
        FROM sessions s
        JOIN session_rollups sr ON sr.session_id = s.id
        GROUP BY substr(
          CASE
            WHEN (s.started_at IS NULL OR s.started_at = '' OR substr(s.started_at, 1, 4) < '2000')
              AND s.ended_at IS NOT NULL AND s.ended_at <> '' AND substr(s.ended_at, 1, 4) >= '2000'
            THEN s.ended_at
            ELSE s.started_at
          END,
          1,
          10
        ), sr.agent
        """,
        (timestamp,),
    )

    conn.execute(
        """
        INSERT INTO tool_rollups(
          tool_name, agent, call_count, success_count, error_count,
          total_duration_ms, updated_at
        )
        SELECT
          tool_name,
          agent,
          COUNT(*),
          SUM(CASE WHEN status <> 'error' THEN 1 ELSE 0 END),
          SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END),
          COALESCE(SUM(duration_ms), 0),
          ?
        FROM (
          SELECT
            tc.tool_name,
            COALESCE(a.name, '') AS agent,
            COALESCE(
              json_extract(tc.raw_attrs_json, '$."gen_ai.client.tool_use_id"'),
              json_extract(tc.raw_attrs_json, '$."tool.id"'),
              tc.id
            ) AS call_identity,
            CASE WHEN SUM(CASE WHEN tc.status = 'error' THEN 1 ELSE 0 END) > 0 THEN 'error' ELSE 'ok' END AS status,
            MAX(COALESCE(tc.duration_ms, 0)) AS duration_ms
          FROM tool_calls tc
          JOIN sessions s ON s.id = tc.session_id
          LEFT JOIN agents a ON a.id = s.agent_id
          GROUP BY tc.tool_name, COALESCE(a.name, ''), call_identity
        )
        GROUP BY tool_name, agent
        """,
        (timestamp,),
    )

    conn.commit()
    return {
        "session_rollups": conn.execute("SELECT COUNT(*) FROM session_rollups").fetchone()[0],
        "daily_rollups": conn.execute("SELECT COUNT(*) FROM daily_rollups").fetchone()[0],
        "tool_rollups": conn.execute("SELECT COUNT(*) FROM tool_rollups").fetchone()[0],
    }
