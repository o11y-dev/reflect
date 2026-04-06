"""Tests for module-level rendering helpers."""

from reflect.core import _fmt_dur, _fmt_model, _fmt_tokens, _safe_ratio, _bar


class TestFmtDur:
    def test_zero(self):
        assert _fmt_dur(0) == "—"

    def test_negative(self):
        assert _fmt_dur(-1) == "—"

    def test_milliseconds(self):
        assert _fmt_dur(500) == "500ms"

    def test_milliseconds_small(self):
        assert _fmt_dur(1) == "1ms"

    def test_milliseconds_boundary(self):
        assert _fmt_dur(999) == "999ms"

    def test_seconds(self):
        assert _fmt_dur(3500) == "4s"

    def test_seconds_exact(self):
        assert _fmt_dur(1000) == "1s"

    def test_seconds_boundary(self):
        assert _fmt_dur(59999) == "60s"

    def test_minutes(self):
        assert _fmt_dur(125000) == "2m05s"

    def test_minutes_exact(self):
        assert _fmt_dur(60000) == "1m00s"

    def test_minutes_large(self):
        assert _fmt_dur(3_600_000) == "60m00s"


class TestFmtModel:
    def test_strips_claude_prefix_and_date(self):
        assert _fmt_model("claude-sonnet-4-20250514") == "sonnet-4"

    def test_strips_claude_prefix_no_date(self):
        assert _fmt_model("claude-opus-4") == "opus-4"

    def test_no_prefix_unchanged(self):
        assert _fmt_model("gpt-4o-2024-11-20") == "gpt-4o-2024-11-20"

    def test_gemini_unchanged(self):
        assert _fmt_model("gemini-2.5-pro") == "gemini-2.5-pro"

    def test_empty_string(self):
        assert _fmt_model("") == ""

    def test_only_prefix(self):
        assert _fmt_model("claude-") == ""


class TestFmtTokens:
    def test_small(self):
        assert _fmt_tokens(42) == "42"

    def test_zero(self):
        assert _fmt_tokens(0) == "0"

    def test_thousands(self):
        assert _fmt_tokens(5400) == "5.4K"

    def test_thousands_exact(self):
        assert _fmt_tokens(1000) == "1.0K"

    def test_millions(self):
        assert _fmt_tokens(1_500_000) == "1.5M"

    def test_millions_exact(self):
        assert _fmt_tokens(1_000_000) == "1.0M"


class TestSafeRatio:
    def test_normal(self):
        assert _safe_ratio(10, 5) == 2.0

    def test_zero_denominator(self):
        assert _safe_ratio(10, 0) == 0.0

    def test_zero_numerator(self):
        assert _safe_ratio(0, 5) == 0.0

    def test_fraction(self):
        assert _safe_ratio(1, 3) == pytest.approx(0.333, rel=1e-2)


class TestBar:
    def test_full(self):
        result = _bar(10, 10, "cyan")
        assert "█" * 10 in result.plain

    def test_empty(self):
        result = _bar(0, 10, "cyan")
        assert "░" * 10 in result.plain

    def test_partial(self):
        result = _bar(3, 10, "cyan")
        plain = result.plain
        assert plain.count("█") == 3
        assert plain.count("░") == 7

    def test_zero_total(self):
        result = _bar(0, 0, "cyan")
        assert result.plain == ""


import pytest
