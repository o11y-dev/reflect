from __future__ import annotations

import pytest

from reflect.insights.scoring import (
    compute_session_quality,
    compute_session_quality_breakdown,
)
from reflect.session_rules import (
    DEFAULT_SESSION_RULE_REGISTRY,
    DEFAULT_SESSION_RULE_SCORER,
    BaseSessionRule,
    SessionRuleContext,
    SessionRuleDefinition,
    SessionRuleRegistry,
    SessionRuleResult,
    SessionRuleScorer,
    context_from_spans,
    context_from_summary,
)


class ConstantSessionRule(BaseSessionRule):
    definition = SessionRuleDefinition(
        id="constant",
        version=1,
        name="Constant",
        description="A custom session score used to exercise the extension contract.",
        max_points=12.0,
        signals=("custom signal",),
    )

    def score(self, context: SessionRuleContext) -> SessionRuleResult:
        return self.result(
            15.0,
            f"Scored {context.session_id}.",
            {"source": context.source},
        )


def test_default_registry_contains_the_eight_quality_dimensions() -> None:
    payload = DEFAULT_SESSION_RULE_SCORER.rules_payload()

    assert len(DEFAULT_SESSION_RULE_REGISTRY) == 8
    assert sum(float(rule["points"]) for rule in payload) == 100.0
    assert {rule["id"] for rule in payload} == {
        "completion",
        "efficiency",
        "tool_reliability",
        "loop_detection",
        "duration_health",
        "error_recovery",
        "tool_diversity",
        "edit_productivity",
    }


def test_custom_session_rule_is_registered_scored_and_clamped() -> None:
    scorer = SessionRuleScorer(SessionRuleRegistry([ConstantSessionRule()]))

    breakdown = scorer.breakdown(SessionRuleContext(session_id="session-1"))

    assert scorer.score(SessionRuleContext(session_id="session-1")) == 12.0
    assert breakdown == [
        {
            "name": "Constant",
            "earned": 12.0,
            "max": 12.0,
            "summary": "Scored session-1.",
            "metrics": {"source": "spans"},
            "inputs": [{"name": "source", "value": "spans"}],
        }
    ]


def test_session_rule_registry_requires_explicit_replacement() -> None:
    registry = SessionRuleRegistry([ConstantSessionRule()])

    with pytest.raises(ValueError, match="already registered"):
        registry.register(ConstantSessionRule())

    registry.register(ConstantSessionRule(), replace=True)
    assert len(registry) == 1


def test_session_rule_rejects_a_result_with_the_wrong_identity() -> None:
    class InvalidSessionRule(ConstantSessionRule):
        def score(self, context: SessionRuleContext) -> SessionRuleResult:
            return SessionRuleResult(
                rule_id="different",
                rule_version=1,
                earned=1.0,
                summary="Invalid identity.",
            )

    with pytest.raises(ValueError, match="different@1"):
        InvalidSessionRule().evaluate(SessionRuleContext(session_id="session-1"))


def test_span_compatibility_api_delegates_to_default_scorer() -> None:
    spans = [
        {"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_000_000_000},
        {"event": "Stop", "tool": "", "ok": True, "t": 61_000_000_000},
    ]
    tokens = {"input": 1000, "output": 500}
    context = context_from_spans("session-1", spans, tokens)

    assert compute_session_quality("session-1", spans, tokens) == (
        DEFAULT_SESSION_RULE_SCORER.score(context)
    )
    assert compute_session_quality_breakdown("session-1", spans, tokens) == (
        DEFAULT_SESSION_RULE_SCORER.breakdown(context)
    )


def test_summary_adapter_marks_unavailable_detail_signals() -> None:
    context = context_from_summary(
        {
            "id": "session-1",
            "status": "completed",
            "tool_call_count": 3,
            "duration_ms": 120_000,
        }
    )
    breakdown = DEFAULT_SESSION_RULE_SCORER.breakdown(context)
    by_name = {item["name"]: item for item in breakdown}

    assert by_name["Completion"]["earned"] == 25.0
    assert by_name["Loop detection"]["metrics"] == {
        "tool_sequence_available": False
    }
    assert by_name["Tool diversity"]["earned"] == 0.0
    assert by_name["Edit productivity"]["earned"] == 0.0
