"""Tests for _process_span — the core span ingestion function."""

from collections import Counter

from conftest import (
    CLAUDE,
    COPILOT,
    DAY1,
    GEMINI,
    HOUR,
    MCP_CORALOGIX,
    MCP_JIRA,
    MODEL_CLAUDE,
    MODEL_COPILOT,
    SEC,
    make_span,
)

from reflect.core import AgentStats, _process_span


def _fresh_counters():
    """Return a fresh set of all _process_span accumulator arguments."""
    return {
        "events_by_type": Counter(),
        "models": Counter(),
        "tools": Counter(),
        "mcp_servers": Counter(),
        "subagent_types": Counter(),
        "sessions_seen": set(),
        "timestamps_ns": [],
        "tool_durations_ms": {},
        "activity_by_day": Counter(),
        "activity_by_hour": Counter(),
        "model_by_day": {},
        "session_events": {},
        "session_models": {},
        "session_first_ts": {},
        "shell_commands": Counter(),
        "session_shell_commands": {},
        "agents": {},
        "session_tool_seq": {},
        "session_span_details": {},
        "token_totals": {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0},
        "session_tokens": {},
        "mcp_server_before": Counter(),
        "mcp_server_after": Counter(),
        "subagent_stops_by_type": Counter(),
    }


def process(span):
    c = _fresh_counters()
    _process_span(span, **c)
    return c


class TestEventCounting:
    def test_counts_event_type(self):
        c = process(make_span("UserPromptSubmit"))
        assert c["events_by_type"]["UserPromptSubmit"] == 1

    def test_counts_pre_tool_use(self):
        c = process(make_span("PreToolUse", tool="Read"))
        assert c["events_by_type"]["PreToolUse"] == 1

    def test_counts_failure(self):
        c = process(make_span("PostToolUseFailure", tool="Edit"))
        assert c["events_by_type"]["PostToolUseFailure"] == 1

    def test_no_event_attribute_not_counted(self):
        span = make_span("UserPromptSubmit")
        del span["attributes"]["gen_ai.client.hook.event"]
        span["name"] = "some.unknown.span"
        c = process(span)
        assert sum(c["events_by_type"].values()) == 0


class TestModelCounting:
    def test_counts_model(self):
        c = process(make_span("UserPromptSubmit", model=MODEL_CLAUDE))
        assert c["models"][MODEL_CLAUDE] == 1

    def test_missing_model_not_counted(self):
        span = make_span("UserPromptSubmit")
        del span["attributes"]["gen_ai.request.model"]
        c = process(span)
        assert sum(c["models"].values()) == 0


class TestToolCounting:
    def test_counts_tool(self):
        c = process(make_span("PreToolUse", tool="Read"))
        assert c["tools"]["Read"] == 1

    def test_missing_tool_not_counted(self):
        c = process(make_span("UserPromptSubmit"))
        assert sum(c["tools"].values()) == 0


class TestMcpTracking:
    def test_mcp_server_counter(self):
        c = process(make_span("BeforeMCPExecution", tool="get_issue", mcp_server=MCP_JIRA))
        assert c["mcp_servers"][MCP_JIRA] == 1

    def test_before_mcp_execution(self):
        c = process(make_span("BeforeMCPExecution", tool="get_issue", mcp_server=MCP_JIRA))
        assert c["mcp_server_before"][MCP_JIRA] == 1
        assert c["mcp_server_after"][MCP_JIRA] == 0

    def test_after_mcp_execution(self):
        c = process(make_span("AfterMCPExecution", tool="get_issue", mcp_server=MCP_JIRA))
        assert c["mcp_server_after"][MCP_JIRA] == 1
        assert c["mcp_server_before"][MCP_JIRA] == 0

    def test_mcp_server_short_name_unchanged(self):
        # Short server names are stored as-is
        c = process(make_span("BeforeMCPExecution", mcp_server=MCP_CORALOGIX))
        assert MCP_CORALOGIX in c["mcp_servers"]

    def test_mcp_server_path_shortened(self):
        # Path-style names longer than 60 chars have last segment extracted
        long_path = "/very/long/absolute/path/that/exceeds/sixty/characters/mcp-server-tool"
        assert len(long_path) > 60
        c = process(make_span("BeforeMCPExecution", mcp_server=long_path))
        key = list(c["mcp_servers"].keys())[0]
        assert "/" not in key
        assert key == "mcp-server-tool"

    def test_mcp_remote_url_is_normalized_without_secrets(self):
        raw = "npx mcp-remote https://api.coralogix.us/mgmt/api/v1/mcp --header Authorization:${CORALOGIX_API_KEY} --verbose"
        c = process(make_span("BeforeMCPExecution", mcp_server=raw))
        assert c["mcp_servers"]["mcp-coralogix-us"] == 1
        assert raw not in c["mcp_servers"]

    def test_docker_atlassian_command_collapses_to_stable_name(self):
        raw = (
            "docker run --rm -i -e JIRA_URL=https://ridewithvia.atlassian.net "
            "-e JIRA_API_TOKEN=secret ghcr.io/sooperset/mcp-atlassian:latest"
        )
        c = process(make_span("BeforeMCPExecution", mcp_server=raw))
        assert c["mcp_servers"]["mcp-atlassian"] == 1
        assert raw not in c["mcp_servers"]


class TestSubagentTracking:
    def test_subagent_start_counted(self):
        c = process(make_span("SubagentStart", subagent_type="explore"))
        assert c["subagent_types"]["explore"] == 1

    def test_subagent_stop_not_in_starts(self):
        c = process(make_span("SubagentStop", subagent_type="explore"))
        assert c["subagent_types"]["explore"] == 0

    def test_subagent_stop_tracked_separately(self):
        c = process(make_span("SubagentStop", subagent_type="explore"))
        assert c["subagent_stops_by_type"]["explore"] == 1


class TestSessionTracking:
    def test_session_id_added(self):
        c = process(make_span("UserPromptSubmit", session="sess-abc"))
        assert "sess-abc" in c["sessions_seen"]

    def test_session_events_incremented(self):
        c = process(make_span("PreToolUse", tool="Read", session="sess-abc"))
        assert c["session_events"]["sess-abc"] == 1

    def test_session_model_tracked(self):
        c = process(make_span("UserPromptSubmit", model=MODEL_CLAUDE, session="sess-abc"))
        assert c["session_models"]["sess-abc"][MODEL_CLAUDE] == 1


class TestTimestamps:
    def test_timestamp_collected(self):
        c = process(make_span("UserPromptSubmit", start_ns=DAY1 + 5*HOUR))
        assert DAY1 + 5*HOUR in c["timestamps_ns"]

    def test_activity_by_day(self):
        c = process(make_span("UserPromptSubmit", start_ns=DAY1 + 5*HOUR))
        assert c["activity_by_day"]["2026-03-24"] == 1

    def test_activity_by_hour(self):
        # DAY1 = 2026-03-24 00:00 UTC, add 9 hours → hour 9
        c = process(make_span("UserPromptSubmit", start_ns=DAY1 + 9*HOUR))
        assert c["activity_by_hour"][9] == 1

    def test_session_first_ts(self):
        c = process(make_span("UserPromptSubmit", session="s1", start_ns=DAY1 + 10*HOUR))
        assert c["session_first_ts"]["s1"] == DAY1 + 10*HOUR


class TestToolDurations:
    def test_duration_calculated(self):
        c = process(make_span("PreToolUse", tool="Read",
                               start_ns=DAY1, duration_ms=250.0))
        assert c["tool_durations_ms"]["Read"] == [250.0]

    def test_no_tool_no_duration(self):
        c = process(make_span("UserPromptSubmit", start_ns=DAY1, duration_ms=100))
        assert c["tool_durations_ms"] == {}

    def test_negative_duration_skipped(self):
        span = make_span("PreToolUse", tool="Edit", start_ns=DAY1, duration_ms=100)
        span["end_time_ns"] = span["start_time_ns"] - 1  # negative
        c = _fresh_counters()
        _process_span(span, **c)
        assert c["tool_durations_ms"].get("Edit") is None


class TestTokenUsage:
    def test_input_tokens(self):
        c = process(make_span("UserPromptSubmit", input_tokens=1000))
        assert c["token_totals"]["input"] == 1000

    def test_output_tokens(self):
        c = process(make_span("UserPromptSubmit", output_tokens=500))
        assert c["token_totals"]["output"] == 500

    def test_cache_create_tokens(self):
        c = process(make_span("UserPromptSubmit", cache_create_tokens=5000))
        assert c["token_totals"]["cache_creation"] == 5000

    def test_cache_read_tokens(self):
        c = process(make_span("UserPromptSubmit", cache_read_tokens=12000))
        assert c["token_totals"]["cache_read"] == 12000

    def test_session_tokens(self):
        c = process(make_span("UserPromptSubmit", session="s1",
                               input_tokens=200, output_tokens=100))
        assert c["session_tokens"]["s1"]["input"] == 200
        assert c["session_tokens"]["s1"]["output"] == 100


class TestShellCommands:
    def test_before_shell_execution(self):
        c = process(make_span("BeforeShellExecution", command="pytest tests/"))
        assert c["shell_commands"]["pytest tests/"] == 1

    def test_bash_tool_pre_tool_use(self):
        c = process(make_span("PreToolUse", tool="Bash", tool_input="git diff --stat"))
        assert c["shell_commands"]["git diff --stat"] == 1

    def test_long_command_truncated(self):
        long_cmd = "a" * 100
        c = process(make_span("BeforeShellExecution", command=long_cmd))
        key = list(c["shell_commands"].keys())[0]
        assert len(key) <= 63  # 60 chars + "..."
        assert key.endswith("...")


class TestAgentStats:
    def test_agent_created(self):
        c = process(make_span("UserPromptSubmit", agent=CLAUDE))
        assert CLAUDE in c["agents"]
        assert isinstance(c["agents"][CLAUDE], AgentStats)

    def test_agent_events_counted(self):
        c = process(make_span("PreToolUse", agent=COPILOT, tool="Grep"))
        assert c["agents"][COPILOT].total_events == 1

    def test_agent_model_tracked(self):
        c = process(make_span("UserPromptSubmit", agent=COPILOT, model=MODEL_COPILOT))
        assert c["agents"][COPILOT].models_by_count[MODEL_COPILOT] == 1

    def test_agent_tool_tracked(self):
        c = process(make_span("PreToolUse", agent=CLAUDE, tool="Edit"))
        assert c["agents"][CLAUDE].tools_by_count["Edit"] == 1

    def test_agent_mcp_tracked(self):
        c = process(make_span("BeforeMCPExecution", agent=CLAUDE, mcp_server=MCP_CORALOGIX))
        assert c["agents"][CLAUDE].mcp_servers[MCP_CORALOGIX] == 1

    def test_agent_tokens_tracked(self):
        c = process(make_span("UserPromptSubmit", agent=GEMINI,
                               input_tokens=500, output_tokens=200))
        assert c["agents"]["gemini-code-assist"].total_input_tokens == 500
        assert c["agents"]["gemini-code-assist"].total_output_tokens == 200


class TestEdgeCases:
    def test_empty_attributes(self):
        span = {
            "name": "gen_ai.client.hook.Stop",
            "traceId": "abc", "spanId": "def", "parentSpanId": "",
            "start_time_ns": DAY1, "end_time_ns": DAY1 + SEC,
            "attributes": {},
        }
        c = _fresh_counters()
        _process_span(span, **c)  # should not raise

    def test_none_attributes(self):
        span = {
            "name": "gen_ai.client.hook.Stop",
            "traceId": "abc", "spanId": "def", "parentSpanId": "",
            "start_time_ns": DAY1, "end_time_ns": DAY1 + SEC,
            "attributes": None,
        }
        c = _fresh_counters()
        _process_span(span, **c)  # should not raise
