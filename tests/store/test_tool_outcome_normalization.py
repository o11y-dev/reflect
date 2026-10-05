import json

import pytest

from reflect.conversation_adapters import CodexConversationAdapter
from reflect.processing import analyze_telemetry
from reflect.store.ingest import ingest_local_spans_file, ingest_native_session_file
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.sqlite import connect_sqlite


def _spans(agent, output, result_event):
    return [{
        "name": event, "traceId": "trace", "spanId": str(i),
        "start_time_ns": 1791183100000000000 + i * 1000000,
        "end_time_ns": 1791183100000000000 + (i + 1) * 1000000,
        "attributes": {
            "gen_ai.client.name": agent, "gen_ai.client.session_id": "session",
            "gen_ai.client.hook.event": event, "gen_ai.client.tool_name": "execute",
            "gen_ai.tool.call.id": "call-1",
            **({"gen_ai.client.tool.output": json.dumps(output)} if i else {}),
        },
    } for i, event in enumerate(("PreToolUse", result_event))]


@pytest.mark.parametrize("agent,result_event", [
    ("claude", "PostToolUse"), ("codex", "PostToolUse"),
    ("cursor", "AfterShellExecution"), ("copilot", "PostToolUse"),
    ("opencode", "PostToolUse"), ("future-provider", "AfterMCPExecution"),
])
@pytest.mark.parametrize("output", [
    {"isError": True, "content": [{"type": "text", "text": "Unavailable"}]},
    {"exit_code": 1},
])
def test_failure_flows_through_canonical_and_stats_paths(tmp_path, agent, result_event, output):
    path = tmp_path / "spans.jsonl"
    path.write_text("\n".join(json.dumps(s) for s in _spans(agent, output, result_event)))
    conn = connect_sqlite(tmp_path / "test.db")
    try:
        migrate(conn)
        ingest_local_spans_file(conn, file_path=path)
        assert normalize_pending_raw_events(conn)["failed"] == 0
        assert tuple(conn.execute("SELECT status, failure_count FROM sessions").fetchone()) == ("error", 1)
        row = conn.execute("SELECT status, error_type, error_message_redacted FROM tool_calls").fetchone()
        assert row[0] == "error"
        assert row[1] and row[2]
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1
        assert normalize_pending_raw_events(conn)["processed"] == 0
    finally:
        conn.close()
    stats = analyze_telemetry(tmp_path / "missing-sessions", tmp_path)
    if result_event == "PostToolUse":
        assert stats.events_by_type["PostToolUseFailure"] == 1
    assert stats.session_span_details["session"][-1]["ok"] is False
    assert stats.session_conversation["session"][-1]["success"] is False


def test_native_execution_failure_envelope_reaches_canonical_ledger(tmp_path):
    path = tmp_path / "rollout.jsonl"
    output = [
        {"type": "input_text", "text": "Script failed\nWall time 0.0 seconds\nOutput:\n"},
        {"type": "input_text", "text": "Script error:\nCreateProcess: Too many open files (os error 24)"},
    ]
    records = [
        {"type": "session_meta", "payload": {"id": "native"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c", "input": "read()"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c", "output": output}},
    ]
    for i, record in enumerate(records):
        record["timestamp"] = f"2026-10-05T06:51:0{i}Z"
    path.write_text("\n".join(json.dumps(r) for r in records))
    transcript = CodexConversationAdapter().load("native", path)
    assert transcript.events[-1].success is False
    conn = connect_sqlite(tmp_path / "native.db")
    try:
        migrate(conn)
        ingest_native_session_file(conn, file_path=path, agent="codex")
        normalize_pending_raw_events(conn)
        row = conn.execute("SELECT tool_name, status, error_type, error_message_redacted FROM tool_calls").fetchone()
        assert tuple(row[:3]) == ("exec", "error", "execution_error")
        assert "Too many open files" in row[3]
        assert conn.execute("SELECT failure_count FROM sessions").fetchone()[0] == 1
    finally:
        conn.close()
