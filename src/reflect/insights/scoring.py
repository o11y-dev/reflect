"""Compatibility API for object-oriented session quality scoring."""
from __future__ import annotations

from reflect.session_rules import DEFAULT_SESSION_RULE_SCORER, context_from_spans

from .types import DataProfile


def compute_session_quality(
    session_id: str,
    spans: list[dict],
    tokens: dict[str, int],
    profile: DataProfile | None = None,
) -> float:
    """Return a 0-100 score from the registered session rules."""
    context = context_from_spans(session_id, spans, tokens, profile)
    return DEFAULT_SESSION_RULE_SCORER.score(context)


def compute_session_quality_breakdown(
    session_id: str,
    spans: list[dict],
    tokens: dict[str, int],
    profile: DataProfile | None = None,
) -> list[dict[str, object]]:
    """Return the registered rule contributions for one session."""
    context = context_from_spans(session_id, spans, tokens, profile)
    return DEFAULT_SESSION_RULE_SCORER.breakdown(context)


__all__ = ["compute_session_quality", "compute_session_quality_breakdown"]
