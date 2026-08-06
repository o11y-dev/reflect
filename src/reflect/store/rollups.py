from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from reflect.store.normalize import refresh_all_session_statuses, refresh_session_statuses

CODEX_DESKTOP_ROLLUP_REBUILD_TASK = "rebuild_rollups_after_codex_desktop_otel"


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def rollup_rebuild_pending(conn: sqlite3.Connection) -> bool:
    try:
        return (
            conn.execute(
                "SELECT 1 FROM maintenance_tasks WHERE task = ?",
                (CODEX_DESKTOP_ROLLUP_REBUILD_TASK,),
            ).fetchone()
            is not None
        )
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return False


class UsageRollupReadinessProbe:
    """Detect usage rollup maintenance and structural coverage gaps."""

    def __init__(self, *, session_ids: Iterable[str] | None = None) -> None:
        normalized = tuple(
            sorted({str(session_id) for session_id in session_ids or () if session_id})
        )
        self.session_ids = normalized or None

    def inspect(self, conn: sqlite3.Connection) -> tuple[str, ...]:
        reasons: list[str] = []
        if rollup_rebuild_pending(conn):
            reasons.append("usage rollups are stale because a rebuild is pending")
        if self.session_ids is None:
            missing, orphaned = conn.execute(
                """
                SELECT
                  (
                    SELECT COUNT(*)
                    FROM sessions s
                    LEFT JOIN session_rollups sr ON sr.session_id = s.id
                    WHERE sr.session_id IS NULL
                  ),
                  (
                    SELECT COUNT(*)
                    FROM session_rollups sr
                    LEFT JOIN sessions s ON s.id = sr.session_id
                    WHERE s.id IS NULL
                  )
                """
            ).fetchone()
        else:
            placeholders = ",".join("?" for _ in self.session_ids)
            missing = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM sessions s
                LEFT JOIN session_rollups sr ON sr.session_id = s.id
                WHERE s.id IN ({placeholders})
                  AND sr.session_id IS NULL
                """,
                self.session_ids,
            ).fetchone()[0]
            orphaned = 0
        if missing:
            reasons.append(
                f"usage rollups are stale: {int(missing)} session(s) are missing rollups"
            )
        if orphaned:
            reasons.append(
                f"usage rollups are stale: {int(orphaned)} orphan rollup row(s) exist"
            )
        return tuple(reasons)


@dataclass(frozen=True)
class ToolRollupDeletionDelta:
    tool_name: str
    agent: str
    call_count: int
    success_count: int
    error_count: int
    total_duration_ms: int


@dataclass(frozen=True)
class RollupDeletionDelta:
    """Bounded aggregate changes captured before sessions are deleted."""

    daily_keys: frozenset[tuple[str, str]]
    tool_rows: tuple[ToolRollupDeletionDelta, ...]

    @classmethod
    def capture(
        cls,
        conn: sqlite3.Connection,
        session_ids: Iterable[str],
    ) -> RollupDeletionDelta:
        scoped_ids = tuple(sorted({str(value) for value in session_ids if value}))
        conn.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS
              reflect_rollup_deleted_sessions(session_id TEXT PRIMARY KEY)
            """
        )
        conn.execute("DELETE FROM reflect_rollup_deleted_sessions")
        conn.executemany(
            "INSERT INTO reflect_rollup_deleted_sessions(session_id) VALUES (?)",
            ((session_id,) for session_id in scoped_ids),
        )
        try:
            daily_keys = frozenset(
                (str(row[0] or ""), str(row[1] or ""))
                for row in conn.execute(
                    """
                    SELECT DISTINCT substr(sr.started_at, 1, 10), sr.agent
                    FROM session_rollups sr
                    JOIN reflect_rollup_deleted_sessions deleted
                      ON deleted.session_id = sr.session_id
                    """
                )
            )
            deduplicated_calls: dict[tuple[str, str, str], tuple[bool, int]] = {}
            for row in conn.execute(
                """
                SELECT
                  tc.tool_name,
                  COALESCE(a.name, ''),
                  COALESCE(
                    NULLIF(tc.logical_call_id, ''),
                    json_extract(
                      tc.raw_attrs_json,
                      '$."gen_ai.client.tool_use_id"'
                    ),
                    json_extract(tc.raw_attrs_json, '$."tool.id"'),
                    tc.id
                  ),
                  tc.status,
                  COALESCE(tc.duration_ms, 0)
                FROM tool_calls tc
                JOIN reflect_rollup_deleted_sessions deleted
                  ON deleted.session_id = tc.session_id
                JOIN sessions s ON s.id = tc.session_id
                LEFT JOIN agents a ON a.id = s.agent_id
                """
            ):
                key = (str(row[0] or ""), str(row[1] or ""), str(row[2] or ""))
                is_error = str(row[3] or "") == "error"
                duration_ms = int(row[4] or 0)
                previous = deduplicated_calls.get(key)
                if previous is None:
                    deduplicated_calls[key] = (is_error, duration_ms)
                else:
                    deduplicated_calls[key] = (
                        previous[0] or is_error,
                        max(previous[1], duration_ms),
                    )

            tool_totals: dict[tuple[str, str], list[int]] = {}
            for (tool_name, agent, _identity), (is_error, duration_ms) in (
                deduplicated_calls.items()
            ):
                totals = tool_totals.setdefault((tool_name, agent), [0, 0, 0, 0])
                totals[0] += 1
                totals[1 if not is_error else 2] += 1
                totals[3] += duration_ms
            tool_rows = tuple(
                ToolRollupDeletionDelta(
                    tool_name=tool_name,
                    agent=agent,
                    call_count=totals[0],
                    success_count=totals[1],
                    error_count=totals[2],
                    total_duration_ms=totals[3],
                )
                for (tool_name, agent), totals in sorted(tool_totals.items())
            )
            return cls(daily_keys=daily_keys, tool_rows=tool_rows)
        finally:
            conn.execute("DROP TABLE IF EXISTS reflect_rollup_deleted_sessions")

    def apply(
        self,
        conn: sqlite3.Connection,
        *,
        commit: bool = True,
    ) -> dict[str, int]:
        aggregate_result = refresh_aggregate_rollups(
            conn,
            daily_keys=set(self.daily_keys),
            tool_keys=set(),
            commit=False,
        )
        timestamp = _now()
        for row in self.tool_rows:
            conn.execute(
                """
                UPDATE tool_rollups
                SET call_count = MAX(call_count - ?, 0),
                    success_count = MAX(success_count - ?, 0),
                    error_count = MAX(error_count - ?, 0),
                    total_duration_ms = MAX(total_duration_ms - ?, 0),
                    updated_at = ?
                WHERE tool_name = ? AND agent = ?
                """,
                (
                    row.call_count,
                    row.success_count,
                    row.error_count,
                    row.total_duration_ms,
                    timestamp,
                    row.tool_name,
                    row.agent,
                ),
            )
            conn.execute(
                """
                DELETE FROM tool_rollups
                WHERE tool_name = ? AND agent = ? AND call_count = 0
                """,
                (row.tool_name, row.agent),
            )
        if commit:
            conn.commit()
        return {
            "session_rollups": aggregate_result["session_rollups"],
            "daily_rollups": aggregate_result["daily_rollups"],
            "tool_rollups": conn.execute("SELECT COUNT(*) FROM tool_rollups").fetchone()[0],
            "refreshed_daily_keys": len(self.daily_keys),
            "adjusted_tool_keys": len(self.tool_rows),
        }


def rebuild_rollups(
    conn: sqlite3.Connection,
    *,
    commit: bool = True,
) -> dict[str, int]:
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
          (SELECT COUNT(*) FROM tool_calls tc WHERE tc.session_id = s.id),
          (SELECT COUNT(*) FROM tool_calls tc WHERE tc.session_id = s.id AND tc.status = 'error'),
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
          tc.tool_name,
          COALESCE(a.name, ''),
          COUNT(*),
          SUM(CASE WHEN tc.status <> 'error' THEN 1 ELSE 0 END),
          SUM(CASE WHEN tc.status = 'error' THEN 1 ELSE 0 END),
          COALESCE(SUM(tc.duration_ms), 0),
          ?
        FROM tool_calls tc
        JOIN sessions s ON s.id = tc.session_id
        LEFT JOIN agents a ON a.id = s.agent_id
        GROUP BY tc.tool_name, COALESCE(a.name, '')
        """,
        (timestamp,),
    )

    if rollup_rebuild_pending(conn):
        conn.execute(
            "DELETE FROM maintenance_tasks WHERE task = ?",
            (CODEX_DESKTOP_ROLLUP_REBUILD_TASK,),
        )
    if commit:
        conn.commit()
    return {
        "session_rollups": conn.execute("SELECT COUNT(*) FROM session_rollups").fetchone()[0],
        "daily_rollups": conn.execute("SELECT COUNT(*) FROM daily_rollups").fetchone()[0],
        "tool_rollups": conn.execute("SELECT COUNT(*) FROM tool_rollups").fetchone()[0],
    }


def refresh_aggregate_rollups(
    conn: sqlite3.Connection,
    *,
    daily_keys: set[tuple[str, str]],
    tool_keys: set[tuple[str, str]],
    commit: bool = True,
) -> dict[str, int]:
    """Recompute only aggregate rollups selected by their natural keys."""
    timestamp = _now()
    for day, agent in sorted(daily_keys):
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

    for tool_name, agent in sorted(tool_keys):
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
              tc.tool_name, COALESCE(a.name, ''), COUNT(*),
              SUM(CASE WHEN tc.status <> 'error' THEN 1 ELSE 0 END),
              SUM(CASE WHEN tc.status = 'error' THEN 1 ELSE 0 END),
              COALESCE(SUM(tc.duration_ms), 0), ?
            FROM tool_calls tc
            JOIN sessions s ON s.id = tc.session_id
            LEFT JOIN agents a ON a.id = s.agent_id
            WHERE tc.tool_name = ? AND COALESCE(a.name, '') = ?
            GROUP BY tc.tool_name, COALESCE(a.name, '')
            """,
            (timestamp, tool_name, agent),
        )

    if commit:
        conn.commit()
    return {
        "session_rollups": conn.execute("SELECT COUNT(*) FROM session_rollups").fetchone()[0],
        "daily_rollups": conn.execute("SELECT COUNT(*) FROM daily_rollups").fetchone()[0],
        "tool_rollups": conn.execute("SELECT COUNT(*) FROM tool_rollups").fetchone()[0],
        "refreshed_daily_keys": len(daily_keys),
        "refreshed_tool_keys": len(tool_keys),
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
              (SELECT COUNT(*) FROM tool_calls tc WHERE tc.session_id = s.id),
              (SELECT COUNT(*) FROM tool_calls tc WHERE tc.session_id = s.id AND tc.status = 'error'),
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
        aggregate_result = refresh_aggregate_rollups(
            conn,
            daily_keys=old_daily_keys | new_daily_keys,
            tool_keys=old_tool_keys | new_tool_keys,
            commit=False,
        )

        conn.commit()
        return {
            "session_rollups": aggregate_result["session_rollups"],
            "daily_rollups": aggregate_result["daily_rollups"],
            "tool_rollups": aggregate_result["tool_rollups"],
            "refreshed_sessions": len(scoped_ids),
        }
    finally:
        conn.execute("DROP TABLE IF EXISTS reflect_changed_sessions")
