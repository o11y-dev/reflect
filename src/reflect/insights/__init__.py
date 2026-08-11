"""Session-quality analysis shared by native ingestion and SQL views."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .profile import build_data_profile
from .scoring import compute_session_quality

if TYPE_CHECKING:
    from reflect.models import TelemetryStats


def recompute_quality_scores(stats: TelemetryStats) -> None:
    """Recompute distribution-aware quality scores and agent aggregates."""
    profile = build_data_profile(stats)
    for session_id in stats.sessions_seen:
        spans = stats.session_span_details.get(session_id, [])
        tokens = stats.session_tokens.get(session_id, {})
        stats.session_quality_scores[session_id] = compute_session_quality(
            session_id,
            spans,
            tokens,
            profile,
        )
        stats.session_goal_completed[session_id] = any(
            span.get("event") in ("Stop", "SubagentStop", "SessionEnd")
            for span in spans
        )
    for agent in stats.agents.values():
        agent.total_quality_score = sum(
            stats.session_quality_scores.get(session_id, 0.0)
            for session_id in agent.sessions_seen
        )
        agent.completed_sessions = sum(
            bool(stats.session_goal_completed.get(session_id))
            for session_id in agent.sessions_seen
        )
        agent.recovered_failures = sum(
            stats.session_recovered_failures.get(session_id, 0)
            for session_id in agent.sessions_seen
        )


__all__ = ["compute_session_quality", "recompute_quality_scores"]
