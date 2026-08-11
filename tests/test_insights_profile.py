"""Tests for the distributions consumed by session-quality rules."""

from collections import Counter

from reflect.insights.profile import build_data_profile
from reflect.insights.types import (
    DistributionStats,
    compute_distribution,
)
from reflect.models import TelemetryStats


class TestDistributionStats:
    def test_empty_returns_sparse(self):
        dist = compute_distribution([])
        assert dist.is_sparse()
        assert dist.count == 0
        assert dist.mean == 0.0

    def test_single_value(self):
        dist = compute_distribution([42.0])
        assert dist.count == 1
        assert dist.mean == 42.0
        assert dist.median == 42.0
        assert dist.stdev == 0.0
        assert dist.is_sparse()

    def test_five_values_not_sparse(self):
        dist = compute_distribution([1.0, 2.0, 3.0, 4.0, 5.0])
        assert not dist.is_sparse()
        assert dist.count == 5
        assert dist.min_val == 1.0
        assert dist.max_val == 5.0

    def test_percentiles_computed(self):
        values = [float(i) for i in range(1, 101)]  # 1..100
        dist = compute_distribution(values)
        assert dist.p25 == 25.0
        assert dist.median == 50.0
        assert dist.p75 == 75.0
        assert dist.p90 == 90.0
        assert dist.p95 == 95.0

    def test_iqr(self):
        values = [float(i) for i in range(1, 101)]
        dist = compute_distribution(values)
        assert dist.iqr() == dist.p75 - dist.p25

    def test_upper_fence(self):
        dist = DistributionStats(
            count=10, mean=5.0, median=5.0,
            p25=3.0, p75=7.0, p90=9.0, p95=9.5,
            min_val=1.0, max_val=10.0, stdev=2.5,
        )
        # IQR = 7 - 3 = 4; upper fence = 7 + 1.5*4 = 13
        assert dist.upper_fence() == 13.0

    def test_is_outlier_high(self):
        dist = DistributionStats(
            count=10, mean=5.0, median=5.0,
            p25=3.0, p75=7.0, p90=9.0, p95=9.5,
            min_val=1.0, max_val=10.0, stdev=2.5,
        )
        assert dist.is_outlier_high(14.0)  # > 13
        assert not dist.is_outlier_high(12.0)  # < 13

    def test_sparse_custom_min_count(self):
        dist = compute_distribution([1.0, 2.0, 3.0])
        assert dist.is_sparse(min_count=5)
        assert not dist.is_sparse(min_count=3)


class TestBuildDataProfile:
    def _make_stats(self, **overrides) -> TelemetryStats:
        defaults = {
            "session_files": 1,
            "span_files": 1,
            "total_events": 0,
            "events_by_type": Counter(),
            "events_by_file": {},
        }
        defaults.update(overrides)
        return TelemetryStats(**defaults)

    def test_empty_stats_returns_sparse_profile(self):
        stats = self._make_stats()
        profile = build_data_profile(stats)
        assert profile.session_total_tokens.is_sparse()

    def test_profile_with_sessions(self):
        stats = self._make_stats(
            sessions_seen={"s1", "s2", "s3", "s4", "s5"},
            session_tokens={
                "s1": {"input": 1000, "output": 500},
                "s2": {"input": 2000, "output": 1000},
                "s3": {"input": 3000, "output": 1500},
                "s4": {"input": 4000, "output": 2000},
                "s5": {"input": 5000, "output": 2500},
            },
            session_span_details={
                "s1": [{"tool": "Read", "ok": True, "event": "PreToolUse", "t": 1_000_000_000}],
                "s2": [{"tool": "Edit", "ok": True, "event": "PreToolUse", "t": 2_000_000_000}],
                "s3": [{"tool": "Grep", "ok": True, "event": "PreToolUse", "t": 3_000_000_000}],
                "s4": [{"tool": "Bash", "ok": True, "event": "PreToolUse", "t": 4_000_000_000}],
                "s5": [{"tool": "Write", "ok": True, "event": "PreToolUse", "t": 5_000_000_000}],
            },
            events_by_type=Counter({"UserPromptSubmit": 10, "PreToolUse": 15}),
            total_input_tokens=15000,
            total_output_tokens=7500,
        )
        profile = build_data_profile(stats)
        assert not profile.session_total_tokens.is_sparse()
        assert profile.session_total_tokens.count == 5
        assert profile.session_tool_count.count == 5
