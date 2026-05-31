"""Tests for OTLP parsing functions."""
import json
from pathlib import Path

import pytest
from conftest import DAY1, HOUR, make_span, wrap_otlp

from reflect.core import (
    _flatten_otlp_attributes,
    _iter_claude_log_spans,
    _iter_codex_log_spans,
    _iter_codex_session_spans,
    _iter_cursor_session_spans,
    _load_json_lines,
    _load_otlp_logs,
    _load_otlp_traces,
)


class TestFlattenOtlpAttributes:
    def test_string_value(self):
        attrs = [{"key": "foo", "value": {"stringValue": "bar"}}]
        assert _flatten_otlp_attributes(attrs) == {"foo": "bar"}

    def test_int_value(self):
        attrs = [{"key": "n", "value": {"intValue": "42"}}]
        result = _flatten_otlp_attributes(attrs)
        assert result["n"] == 42
        assert isinstance(result["n"], int)

    def test_double_value(self):
        attrs = [{"key": "d", "value": {"doubleValue": 3.14}}]
        result = _flatten_otlp_attributes(attrs)
        assert result["d"] == pytest.approx(3.14)

    def test_bool_value(self):
        attrs = [{"key": "flag", "value": {"boolValue": True}}]
        assert _flatten_otlp_attributes(attrs) == {"flag": True}

    def test_array_value(self):
        arr = {"values": [{"stringValue": "a"}, {"stringValue": "b"}]}
        attrs = [{"key": "tags", "value": {"arrayValue": arr}}]
        result = _flatten_otlp_attributes(attrs)
        assert result["tags"] == arr

    def test_empty_list(self):
        assert _flatten_otlp_attributes([]) == {}

    def test_multiple_attrs(self):
        attrs = [
            {"key": "a", "value": {"stringValue": "x"}},
            {"key": "b", "value": {"intValue": "7"}},
        ]
        result = _flatten_otlp_attributes(attrs)
        assert result == {"a": "x", "b": 7}

    def test_missing_key_skipped(self):
        attrs = [{"value": {"stringValue": "orphan"}}]
        result = _flatten_otlp_attributes(attrs)
        assert "" in result or result == {}  # key defaults to ""

    def test_unknown_value_type_skipped(self):
        attrs = [{"key": "x", "value": {"unknownType": "foo"}}]
        result = _flatten_otlp_attributes(attrs)
        assert "x" not in result


class TestLoadOtlpTraces:
    def test_single_span(self, tmp_path):
        spans = [make_span("UserPromptSubmit")]
        p = tmp_path / "traces.json"
        p.write_text(wrap_otlp(spans) + "\n")
        result = list(_load_otlp_traces(p))
        assert len(result) == 1
        assert result[0]["attributes"]["gen_ai.client.hook.event"] == "UserPromptSubmit"

    def test_multiple_spans(self, tmp_path):
        spans = [
            make_span("UserPromptSubmit"),
            make_span("PreToolUse", tool="Read"),
            make_span("Stop"),
        ]
        p = tmp_path / "traces.json"
        p.write_text(wrap_otlp(spans) + "\n")
        result = list(_load_otlp_traces(p))
        assert len(result) == 3

    def test_multiple_lines(self, tmp_path):
        line1 = wrap_otlp([make_span("UserPromptSubmit", agent="claude")])
        line2 = wrap_otlp([make_span("PreToolUse", agent="copilot", tool="Grep")],
                          agent="copilot")
        p = tmp_path / "traces.json"
        p.write_text(line1 + "\n" + line2 + "\n")
        result = list(_load_otlp_traces(p))
        assert len(result) == 2

    def test_blank_lines_skipped(self, tmp_path):
        spans = [make_span("Stop")]
        p = tmp_path / "traces.json"
        p.write_text("\n\n" + wrap_otlp(spans) + "\n\n")
        result = list(_load_otlp_traces(p))
        assert len(result) == 1

    def test_malformed_json_skipped(self, tmp_path):
        good = wrap_otlp([make_span("Stop")])
        p = tmp_path / "traces.json"
        p.write_text("{{not valid json}}\n" + good + "\n")
        result = list(_load_otlp_traces(p))
        assert len(result) == 1

    def test_resource_attrs_merged(self, tmp_path):
        spans = [make_span("UserPromptSubmit")]
        p = tmp_path / "traces.json"
        p.write_text(wrap_otlp(spans, agent="claude", service="ide-agent") + "\n")
        result = list(_load_otlp_traces(p))
        assert result[0]["attributes"].get("service.name") == "ide-agent"

    def test_low_level_codex_runtime_spans_skipped(self, tmp_path):
        payload = {
            "resourceSpans": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "codex_cli_rs"}},
                    ],
                },
                "scopeSpans": [{
                    "spans": [{
                        "traceId": "a" * 32,
                        "spanId": "b" * 16,
                        "name": "FramedRead::poll_next",
                        "startTimeUnixNano": str(DAY1),
                        "endTimeUnixNano": str(DAY1 + 1),
                        "attributes": [
                            {"key": "code.module.name", "value": {"stringValue": "h2::codec"}},
                        ],
                    }],
                }],
            }],
        }
        p = tmp_path / "traces.json"
        p.write_text(json.dumps(payload) + "\n")
        assert list(_load_otlp_traces(p)) == []

    def test_start_end_time_ns(self, tmp_path):
        span = make_span("UserPromptSubmit", start_ns=DAY1 + 5*HOUR, duration_ms=100)
        p = tmp_path / "traces.json"
        p.write_text(wrap_otlp([span]) + "\n")
        result = list(_load_otlp_traces(p))
        assert result[0]["start_time_ns"] == DAY1 + 5*HOUR
        assert result[0]["end_time_ns"] == DAY1 + 5*HOUR + 100_000_000

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.json"
        p.write_text("")
        assert list(_load_otlp_traces(p)) == []


class TestCodexOtlpLogs:
    def _write_logs(self, tmp_path, records):
        p = tmp_path / "otel-logs.json"
        p.write_text(json.dumps({
            "resourceLogs": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "codex_cli_rs"}},
                    ],
                },
                "scopeLogs": [{
                    "logRecords": records,
                }],
            }],
        }) + "\n")
        return p

    def _record(self, attrs):
        return {
            "timeUnixNano": str(DAY1),
            "attributes": [
                {"key": key, "value": {"boolValue": value}}
                if isinstance(value, bool)
                else {"key": key, "value": {"stringValue": str(value)}}
                for key, value in attrs.items()
            ],
        }

    def test_codex_log_events_normalize_to_agent_spans(self, tmp_path):
        p = self._write_logs(tmp_path, [
            self._record({
                "event.name": "codex.conversation_starts",
                "event.timestamp": "2026-03-24T10:00:00Z",
                "conversation.id": "codex-session-1",
                "model": "gpt-5.5",
                "slug": "gpt-5.5",
            }),
            self._record({
                "event.name": "codex.user_prompt",
                "event.timestamp": "2026-03-24T10:00:01Z",
                "conversation.id": "codex-session-1",
                "model": "gpt-5.5",
                "prompt": "[REDACTED]",
            }),
        ])

        spans = list(_iter_codex_log_spans(_load_otlp_logs(p)))

        assert [s["attributes"]["gen_ai.client.hook.event"] for s in spans] == [
            "SessionStart",
            "UserPromptSubmit",
        ]
        assert {s["attributes"]["gen_ai.client.name"] for s in spans} == {"codex"}
        assert spans[0]["attributes"]["gen_ai.client.session_id"] == "codex-session-1"
        assert spans[0]["attributes"]["gen_ai.request.model"] == "gpt-5.5"

    def test_codex_log_tool_and_token_events_normalize(self, tmp_path):
        p = self._write_logs(tmp_path, [
            self._record({
                "event.name": "codex.tool_decision",
                "event.timestamp": "2026-03-24T10:00:02Z",
                "conversation.id": "codex-session-1",
                "model": "gpt-5.5",
                "tool_name": "exec_command",
                "call_id": "call-1",
                "decision": "approved",
                "source": "Config",
            }),
            self._record({
                "event.name": "codex.tool_result",
                "event.timestamp": "2026-03-24T10:00:03Z",
                "conversation.id": "codex-session-1",
                "model": "gpt-5.5",
                "tool_name": "exec_command",
                "call_id": "call-1",
                "duration_ms": "200",
                "success": "true",
                "arguments": "{\"cmd\":\"git status\"}",
            }),
            self._record({
                "event.name": "codex.sse_event",
                "event.kind": "response.completed",
                "event.timestamp": "2026-03-24T10:00:04Z",
                "conversation.id": "codex-session-1",
                "model": "gpt-5.5",
                "input_token_count": "1000",
                "cached_token_count": "250",
                "output_token_count": "80",
            }),
        ])

        spans = list(_iter_codex_log_spans(_load_otlp_logs(p)))

        assert [s["attributes"]["gen_ai.client.hook.event"] for s in spans] == [
            "PreToolUse",
            "PostToolUse",
            "Stop",
        ]
        assert spans[0]["attributes"]["gen_ai.client.tool_name"] == "exec_command"
        assert spans[1]["end_time_ns"] - spans[1]["start_time_ns"] == 200_000_000
        assert spans[2]["attributes"]["gen_ai.usage.input_tokens"] == 750
        assert spans[2]["attributes"]["gen_ai.usage.cache_read.input_tokens"] == 250
        assert spans[2]["attributes"]["gen_ai.usage.output_tokens"] == 80


class TestClaudeOtlpLogs:
    def test_claude_api_request_normalizes_model_tokens_and_cost(self, tmp_path):
        p = tmp_path / "otel-logs.json"
        p.write_text(json.dumps({
            "resourceLogs": [{
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "claude-code"}}]},
                "scopeLogs": [{
                    "logRecords": [{
                        "timeUnixNano": "3000",
                        "attributes": [
                            {"key": "event.name", "value": {"stringValue": "claude_code.api_request"}},
                            {"key": "event.timestamp", "value": {"stringValue": "2026-05-28T14:47:57Z"}},
                            {"key": "session.id", "value": {"stringValue": "claude-sess-1"}},
                            {"key": "model", "value": {"stringValue": "claude-opus-4-6"}},
                            {"key": "input_tokens", "value": {"stringValue": "9"}},
                            {"key": "output_tokens", "value": {"stringValue": "7238"}},
                            {"key": "cache_read_tokens", "value": {"stringValue": "0"}},
                            {"key": "cache_creation_tokens", "value": {"stringValue": "41530"}},
                            {"key": "cost_usd", "value": {"stringValue": "0.4405575"}},
                            {"key": "duration_ms", "value": {"stringValue": "135933"}},
                        ],
                    }]
                }],
            }]
        }) + "\n")

        spans = list(_iter_claude_log_spans(_load_otlp_logs(p)))

        assert len(spans) == 1
        attrs = spans[0]["attributes"]
        assert attrs["gen_ai.client.name"] == "claude"
        assert attrs["gen_ai.request.model"] == "claude-opus-4-6"
        assert attrs["gen_ai.usage.input_tokens"] == 9
        assert attrs["gen_ai.usage.output_tokens"] == 7238
        assert attrs["gen_ai.usage.cache_creation.input_tokens"] == 41530
        assert attrs["gen_ai.usage.cost_usd"] == "0.4405575"


class TestCursorNativeSessions:
    def test_cursor_tool_use_content_blocks_normalize_to_tool_spans(self, tmp_path):
        session = tmp_path / "cursor-session-1.jsonl"
        session.write_text(
            "\n".join([
                json.dumps({
                    "role": "user",
                    "message": {"content": [{"type": "text", "text": "inspect the repo"}]},
                }),
                json.dumps({
                    "role": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "I will inspect it."},
                            {
                                "type": "tool_use",
                                "name": "Shell",
                                "input": {"command": "git status --short", "description": "check status"},
                            },
                            {
                                "type": "tool_use",
                                "name": "CallMcpTool",
                                "input": {"server": "jira", "toolName": "search", "arguments": {"jql": "project = O11Y"}},
                            },
                        ],
                    },
                }),
            ])
            + "\n",
            encoding="utf-8",
        )

        spans = list(_iter_cursor_session_spans(session))
        tool_spans = [span for span in spans if span["attributes"].get("gen_ai.client.hook.event") == "PreToolUse"]

        assert [span["attributes"]["gen_ai.client.tool_name"] for span in tool_spans] == ["Shell", "CallMcpTool"]
        assert "git status --short" in tool_spans[0]["attributes"]["gen_ai.client.tool.input"]
        assert tool_spans[1]["attributes"]["gen_ai.client.mcp_server"] == "jira"
        assert tool_spans[1]["attributes"]["gen_ai.client.mcp_tool"] == "search"


class TestCodexSessionFiles:
    def test_codex_session_events_normalize_to_agent_spans(self, tmp_path):
        p = tmp_path / "rollout-2026-05-08T03-40-01-019e0506-efd5-7030-b2d2-6c41433270fb.jsonl"
        records = [
            {
                "timestamp": "2026-05-08T00:42:07.990Z",
                "type": "session_meta",
                "payload": {
                    "id": "019e0506-efd5-7030-b2d2-6c41433270fb",
                    "cwd": "/work/repo",
                    "model": "gpt-5.5",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-05-08T00:42:07.992Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "please inspect the tests"}],
                },
            },
            {
                "timestamp": "2026-05-08T00:42:16.256Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call-1",
                    "arguments": "{\"cmd\":\"pytest\"}",
                },
            },
            {
                "timestamp": "2026-05-08T00:42:18.256Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "passed",
                },
            },
            {
                "timestamp": "2026-05-08T00:42:20.256Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Tests pass."}],
                },
            },
        ]
        p.write_text("\n".join(json.dumps(record) for record in records) + "\n")

        spans = list(_iter_codex_session_spans(p))

        assert [s["attributes"]["gen_ai.client.hook.event"] for s in spans] == [
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "Stop",
            "SessionStart",
            "SessionEnd",
        ]
        assert {s["attributes"]["gen_ai.client.name"] for s in spans} == {"codex"}
        assert spans[0]["attributes"]["gen_ai.client.session_id"] == "019e0506-efd5-7030-b2d2-6c41433270fb"
        assert spans[0]["attributes"]["gen_ai.client.prompt"] == "please inspect the tests"
        assert spans[1]["attributes"]["gen_ai.client.tool_name"] == "exec_command"
        assert spans[2]["attributes"]["gen_ai.client.tool_use_id"] == "call-1"
        assert spans[3]["attributes"]["gen_ai.client.output"] == "Tests pass."
        assert spans[4]["attributes"]["gen_ai.request.model"] == "gpt-5.5"

    def test_codex_session_skips_environment_context_prompts(self, tmp_path):
        p = tmp_path / "rollout-session.jsonl"
        records = [
            {
                "timestamp": "2026-05-08T00:42:07.990Z",
                "type": "session_meta",
                "payload": {"id": "codex-session"},
            },
            {
                "timestamp": "2026-05-08T00:42:07.992Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "<environment_context>\nignored\n</environment_context>"}],
                },
            },
        ]
        p.write_text("\n".join(json.dumps(record) for record in records) + "\n")

        spans = list(_iter_codex_session_spans(p))

        assert [s["attributes"]["gen_ai.client.hook.event"] for s in spans] == [
            "SessionStart",
            "SessionEnd",
        ]

    def test_codex_session_uses_config_model_when_meta_omits_model(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: home)
        p = tmp_path / "rollout-codex-session.jsonl"
        p.write_text(
            "\n".join([
                json.dumps({
                    "timestamp": "2026-05-08T00:42:07.990Z",
                    "type": "session_meta",
                    "payload": {"id": "codex-session", "model_provider": "openai"},
                }),
                json.dumps({
                    "timestamp": "2026-05-08T00:42:08.990Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 1000,
                                "cached_input_tokens": 250,
                                "output_tokens": 80,
                            }
                        },
                    },
                }),
            ])
            + "\n"
        )

        spans = list(_iter_codex_session_spans(p))

        token_span = next(span for span in spans if span["attributes"].get("gen_ai.usage.output_tokens") == 80)
        assert token_span["attributes"]["gen_ai.request.model"] == "gpt-5.5"
        assert token_span["attributes"]["gen_ai.request.model_source"] == "codex_config_default"


class TestLoadJsonLines:
    def test_basic(self, tmp_path):
        p = tmp_path / "spans.jsonl"
        p.write_text(
            json.dumps({"event": "PreToolUse", "tool": "Read"}) + "\n" +
            json.dumps({"event": "Stop"}) + "\n"
        )
        result = list(_load_json_lines(p))
        assert len(result) == 2
        assert result[0]["event"] == "PreToolUse"

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        assert list(_load_json_lines(p)) == []

    def test_blank_lines_skipped(self, tmp_path):
        p = tmp_path / "spans.jsonl"
        p.write_text(
            "\n" +
            json.dumps({"event": "Stop"}) + "\n" +
            "\n"
        )
        result = list(_load_json_lines(p))
        assert len(result) == 1

    def test_non_dict_json_skipped(self, tmp_path):
        p = tmp_path / "spans.jsonl"
        p.write_text(
            json.dumps(["array", "not", "dict"]) + "\n" +
            json.dumps({"event": "Stop"}) + "\n"
        )
        result = list(_load_json_lines(p))
        assert len(result) == 1
        assert result[0]["event"] == "Stop"

    def test_malformed_json_skipped(self, tmp_path):
        p = tmp_path / "spans.jsonl"
        p.write_text(
            "{bad json\n" +
            json.dumps({"event": "Stop"}) + "\n"
        )
        result = list(_load_json_lines(p))
        assert len(result) == 1
