from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from reflect.store.normalize import refresh_all_session_statuses, refresh_session_statuses


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


def refresh_rollups(
    conn: sqlite3.Connection,
    session_ids: set[str],
) -> dict[str, int]:
    """Refresh rollups affected by a bounded set of changed sessions."""
    scoped_ids = sorted(str(session_id) for session_id in session_ids if session_id)
    if not scoped_ids:
        return {
            "session_rollups": conn.execute("SELECT COUNT(*) FROM session_rollups").fetchone()[0],
            "daily_rollups": conn.execute("SELECT COUNT(*) FROM daily_rollups").fetchone()[0],
            "tool_rollups": conn.execute("SELECT COUNT(*) FROM tool_rollups").fetchone()[0],
            "refreshed_sessions": 0,
        }

    timestamp = _now()
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS reflect_changed_sessions(session_id TEXT PRIMARY KEY)"
    )
    conn.execute("DELETE FROM reflect_changed_sessions")
    conn.executemany(
        "INSERT INTO reflect_changed_sessions(session_id) VALUES (?)",
        ((session_id,) for session_id in scoped_ids),
    )
    try:
        old_daily_keys = {
            (str(row[0] or ""), str(row[1] or ""))
            for row in conn.execute(
                """
                SELECT substr(sr.started_at, 1, 10), sr.agent
                FROM session_rollups sr
                JOIN reflect_changed_sessions changed ON changed.session_id = sr.session_id
                """
            )
        }
        old_tool_keys = {
            (str(row[0] or ""), str(row[1] or ""))
            for row in conn.execute(
                """
                SELECT DISTINCT tc.tool_name, sr.agent
                FROM tool_calls tc
                JOIN reflect_changed_sessions changed ON changed.session_id = tc.session_id
                JOIN session_rollups sr ON sr.session_id = tc.session_id
                """
            )
        }

        refresh_session_statuses(conn, set(scoped_ids))
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
            JOIN reflect_changed_sessions changed ON changed.session_id = s.id
            LEFT JOIN agents a ON a.id = s.agent_id
            LEFT JOIN steps st ON st.session_id = s.id
            GROUP BY s.id
            ON CONFLICT(session_id) DO UPDATE SET
              agent = excluded.agent,
              started_at = excluded.started_at,
              ended_at = excluded.ended_at,
              duration_ms = excluded.duration_ms,
              prompt_count = excluded.prompt_count,
              tool_call_count = excluded.tool_call_count,
              error_count = excluded.error_count,
              input_tokens = excluded.input_tokens,
              output_tokens = excluded.output_tokens,
              cache_read_tokens = excluded.cache_read_tokens,
              cache_write_tokens = excluded.cache_write_tokens,
              total_cost = excluded.total_cost,
              updated_at = excluded.updated_at
            """,
            (timestamp,),
        )

        new_daily_keys = {
            (str(row[0] or ""), str(row[1] or ""))
            for row in conn.execute(
                """
                SELECT substr(sr.started_at, 1, 10), sr.agent
                FROM session_rollups sr
                JOIN reflect_changed_sessions changed ON changed.session_id = sr.session_id
                """
            )
        }
        for day, agent in sorted(old_daily_keys | new_daily_keys):
            conn.execute("DELETE FROM daily_rollups WHERE day = ? AND agent = ?", (day, agent))
            conn.execute(
                """
                INSERT INTO daily_rollups(
                  day, agent, session_count, prompt_count, tool_call_count, error_count,
                  input_tokens, output_tokens, total_cost, updated_at
                )
                SELECT
                  ?, ?, COUNT(*), COALESCE(SUM(prompt_count), 0),
                  COALESCE(SUM(tool_call_count), 0), COALESCE(SUM(error_count), 0),
                  COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                  COALESCE(SUM(total_cost), 0), ?
                FROM session_rollups
                WHERE substr(started_at, 1, 10) = ? AND agent = ?
                HAVING COUNT(*) > 0
                """,
                (day, agent, timestamp, day, agent),
            )

        new_tool_keys = {
            (str(row[0] or ""), str(row[1] or ""))
            for row in conn.execute(
                """
                SELECT DISTINCT tc.tool_name, COALESCE(a.name, '')
                FROM tool_calls tc
                JOIN reflect_changed_sessions changed ON changed.session_id = tc.session_id
                JOIN sessions s ON s.id = tc.session_id
                LEFT JOIN agents a ON a.id = s.agent_id
                """
            )
        }
        for tool_name, agent in sorted(old_tool_keys | new_tool_keys):
            conn.execute(
                "DELETE FROM tool_rollups WHERE tool_name = ? AND agent = ?",
                (tool_name, agent),
            )
            conn.execute(
                """
                INSERT INTO tool_rollups(
                  tool_name, agent, call_count, success_count, error_count,
                  total_duration_ms, updated_at
                )
                SELECT
                  tool_name, agent, COUNT(*),
                  SUM(CASE WHEN status <> 'error' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END),
                  COALESCE(SUM(duration_ms), 0), ?
                FROM (
                  SELECT
                    tc.tool_name,
                    COALESCE(a.name, '') AS agent,
                    COALESCE(
                      json_extract(tc.raw_attrs_json, '$."gen_ai.client.tool_use_id"'),
                      json_extract(tc.raw_attrs_json, '$."tool.id"'),
                      tc.id
                    ) AS call_identity,
                    CASE WHEN SUM(CASE WHEN tc.status = 'error' THEN 1 ELSE 0 END) > 0
                      THEN 'error' ELSE 'ok' END AS status,
                    MAX(COALESCE(tc.duration_ms, 0)) AS duration_ms
                  FROM tool_calls tc
                  JOIN sessions s ON s.id = tc.session_id
                  LEFT JOIN agents a ON a.id = s.agent_id
                  WHERE tc.tool_name = ? AND COALESCE(a.name, '') = ?
                  GROUP BY tc.tool_name, COALESCE(a.name, ''), call_identity
                )
                GROUP BY tool_name, agent
                """,
                (timestamp, tool_name, agent),
            )

        conn.commit()
        return {
            "session_rollups": conn.execute("SELECT COUNT(*) FROM session_rollups").fetchone()[0],
            "daily_rollups": conn.execute("SELECT COUNT(*) FROM daily_rollups").fetchone()[0],
            "tool_rollups": conn.execute("SELECT COUNT(*) FROM tool_rollups").fetchone()[0],
            "refreshed_sessions": len(scoped_ids),
        }
    finally:
        conn.execute("DROP TABLE IF EXISTS reflect_changed_sessions")
