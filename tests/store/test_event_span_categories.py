"""Event categories preserve runtime spans without inventing tool/model calls."""

import json
from unittest.mock import patch

import pytest

from reflect.store.migrate import load_migrations, migrate
from reflect.store.normalize import _step_type
from reflect.store.sqlite import connect_sqlite

TIMING = {"busy_ns": 2000, "idle_ns": 3000, "code.module.name": "runtime::scheduler"}


@pytest.mark.parametrize("event,attrs,expected", [
    ("runtime.tool_queue_wait", TIMING, "runtime_span"),
    ("codex.sampling", {**TIMING, "busy_ns": 0, "idle_ns": 0}, "runtime_span"),
    ("gen_ai.client.session", {"gen_ai.client.session_id": "session"}, "session_span"),
    ("gen_ai.client.generation", {"gen_ai.client.session_id": "session", "gen_ai.client.generation_id": "generation"}, "generation_span"),
    ("wrapped", {"event.name": "gen_ai.client.session", "gen_ai.client.session_id": "session"}, "session_span"),
    ("session", {"gen_ai.client.session_id": "session"}, "unknown"),
    ("gen_ai.client.session", {}, "unknown"),
    ("gen_ai.client.generation", {"gen_ai.client.session_id": "session"}, "unknown"),
    ("gen_ai.client.generation", {"gen_ai.client.generation_id": "generation"}, "unknown"),
    ("reflect.hook.endpoint.probe", {}, "unknown"),
    ("runtime.wait", {**TIMING, "busy_ns": "2000"}, "unknown"),
    ("runtime.wait", {**TIMING, "busy_ns": True}, "unknown"),
    ("runtime.wait", {**TIMING, "idle_ns": -1}, "unknown"),
    ("runtime.wait", {**TIMING, "idle_ns": float("inf")}, "unknown"),
    ("runtime.wait", {**TIMING, "idle_ns": float("nan")}, "unknown"),
    ("runtime.wait", {"busy_ns": 2000, "idle_ns": 3000}, "unknown"),
    ("runtime.wait", {**TIMING, "code.module.name": " "}, "unknown"),
    ("PreToolUse", TIMING, "tool_call"),
    ("provider.operation", {**TIMING, "tool_name": "exec_command"}, "tool_call"),
    ("provider.operation", {**TIMING, "gen_ai.operation.name": "execute_tool"}, "tool_call"),
    ("gen_ai.client.generation", {**TIMING, "gen_ai.request.model": "fixture-model"}, "llm_call"),
    ("runtime.error", TIMING, "error"),
    ("runtime.wait", {**TIMING, "gen_ai.memory.id": "memory"}, "memory_event"),
])
def test_non_operation_categories_require_recorded_structure_and_preserve_semantics(event, attrs, expected):
    assert _step_type(event, attrs) == expected


def test_event_category_backfill_is_bounded_idempotent_and_preserves_accounting(tmp_path):
    conn = connect_sqlite(tmp_path / "historical.db")
    try:
        with patch("reflect.store.migrate.load_migrations", return_value=[m for m in load_migrations() if m.version <= 50]):
            migrate(conn)
        conn.execute("""INSERT INTO sessions(id,started_at,input_tokens,output_tokens,estimated_cost_usd,created_at,updated_at)
            VALUES ('session','2026-11-09',123,45,0.25,'2026-11-09','2026-11-09')""")
        fixtures = [(f"timing-{index}", "runtime.wait", TIMING) for index in range(600)] + [
            ("wrapper", "gen_ai.client.session", {"gen_ai.client.session_id": "session"}),
            ("generation", "gen_ai.client.generation", {"gen_ai.client.session_id": "session", "gen_ai.client.generation_id": "generation"}),
            ("ambiguous", "exec_command", {**TIMING, "tool_name": "exec_command", "call_id": "old-call"}),
            ("unknown", "reflect.hook.endpoint.probe", {}),
            ("missing-structure", "gen_ai.client.generation", {}),
        ]
        for seq, (identity, event, attrs) in enumerate(fixtures):
            raw = json.dumps(attrs)
            conn.execute("""INSERT INTO steps(id,session_id,seq,type,started_at,summary,raw_attrs_json,created_at,updated_at)
                VALUES (?,'session',?,'unknown','2026-11-09',?,?,'2026-11-09','2026-11-09')""", (identity, seq, event, raw))
            conn.execute("""INSERT INTO raw_events(id,source_id,source_type,event_type,session_id,observed_at,received_at,attrs_json,content_hash,created_at,normalized_status)
                VALUES (?,'fixture','fixture',?,'session','2026-11-09','2026-11-09',?,?,'2026-11-09','ok')""", (identity, event, raw, identity))
        conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,raw_attrs_json,created_at,updated_at)
            VALUES ('tool','ambiguous','session','exec_command','{}','2026-11-09','2026-11-09')""")
        conn.execute("""INSERT INTO llm_calls(id,step_id,session_id,input_tokens,output_tokens,estimated_cost_usd,raw_attrs_json,created_at,updated_at)
            VALUES ('llm','unknown','session',123,45,0.25,'{}','2026-11-09','2026-11-09')""")
        conn.executemany("INSERT INTO store_metadata(key,value,updated_at) VALUES (?,?,?)", [
            ("prepared_snapshot", "incomplete", "2026-11-08"),
            ("prepared_usage", "ready", "2026-11-08"),
        ])
        conn.commit()
        tables = ("raw_events", "sessions", "llm_calls", "tool_calls", "pending_refresh", "session_rollups")
        before = {name: conn.execute(f"SELECT * FROM {name}").fetchall() for name in tables}
        attrs_before = conn.execute("SELECT id,raw_attrs_json FROM steps ORDER BY id").fetchall()
        assert migrate(conn) == [51]
        categories = dict(conn.execute("SELECT type,COUNT(*) FROM steps GROUP BY type"))
        assert categories == {"runtime_span": 600, "session_span": 1, "generation_span": 1, "unknown": 3}
        assert {name: conn.execute(f"SELECT * FROM {name}").fetchall() for name in tables} == before
        assert conn.execute("SELECT id,raw_attrs_json FROM steps ORDER BY id").fetchall() == attrs_before
        assert dict(conn.execute("SELECT key,value FROM store_metadata WHERE key LIKE 'prepared_%'")) == {
            "prepared_snapshot": "incomplete", "prepared_usage": "ready"}
        assert all(row[0] != "2026-11-08" for row in conn.execute("SELECT updated_at FROM store_metadata WHERE key LIKE 'prepared_%'"))
        after = conn.execute("SELECT * FROM steps ORDER BY id").fetchall()
        assert migrate(conn) == []
        assert conn.execute("SELECT * FROM steps ORDER BY id").fetchall() == after
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
