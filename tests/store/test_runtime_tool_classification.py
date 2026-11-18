import json

import pytest

from reflect.store.ingest import ingest_local_spans_file
from reflect.store.migrate import migrate
from reflect.store.normalize import _step_type, normalize_pending_raw_events
from reflect.store.sqlite import connect_sqlite


@pytest.mark.parametrize("event", ["codex.tool_blocking", "runtime.tool_queue_wait"])
def test_runtime_timing_is_not_a_tool_invocation(event):
    assert _step_type(event, {"busy_ns": 2000, "idle_ns": 3000}) == "unknown"


@pytest.mark.parametrize("event,attrs", [
    ("PreToolUse", {}),
    ("gen_ai.client.hook.postToolUse", {}),
    ("PostToolUseFailure", {}),
    ("tool.execution_start", {"event.name": "tool.execution_start"}),
    ("tool.execution_complete", {"event.name": "tool.execution_complete"}),
    ("operation", {"gen_ai.operation.name": "execute_tool"}),
    ("code_mode.broker.invoke_tool", {"tool_name": "exec_command"}),
    ("provider.action", {"gen_ai.tool.name": "Read"}),
])
def test_recorded_invocation_semantics_survive_without_provider_specific_span_names(event, attrs):
    assert _step_type(event, attrs) == "tool_call"


def test_runtime_timing_remains_retained_without_inflating_canonical_tool_counts(tmp_path):
    path = tmp_path / "runtime.jsonl"
    events = [
        ("codex.tool_blocking", {"turn.id": "turn-1", "busy_ns": 2000, "idle_ns": 3000,
                                 "code.module.name": "codex_core::session::turn"}),
        ("code_mode.broker.invoke_tool", {"tool_name": "exec_command", "cell.id": "1",
                                          "runtime_tool_call_id": "tool-1"}),
        # Runtime IDs are cell-local and can repeat after a process restart.
        ("code_mode.broker.invoke_tool", {"tool_name": "exec_command", "cell.id": "1",
                                          "runtime_tool_call_id": "tool-1"}),
        ("PostToolUse", {"gen_ai.client.tool_name": "Read", "gen_ai.tool.call.id": "read-1"}),
    ]
    path.write_text("\n".join(json.dumps({
        "name": event, "traceId": "trace", "spanId": f"span-{index}",
        "start_time_ns": 1784728800000000000 + index * 1000000,
        "end_time_ns": 1784728800000000000 + index * 1000000 + 1000,
        "attributes": {"service.name": "codex-app-server", "conversation.id": "session", **attrs},
    }) for index, (event, attrs) in enumerate(events)))
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        ingest_local_spans_file(conn, file_path=path)
        assert normalize_pending_raw_events(conn)["failed"] == 0
        assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 4
        assert conn.execute("SELECT type FROM steps WHERE summary='codex.tool_blocking'").fetchone()[0] == "runtime_span"
        assert sorted(tuple(row) for row in conn.execute(
            "SELECT tool_name,COUNT(*) FROM tool_calls GROUP BY tool_name"
        )) == [("Read", 1), ("exec_command", 2)]
        assert normalize_pending_raw_events(conn)["processed"] == 0
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 3
    finally:
        conn.close()
