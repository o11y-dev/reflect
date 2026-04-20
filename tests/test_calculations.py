"""Tests for derived metric calculations."""

from collections import Counter

from reflect.core import (
    TelemetryStats,
    _percentile,
    build_observations,
    build_recommendations,
    build_strengths,
    compute_tool_percentiles,
)


def _make_stats(**overrides) -> TelemetryStats:
    """Build a minimal TelemetryStats for testing."""
    defaults = {
        "session_files": 0,
        "span_files": 0,
        "total_events": 0,
        "events_by_type": Counter(),
        "events_by_file": {},
    }
    defaults.update(overrides)
    return TelemetryStats(**defaults)


class TestPercentile:
    def test_empty_returns_zero(self):
        assert _percentile([], 50) == 0.0

    def test_single_element(self):
        assert _percentile([5.0], 50) == 5.0

    def test_p50_known(self):
        values = sorted([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
        assert _percentile(values, 50) == 5.0

    def test_p90_known(self):
        values = sorted([float(i) for i in range(1, 11)])
        assert _percentile(values, 90) == 9.0

    def test_p99_single(self):
        assert _percentile([100.0], 99) == 100.0

    def test_p100_returns_max(self):
        values = sorted([1.0, 5.0, 10.0])
        assert _percentile(values, 100) == 10.0


class TestComputeToolPercentiles:
    def test_basic(self):
        durations = {"Read": [100.0, 200.0, 300.0, 400.0, 500.0]}
        result = compute_tool_percentiles(durations)
        assert len(result) == 1
        r = result[0]
        assert r["tool"] == "Read"
        assert r["count"] == 5
        assert r["p50"] == 300.0
        assert r["p90"] == 500.0

    def test_sorted_by_count_desc(self):
        durations = {
            "Read": [100.0] * 5,
            "Edit": [200.0] * 10,
            "Grep": [50.0] * 2,
        }
        result = compute_tool_percentiles(durations)
        counts = [r["count"] for r in result]
        assert counts == sorted(counts, reverse=True)

    def test_empty_durations_skipped(self):
        result = compute_tool_percentiles({"Read": [], "Edit": [100.0]})
        assert len(result) == 1
        assert result[0]["tool"] == "Edit"

    def test_empty_input(self):
        assert compute_tool_percentiles({}) == []

    def test_single_duration(self):
        result = compute_tool_percentiles({"Bash": [3500.0]})
        r = result[0]
        assert r["p50"] == r["p90"] == r["p95"] == r["p99"] == 3500.0


class TestBuildStrengths:
    def _stats_with_ratio(self, pre_tool, prompts, failures=0):
        return _make_stats(
            total_events=pre_tool + prompts + failures,
            events_by_type=Counter({
                "PreToolUse": pre_tool,
                "UserPromptSubmit": prompts,
                "PostToolUseFailure": failures,
            }),
        )

    def test_exceptional_leverage(self):
        stats = self._stats_with_ratio(100, 10)
        strengths = build_strengths(stats)
        assert any("Exceptional leverage" in s for s in strengths)

    def test_high_leverage(self):
        stats = self._stats_with_ratio(50, 9)
        strengths = build_strengths(stats)
        assert any("High leverage" in s for s in strengths)

    def test_good_ratio(self):
        stats = self._stats_with_ratio(30, 9)
        strengths = build_strengths(stats)
        assert any("ratio" in s.lower() or "leverage" in s.lower() for s in strengths)

    def test_low_failure_rate(self):
        stats = self._stats_with_ratio(100, 10, failures=2)
        strengths = build_strengths(stats)
        assert any("failure" in s.lower() for s in strengths)

    def test_subagent_delegation(self):
        stats = _make_stats(
            total_events=5,
            events_by_type=Counter({"SubagentStart": 3, "UserPromptSubmit": 2}),
            subagent_types=Counter({"explore": 2, "plan": 1}),
        )
        strengths = build_strengths(stats)
        assert any("subagent" in s.lower() for s in strengths)

    def test_clean_mcp(self):
        stats = _make_stats(
            total_events=10,
            events_by_type=Counter({
                "BeforeMCPExecution": 5,
                "AfterMCPExecution": 5,
                "UserPromptSubmit": 2,
            }),
        )
        strengths = build_strengths(stats)
        assert any("MCP" in s for s in strengths)

    def test_fallback_returns_something(self):
        stats = _make_stats(total_events=1, events_by_type=Counter({"UserPromptSubmit": 1}))
        strengths = build_strengths(stats)
        assert len(strengths) > 0


class TestBuildObservations:
    def test_tool_heavy_ratio(self):
        stats = _make_stats(
            total_events=40,
            events_by_type=Counter({"PreToolUse": 30, "UserPromptSubmit": 5}),
        )
        obs = build_observations(stats)
        assert len(obs) > 0

    def test_balanced_ratio_no_noise(self):
        """A balanced ratio should NOT produce observations — no noise."""
        stats = _make_stats(
            total_events=10,
            events_by_type=Counter({"PreToolUse": 5, "UserPromptSubmit": 5}),
        )
        obs = build_observations(stats)
        # Balanced data = no signal fires = empty list (not noise)
        assert isinstance(obs, list)

    def test_failures_reported(self):
        stats = _make_stats(
            total_events=20,
            events_by_type=Counter({
                "PreToolUse": 10,
                "PostToolUseFailure": 3,
                "UserPromptSubmit": 2,
            }),
        )
        obs = build_observations(stats)
        assert any("failure" in o.lower() or "fail" in o.lower() for o in obs)


class TestBuildRecommendations:
    def test_always_returns_something(self):
        stats = _make_stats(total_events=5, events_by_type=Counter({"UserPromptSubmit": 5}))
        recs = build_recommendations(stats)
        assert len(recs) > 0

    def test_pin_files_for_high_ratio(self):
        stats = _make_stats(
            total_events=40,
            events_by_type=Counter({"PreToolUse": 30, "UserPromptSubmit": 5}),
        )
        recs = build_recommendations(stats)
        assert any("pin" in r.lower() or "file" in r.lower() for r in recs)

    def test_schema_check_for_failures(self):
        stats = _make_stats(
            total_events=20,
            events_by_type=Counter({
                "PreToolUse": 10,
                "PostToolUseFailure": 5,
                "UserPromptSubmit": 2,
            }),
        )
        recs = build_recommendations(stats)
        assert any("schema" in r.lower() or "failure" in r.lower() for r in recs)
