"""Captured analysis bounds, separate from retained canonical accounting."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from reflect.store.refresh_plan import session_batches
from reflect.utils import _json_dumps


@dataclass(frozen=True)
class AnalysisWindow:
    days: int | None
    captured_at: datetime

    @classmethod
    def capture(cls, days: int | None, *, now: datetime | None = None) -> AnalysisWindow:
        if days is not None and days <= 0:
            raise ValueError("analysis days must be positive")
        return cls(days, now or datetime.now(UTC))

    @property
    def date_from(self) -> datetime | None:
        return self.captured_at - timedelta(days=self.days) if self.days else None

    def session_ids(self, conn: sqlite3.Connection) -> set[str]:
        """Select observed activity, never derived updated_at or arrival time.

        The UTC window is half-open. Whole sessions remain available to assess
        straddling turns. A missing
        terminal marker does not keep an old session permanently in the window.
        """
        if self.days is None:
            return {str(row[0]) for row in conn.execute("SELECT id FROM sessions")}
        bounds = (self.date_from.isoformat(), self.captured_at.isoformat())
        # Expression indexes normalize offsets and bound work before UNION.
        # One malformed/future timestamp cannot hide another valid observation.
        return {str(row[0]) for row in conn.execute(
            """SELECT id FROM sessions INDEXED BY idx_sessions_analysis_start
               WHERE CASE WHEN started_at GLOB '[0-9][0-9][0-9][0-9]-*' THEN julianday(started_at) END >= julianday(?)
                 AND CASE WHEN started_at GLOB '[0-9][0-9][0-9][0-9]-*' THEN julianday(started_at) END < julianday(?)
               UNION
               SELECT session_id FROM steps INDEXED BY idx_steps_analysis_start
               WHERE COALESCE(json_extract(raw_attrs_json,'$."reflect.time.provenance"'),'observed')
                         NOT IN ('synthetic','inferred')
                 AND CASE WHEN started_at GLOB '[0-9][0-9][0-9][0-9]-*' THEN julianday(started_at) END >= julianday(?)
                 AND CASE WHEN started_at GLOB '[0-9][0-9][0-9][0-9]-*' THEN julianday(started_at) END < julianday(?)
               UNION
               SELECT session_id FROM steps INDEXED BY idx_steps_analysis_end
               WHERE COALESCE(json_extract(raw_attrs_json,'$."reflect.time.provenance"'),'observed')
                         NOT IN ('synthetic','inferred')
                 AND CASE WHEN ended_at GLOB '[0-9][0-9][0-9][0-9]-*' THEN julianday(ended_at) END >= julianday(?)
                 AND CASE WHEN ended_at GLOB '[0-9][0-9][0-9][0-9]-*' THEN julianday(ended_at) END < julianday(?)""",
            bounds * 3,
        )}

    def as_dict(
        self, session_ids: set[str], *, pending_history: int = 0,
        historical_cohort_sessions: int = 0,
    ) -> dict:
        return {
            "days": self.days,
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.captured_at.isoformat(),
            "session_count": len(session_ids),
            "pending_historical_sessions": pending_history,
            "historical_cohort_sessions": historical_cohort_sessions,
            "history_retained": True,
        }


def historical_cohort_session_ids(
    conn: sqlite3.Connection, *, corrected_session_ids: set[str], hot_session_ids: set[str],
) -> set[str]:
    """Complete repository cohorts for corrections outside the normal window.

    Preserve both old execution ownership and current session ownership on
    moves. A null repository is the complete user/local cohort. If a vanished
    session has neither retained owner, completeness requires all retained work.
    Returned IDs may include hot sessions; subtract those for history coverage.
    """
    corrected = corrected_session_ids - hot_session_ids
    if not corrected:
        return set()
    scopes: set[str | None] = set()
    located: set[str] = set()
    for batch in session_batches(corrected):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""SELECT id, repo_id FROM sessions WHERE id IN ({placeholders})
                UNION SELECT session_id, repo_id FROM execution_units
                WHERE session_id IN ({placeholders})""", batch + batch,
        )
        for session_id, repo_id in rows:
            located.add(str(session_id))
            scopes.add(str(repo_id) if repo_id is not None else None)
    if corrected - located:
        return {str(row[0]) for row in conn.execute("SELECT id FROM sessions")}
    result = set()
    if None in scopes:
        result.update(str(row[0]) for row in conn.execute("SELECT id FROM sessions WHERE repo_id IS NULL"))
    for batch in session_batches(scope for scope in scopes if scope is not None):
        placeholders = ",".join("?" for _ in batch)
        result.update(str(row[0]) for row in conn.execute(
            f"SELECT id FROM sessions WHERE repo_id IN ({placeholders})", batch,
        ))
    return result


def analysis_inputs_fingerprint(session_ids: set[str], *, days: int | None, contracts: str = "") -> str:
    """Membership/config changes invalidate a cohort even without new events."""
    from reflect.improvements.rules import DEFAULT_RULE_REGISTRY

    payload = {
        "version": 1, "days": days, "sessions": sorted(session_ids),
        "contracts": contracts,
        "rules": [rule.definition.model_dump(mode="json") for rule in DEFAULT_RULE_REGISTRY.rules],
    }
    return hashlib.sha256(_json_dumps(payload).encode()).hexdigest()
