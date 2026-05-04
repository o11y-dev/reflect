"""Tests for OTLP parsing functions."""
import json

import pytest
from conftest import DAY1, HOUR, make_span, wrap_otlp

from reflect.core import (
    _flatten_otlp_attributes,
    _iter_codex_log_spans,
    _load_json_lines,
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

        spans = list(_iter_codex_log_spans(p))

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

        spans = list(_iter_codex_log_spans(p))

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
