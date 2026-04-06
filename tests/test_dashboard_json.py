"""Tests for dashboard JSON generation."""

import json
from collections import Counter

import pytest

from reflect.core import _build_dashboard_json, analyze_telemetry
from reflect.dashboard import (
    _build_filtered_comparison_payload,
    _build_filtered_stats,
    _filter_dashboard_sessions,
    _load_session_telemetry,
)
from reflect.models import AgentStats, TelemetryStats


@pytest.fixture
def rich_stats(otlp_traces_file, tmp_path):
    return analyze_telemetry(tmp_path / "s", tmp_path / "sp", otlp_traces_file)


class TestBuildDashboardJson:
    def test_returns_valid_json(self, rich_stats):
        result = _build_dashboard_json(rich_stats)
        data = json.loads(result)
        assert isinstance(data, dict)

    def test_required_top_level_keys(self, rich_stats):
        data = json.loads(_build_dashboard_json(rich_stats))
        for key in [
            "total_spans", "sessions", "tools_by_count", "models_by_count",
            "skills_by_count",
            "activity_by_day", "activity_by_hour",
            "graph_tool_transitions", "graph_cooccurrence",
            "graph_latency_histograms", "graph_dep", "graph_session_timeline",
            "agents", "strengths", "observations", "recommendations",
            "practical_examples", "achievements", "token_economy",
        ]:
            assert key in data, f"Missing key: {key}"

    def test_sessions_list(self, rich_stats):
        data = json.loads(_build_dashboard_json(rich_stats))
        assert isinstance(data["sessions"], list)
        assert len(data["sessions"]) == 6

    def test_activity_by_hour_24_entries(self, rich_stats):
        data = json.loads(_build_dashboard_json(rich_stats))
        hours = data["activity_by_hour"]
        if isinstance(hours, dict):
            assert len(hours) <= 24
        else:
            assert len(hours) <= 24

    def test_graph_data_present(self, rich_stats):
        data = json.loads(_build_dashboard_json(rich_stats))
        assert isinstance(data["graph_tool_transitions"], list)
        assert "tools" in data["graph_cooccurrence"]
        assert "labels" in data["graph_latency_histograms"]

    def test_agents_section(self, rich_stats):
        data = json.loads(_build_dashboard_json(rich_stats))
        assert "claude" in data["agents"]
        assert "copilot" in data["agents"]
        assert "gemini-code-assist" in data["agents"]

    def test_mcp_servers_present(self, rich_stats):
        data = json.loads(_build_dashboard_json(rich_stats))
        mcp_data = data.get("mcp_servers_by_count") or data.get("mcp_server_before") or {}
        assert "mcp-gitlab" in mcp_data
        assert "mcp-coralogix" in mcp_data

    def test_cursor_sessions_without_exact_tokens_get_provenance_note(self):
        stats = TelemetryStats(
            session_files=0,
            span_files=0,
            total_events=0,
            events_by_type=Counter(),
            events_by_file={},
            sessions_seen={"cursor-session-1"},
            session_events={"cursor-session-1": 2},
            session_models={},
            session_first_ts={},
            agents={},
            session_tokens={},
            session_source={"cursor-session-1": ("cursor", "/tmp/cursor-session-1.jsonl")},
            session_conversation={"cursor-session-1": [{"type": "prompt", "preview": "hello"}]},
        )

        data = json.loads(_build_dashboard_json(stats))
        session = data["sessions"][0]

        assert session["token_source"] == "cursor_local_unavailable"
        assert "Exact per-session Cursor token usage is not present" in session["token_note"]

    def test_extracts_skill_usage_from_tool_call_previews(self):
        stats = TelemetryStats(
            session_files=0,
            span_files=0,
            total_events=0,
            events_by_type=Counter(),
            events_by_file={},
            sessions_seen={"skill-session-1"},
            session_events={"skill-session-1": 3},
            session_models={},
            session_first_ts={},
            agents={},
            session_tokens={},
            session_source={"skill-session-1": ("copilot", "/tmp/skill-session-1.jsonl")},
            session_conversation={
                "skill-session-1": [
                    {"type": "tool_call", "tool_name": "skill", "preview": '{"skill":"reflect"}'},
                    {"type": "tool_call", "tool_name": "skill", "preview": '{"skill":"reflect"}'},
                    {"type": "tool_call", "tool_name": "skill", "preview": '{"skill":"opentelemetry-skill"}'},
                ]
            },
        )

        data = json.loads(_build_dashboard_json(stats))

        assert data["skills_by_count"]["reflect"] == 2
        assert data["skills_by_count"]["opentelemetry-skill"] == 1
        assert data["sessions"][0]["skills"]["reflect"] == 2

    def test_filter_dashboard_sessions_by_agent(self, rich_stats):
        data = json.loads(_build_dashboard_json(rich_stats))
        filtered = _filter_dashboard_sessions(data["sessions"], agents={"copilot"})

        assert filtered
        assert len(filtered) < len(data["sessions"])
        assert all(session["agent"] == "copilot" for session in filtered)

    def test_build_filtered_stats_limits_payload_to_selected_sessions(self, rich_stats):
        full_data = json.loads(_build_dashboard_json(rich_stats))
        filtered_sessions = _filter_dashboard_sessions(full_data["sessions"], agents={"copilot"})

        filtered_stats = _build_filtered_stats(rich_stats, filtered_sessions)
        filtered_data = json.loads(_build_dashboard_json(filtered_stats))

        assert filtered_data["unique_sessions"] == len(filtered_sessions)
        assert filtered_data["sessions"]
        assert all(session["agent"] == "copilot" for session in filtered_data["sessions"])
        assert set(filtered_data["agents"].keys()) == {"copilot"}

    def test_build_filtered_stats_with_no_matches_returns_empty_dashboard(self, rich_stats):
        filtered_stats = _build_filtered_stats(rich_stats, [])
        filtered_data = json.loads(_build_dashboard_json(filtered_stats))

        assert filtered_data["unique_sessions"] == 0
        assert filtered_data["sessions"] == []
        assert filtered_data["agents"] == {}

    def test_build_filtered_stats_uses_raw_session_telemetry_not_truncated_session_summaries(self):
        session_id = "sess-raw-1"
        tool_spans = [
            {"t": 1_000_000_000 + index, "tool": f"tool-{index}", "dur": 5.0, "ok": True, "event": "PreToolUse"}
            for index in range(11)
        ]
        raw_commands = Counter({f"command-{index}": 1 for index in range(11)})
        stats = TelemetryStats(
            session_files=1,
            span_files=1,
            total_events=25,
            events_by_type=Counter({"PreToolUse": 11, "UserPromptSubmit": 1}),
            events_by_file={},
            sessions_seen={session_id},
            session_events={session_id: 25},
            session_models={session_id: Counter({"gpt-5.4": 1})},
            session_first_ts={session_id: 1_000_000_000},
            session_shell_commands={session_id: raw_commands},
            shell_commands=raw_commands.copy(),
            session_span_details={session_id: tool_spans},
            tools_by_count=Counter({span["tool"]: 1 for span in tool_spans}),
            tool_durations_ms={span["tool"]: [5.0] for span in tool_spans},
            session_conversation={session_id: [{"type": "prompt", "ts": 1_000, "preview": "needle prompt"}]},
            session_source={session_id: ("copilot", "/tmp/copilot.jsonl")},
            agents={"copilot": AgentStats(name="copilot", sessions_seen={session_id})},
        )

        full_data = json.loads(_build_dashboard_json(stats))
        filtered_sessions = _filter_dashboard_sessions(full_data["sessions"], q="needle")
        filtered_stats = _build_filtered_stats(stats, filtered_sessions)
        filtered_data = json.loads(_build_dashboard_json(filtered_stats))

        assert filtered_data["unique_sessions"] == 1
        assert len(filtered_data["tools_by_count"]) == 11
        assert set(filtered_data["tools_by_count"].keys()) == {f"tool-{index}" for index in range(11)}
        assert filtered_data["unique_commands"] == 11
        assert {entry["command"] for entry in filtered_data["top_commands"]} == {f"command-{index}" for index in range(11)}

    def test_build_dashboard_json_redacts_command_paths_for_display(self):
        session_id = "sess-safe-1"
        stats = TelemetryStats(
            session_files=1,
            span_files=1,
            total_events=6,
            events_by_type=Counter({"BeforeShellExecution": 5}),
            events_by_file={},
            sessions_seen={session_id},
            session_events={session_id: 6},
            session_models={session_id: Counter({"gpt-5.4": 1})},
            session_first_ts={session_id: 1_000_000_000},
            shell_commands=Counter({
                "python /Users/alice/work/app/train.py --config /Users/alice/work/app/config.yaml": 2,
                "python /Users/bob/work/app/train.py --config /Users/bob/work/app/config.yaml": 3,
            }),
            tools_by_count=Counter({
                "python /Users/alice/work/app/train.py": 2,
                "python /Users/bob/work/app/train.py": 3,
            }),
            tool_durations_ms={
                "python /Users/alice/work/app/train.py": [10.0, 20.0],
                "python /Users/bob/work/app/train.py": [30.0, 40.0, 50.0],
            },
            session_shell_commands={
                session_id: Counter({
                    "python /Users/alice/work/app/train.py --config /Users/alice/work/app/config.yaml": 2,
                    "python /Users/bob/work/app/train.py --config /Users/bob/work/app/config.yaml": 3,
                })
            },
            session_span_details={
                session_id: [
                    {
                        "t": 1_000_000_000,
                        "tool": "python /Users/alice/work/app/train.py",
                        "dur": 50.0,
                        "ok": True,
                        "event": "BeforeShellExecution",
                    }
                ]
            },
            session_source={session_id: ("copilot", "/tmp/copilot.jsonl")},
            session_conversation={session_id: [{"type": "prompt", "preview": "redact commands"}]},
        )

        data = json.loads(_build_dashboard_json(stats))

        assert data["unique_commands"] == 1
        assert data["top_commands"][0]["command"] == "python <path>/train.py --config <path>/config.yaml"
        assert data["top_commands"][0]["count"] == 5
        assert data["signature_command"] == "python <path>/train.py --config <path>/config.yaml"
        assert data["sessions"][0]["commands"] == {"python <path>/train.py --config <path>/config.yaml": 5}
        assert data["tools_by_count"] == {"python <path>/train.py": 5}
        assert data["tool_percentiles"][0]["tool"] == "python <path>/train.py"
        assert data["graph_session_timeline"][0]["spans"][0]["tool"] == "python <path>/train.py"

    def test_build_filtered_comparison_payload_compares_single_agent_to_rest_of_scope(self, rich_stats):
        full_data = json.loads(_build_dashboard_json(rich_stats))
        filtered_sessions = _filter_dashboard_sessions(full_data["sessions"], agents={"copilot"})

        comparison = _build_filtered_comparison_payload(
            rich_stats,
            full_data["sessions"],
            filtered_sessions,
            model="all",
            status="all",
            range_name="all",
        )

        assert comparison is not None
        assert comparison["mode"] == "cohort-vs-rest"
        assert comparison["primary"]["label"] == "copilot"
        assert comparison["primary"]["agents"] == ["copilot"]
        assert comparison["primary"]["sessions"] == len(filtered_sessions)
        assert comparison["baseline"]["label"] == "All other agents in scope"
        assert comparison["baseline"]["sessions"] > 0
        assert all(agent["name"] != "copilot" for agent in comparison["baseline_agents"])

    def test_build_dashboard_json_handles_zero_prompt_filtered_payloads(self):
        stats = TelemetryStats(
            session_files=1,
            span_files=1,
            total_events=12,
            events_by_type=Counter({"BeforeReadFile": 12, "PreToolUse": 4}),
            events_by_file={},
            sessions_seen={"sess-zero-prompts"},
            session_events={"sess-zero-prompts": 12},
            session_models={"sess-zero-prompts": Counter({"gpt-5.4": 1})},
            session_first_ts={"sess-zero-prompts": 1_000_000_000},
            session_source={"sess-zero-prompts": ("copilot", "/tmp/copilot.jsonl")},
            session_conversation={"sess-zero-prompts": []},
        )

        data = json.loads(_build_dashboard_json(stats))

        assert data["prompt_submits"] == 0
        assert data["observations"]

    def test_load_session_telemetry_filters_traces_and_logs(self, tmp_path):
        session_id = "sess-telemetry-1"
        other_session = "sess-telemetry-2"
        trace_path = tmp_path / "otel-traces.json"
        log_path = tmp_path / "otel-logs.json"

        trace_payload = {
            "resourceSpans": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "claude-code"}}
                    ]
                },
                "scopeSpans": [{
                    "scope": {"name": "ide-hooks"},
                    "spans": [
                        {
                            "traceId": "trace-a",
                            "spanId": "span-a",
                            "parentSpanId": "",
                            "name": "gen_ai.client.hook.PreToolUse",
                            "startTimeUnixNano": "1000000000",
                            "endTimeUnixNano": "2000000000",
                            "attributes": [
                                {"key": "gen_ai.client.session_id", "value": {"stringValue": session_id}},
                                {"key": "gen_ai.client.hook.event", "value": {"stringValue": "PreToolUse"}},
                                {"key": "gen_ai.client.tool_name", "value": {"stringValue": "Bash"}},
                                {"key": "gen_ai.client.name", "value": {"stringValue": "claude"}},
                            ],
                        },
                        {
                            "traceId": "trace-b",
                            "spanId": "span-b",
                            "parentSpanId": "",
                            "name": "gen_ai.client.hook.PreToolUse",
                            "startTimeUnixNano": "3000000000",
                            "endTimeUnixNano": "3500000000",
                            "attributes": [
                                {"key": "gen_ai.client.session_id", "value": {"stringValue": other_session}},
                                {"key": "gen_ai.client.hook.event", "value": {"stringValue": "PreToolUse"}},
                            ],
                        },
                    ],
                }],
            }],
        }
        trace_path.write_text(json.dumps(trace_payload) + "\n", encoding="utf-8")

        log_payload = {
            "resourceLogs": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "claude-code"}}
                    ]
                },
                "scopeLogs": [{
                    "scope": {"name": "ide-hooks"},
                    "logRecords": [
                        {
                            "timeUnixNano": "1500000000",
                            "severityText": "ERROR",
                            "severityNumber": 17,
                            "traceId": "trace-a",
                            "spanId": "span-a",
                            "body": {"stringValue": "claude_code.api_error"},
                            "attributes": [
                                {"key": "gen_ai.client.session_id", "value": {"stringValue": session_id}},
                                {"key": "gen_ai.client.hook.event", "value": {"stringValue": "PostToolUseFailure"}},
                            ],
                        },
                        {
                            "timeUnixNano": "2500000000",
                            "severityText": "INFO",
                            "severityNumber": 9,
                            "traceId": "trace-b",
                            "spanId": "span-b",
                            "body": {"stringValue": "other"},
                            "attributes": [
                                {"key": "gen_ai.client.session_id", "value": {"stringValue": other_session}},
                            ],
                        },
                    ],
                }],
            }],
        }
        log_path.write_text(json.dumps(log_payload) + "\n", encoding="utf-8")

        telemetry = _load_session_telemetry(session_id, otlp_traces_file=trace_path, otlp_logs_file=log_path)

        assert telemetry["summary"]["spans"] == 1
        assert telemetry["summary"]["logs"] == 1
        assert telemetry["summary"]["errors"] == 1
        assert telemetry["spans"][0]["tool_name"] == "Bash"
        assert telemetry["logs"][0]["severity"] == "ERROR"
        assert telemetry["logs"][0]["body"] == "claude_code.api_error"
