"""Tests for OTLP parsing functions."""
import json
import sqlite3

import pytest
from conftest import DAY1, HOUR, make_span, wrap_otlp

from reflect.core import _flatten_otlp_attributes, _load_json_lines, _load_otlp_traces
from reflect.parsing import _iter_hook_batch_spans, _iter_opencode_session_spans


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_opencode_db(tmp_path, sessions, messages, parts):
    """Create a minimal OpenCode-style SQLite DB for testing."""
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE session (id TEXT, title TEXT, directory TEXT,"
        " time_created INTEGER, time_updated INTEGER)"
    )
    cur.execute(
        "CREATE TABLE message (id TEXT, session_id TEXT,"
        " time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    cur.execute(
        "CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,"
        " time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    cur.executemany("INSERT INTO session VALUES (?,?,?,?,?)", sessions)
    cur.executemany("INSERT INTO message VALUES (?,?,?,?,?)", messages)
    cur.executemany("INSERT INTO part VALUES (?,?,?,?,?,?)", parts)
    conn.commit()
    conn.close()
    return db


def _spans_by_event(spans):
    """Return dict: event_name -> list[span]."""
    result = {}
    for sp in spans:
        ev = sp.get("attributes", {}).get("gen_ai.client.hook.event", "")
        result.setdefault(ev, []).append(sp)
    return result


# ---------------------------------------------------------------------------
# _iter_opencode_session_spans
# ---------------------------------------------------------------------------

class TestIterOpencodeSessionSpans:
    def test_empty_db_yields_nothing(self, tmp_path):
        db = _make_opencode_db(tmp_path, [], [], [])
        assert list(_iter_opencode_session_spans(db)) == []

    def test_single_session_start_end(self, tmp_path):
        sess_created_ms = DAY1 // 1_000_000
        sess_updated_ms = sess_created_ms + 60_000
        db = _make_opencode_db(
            tmp_path,
            [("sess-1", "Test", "/work", sess_created_ms, sess_updated_ms)],
            [], [],
        )
        spans = list(_iter_opencode_session_spans(db))
        by_ev = _spans_by_event(spans)
        assert "SessionStart" in by_ev
        assert "SessionEnd" in by_ev
        start = by_ev["SessionStart"][0]
        assert start["attributes"]["gen_ai.client.name"] == "opencode"
        assert start["attributes"]["session.id"] == "sess-1"
        assert start["attributes"]["code.workspace.root"] == "/work"
        assert start["start_time_ns"] == sess_created_ms * 1_000_000

    def test_user_message_emits_prompt_submit(self, tmp_path):
        ts_ms = DAY1 // 1_000_000
        db = _make_opencode_db(
            tmp_path,
            [("sess-1", "T", "/w", ts_ms, ts_ms + 1000)],
            [("msg-1", "sess-1", ts_ms + 100, ts_ms + 200,
              json.dumps({"role": "user", "time": {"created": ts_ms + 100}}))],
            [],
        )
        spans = list(_iter_opencode_session_spans(db))
        by_ev = _spans_by_event(spans)
        assert "UserPromptSubmit" in by_ev

    def test_assistant_message_emits_stop_with_tokens(self, tmp_path):
        ts_ms = DAY1 // 1_000_000
        db = _make_opencode_db(
            tmp_path,
            [("sess-1", "T", "/w", ts_ms, ts_ms + 5000)],
            [("msg-1", "sess-1", ts_ms + 500, ts_ms + 3000,
              json.dumps({
                  "role": "assistant",
                  "modelID": "claude-sonnet",
                  "tokens": {"input": 500, "output": 300, "cache": {"read": 4000, "write": 100}},
                  "time": {"created": ts_ms + 500, "completed": ts_ms + 3000},
              }))],
            [],
        )
        spans = list(_iter_opencode_session_spans(db))
        by_ev = _spans_by_event(spans)
        assert "Stop" in by_ev
        stop = by_ev["Stop"][0]
        attrs = stop["attributes"]
        assert attrs["gen_ai.request.model"] == "claude-sonnet"
        assert attrs["gen_ai.usage.input_tokens"] == 500
        assert attrs["gen_ai.usage.output_tokens"] == 300
        assert attrs["gen_ai.usage.cache_read.input_tokens"] == 4000
        assert attrs["gen_ai.usage.cache_creation.input_tokens"] == 100

    def test_tool_parts_emit_pre_and_post(self, tmp_path):
        ts_ms = DAY1 // 1_000_000
        db = _make_opencode_db(
            tmp_path,
            [("sess-1", "T", "/w", ts_ms, ts_ms + 5000)],
            [],
            [
                ("part-1", "msg-1", "sess-1", ts_ms + 100, ts_ms + 100,
                 json.dumps({"type": "tool", "tool": "bash", "callID": "c1",
                             "state": {"status": "running", "input": {"command": "ls"}}})),
                ("part-2", "msg-1", "sess-1", ts_ms + 500, ts_ms + 500,
                 json.dumps({"type": "tool", "tool": "bash", "callID": "c1",
                             "state": {"status": "completed", "metadata": {"exit": 0}}})),
            ],
        )
        spans = list(_iter_opencode_session_spans(db))
        by_ev = _spans_by_event(spans)
        assert "PreToolUse" in by_ev
        assert "PostToolUse" in by_ev
        pre = by_ev["PreToolUse"][0]
        assert pre["attributes"]["gen_ai.client.tool_name"] == "bash"

    def test_failed_tool_emits_post_tool_use_failure(self, tmp_path):
        ts_ms = DAY1 // 1_000_000
        db = _make_opencode_db(
            tmp_path,
            [("sess-1", "T", "/w", ts_ms, ts_ms + 5000)],
            [],
            [
                ("part-1", "msg-1", "sess-1", ts_ms + 100, ts_ms + 100,
                 json.dumps({"type": "tool", "tool": "bash", "callID": "c2",
                             "state": {"status": "running"}})),
                ("part-2", "msg-1", "sess-1", ts_ms + 200, ts_ms + 200,
                 json.dumps({"type": "tool", "tool": "bash", "callID": "c2",
                             "state": {"status": "error", "metadata": {"exit": 1}}})),
            ],
        )
        spans = list(_iter_opencode_session_spans(db))
        by_ev = _spans_by_event(spans)
        assert "PostToolUseFailure" in by_ev

    def test_nonexistent_db_yields_nothing(self, tmp_path):
        missing = tmp_path / "nonexistent.db"
        assert list(_iter_opencode_session_spans(missing)) == []

    def test_multiple_sessions(self, tmp_path):
        ts_ms = DAY1 // 1_000_000
        db = _make_opencode_db(
            tmp_path,
            [
                ("sess-A", "A", "/a", ts_ms, ts_ms + 1000),
                ("sess-B", "B", "/b", ts_ms + 2000, ts_ms + 3000),
            ],
            [], [],
        )
        spans = list(_iter_opencode_session_spans(db))
        session_ids = {sp["attributes"]["session.id"] for sp in spans}
        assert "sess-A" in session_ids
        assert "sess-B" in session_ids

    def test_all_spans_have_required_attributes(self, tmp_path):
        ts_ms = DAY1 // 1_000_000
        db = _make_opencode_db(
            tmp_path,
            [("sess-1", "T", "/w", ts_ms, ts_ms + 1000)],
            [("m1", "sess-1", ts_ms + 10, ts_ms + 10,
              json.dumps({"role": "user", "time": {"created": ts_ms + 10}}))],
            [],
        )
        for sp in _iter_opencode_session_spans(db):
            attrs = sp["attributes"]
            assert attrs.get("gen_ai.client.name") == "opencode"
            assert attrs.get("session.id") == "sess-1"
            assert "gen_ai.client.hook.event" in attrs
            assert sp.get("traceId")
            assert sp.get("spanId")


# ---------------------------------------------------------------------------
# _iter_hook_batch_spans
# ---------------------------------------------------------------------------

class TestIterHookBatchSpans:
    def _write_batch(self, tmp_path, records, name="abc123_session.jsonl"):
        p = tmp_path / name
        p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        return p

    def test_empty_file_yields_nothing(self, tmp_path):
        p = tmp_path / "empty_session.jsonl"
        p.write_text("")
        assert list(_iter_hook_batch_spans(p)) == []

    def test_session_start_event(self, tmp_path):
        records = [
            {"event": "SessionStart", "timestamp_ns": DAY1,
             "data": {"session_id": "s1", "source_app": "OpenCode", "cwd": "/work"}},
        ]
        spans = list(_iter_hook_batch_spans(self._write_batch(tmp_path, records)))
        by_ev = _spans_by_event(spans)
        assert "SessionStart" in by_ev
        start = by_ev["SessionStart"][0]
        assert start["attributes"]["gen_ai.client.name"] == "opencode"
        assert start["attributes"]["session.id"] == "s1"
        assert start["attributes"]["code.workspace.root"] == "/work"

    def test_pre_post_tool_use(self, tmp_path):
        records = [
            {"event": "PreToolUse", "timestamp_ns": DAY1 + HOUR,
             "data": {"session_id": "s1", "tool_name": "bash", "tool_id": "t1"}},
            {"event": "PostToolUse", "timestamp_ns": DAY1 + HOUR + 2_000_000_000,
             "data": {"session_id": "s1", "tool_name": "bash", "tool_id": "t1"}},
        ]
        spans = list(_iter_hook_batch_spans(self._write_batch(tmp_path, records)))
        by_ev = _spans_by_event(spans)
        assert "PreToolUse" in by_ev
        assert "PostToolUse" in by_ev
        post = by_ev["PostToolUse"][0]
        # start_ns should be PreToolUse timestamp (duration tracking)
        assert post["start_time_ns"] == DAY1 + HOUR

    def test_post_tool_use_failure(self, tmp_path):
        records = [
            {"event": "PostToolUseFailure", "timestamp_ns": DAY1,
             "data": {"session_id": "s1", "tool_name": "bash"}},
        ]
        spans = list(_iter_hook_batch_spans(self._write_batch(tmp_path, records)))
        by_ev = _spans_by_event(spans)
        assert "PostToolUseFailure" in by_ev

    def test_session_id_from_filename_stem(self, tmp_path):
        records = [
            {"event": "Stop", "timestamp_ns": DAY1,
             "data": {}},  # no session_id in data
        ]
        p = self._write_batch(tmp_path, records, "myid-abc_session.jsonl")
        spans = list(_iter_hook_batch_spans(p))
        for sp in spans:
            sid = sp["attributes"].get("session.id") or sp["attributes"].get("gen_ai.client.session_id")
            assert sid == "myid-abc"

    def test_synthetic_session_end_added_when_missing(self, tmp_path):
        records = [
            {"event": "SessionStart", "timestamp_ns": DAY1,
             "data": {"session_id": "s1"}},
            {"event": "UserPromptSubmit", "timestamp_ns": DAY1 + HOUR,
             "data": {"session_id": "s1"}},
        ]
        spans = list(_iter_hook_batch_spans(self._write_batch(tmp_path, records)))
        by_ev = _spans_by_event(spans)
        assert "SessionEnd" in by_ev

    def test_no_synthetic_session_end_when_present(self, tmp_path):
        records = [
            {"event": "SessionStart", "timestamp_ns": DAY1,
             "data": {"session_id": "s1"}},
            {"event": "SessionEnd", "timestamp_ns": DAY1 + HOUR,
             "data": {"session_id": "s1"}},
        ]
        spans = list(_iter_hook_batch_spans(self._write_batch(tmp_path, records)))
        by_ev = _spans_by_event(spans)
        assert len(by_ev.get("SessionEnd", [])) == 1

    def test_model_attribute_forwarded(self, tmp_path):
        records = [
            {"event": "Stop", "timestamp_ns": DAY1,
             "data": {"session_id": "s1", "model": "claude-sonnet"}},
        ]
        spans = list(_iter_hook_batch_spans(self._write_batch(tmp_path, records)))
        assert any(
            sp["attributes"].get("gen_ai.request.model") == "claude-sonnet"
            for sp in spans
        )

    def test_cursor_source_app_sets_agent_name(self, tmp_path):
        records = [
            {"event": "SessionStart", "timestamp_ns": DAY1,
             "data": {"session_id": "s1", "source_app": "Cursor"}},
        ]
        spans = list(_iter_hook_batch_spans(self._write_batch(tmp_path, records)))
        by_ev = _spans_by_event(spans)
        assert by_ev["SessionStart"][0]["attributes"]["gen_ai.client.name"] == "cursor"

    def test_all_spans_have_trace_and_span_ids(self, tmp_path):
        records = [
            {"event": "UserPromptSubmit", "timestamp_ns": DAY1, "data": {"session_id": "s1"}},
            {"event": "PreToolUse", "timestamp_ns": DAY1 + 1, "data": {"session_id": "s1", "tool_name": "Read"}},
        ]
        for sp in _iter_hook_batch_spans(self._write_batch(tmp_path, records)):
            assert sp.get("traceId")
            assert sp.get("spanId")


# ---------------------------------------------------------------------------
# Original tests (unchanged)
# ---------------------------------------------------------------------------

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

