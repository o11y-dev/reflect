"""Insights engine — backward-compatible public API.

All existing call sites (core.py, dashboard.py, report.py, terminal.py)
import from ``reflect.insights``.  This package re-exports every public
name so those imports keep working as the internals are refactored.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

# --- New modules --------------------------------------------------------
from .badges import build_achievement_badges  # noqa: F401

# --- Moved unchanged ---------------------------------------------------
from .economy import compute_token_economy  # noqa: F401
from .percentiles import _percentile, compute_tool_percentiles  # noqa: F401
from .profile import build_data_profile  # noqa: F401
from .renderers import insights_to_example_tuples, insights_to_strings  # noqa: F401
from .scoring import compute_session_quality  # noqa: F401
from .signals import run_signals  # noqa: F401
from .types import (  # noqa: F401
    DataProfile,
    DistributionStats,
    Insight,
    Severity,
    compute_distribution,
    confidence_for,
)

if TYPE_CHECKING:
    from reflect.models import TelemetryStats


# ---------------------------------------------------------------------------
# Backward-compatible public API
# ---------------------------------------------------------------------------

def build_strengths(stats: TelemetryStats) -> list[str]:
    profile = build_data_profile(stats)
    insights = run_signals(stats, profile, "strength")
    result = insights_to_strings(insights)
    if not result:
        result = ["**Active usage** — Generating telemetry data across multiple sessions "
                  "is a good foundation for continuous improvement."]
    return result


def build_observations(stats: TelemetryStats) -> list[str]:
    profile = build_data_profile(stats)
    insights = run_signals(stats, profile, "observation")
    return insights_to_strings(insights)


def build_recommendations(stats: TelemetryStats) -> list[str]:
    profile = build_data_profile(stats)
    insights = run_signals(stats, profile, "recommendation")
    return insights_to_strings(insights)


def build_practical_examples(stats: TelemetryStats) -> list[tuple[str, str, str]]:
    profile = build_data_profile(stats)
    insights = run_signals(stats, profile, "example")
    return insights_to_example_tuples(insights)


def build_session_insights(
    session_id: str,
    stats: TelemetryStats,
    profile: DataProfile | None = None,
) -> list[Insight]:
    """Build observations, strengths, and recommendations for a single session."""
    from .signals.session import run_session_signals

    if profile is None:
        profile = build_data_profile(stats)
    spans = stats.session_span_details.get(session_id, [])
    tokens = stats.session_tokens.get(session_id, {})
    return run_session_signals(session_id, spans, tokens, stats, profile)


def build_all_insights(stats: TelemetryStats) -> dict:
    """Compute profile once, run all signal categories. Returns structured data."""
    profile = build_data_profile(stats)
    return {
        "profile": profile,
        "strengths": run_signals(stats, profile, "strength"),
        "observations": run_signals(stats, profile, "observation"),
        "recommendations": run_signals(stats, profile, "recommendation"),
        "examples": run_signals(stats, profile, "example"),
        "badges": build_achievement_badges(stats, profile),
    }


# ---------------------------------------------------------------------------
# Quality-score recomputation
# ---------------------------------------------------------------------------

def recompute_quality_scores(stats: TelemetryStats) -> None:
    """Recompute quality scores with distribution awareness. Mutates stats."""
    profile = build_data_profile(stats)
    for sid in stats.sessions_seen:
        spans = stats.session_span_details.get(sid, [])
        tokens = stats.session_tokens.get(sid, {})
        stats.session_quality_scores[sid] = compute_session_quality(sid, spans, tokens, profile)
        stats.session_goal_completed[sid] = any(
            s.get("event") in ("Stop", "SubagentStop", "SessionEnd") for s in spans
        )
    for ag in stats.agents.values():
        ag.total_quality_score = 0.0
        ag.completed_sessions = 0
        ag.recovered_failures = 0
        for sid in ag.sessions_seen:
            if sid in stats.session_quality_scores:
                ag.total_quality_score += stats.session_quality_scores[sid]
            if stats.session_goal_completed.get(sid):
                ag.completed_sessions += 1
            if sid in stats.session_recovered_failures:
                ag.recovered_failures += stats.session_recovered_failures[sid]
