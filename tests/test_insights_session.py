"""Tests for per-session signal functions — distribution-aware comparisons."""

from collections import Counter

from reflect.insights import build_session_insights
from reflect.insights.signals.session import (
    signal_session_cache_utilization,
    signal_session_completion,
    signal_session_duration_outlier,
    signal_session_failure_rate,
    signal_session_loop_detected,
    signal_session_model_mix,
    signal_session_productive_edits,
    signal_session_recovery,
    signal_session_token_outlier,
    signal_session_zero_failures,
)
from reflect.insights.types import DataProfile, DistributionStats, Severity
from reflect.models import TelemetryStats


def _dist(count=10, mean=50000.0, median=50000.0, p25=30000.0, p75=70000.0,
          p90=80000.0, p95=90000.0, min_val=10000.0, max_val=100000.0, stdev=20000.0):
    return DistributionStats(
        count=count, mean=mean, median=median,
        p25=p25, p75=p75, p90=p90, p95=p95,
        min_val=min_val, max_val=max_val, stdev=stdev,
    )


def _profile(**overrides):
    defaults = {
        "session_total_tokens": _dist(),
        "session_input_tokens": _dist(),
        "session_output_tokens": _dist(),
        "session_tool_count": _dist(mean=10, median=10, p25=5, p75=15, p90=20, p95=25,
                                    min_val=1, max_val=30),
        "session_prompt_count": _dist(mean=3, median=3, p25=2, p75=5, p90=7, p95=9,
                                      min_val=1, max_val=12),
        "session_failure_count": _dist(mean=1, median=1, p25=0, p75=2, p90=3, p95=4,
                                       min_val=0, max_val=5),
        "session_duration_ms": _dist(mean=300000, median=300000, p25=120000, p75=600000,
                                     p90=900000, p95=1200000, min_val=30000, max_val=1800000),
        "session_quality_scores": _dist(),
        "tokens_per_tool": _dist(mean=5000, median=5000, p25=3000, p75=8000, p90=12000,
                                 p95=15000, min_val=1000, max_val=25000),
        "tools_per_prompt": _dist(mean=3, median=3, p25=2, p75=5, p90=7, p95=10,
                                  min_val=1, max_val=15),
        "reads_per_prompt": _dist(mean=2, median=2, p25=1, p75=3, p90=5, p95=7,
                                  min_val=0, max_val=10),
        "session_token_share": _dist(),
        "total_sessions": 10,
        "total_prompts": 30,
        "total_tool_calls": 90,
        "total_failures": 5,
        "cache_reuse_ratio": 0.15,
        "heavy_model_share": 30.0,
    }
    defaults.update(overrides)
    return DataProfile(**defaults)


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


class TestSessionTokenOutlier:
    def test_fires_when_tokens_above_fence(self):
        """Session with outlier-high tokens produces token_outlier insight."""
        profile = _profile()
        # upper_fence = p75 + 1.5 * IQR = 70000 + 1.5 * 40000 = 130000
        # So 200000 tokens should be an outlier
        spans = [{"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_000_000_000_000}]
        tokens = {"input": 150000, "output": 50000}  # total = 200000
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_token_outlier("s1", spans, tokens, stats, profile)
        assert result is not None
        assert result.title == "Token usage outlier"
        assert result.severity >= Severity.MEDIUM

    def test_does_not_fire_for_normal_tokens(self):
        profile = _profile()
        spans = [{"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_000_000_000_000}]
        tokens = {"input": 30000, "output": 10000}  # total = 40000, within fence
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_token_outlier("s1", spans, tokens, stats, profile)
        assert result is None

    def test_cold_start_threshold(self):
        """With sparse profile, falls back to 500k threshold."""
        sparse_profile = _profile(session_total_tokens=_dist(count=2))
        spans = [{"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_000_000_000_000}]
        tokens = {"input": 400000, "output": 200000}  # 600k > 500k
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_token_outlier("s1", spans, tokens, stats, sparse_profile)
        assert result is not None


class TestSessionLoopDetected:
    def test_fires_for_repeated_tool(self):
        """Tool repeated 5x consecutively triggers loop detection."""
        profile = _profile()
        spans = [
            {"event": "PreToolUse", "tool": "Bash", "ok": True, "t": 1_000_000_000_000 + i * 1_000_000_000}
            for i in range(6)
        ]
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_loop_detected("s1", spans, {}, stats, profile)
        assert result is not None
        assert result.title == "Tool loop detected"
        assert result.severity == Severity.HIGH  # >= 5 repetitions

    def test_does_not_fire_for_varied_tools(self):
        profile = _profile()
        spans = [
            {"event": "PreToolUse", "tool": tool, "ok": True, "t": 1_000_000_000_000 + i * 1_000_000_000}
            for i, tool in enumerate(["Read", "Grep", "Edit", "Bash", "Write", "Read"])
        ]
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_loop_detected("s1", spans, {}, stats, profile)
        assert result is None

    def test_medium_severity_for_3_repetitions(self):
        profile = _profile()
        spans = [
            {"event": "PreToolUse", "tool": "Bash", "ok": True, "t": 1_000_000_000_000 + i * 1_000_000_000}
            for i in range(3)
        ] + [
            {"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_010_000_000_000},
        ]
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_loop_detected("s1", spans, {}, stats, profile)
        assert result is not None
        assert result.severity == Severity.MEDIUM


class TestSessionZeroFailures:
    def test_fires_when_clean(self):
        """Session with >5 tools and 0 failures produces strength."""
        profile = _profile()
        spans = [
            {"event": "PreToolUse", "tool": f"tool{i}", "ok": True, "t": 1_000_000_000_000 + i * 1_000_000_000}
            for i in range(8)
        ]
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_zero_failures("s1", spans, {}, stats, profile)
        assert result is not None
        assert result.kind == "strength"
        assert result.title == "Clean execution"

    def test_does_not_fire_with_few_tools(self):
        profile = _profile()
        spans = [
            {"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_000_000_000_000},
            {"event": "PreToolUse", "tool": "Edit", "ok": True, "t": 1_001_000_000_000},
        ]
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_zero_failures("s1", spans, {}, stats, profile)
        assert result is None

    def test_does_not_fire_with_failures(self):
        profile = _profile()
        spans = [
            {"event": "PreToolUse", "tool": f"tool{i}", "ok": i != 3, "t": 1_000_000_000_000 + i * 1_000_000_000}
            for i in range(8)
        ]
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_zero_failures("s1", spans, {}, stats, profile)
        assert result is None


class TestSessionFailureRate:
    def test_fires_for_high_rate(self):
        profile = _profile()
        spans = [
            {"event": "PreToolUse", "tool": "Bash", "ok": False, "t": 1_000_000_000_000 + i * 1_000_000_000}
            for i in range(5)
        ] + [
            {"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_010_000_000_000},
        ]
        stats = _make_stats(
            sessions_seen={"s1"},
            session_recovered_failures={"s1": 1},
        )

        result = signal_session_failure_rate("s1", spans, {}, stats, profile)
        assert result is not None
        assert result.severity >= Severity.HIGH  # 5/6 > 0.30 → CRITICAL

    def test_does_not_fire_for_zero_failures(self):
        profile = _profile()
        spans = [
            {"event": "PreToolUse", "tool": f"tool{i}", "ok": True, "t": 1_000_000_000_000 + i * 1_000_000_000}
            for i in range(10)
        ]
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_failure_rate("s1", spans, {}, stats, profile)
        assert result is None


class TestSessionCompletion:
    def test_strength_when_stopped(self):
        profile = _profile()
        spans = [
            {"event": "UserPromptSubmit", "tool": None, "ok": True, "t": 1_000_000_000_000},
            {"event": "Stop", "tool": None, "ok": True, "t": 1_060_000_000_000},
        ]
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_completion("s1", spans, {}, stats, profile)
        assert result is not None
        assert result.kind == "strength"

    def test_observation_when_no_stop(self):
        profile = _profile()
        spans = [
            {"event": "UserPromptSubmit", "tool": None, "ok": True, "t": 1_000_000_000_000},
        ] + [
            {"event": "PreToolUse", "tool": f"tool{i}", "ok": True, "t": 1_000_000_000_000 + i * 1_000_000_000}
            for i in range(1, 8)
        ]
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_completion("s1", spans, {}, stats, profile)
        assert result is not None
        assert result.kind == "observation"


class TestSessionDurationOutlier:
    def test_fires_for_long_session(self):
        profile = _profile()
        # upper_fence for duration = 600000 + 1.5 * (600000 - 120000) = 600000 + 720000 = 1320000
        # So > 1320000 ms should be outlier
        spans = [
            {"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_000_000_000_000},
            {"event": "PreToolUse", "tool": "Edit", "ok": True, "t": 1_000_000_000_000 + 2_000_000_000_000},
            # 2000 seconds = 2_000_000 ms > 1_320_000 ms
        ]
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_duration_outlier("s1", spans, {}, stats, profile)
        assert result is not None
        assert result.title == "Long session"

    def test_does_not_fire_for_normal_duration(self):
        profile = _profile()
        spans = [
            {"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_000_000_000_000},
            {"event": "PreToolUse", "tool": "Edit", "ok": True, "t": 1_000_000_000_000 + 300_000_000_000},
            # 300 seconds = 300_000 ms, within fence
        ]
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_duration_outlier("s1", spans, {}, stats, profile)
        assert result is None


class TestSessionRecovery:
    def test_fires_when_recovered(self):
        profile = _profile()
        spans = [
            {"event": "PreToolUse", "tool": "Bash", "ok": False, "t": 1_000_000_000_000},
            {"event": "PreToolUse", "tool": "Bash", "ok": True, "t": 1_001_000_000_000},
        ]
        stats = _make_stats(
            sessions_seen={"s1"},
            session_recovered_failures={"s1": 2},
        )

        result = signal_session_recovery("s1", spans, {}, stats, profile)
        assert result is not None
        assert result.kind == "strength"
        assert "Recovered" in result.body

    def test_does_not_fire_when_no_recovery(self):
        profile = _profile()
        spans = [
            {"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_000_000_000_000},
        ]
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_recovery("s1", spans, {}, stats, profile)
        assert result is None


class TestSessionProductiveEdits:
    def test_fires_for_good_edit_ratio(self):
        profile = _profile()
        spans = [
            {"event": "BeforeReadFile", "tool": "Read", "ok": True, "t": 1_000_000_000_000},
            {"event": "BeforeReadFile", "tool": "Read", "ok": True, "t": 1_001_000_000_000},
            {"event": "AfterFileEdit", "tool": "Edit", "ok": True, "t": 1_002_000_000_000},
            {"event": "AfterFileEdit", "tool": "Edit", "ok": True, "t": 1_003_000_000_000},
        ]
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_productive_edits("s1", spans, {}, stats, profile)
        assert result is not None
        assert result.kind == "strength"

    def test_does_not_fire_for_no_edits(self):
        profile = _profile()
        spans = [
            {"event": "BeforeReadFile", "tool": "Read", "ok": True, "t": 1_000_000_000_000},
        ]
        stats = _make_stats(sessions_seen={"s1"})

        result = signal_session_productive_edits("s1", spans, {}, stats, profile)
        assert result is None


class TestSessionModelMix:
    def test_fires_for_multiple_models(self):
        profile = _profile()
        spans = []
        stats = _make_stats(
            sessions_seen={"s1"},
            session_models={"s1": Counter({"claude-opus-4": 5, "claude-sonnet-4": 3})},
        )

        result = signal_session_model_mix("s1", spans, {}, stats, profile)
        assert result is not None
        assert "2 models" in result.body

    def test_does_not_fire_for_single_model(self):
        profile = _profile()
        spans = []
        stats = _make_stats(
            sessions_seen={"s1"},
            session_models={"s1": Counter({"claude-opus-4": 10})},
        )

        result = signal_session_model_mix("s1", spans, {}, stats, profile)
        assert result is None


class TestSessionCacheUtilization:
    def test_strength_for_good_cache(self):
        profile = _profile()
        spans = []
        tokens = {"input": 200000, "output": 50000, "cache_read": 60000}

        result = signal_session_cache_utilization("s1", spans, tokens, _make_stats(), profile)
        assert result is not None
        assert result.kind == "strength"

    def test_observation_for_weak_cache(self):
        profile = _profile()
        spans = []
        tokens = {"input": 600000, "output": 100000, "cache_read": 5000}

        result = signal_session_cache_utilization("s1", spans, tokens, _make_stats(), profile)
        assert result is not None
        assert result.kind == "observation"
        assert "Weak cache" in result.title

    def test_does_not_fire_for_low_volume(self):
        profile = _profile()
        spans = []
        tokens = {"input": 5000, "output": 1000, "cache_read": 100}

        result = signal_session_cache_utilization("s1", spans, tokens, _make_stats(), profile)
        assert result is None


class TestBuildSessionInsightsIntegration:
    def test_returns_sorted_insights(self):
        stats = _make_stats(
            sessions_seen={"s1"},
            session_tokens={"s1": {"input": 500000, "output": 200000}},
            session_span_details={"s1": [
                {"event": "PreToolUse", "tool": "Bash", "ok": False, "t": 1_000_000_000_000},
                {"event": "PreToolUse", "tool": "Bash", "ok": False, "t": 1_001_000_000_000},
                {"event": "PreToolUse", "tool": "Bash", "ok": False, "t": 1_002_000_000_000},
                {"event": "PreToolUse", "tool": "Bash", "ok": True, "t": 1_003_000_000_000},
                {"event": "PreToolUse", "tool": "Bash", "ok": True, "t": 1_004_000_000_000},
                {"event": "PreToolUse", "tool": "Bash", "ok": True, "t": 1_005_000_000_000},
            ]},
            session_recovered_failures={"s1": 1},
        )
        insights = build_session_insights("s1", stats)
        assert len(insights) > 0
        # Verify sorted by priority descending
        priorities = [i.priority for i in insights]
        assert priorities == sorted(priorities, reverse=True)

    def test_severity_scales_with_deviation(self):
        """More extreme outliers get higher severity."""
        # Use 20 normal sessions so p95 is well below the outlier
        n = 20
        stats = _make_stats(
            sessions_seen={f"s{i}" for i in range(n + 1)},
            session_tokens={
                **{f"s{i}": {"input": 50000, "output": 20000} for i in range(n)},
                f"s{n}": {"input": 2000000, "output": 500000},  # extreme outlier
            },
            session_span_details={f"s{i}": [
                {"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_000_000_000_000},
            ] for i in range(n + 1)},
        )
        insights = build_session_insights(f"s{n}", stats)
        token_insight = next((i for i in insights if i.title == "Token usage outlier"), None)
        assert token_insight is not None
        assert token_insight.severity >= Severity.HIGH

    def test_sparse_profile_uses_cold_start(self):
        """With few sessions, cold-start thresholds are used."""
        stats = _make_stats(
            sessions_seen={"s1", "s2"},
            session_tokens={
                "s1": {"input": 600000, "output": 200000},
                "s2": {"input": 5000, "output": 1000},
            },
            session_span_details={
                "s1": [{"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_000_000_000_000}],
                "s2": [{"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_000_000_000_000}],
            },
        )
        # s1 has 800k tokens, which exceeds the cold-start 500k threshold
        insights = build_session_insights("s1", stats)
        token_insight = next((i for i in insights if i.title == "Token usage outlier"), None)
        assert token_insight is not None
