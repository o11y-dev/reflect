import hashlib
import json
from unittest.mock import patch

from reflect.store.ingest import _prepare_raw_span
from reflect.store.migrate import load_migrations, migrate
from reflect.store.normalize import _logical_tool_call_id, _upsert_tool_call
from reflect.store.sqlite import connect_sqlite


def _codex_dispatch(**overrides):
    attrs = {
        "service.name": "codex-app-server",
        "reflect.telemetry.origin": "native_otlp_trace",
        "code.module.name": "codex_core::tools::parallel",
        "codex.tool.phase": "dispatch",
        "conversation.id": "session-1",
        "turn.id": "turn-1",
        "call_id": "call-1",
        "tool_name": "exec_command",
    }
    attrs.update(overrides)
    return attrs


def test_otlp_span_identity_is_independent_of_segment_path():
    span = {
        "name": "unified_exec.exec_command",
        "traceId": "trace-1",
        "spanId": "span-1",
        "parentSpanId": "parent-1",
        "start_time_ns": 100,
        "end_time_ns": 200,
        "attributes": {"service.name": "codex-app-server"},
    }
    left = _prepare_raw_span(span, "/segments/active.jsonl", "otlp_traces_json")
    right = _prepare_raw_span(span, "/segments/rotated.jsonl", "otlp_traces_json")

    assert left.content_hash == right.content_hash
    assert left.event_id == right.event_id


def test_codex_dispatch_call_id_is_reused_only_with_complete_codex_identity():
    assert _logical_tool_call_id(_codex_dispatch(), "raw-event-1") == "call-1"

    assert _logical_tool_call_id(
        _codex_dispatch(**{"service.name": "other-agent"}), "raw-event-1"
    ) == "raw-event-1"
    assert _logical_tool_call_id(
        _codex_dispatch(**{"codex.tool.phase": "complete"}), "raw-event-1"
    ) == "raw-event-1"
    assert _logical_tool_call_id(
        _codex_dispatch(**{"turn.id": None}), "raw-event-1"
    ) == "raw-event-1"


def test_codex_dispatch_enriches_existing_call_without_overwriting_result_facts(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        conn.execute("""INSERT INTO sessions(id,started_at,created_at,updated_at)
            VALUES ('session-1','2026-11-09','2026-11-09','2026-11-09')""")
        conn.execute("""INSERT INTO steps(id,session_id,seq,type,started_at,created_at,updated_at)
            VALUES ('step-1','session-1',0,'tool_call','2026-11-09','2026-11-09','2026-11-09')""")
        conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,logical_call_id,tool_name,
            status,duration_ms,tool_type,raw_attrs_json,created_at,updated_at)
            VALUES ('tool-1','step-1','session-1','call-1','mcp__server__run','ok',150,'mcp',
            '{"gen_ai.client.tool_name":"mcp__server__run","turn.id":"turn-1"}',
            '2026-11-09','2026-11-09')""")
        attrs = _codex_dispatch()
        _upsert_tool_call(
            conn,
            raw_event={"id": "raw-1", "event_type": "unified_exec.exec_command"},
            attrs=attrs,
            session_id="session-1",
            step_id="step-2",
            timestamp="2026-11-09T00:00:01Z",
            status="unknown",
            duration_ms=200,
        )
        row = conn.execute(
            "SELECT tool_name,status,duration_ms FROM tool_calls WHERE logical_call_id='call-1'"
        ).fetchone()
        assert tuple(row) == ("mcp__server__run", "ok", 150)
    finally:
        conn.close()


def test_codex_dispatch_does_not_merge_a_reused_call_id_across_turns(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        conn.execute("""INSERT INTO sessions(id,started_at,created_at,updated_at)
            VALUES ('session-1','2026-11-09','2026-11-09','2026-11-09')""")
        for seq, step_id in enumerate(("step-1", "step-2", "step-3")):
            conn.execute("""INSERT INTO steps(id,session_id,seq,type,started_at,created_at,updated_at)
                VALUES (?, 'session-1',?,'tool_call','2026-11-09','2026-11-09','2026-11-09')""", (step_id, seq))
        attrs_a = _codex_dispatch()
        attrs_b = _codex_dispatch(**{"turn.id": "turn-2"})
        attrs_c = _codex_dispatch(**{"tool_name": "apply_patch"})
        _upsert_tool_call(
            conn,
            raw_event={"id": "raw-a", "event_type": "unified_exec.exec_command"},
            attrs=attrs_a,
            session_id="session-1",
            step_id="step-1",
            timestamp="2026-11-09T00:00:01Z",
            status="unknown",
            duration_ms=10,
        )
        _upsert_tool_call(
            conn,
            raw_event={"id": "raw-b", "event_type": "unified_exec.exec_command"},
            attrs=attrs_b,
            session_id="session-1",
            step_id="step-2",
            timestamp="2026-11-09T00:00:02Z",
            status="unknown",
            duration_ms=10,
        )
        _upsert_tool_call(
            conn,
            raw_event={"id": "raw-c", "event_type": "unified_exec.apply_patch"},
            attrs=attrs_c,
            session_id="session-1",
            step_id="step-3",
            timestamp="2026-11-09T00:00:03Z",
            status="unknown",
            duration_ms=10,
        )
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 3
    finally:
        conn.close()


def test_migration_52_collapses_exact_dispatch_copies_and_projects_one_call(tmp_path):
    conn = connect_sqlite(tmp_path / "historical.db")
    try:
        with patch("reflect.store.migrate.load_migrations", return_value=[
            migration for migration in load_migrations() if migration.version <= 51
        ]):
            migrate(conn)
        attrs = _codex_dispatch()
        conn.execute("""INSERT INTO sessions(id,started_at,created_at,updated_at)
            VALUES ('session-1','2026-11-09','2026-11-09','2026-11-09')""")
        conn.execute("""INSERT INTO execution_units(id,session_id,source,started_at,created_at,updated_at)
            VALUES ('unit-1','session-1','native','2026-11-09','2026-11-09','2026-11-09')""")
        for event_id, source, seq, content_hash, event_attrs in (
            ("raw-a", "active", 0, "same-content", attrs),
            ("raw-b", "rotated", 1, "same-content", attrs),
            ("raw-conflict", "other", 2, "different-content", _codex_dispatch(**{"call_id": "call-other", "turn.id": "turn-2"})),
        ):
            conn.execute("""INSERT INTO raw_events(id,source_id,source_type,event_type,trace_id,span_id,
                session_id,observed_at,received_at,origin_kind,attrs_json,content_hash,created_at,normalized_status)
                VALUES (?,?,'otlp_traces_json','unified_exec.exec_command','trace-1','span-1',
                'session-1','2026-11-09T00:00:00Z','2026-11-09T00:00:01Z','native_otlp_trace',?,?,
                '2026-11-09','ok')""", (event_id, source, json.dumps(event_attrs), content_hash))
            step_id = "step_" + hashlib.sha1(event_id.encode()).hexdigest()
            conn.execute("""INSERT INTO steps(id,session_id,seq,type,started_at,summary,raw_attrs_json,
                origin_kind,created_at,updated_at) VALUES (?,'session-1',?,'unknown',
                '2026-11-09T00:00:00Z','unified_exec.exec_command',?,'native_otlp_trace','2026-11-09','2026-11-09')""",
                (step_id, seq, json.dumps(attrs)))
            conn.execute("INSERT INTO execution_unit_steps(execution_unit_id,step_id,session_id,created_at) VALUES ('unit-1',?,'session-1','2026-11-09')", (step_id,))
            if event_id == "raw-a":
                conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,logical_call_id,tool_name,
                    status,duration_ms,raw_attrs_json,created_at,updated_at)
                    VALUES ('legacy-tool',?,'session-1','raw-a','exec_command','unknown',
                    100,?,'2026-11-09','2026-11-09')""", (step_id, json.dumps(attrs)))
        conn.commit()

        assert migrate(conn) == [52, 53, 54, 55, 56, 57]
        assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*),SUM(type='tool_call') FROM steps").fetchone() == (3, 3)
        assert conn.execute("SELECT COUNT(*) FROM execution_unit_steps").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*),MIN(status) FROM tool_calls").fetchone() == (2, "unknown")
        assert {row[0] for row in conn.execute("SELECT logical_call_id FROM tool_calls")} == {"call-1", "call-other"}
        assert conn.execute("SELECT COUNT(*) FROM pending_refresh WHERE session_id='session-1'").fetchone()[0] == 3
        assert migrate(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 3
    finally:
        conn.close()
