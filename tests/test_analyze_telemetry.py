"""Integration tests: files → TelemetryStats via analyze_telemetry."""

from conftest import (
    CLAUDE,
    COPILOT,
    DAY1,
    EXPECTED,
    GEMINI,
    HOUR,
    MCP_CLOUDFLARE,
    MCP_GITLAB,
    MCP_POSTGRES,
    MIN,
    MODEL_CLAUDE,
    make_span,
    wrap_otlp,
)

from reflect.core import analyze_telemetry


class TestAnalyzeOtlpTraces:
    def test_basic_event_count(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "no-sessions", tmp_path / "no-spans", otlp_traces_file)
        assert stats.total_events == EXPECTED["total_spans"]

    def test_session_count(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        assert len(stats.sessions_seen) == EXPECTED["sessions"]

    def test_agents_detected(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        assert set(stats.agents.keys()) == EXPECTED["agents"]

    def test_models_detected(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        assert set(stats.models_by_count.keys()) == EXPECTED["models"]

    def test_mcp_servers_detected(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        assert set(stats.mcp_servers.keys()) == EXPECTED["mcp_servers"]

    def test_mcp_before_after_counts(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        assert stats.events_by_type["BeforeMCPExecution"] == EXPECTED["BeforeMCPExecution"]
        assert stats.events_by_type["AfterMCPExecution"] == EXPECTED["AfterMCPExecution"]

    def test_mcp_availability_gap_postgres(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        # Postgres has more befores than afters
        assert stats.mcp_server_before[MCP_POSTGRES] > stats.mcp_server_after[MCP_POSTGRES]

    def test_mcp_availability_gap_cloudflare(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        assert stats.mcp_server_before[MCP_CLOUDFLARE] > stats.mcp_server_after[MCP_CLOUDFLARE]

    def test_subagent_counts(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        assert stats.events_by_type["SubagentStart"] == EXPECTED["SubagentStart"]
        assert stats.events_by_type["SubagentStop"] == EXPECTED["SubagentStop"]

    def test_days_active(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        assert stats.days_active == EXPECTED["days_active"]

    def test_date_range(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        # Spans span 3 days: DAY1=2026-03-24, DAY2=2026-03-25, DAY3=2026-03-26
        assert "2026-03-24" in stats.first_event_ts or stats.first_event_ts[:10] <= "2026-03-24"
        assert stats.last_event_ts[:10] >= "2026-03-26"

    def test_token_totals(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        assert stats.total_input_tokens > 0
        assert stats.total_output_tokens > 0

    def test_shell_commands_tracked(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        assert "pytest tests/ -v --tb=short" in stats.shell_commands
        assert "alembic upgrade head" in stats.shell_commands

    def test_tool_durations_tracked(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        assert "Read" in stats.tool_durations_ms
        assert "Edit" in stats.tool_durations_ms


class TestAnalyzeEmptyInputs:
    def test_no_files_zero_events(self, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", None)
        assert stats.total_events == 0
        assert len(stats.sessions_seen) == 0

    def test_empty_otlp_file(self, empty_otlp_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", empty_otlp_file)
        assert stats.total_events == 0

    def test_single_span(self, single_span_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", single_span_file)
        assert stats.total_events == 1
        assert stats.events_by_type["UserPromptSubmit"] == 1

    def test_session_metadata_last_known_model_fills_blank_session_model(self, tmp_path):
        session_id = "sess-metadata-001"
        sessions_dir = tmp_path / "s"
        sessions_dir.mkdir()
        (sessions_dir / f"{session_id}.json").write_text(
            '{"created_at":"2026-03-29T19:16:44.080251+00:00","ide":"cursor","last_known_model":"claude-4.6-opus-high"}'
        )

        traces = tmp_path / "traces.json"
        span = make_span(
            "UserPromptSubmit",
            session=session_id,
            model="",
            start_ns=DAY1 + 12 * HOUR,
            duration_ms=10,
        )
        span["attributes"].pop("gen_ai.request.model", None)
        traces.write_text(wrap_otlp([span]) + "\n")

        stats = analyze_telemetry(sessions_dir, tmp_path / "sp", traces)
        assert stats.session_models[session_id].most_common(1)[0][0] == "claude-4.6-opus-high"

    def test_session_end_counts_as_completed_session(self, tmp_path):
        session_id = "sess-session-end-only"
        traces = tmp_path / "traces.json"
        traces.write_text(wrap_otlp([
            make_span("SessionStart", session=session_id, start_ns=DAY1 + 9 * HOUR, duration_ms=1),
            make_span("UserPromptSubmit", session=session_id, start_ns=DAY1 + 9 * HOUR + MIN, duration_ms=10),
            make_span("SessionEnd", session=session_id, start_ns=DAY1 + 9 * HOUR + 2 * MIN, duration_ms=1),
        ]) + "\n")

        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", traces)

        assert stats.session_goal_completed[session_id] is True
        assert stats.session_quality_scores[session_id] >= 40


class TestAgentBreakdown:
    def test_claude_session_count(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        assert len(stats.agents[CLAUDE].sessions_seen) == 2

    def test_copilot_session_count(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        assert len(stats.agents[COPILOT].sessions_seen) == 2

    def test_gemini_session_count(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        assert len(stats.agents[GEMINI].sessions_seen) == 2

    def test_claude_model(self, otlp_traces_file, tmp_path):
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        assert stats.agents[CLAUDE].models_by_count.most_common(1)[0][0] == MODEL_CLAUDE

    def test_mcp_cross_agent_gitlab(self, otlp_traces_file, tmp_path):
        """GitLab MCP is used by all 3 agents."""
        stats = analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)
        for agent in [CLAUDE, COPILOT, GEMINI]:
            assert stats.agents[agent].mcp_servers.get(MCP_GITLAB, 0) > 0
