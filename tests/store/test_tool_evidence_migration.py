import json
from unittest.mock import patch

import pytest

from reflect.store.migrate import load_migrations, migrate
from reflect.store.pending_refresh import PendingRefreshState
from reflect.store.sqlite import connect_sqlite
from reflect.views.report_tabs import _usage_subagent_counts


def _historical_store(tmp_path):
    conn = connect_sqlite(tmp_path / "historical.db")
    with patch("reflect.store.migrate.load_migrations", return_value=[m for m in load_migrations() if m.version <= 49]):
        migrate(conn)
    for session in ("aliases", "timing", "delegation", "untouched"):
        conn.execute("INSERT INTO sessions(id,started_at,created_at,updated_at) VALUES (?, '2026-01-01', '2026-01-01', '2026-01-01')", (session,))
    fixtures = [
        ("alias", "aliases", "PreToolUse", "PreToolUse", {"ide.tool_name": "Read"}),
        ("broker", "aliases", "code_mode.broker.invoke_tool", "code_mode.broker.invoke_tool",
         {"tool_name": "exec_command", "runtime_tool_call_id": "tool-1", "cell.id": "1"}),
        ("shell", "aliases", "BeforeShellExecution", "git status", {"gen_ai.client.command": "git status"}),
        ("timing", "timing", "codex.tool_blocking", "codex.tool_blocking",
         {"service.name": "codex-app-server", "reflect.telemetry.origin": "native_otlp_trace",
          "code.module.name": "codex_core::session::turn", "busy_ns": 2000, "idle_ns": 3000}),
        ("spawn", "delegation", "PreToolUse", "collaboration.spawn_agent",
         {"gen_ai.client.tool_name": "collaboration.spawn_agent", "gen_ai.tool.call.id": "call-1",
          "gen_ai.client.tool.input": '{"agent_type":"explorer","task_name":"inspect"}',
          "gen_ai.client.tool.output": '{"agent_id":"child-1"}'}),
        ("task", "delegation", "PostToolUse", "Task",
         {"gen_ai.client.tool_name": "Task", "gen_ai.client.tool.input": '{"subagent_type":"Explore"}'}),
        ("missing", "untouched", "PreToolUse", "PreToolUse", {}),
        ("known", "untouched", "PreToolUse", "Bash", {"gen_ai.client.tool_name": "Bash"}),
    ]
    for seq, (identity, session, event, name, attrs) in enumerate(fixtures):
        attrs = {"gen_ai.client.hook.event": event, **attrs}
        payload = json.dumps(attrs)
        conn.execute("""INSERT INTO raw_events(id,source_id,source_type,event_type,session_id,
            observed_at,received_at,attrs_json,content_hash,created_at,normalized_status)
            VALUES (?, 'fixture', 'fixture', ?, ?, '2026-01-01', '2026-01-01', ?, ?, '2026-01-01', 'ok')""",
                     (identity, event, session, payload, identity))
        conn.execute("""INSERT INTO steps(id,session_id,seq,type,started_at,summary,raw_attrs_json,created_at,updated_at)
            VALUES (?, ?, ?, 'tool_call', '2026-01-01', ?, ?, '2026-01-01', '2026-01-01')""",
                     (identity, session, seq, event, payload))
        conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,logical_call_id,raw_attrs_json,created_at,updated_at)
            VALUES (?, ?, ?, ?, ?, ?, '2026-01-01', '2026-01-01')""",
                     (identity, identity, session, name, "call-1" if identity == "spawn" else identity, payload))
    lifecycle = json.dumps({"gen_ai.client.hook.event": "SubagentStop", "gen_ai.client.agent_id": "child-1"})
    conn.execute("""INSERT INTO steps(id,session_id,seq,type,started_at,summary,raw_attrs_json,created_at,updated_at)
        VALUES ('stop', 'delegation', 100, 'agent_call', '2026-01-01', 'SubagentStop', ?, '2026-01-01', '2026-01-01')""", (lifecycle,))
    conn.execute("""INSERT INTO llm_calls(id,step_id,session_id,input_tokens,output_tokens,estimated_cost_usd,created_at,updated_at)
        VALUES ('usage', 'known', 'untouched', 17, 9, 0.0017, '2026-01-01', '2026-01-01')""")
    conn.execute("DELETE FROM pending_refresh")
    conn.commit()
    return conn


def test_tool_evidence_upgrade_repairs_retained_history_without_usage_replay(tmp_path):
    conn = _historical_store(tmp_path)
    try:
        raw_before = conn.execute("SELECT * FROM raw_events ORDER BY id").fetchall()
        usage_before = conn.execute("SELECT * FROM llm_calls").fetchall()
        attrs_before = conn.execute("SELECT id,raw_attrs_json FROM steps ORDER BY id").fetchall()
        assert migrate(conn) == [50, 51, 52, 53, 54, 55, 56]
        assert dict(conn.execute("SELECT id,tool_name FROM tool_calls")) == {
            "alias": "Read", "broker": "exec_command", "shell": "Shell",
            "spawn": "collaboration.spawn_agent", "task": "Task", "missing": "PreToolUse", "known": "Bash",
        }
        assert conn.execute("SELECT type FROM steps WHERE id='timing'").fetchone()[0] == "runtime_span"
        assert conn.execute("SELECT * FROM raw_events ORDER BY id").fetchall() == raw_before
        assert conn.execute("SELECT * FROM llm_calls").fetchall() == usage_before
        assert conn.execute("SELECT id,raw_attrs_json FROM steps ORDER BY id").fetchall() == attrs_before
        assert conn.execute("SELECT command_template,is_shell_call FROM tool_calls WHERE id='shell'").fetchone() == ("git status", 1)
        assert conn.execute("SELECT command_template,is_shell_call FROM tool_calls WHERE id='broker'").fetchone() == (None, 1)
        counts = _usage_subagent_counts(conn, ["delegation"])
        assert counts["subagent_starts"] == {"explorer": 1, "Explore": 1}
        assert counts["subagent_stops"] == {"explorer": 1}
        pending = PendingRefreshState(conn).capture()
        assert pending.pending_for("canonical") == set()
        assert pending.pending_for("cost") == set()
        for phase in ("rollup", "graph", "improvements"):
            assert pending.step_scope(phase) == {
                "aliases": {"alias", "broker", "shell"}, "timing": {"timing"},
                "delegation": {"spawn", "task", "stop"},
            }
        facts_before = conn.execute("SELECT * FROM agent_events ORDER BY id").fetchall()
        assert migrate(conn) == []
        assert conn.execute("SELECT * FROM agent_events ORDER BY id").fetchall() == facts_before
        assert PendingRefreshState(conn).capture() == pending
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_tool_evidence_upgrade_rolls_back_projection_changes_when_fact_repair_fails(tmp_path):
    conn = _historical_store(tmp_path)
    try:
        before = conn.execute("SELECT * FROM tool_calls ORDER BY id").fetchall()
        with (
            patch("reflect.store.hook_facts.HookFactRepository.persist_tool_delegation", side_effect=RuntimeError("fixture failure")),
            pytest.raises(RuntimeError, match="fixture failure"),
        ):
            migrate(conn)
        assert conn.execute("SELECT * FROM tool_calls ORDER BY id").fetchall() == before
        assert conn.execute("SELECT type FROM steps WHERE id='timing'").fetchone()[0] == "tool_call"
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=50").fetchone()[0] == 0
        assert PendingRefreshState(conn).capture().entries == ()
    finally:
        conn.close()
