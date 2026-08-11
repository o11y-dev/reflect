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
            "available": True,
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


def test_rule_context_uses_cache_in_total_token_volume() -> None:
    tokens = {
        "input": 100,
        "output": 20,
        "cache_creation": 30,
        "cache_read": 50,
    }

    span_context = context_from_spans("session-1", [], tokens)
    summary_context = context_from_summary(
        {
            "id": "session-1",
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_tokens": 30,
            "cache_read_tokens": 50,
        }
    )

    assert span_context.total_tokens == 200
    assert summary_context.total_tokens == 200
    assert span_context.token_provenance == "local_telemetry"
    assert summary_context.token_provenance == "local_telemetry"


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
    assert by_name["Efficiency"]["available"] is False
    assert by_name["Loop detection"]["metrics"] == {
        "tool_sequence_available": False
    }
    assert by_name["Loop detection"]["available"] is False
    assert by_name["Tool diversity"]["earned"] == 0.0
    assert by_name["Tool diversity"]["available"] is False
    assert by_name["Edit productivity"]["earned"] == 0.0
    assert by_name["Edit productivity"]["available"] is False
    assert DEFAULT_SESSION_RULE_SCORER.coverage_from_breakdown(breakdown) == 60.0
    assert DEFAULT_SESSION_RULE_SCORER.score_from_breakdown(breakdown) == 95.0


def test_measured_zero_tokens_are_distinct_from_unavailable_tokens() -> None:
    unavailable = context_from_summary(
        {
            "id": "missing",
            "status": "completed",
            "tool_call_count": 2,
            "duration_ms": 60_000,
            "token_provenance": "unavailable",
        }
    )
    measured_zero = context_from_summary(
        {
            "id": "measured-zero",
            "status": "completed",
            "tool_call_count": 2,
            "duration_ms": 60_000,
            "token_provenance": "local_telemetry",
        }
    )

    unavailable_efficiency = DEFAULT_SESSION_RULE_SCORER.breakdown(unavailable)[1]
    measured_efficiency = DEFAULT_SESSION_RULE_SCORER.breakdown(measured_zero)[1]

    assert unavailable_efficiency["available"] is False
    assert unavailable_efficiency["earned"] == 0.0
    assert measured_efficiency["available"] is True
    assert measured_efficiency["earned"] == 20.0
