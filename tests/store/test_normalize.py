import json

from reflect.store.ingest import ingest_local_spans_file
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.sqlite import connect_sqlite


def _write_spans(path):
    spans = [
        {
            "name": "UserPromptSubmit",
            "traceId": "trace-1",
            "spanId": "span-1",
            "parentSpanId": "",
            "start_time_ns": 100,
            "end_time_ns": 200,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-1",
                "gen_ai.request.model": "claude-4.6-opus",
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 50,
            },
        },
        {
            "name": "PreToolUse",
            "traceId": "trace-1",
            "spanId": "span-2",
            "parentSpanId": "span-1",
            "start_time_ns": 300,
            "end_time_ns": 500,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-1",
                "gen_ai.client.tool_name": "Read",
            },
        },
        {
            "name": "BeforeMCPExecution",
            "traceId": "trace-1",
            "spanId": "span-3",
            "parentSpanId": "span-2",
            "start_time_ns": 600,
            "end_time_ns": 900,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-1",
                "gen_ai.client.mcp_server": "mcp-github",
                "gen_ai.client.mcp_tool": "get_issue",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(span) for span in spans) + "\n", encoding="utf-8")


def test_normalize_pending_raw_events_populates_canonical_tables(tmp_path):
    db = tmp_path / "reflect.db"
    spans = tmp_path / "spans.jsonl"
    _write_spans(spans)

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        assert ingest_local_spans_file(conn, file_path=spans) == {"inserted": 3, "skipped": 0}
        result = normalize_pending_raw_events(conn)
        second = normalize_pending_raw_events(conn)

        assert result == {"processed": 3, "failed": 0, "skipped": 0}
        assert second == {"processed": 0, "failed": 0, "skipped": 0}
        assert conn.execute("SELECT COUNT(*) FROM raw_events WHERE normalized_status = 'ok'").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1
        session = conn.execute(
            "SELECT id, input_tokens, output_tokens FROM sessions WHERE id = 'sess-1'"
        ).fetchone()
        assert tuple(session) == ("sess-1", 100, 50)
        assert conn.execute("SELECT COUNT(*) FROM steps WHERE session_id = 'sess-1'").fetchone()[0] == 3
        parent_rows = conn.execute(
            """
            SELECT child.summary, parent.summary
            FROM steps child
            LEFT JOIN steps parent ON parent.id = child.parent_step_id
            WHERE child.session_id = 'sess-1'
            """
        ).fetchall()
        parent_by_summary = {row[0]: row[1] for row in parent_rows}
        assert parent_by_summary == {
            "UserPromptSubmit": None,
            "PreToolUse": "UserPromptSubmit",
            "BeforeMCPExecution": "PreToolUse",
        }
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM mcp_calls").fetchone()[0] == 1
    finally:
        conn.close()


def test_normalize_promotes_memory_and_privacy_attrs(tmp_path):
    db = tmp_path / "reflect.db"
    spans = tmp_path / "spans.jsonl"
    span = {
        "name": "MemoryWrite",
        "traceId": "trace-2",
        "spanId": "span-4",
        "start_time_ns": 100,
        "end_time_ns": 200,
        "attributes": {
            "gen_ai.client.session_id": "sess-2",
            "gen_ai.memory.id": "mem-1",
            "gen_ai.memory.scope": "repo",
            "gen_ai.memory.type": "repo_convention",
            "gen_ai.memory.source": "opentelemetry_hook",
            "gen_ai.privacy.finding_type": "secret",
            "gen_ai.privacy.severity": "high",
            "gen_ai.privacy.action_taken": "redacted",
        },
    }
    spans.write_text(json.dumps(span) + "\n", encoding="utf-8")

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        ingest_local_spans_file(conn, file_path=spans)
        assert normalize_pending_raw_events(conn) == {"processed": 1, "failed": 0, "skipped": 0}

        memory = conn.execute("SELECT id, scope, type FROM memories").fetchone()
        assert tuple(memory) == ("mem-1", "repo", "repo_convention")
        finding = conn.execute("SELECT finding_type, severity, action_taken FROM privacy_findings").fetchone()
        assert tuple(finding) == ("secret", "high", "redacted")
    finally:
        conn.close()
