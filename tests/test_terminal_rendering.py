"""Tests for Rich terminal dashboard output."""

import io
from collections import Counter

import pytest
from conftest import make_span, wrap_otlp
from rich.console import Console

from reflect.core import _render_terminal, analyze_telemetry
from reflect.models import TelemetryStats


def render_to_string(stats, **kwargs) -> str:
    """Run _render_terminal with a captured console and return plain text."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=120, highlight=False)
    _render_terminal(stats, console=console, **kwargs)
    return buf.getvalue()


@pytest.fixture
def rich_stats(otlp_traces_file, tmp_path):
    return analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)


@pytest.fixture
def empty_stats(tmp_path):
    return analyze_telemetry(tmp_path / "s", tmp_path / "sp", None)


class TestHeaderSection:
    def test_header_present(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "AI Usage Dashboard" in output

    def test_date_range_present(self, rich_stats):
        output = render_to_string(rich_stats)
        # DAY1=2026-03-24, DAY3 spans end 2026-03-26
        assert "2026-03" in output  # some March 2026 date shown


class TestSummaryCards:
    def test_events_card(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "Events" in output

    def test_sessions_card(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "Sessions" in output

    def test_active_days_card(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "Active Days" in output

    def test_prompts_card(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "Prompts" in output

    def test_tool_prompt_card(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "Tool/Prompt" in output

    def test_failure_card(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "Failure" in output

    def test_top_model_card(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "Top Model" in output


class TestTokenCards:
    def test_token_cards_present_when_tokens_exist(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "Input Tokens" in output
        assert "Output Tokens" in output

    def test_token_cards_absent_when_no_tokens(self, tmp_path):
        spans = [make_span("UserPromptSubmit", session="s1")]  # no tokens
        p = tmp_path / "t.json"
        p.write_text(wrap_otlp(spans) + "\n")
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", p)
        output = render_to_string(stats)
        assert "Input Tokens" not in output

    def test_cache_hit_card(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "Cache Hit" in output

    def test_cost_cards_present_when_tokens_exist(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "Est. Total Cost" in output
        assert "Pricing Source" in output


class TestActivitySection:
    def test_heatmap_present(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "Activity" in output

    def test_hourly_chart_present(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "Hour" in output


class TestToolsSection:
    def test_tools_table_present(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "Read" in output or "Edit" in output or "Grep" in output

    def test_tools_table_has_cost_column_and_attribution(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "Top Tools" in output
        assert "Cost" in output


class TestMcpSection:
    def test_mcp_servers_table_present(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "MCP" in output

    def test_mcp_server_names_shown(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "mcp-gitlab" in output or "gitlab" in output.lower()


class TestModelsSection:
    def test_models_present(self, rich_stats):
        output = render_to_string(rich_stats)
        # Model names are shortened (claude- prefix stripped)
        assert "sonnet" in output or "Models" in output


class TestSessionsSection:
    def test_sessions_table_present(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "sess-claude" in output or "sess-copilot" in output or "Sessions" in output

    def test_sessions_table_has_cost_column_and_attribution(self, rich_stats):
        output = render_to_string(rich_stats)
        assert "Cost" in output


class TestPublishUrl:
    def test_publish_url_shown(self, rich_stats):
        output = render_to_string(rich_stats, publish_url="https://reflect.o11y.dev/?data=abc123")
        assert "reflect.o11y.dev" in output


class TestCommandRedaction:
    def test_terminal_redacts_command_paths(self):
        stats = TelemetryStats(
            session_files=0,
            span_files=0,
            total_events=4,
            events_by_type=Counter({"BeforeShellExecution": 4}),
            events_by_file={},
            shell_commands=Counter({
                "python /Users/alice/work/app/train.py --config /Users/alice/work/app/config.yaml": 1,
                "python /Users/bob/work/app/train.py --config /Users/bob/work/app/config.yaml": 3,
            }),
        )

        output = render_to_string(stats)

        assert "python <path>/train.py --config <path>/config.yaml" in output
        assert "/Users/alice/" not in output
        assert "/Users/bob/" not in output


class TestEmptyStats:
    def test_empty_stats_no_crash(self, empty_stats):
        render_to_string(empty_stats)  # should not raise

    def test_empty_stats_has_header(self, empty_stats):
        output = render_to_string(empty_stats)
        assert "AI Usage Dashboard" in output
