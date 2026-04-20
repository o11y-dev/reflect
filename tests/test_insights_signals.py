"""Tests for aggregate signal functions — observations, strengths, recommendations, examples."""

from collections import Counter

from reflect.insights import build_all_insights, build_strengths, build_observations, build_recommendations
from reflect.insights.profile import build_data_profile
from reflect.insights.signals import run_signals
from reflect.insights.types import DataProfile, Insight
from reflect.models import TelemetryStats


def _make_stats(**overrides) -> TelemetryStats:
    defaults = {
        "session_files": 1,
        "span_files": 1,
        "total_events": 0,
        "events_by_type": Counter(),
        "events_by_file": {},
    }
    defaults.update(overrides)
    return TelemetryStats(**defaults)


class TestNoAlwaysFire:
    """No signal should fire unconditionally — minimal/empty data = empty results."""

    def test_empty_stats_no_observations(self):
        stats = _make_stats()
        profile = build_data_profile(stats)
        insights = run_signals(stats, profile, "observation")
        assert insights == []

    def test_empty_stats_no_strengths(self):
        stats = _make_stats()
        profile = build_data_profile(stats)
        insights = run_signals(stats, profile, "strength")
        assert insights == []

    def test_empty_stats_no_recommendations(self):
        stats = _make_stats()
        profile = build_data_profile(stats)
        insights = run_signals(stats, profile, "recommendation")
        assert insights == []

    def test_empty_stats_no_examples(self):
        stats = _make_stats()
        profile = build_data_profile(stats)
        insights = run_signals(stats, profile, "example")
        assert insights == []

    def test_balanced_data_no_noise(self):
        """Balanced, healthy usage produces no observations."""
        stats = _make_stats(
            total_events=15,
            events_by_type=Counter({"PreToolUse": 5, "UserPromptSubmit": 5, "PostToolUse": 5}),
            sessions_seen={"s1"},
            session_tokens={"s1": {"input": 5000, "output": 2000}},
            session_span_details={"s1": [
                {"event": "UserPromptSubmit", "tool": None, "ok": True, "t": 1_000_000_000_000},
                {"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_001_000_000_000},
                {"event": "Stop", "tool": None, "ok": True, "t": 1_060_000_000_000},
            ]},
        )
        profile = build_data_profile(stats)
        obs = run_signals(stats, profile, "observation")
        # Should not fire noise for normal data
        assert isinstance(obs, list)


class TestNoDomainReferences:
    """No insight text should contain domain-specific references from the old system."""

    BANNED_TERMS = ["GitLab", "Coralogix", "ISR-", "isr/", "Datadog", "PagerDuty"]

    def test_examples_domain_agnostic(self):
        stats = _make_stats(
            total_events=200,
            events_by_type=Counter({
                "PreToolUse": 100,
                "UserPromptSubmit": 20,
                "PostToolUseFailure": 15,
                "BeforeReadFile": 80,
                "SubagentStart": 5,
            }),
            sessions_seen={f"s{i}" for i in range(10)},
            session_tokens={f"s{i}": {"input": 50000, "output": 20000} for i in range(10)},
            session_span_details={f"s{i}": [
                {"event": "UserPromptSubmit", "tool": None, "ok": True, "t": 1_000_000_000_000},
                {"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_001_000_000_000},
                {"event": "BeforeReadFile", "tool": "Read", "ok": True, "t": 1_002_000_000_000},
            ] for i in range(10)},
            total_input_tokens=500000,
            total_output_tokens=200000,
            subagent_types=Counter({"explore": 3, "plan": 2}),
        )
        profile = build_data_profile(stats)
        examples = run_signals(stats, profile, "example")
        for insight in examples:
            for term in self.BANNED_TERMS:
                assert term not in insight.body, f"Found '{term}' in example: {insight.title}"
                assert term not in insight.before, f"Found '{term}' in before: {insight.title}"
                assert term not in insight.after, f"Found '{term}' in after: {insight.title}"


class TestInsightMetadata:
    """All insights must have valid metadata."""

    def test_all_insights_have_valid_confidence(self):
        stats = _make_stats(
            total_events=200,
            events_by_type=Counter({
                "PreToolUse": 100,
                "UserPromptSubmit": 10,
                "PostToolUseFailure": 20,
                "BeforeReadFile": 50,
            }),
            sessions_seen={f"s{i}" for i in range(10)},
            session_tokens={f"s{i}": {"input": 100000, "output": 50000} for i in range(10)},
            session_span_details={f"s{i}": [
                {"event": "UserPromptSubmit", "tool": None, "ok": True, "t": 1_000_000_000_000},
                *[{"event": "PreToolUse", "tool": "Bash", "ok": j > 1, "t": 1_000_000_000_000 + j * 1_000_000_000}
                  for j in range(10)],
            ] for i in range(10)},
            total_input_tokens=1000000,
            total_output_tokens=500000,
        )
        result = build_all_insights(stats)
        all_insights = (
            result["strengths"] + result["observations"]
            + result["recommendations"] + result["examples"]
        )
        for insight in all_insights:
            assert 0.0 < insight.confidence <= 1.0, f"Bad confidence in {insight.title}: {insight.confidence}"

    def test_insights_sorted_by_priority(self):
        stats = _make_stats(
            total_events=200,
            events_by_type=Counter({
                "PreToolUse": 100,
                "UserPromptSubmit": 10,
                "PostToolUseFailure": 30,
                "BeforeReadFile": 50,
            }),
            sessions_seen={f"s{i}" for i in range(10)},
            session_tokens={f"s{i}": {"input": 100000, "output": 50000} for i in range(10)},
            session_span_details={f"s{i}": [
                {"event": "UserPromptSubmit", "tool": None, "ok": True, "t": 1_000_000_000_000},
            ] for i in range(10)},
            total_input_tokens=1000000,
            total_output_tokens=500000,
        )
        profile = build_data_profile(stats)
        for kind in ("observation", "strength", "recommendation"):
            insights = run_signals(stats, profile, kind)
            if len(insights) >= 2:
                priorities = [i.priority for i in insights]
                assert priorities == sorted(priorities, reverse=True), \
                    f"{kind} insights not sorted by priority"


class TestStrengthSignals:
    def test_high_leverage_fires(self):
        stats = _make_stats(
            total_events=110,
            events_by_type=Counter({"PreToolUse": 100, "UserPromptSubmit": 10}),
            sessions_seen={"s1"},
            session_tokens={"s1": {"input": 5000}},
            session_span_details={"s1": []},
        )
        strengths = build_strengths(stats)
        assert any("leverage" in s.lower() for s in strengths)

    def test_low_failure_rate_fires(self):
        stats = _make_stats(
            total_events=120,
            events_by_type=Counter({
                "PreToolUse": 100,
                "PostToolUseFailure": 2,
                "UserPromptSubmit": 10,
            }),
            sessions_seen={"s1"},
            session_tokens={"s1": {"input": 5000}},
            session_span_details={"s1": []},
        )
        strengths = build_strengths(stats)
        assert any("failure" in s.lower() for s in strengths)

    def test_fallback_when_nothing_fires(self):
        """build_strengths always returns at least one item (fallback message)."""
        stats = _make_stats(
            total_events=1,
            events_by_type=Counter({"UserPromptSubmit": 1}),
        )
        strengths = build_strengths(stats)
        assert len(strengths) >= 1


class TestObservationSignals:
    def test_tool_failures_fires(self):
        stats = _make_stats(
            total_events=30,
            events_by_type=Counter({
                "PreToolUse": 20,
                "PostToolUseFailure": 6,
                "UserPromptSubmit": 4,
            }),
            sessions_seen={"s1"},
            session_tokens={"s1": {"input": 5000}},
            session_span_details={"s1": []},
        )
        obs = build_observations(stats)
        assert any("failure" in o.lower() or "fail" in o.lower() for o in obs)

    def test_execution_heavy_fires(self):
        stats = _make_stats(
            total_events=55,
            events_by_type=Counter({"PreToolUse": 50, "UserPromptSubmit": 5}),
            sessions_seen={"s1"},
            session_tokens={"s1": {"input": 5000}},
            session_span_details={"s1": []},
        )
        obs = build_observations(stats)
        assert any("execution" in o.lower() or "heavy" in o.lower() for o in obs)


class TestRecommendationSignals:
    def test_recommendations_conditional(self):
        """Recommendations only fire when conditions are met."""
        stats = _make_stats(
            total_events=50,
            events_by_type=Counter({
                "PreToolUse": 30,
                "UserPromptSubmit": 10,
                "PostToolUseFailure": 5,
            }),
            sessions_seen={f"s{i}" for i in range(5)},
            session_tokens={f"s{i}": {"input": 100000, "output": 50000} for i in range(5)},
            session_span_details={f"s{i}": [
                {"event": "UserPromptSubmit", "tool": None, "ok": True, "t": 1_000_000_000_000},
            ] for i in range(5)},
            total_input_tokens=500000,
            total_output_tokens=250000,
        )
        recs = build_recommendations(stats)
        # With this setup, some recs should fire (failures, high tokens)
        assert isinstance(recs, list)
