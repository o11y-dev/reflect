"""Tests for the distribution-aware session quality scorer."""

from reflect.insights.scoring import compute_session_quality
from reflect.insights.types import DataProfile, DistributionStats


def _dist(count=10, mean=50.0, median=50.0, p25=30.0, p75=70.0, p90=80.0,
          p95=90.0, min_val=10.0, max_val=100.0, stdev=20.0):
    return DistributionStats(
        count=count, mean=mean, median=median,
        p25=p25, p75=p75, p90=p90, p95=p95,
        min_val=min_val, max_val=max_val, stdev=stdev,
    )


def _make_profile(**overrides):
    defaults = dict(
        session_total_tokens=_dist(),
        session_input_tokens=_dist(),
        session_output_tokens=_dist(),
        session_tool_count=_dist(mean=10, median=10, p25=5, p75=15, p90=20, p95=25),
        session_prompt_count=_dist(mean=3, median=3, p25=2, p75=5, p90=7, p95=9),
        session_failure_count=_dist(mean=1, median=1, p25=0, p75=2, p90=3, p95=4, min_val=0),
        session_duration_ms=_dist(mean=300000, median=300000, p25=120000, p75=600000,
                                  p90=900000, p95=1200000, min_val=30000, max_val=1800000),
        session_quality_scores=_dist(),
        tokens_per_tool=_dist(mean=5000, median=5000, p25=3000, p75=8000, p90=12000,
                              p95=15000, min_val=1000, max_val=25000),
        tools_per_prompt=_dist(mean=3, median=3, p25=2, p75=5, p90=7, p95=10),
        reads_per_prompt=_dist(mean=2, median=2, p25=1, p75=3, p90=5, p95=7),
        session_token_share=_dist(),
        total_sessions=10,
        total_prompts=30,
        total_tool_calls=90,
        total_failures=5,
        cache_reuse_ratio=0.15,
        heavy_model_share=30.0,
    )
    defaults.update(overrides)
    return DataProfile(**defaults)


class TestComputeSessionQuality:
    def test_perfect_session(self):
        """Session with completion, no failures, diverse tools, edits."""
        spans = [
            {"event": "UserPromptSubmit", "tool": None, "ok": True, "t": 1_000_000_000_000},
            {"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_001_000_000_000},
            {"event": "PreToolUse", "tool": "Grep", "ok": True, "t": 1_002_000_000_000},
            {"event": "PreToolUse", "tool": "Edit", "ok": True, "t": 1_003_000_000_000},
            {"event": "AfterFileEdit", "tool": "Edit", "ok": True, "t": 1_004_000_000_000},
            {"event": "BeforeReadFile", "tool": "Read", "ok": True, "t": 1_005_000_000_000},
            {"event": "PreToolUse", "tool": "Bash", "ok": True, "t": 1_006_000_000_000},
            {"event": "PreToolUse", "tool": "Write", "ok": True, "t": 1_007_000_000_000},
            {"event": "Stop", "tool": None, "ok": True, "t": 1_060_000_000_000},
        ]
        tokens = {"input": 5000, "output": 2000}
        score = compute_session_quality("s1", spans, tokens, profile=_make_profile())
        assert score >= 70.0  # high quality session

    def test_minimal_session_no_profile(self):
        """Cold-start: no profile, minimal session."""
        spans = [
            {"event": "UserPromptSubmit", "tool": None, "ok": True, "t": 1_000_000_000_000},
            {"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_001_000_000_000},
            {"event": "Stop", "tool": None, "ok": True, "t": 1_010_000_000_000},
        ]
        tokens = {"input": 2000, "output": 500}
        score = compute_session_quality("s1", spans, tokens, profile=None)
        assert 0 <= score <= 100

    def test_failed_session_low_score(self):
        """Session with many failures scores lower."""
        spans = [
            {"event": "UserPromptSubmit", "tool": None, "ok": True, "t": 1_000_000_000_000},
            {"event": "PreToolUse", "tool": "Bash", "ok": False, "t": 1_001_000_000_000},
            {"event": "PostToolUseFailure", "tool": "Bash", "ok": False, "t": 1_002_000_000_000},
            {"event": "PreToolUse", "tool": "Bash", "ok": False, "t": 1_003_000_000_000},
            {"event": "PostToolUseFailure", "tool": "Bash", "ok": False, "t": 1_004_000_000_000},
            {"event": "PreToolUse", "tool": "Bash", "ok": False, "t": 1_005_000_000_000},
            {"event": "PostToolUseFailure", "tool": "Bash", "ok": False, "t": 1_006_000_000_000},
        ]
        tokens = {"input": 10000, "output": 5000}
        score = compute_session_quality("s1", spans, tokens, profile=_make_profile())
        assert score < 50.0

    def test_looping_session_penalized(self):
        """Repeated same tool consecutively gets loop penalty."""
        spans = [
            {"event": "UserPromptSubmit", "tool": None, "ok": True, "t": 1_000_000_000_000},
        ] + [
            {"event": "PreToolUse", "tool": "Bash", "ok": True, "t": 1_000_000_000_000 + i * 1_000_000_000}
            for i in range(1, 8)
        ] + [
            {"event": "Stop", "tool": None, "ok": True, "t": 1_100_000_000_000},
        ]
        tokens = {"input": 5000, "output": 2000}
        score_looped = compute_session_quality("s1", spans, tokens, profile=_make_profile())

        # Compare against non-looping session
        spans_diverse = [
            {"event": "UserPromptSubmit", "tool": None, "ok": True, "t": 1_000_000_000_000},
            {"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_001_000_000_000},
            {"event": "PreToolUse", "tool": "Grep", "ok": True, "t": 1_002_000_000_000},
            {"event": "PreToolUse", "tool": "Edit", "ok": True, "t": 1_003_000_000_000},
            {"event": "PreToolUse", "tool": "Bash", "ok": True, "t": 1_004_000_000_000},
            {"event": "PreToolUse", "tool": "Write", "ok": True, "t": 1_005_000_000_000},
            {"event": "PreToolUse", "tool": "Glob", "ok": True, "t": 1_006_000_000_000},
            {"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_007_000_000_000},
            {"event": "Stop", "tool": None, "ok": True, "t": 1_100_000_000_000},
        ]
        score_diverse = compute_session_quality("s1", spans_diverse, tokens, profile=_make_profile())
        assert score_looped < score_diverse

    def test_score_always_0_to_100(self):
        """Score is always clamped between 0 and 100."""
        # Extreme case: lots of everything
        spans = [
            {"event": "PreToolUse", "tool": "Bash", "ok": False, "t": 1_000_000_000_000 + i * 1_000_000}
            for i in range(50)
        ]
        tokens = {"input": 5_000_000, "output": 2_000_000}
        score = compute_session_quality("s1", spans, tokens, profile=_make_profile())
        assert 0.0 <= score <= 100.0

    def test_completion_bonus(self):
        """Sessions with Stop event get completion bonus."""
        base_spans = [
            {"event": "UserPromptSubmit", "tool": None, "ok": True, "t": 1_000_000_000_000},
            {"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_001_000_000_000},
        ]
        tokens = {"input": 2000, "output": 500}

        score_no_stop = compute_session_quality(
            "s1", base_spans, tokens, profile=_make_profile()
        )
        score_with_stop = compute_session_quality(
            "s1", base_spans + [{"event": "Stop", "tool": None, "ok": True, "t": 1_010_000_000_000}],
            tokens, profile=_make_profile(),
        )
        assert score_with_stop > score_no_stop

    def test_profile_none_uses_cold_start(self):
        """When profile is None, cold-start thresholds are used."""
        spans = [
            {"event": "UserPromptSubmit", "tool": None, "ok": True, "t": 1_000_000_000_000},
            {"event": "PreToolUse", "tool": "Read", "ok": True, "t": 1_001_000_000_000},
            {"event": "Stop", "tool": None, "ok": True, "t": 1_060_000_000_000},
        ]
        tokens = {"input": 3000, "output": 1000}
        score = compute_session_quality("s1", spans, tokens, profile=None)
        assert 0.0 <= score <= 100.0
