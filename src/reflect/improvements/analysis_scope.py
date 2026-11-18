"""A bounded session cohort shared by detector SQL and retirement checks."""
from __future__ import annotations

import sqlite3
from types import TracebackType


class AnalysisScope:
    """Own one temporary ID set; never filter already-aggregated detector rows.

    None is full evaluation and an empty set is no evaluation. Callers must
    supply complete cohorts for repository rules, not merely changed sessions.
    """

    def __init__(
        self, conn: sqlite3.Connection | None = None, session_ids: set[str] | None = None,
        *, current_execution_ids: set[str] | frozenset[str] = frozenset(),
    ):
        self.conn = conn
        self.session_ids = session_ids
        # Internal detector context after successful evidence preparation.
        # This does not acknowledge durable work for any external reader.
        self.current_execution_ids = frozenset(current_execution_ids)

    def __enter__(self) -> AnalysisScope:
        if self.session_ids is not None:
            assert self.conn is not None
            self.conn.execute("CREATE TEMP TABLE reflect_analysis_scope(session_id TEXT PRIMARY KEY)")
            self.conn.executemany(
                "INSERT INTO reflect_analysis_scope VALUES (?)",
                ((item,) for item in sorted(self.session_ids)),
            )
        return self

    def __exit__(self, _kind: type | None, _value: BaseException | None, _trace: TracebackType | None) -> None:
        if self.session_ids is not None:
            assert self.conn is not None
            self.conn.execute("DROP TABLE IF EXISTS temp.reflect_analysis_scope")

    def source(self, table: str, column: str = "session_id") -> str:
        if table not in {"sessions", "execution_units", "graph_edges"}:
            raise ValueError(f"Unsupported analysis table: {table}")
        if column not in {"id", "session_id"}:
            raise ValueError(f"Unsupported session column: {column}")
        if self.session_ids is None:
            return table
        return (
            f"(SELECT source.* FROM main.{table} source "
            f"WHERE source.{column} IN (SELECT session_id FROM temp.reflect_analysis_scope))"
        )

    @property
    def sessions(self) -> str:
        return self.source("sessions", "id")

    @property
    def execution_units(self) -> str:
        return self.source("execution_units")

    def contains(self, column: str) -> str:
        return "1=1" if self.session_ids is None else f"{column} IN (SELECT session_id FROM temp.reflect_analysis_scope)"

    def observation_predicate(self, alias: str = "observations") -> str:
        if self.session_ids is None:
            return "1=1"
        # Missing findings only establish absence when all supporting sessions
        # were evaluated. Aged-out and partially covered findings stay historical.
        return f"""(
            ({alias}.scope_type = 'session' AND {self.contains(alias + '.scope_id')})
            OR (
              EXISTS (SELECT 1 FROM observation_sessions support
                      WHERE support.observation_id = {alias}.id)
              AND {alias}.affected_session_count <= (
                SELECT COUNT(DISTINCT support.session_id) FROM observation_sessions support
                WHERE support.observation_id = {alias}.id
              )
              AND NOT EXISTS (
                SELECT 1 FROM observation_sessions support
                WHERE support.observation_id = {alias}.id
                  AND NOT ({self.contains('support.session_id')})
              )
            )
        )"""
