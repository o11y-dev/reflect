import json

from reflect.store import normalize as normalize_mod
from reflect.store.ingest import ingest_local_spans_file
from reflect.store.migrate import migrate
from reflect.store.normalize import (
    normalize_pending_raw_events,
    refresh_all_session_statuses,
    repair_telemetry_provenance,
)
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
                "gen_ai.client.prompt.text": "Review the graph normalization",
                "gen_ai.response.text": "I will inspect the graph normalizer.",
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
        origins = conn.execute(
            "SELECT DISTINCT origin_kind FROM steps WHERE session_id = 'sess-1'"
        ).fetchall()
        assert origins == [("hook_jsonl",)]
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
        llm_call = conn.execute(
            """
            SELECT prompt_hash, response_hash, prompt_preview_redacted, response_preview_redacted
            FROM llm_calls
            """
        ).fetchone()
        assert llm_call[0]
        assert llm_call[1]
        assert llm_call[2] == "Review the graph normalization"
        assert llm_call[3] == "I will inspect the graph normalizer."
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM mcp_calls").fetchone()[0] == 1
    finally:
        conn.close()


def test_normalize_derives_session_status_from_hook_outcomes(tmp_path):
    db = tmp_path / "reflect.db"
    spans = tmp_path / "spans.jsonl"
    spans_data = [
        {
            "name": "gen_ai.client.hook.PreToolUse",
            "traceId": "trace-status-1",
            "spanId": "span-status-1",
            "start_time_ns": 100,
            "end_time_ns": 200,
            "attributes": {
                "gen_ai.client.name": "cursor",
                "gen_ai.client.session_id": "sess-error",
                "gen_ai.client.tool_name": "Read",
            },
        },
        {
            "name": "gen_ai.client.hook.PostToolUseFailure",
            "traceId": "trace-status-1",
            "spanId": "span-status-2",
            "parentSpanId": "span-status-1",
            "start_time_ns": 300,
            "end_time_ns": 400,
            "attributes": {
                "gen_ai.client.name": "cursor",
                "gen_ai.client.session_id": "sess-error",
                "gen_ai.client.tool_name": "Read",
            },
        },
        {
            "name": "gen_ai.client.hook.SessionEnd",
            "traceId": "trace-status-2",
            "spanId": "span-status-3",
            "start_time_ns": 500,
            "end_time_ns": 600,
            "attributes": {
                "gen_ai.client.name": "codex",
                "gen_ai.client.session_id": "sess-ok",
            },
        },
    ]
    spans.write_text("\n".join(json.dumps(span) for span in spans_data) + "\n", encoding="utf-8")

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        assert ingest_local_spans_file(conn, file_path=spans) == {"inserted": 3, "skipped": 0}
        assert normalize_pending_raw_events(conn) == {"processed": 3, "failed": 0, "skipped": 0}

        statuses = dict(conn.execute("SELECT id, status FROM sessions").fetchall())
        assert statuses == {"sess-error": "error", "sess-ok": "ok"}
        failures = dict(conn.execute("SELECT id, failure_count FROM sessions").fetchall())
        assert failures == {"sess-error": 1, "sess-ok": 0}
        step_statuses = dict(conn.execute("SELECT summary, status FROM steps").fetchall())
        assert step_statuses["gen_ai.client.hook.PostToolUseFailure"] == "error"
        assert step_statuses["gen_ai.client.hook.SessionEnd"] == "ok"
    finally:
        conn.close()


def test_normalize_mcp_call_falls_back_to_tool_input_payload(tmp_path):
    db = tmp_path / "reflect.db"
    spans = tmp_path / "spans.jsonl"
    spans.write_text(
        json.dumps(
            {
                "name": "BeforeMCPExecution",
                "traceId": "trace-mcp-fallback",
                "spanId": "span-mcp-fallback",
                "parentSpanId": "",
                "start_time_ns": 100,
                "end_time_ns": 200,
                "attributes": {
                    "gen_ai.client.name": "cursor",
                    "gen_ai.client.session_id": "sess-mcp-fallback",
                    "gen_ai.client.tool_name": "CallMcpTool",
                    "gen_ai.client.tool.input": json.dumps(
                        {"server": "mcp-github", "toolName": "search_code"}
                    ),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        ingest_local_spans_file(conn, file_path=spans)
        normalize_pending_raw_events(conn)
        mcp_row = conn.execute(
            "SELECT server_name, tool_name FROM mcp_calls WHERE session_id = 'sess-mcp-fallback'"
        ).fetchone()
        assert tuple(mcp_row) == ("mcp-github", "search_code")
    finally:
        conn.close()


def test_refresh_all_session_statuses_repairs_existing_unknown_rows(tmp_path):
    db = tmp_path / "reflect.db"
    conn = connect_sqlite(db)
    try:
        migrate(conn)
        conn.execute(
            """
            INSERT INTO agents(id, name, raw_json, created_at, updated_at)
            VALUES ('agent-1', 'codex', '{}', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO sessions(id, agent_id, started_at, status, created_at, updated_at)
            VALUES ('sess-old', 'agent-1', '2026-01-01T00:00:00+00:00', 'unknown',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO steps(id, session_id, seq, type, started_at, status, summary, raw_attrs_json, created_at, updated_at)
            VALUES ('step-old', 'sess-old', 0, 'unknown', '2026-01-01T00:00:01+00:00',
                    'ok', 'gen_ai.client.hook.SessionEnd', '{}',
                    '2026-01-01T00:00:01+00:00', '2026-01-01T00:00:01+00:00')
            """
        )

        assert refresh_all_session_statuses(conn) == {"sessions": 1}

        session = conn.execute("SELECT status, failure_count FROM sessions WHERE id = 'sess-old'").fetchone()
        assert tuple(session) == ("ok", 0)
    finally:
        conn.close()


def test_repair_telemetry_provenance_backfills_existing_raw_events_and_steps(tmp_path):
    db = tmp_path / "reflect.db"
    conn = connect_sqlite(db)
    try:
        migrate(conn)
        raw_event_id = "raw-old-native-log"
        step_id = normalize_mod._stable_id("step", raw_event_id)
        attrs = json.dumps(
            {
                "service.name": "claude-code",
                "gen_ai.client.name": "claude",
                "gen_ai.client.hook.event": "Stop",
                "session.id": "sess-old-native-log",
            },
            sort_keys=True,
        )
        conn.execute(
            """
            INSERT INTO raw_events(
              id, source_id, source_type, event_type, trace_id, span_id, parent_span_id,
              session_id, observed_at, received_at, attrs_json, body_json,
              normalized_status, normalization_error, content_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_event_id,
                "otel-logs.json",
                "otlp_logs_json",
                "gen_ai.client.hook.Stop",
                "",
                "",
                "",
                "sess-old-native-log",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:01+00:00",
                attrs,
                "{}",
                "ok",
                None,
                "hash-old-native-log",
                "2026-01-01T00:00:01+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO agents(id, name, raw_json, created_at, updated_at)
            VALUES ('agent-claude', 'claude', '{}', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO sessions(id, agent_id, started_at, status, source_kind, source_ref, created_at, updated_at)
            VALUES (
              'sess-old-native-log', 'agent-claude', '2026-01-01T00:00:00+00:00', 'ok',
              'otlp_logs_json', 'otel-logs.json', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, ended_at, duration_ms, status,
              summary, raw_attrs_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                step_id,
                "sess-old-native-log",
                0,
                "llm_call",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:01+00:00",
                1000,
                "ok",
                "gen_ai.client.hook.Stop",
                attrs,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()

        repaired = repair_telemetry_provenance(conn)

        assert repaired == {"raw_events": 1, "steps": 1}
        raw_row = conn.execute(
            "SELECT origin_kind, attrs_json FROM raw_events WHERE id = ?",
            (raw_event_id,),
        ).fetchone()
        step_row = conn.execute(
            "SELECT origin_kind, raw_attrs_json FROM steps WHERE id = ?",
            (step_id,),
        ).fetchone()
        assert raw_row[0] == "native_otlp_log"
        assert step_row[0] == "native_otlp_log"
        assert json.loads(raw_row[1])["reflect.telemetry.origin"] == "native_otlp_log"
        assert json.loads(step_row[1])["reflect.telemetry.origin"] == "native_otlp_log"
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


def test_normalize_rolls_back_partial_event_on_failure(tmp_path, monkeypatch):
    db = tmp_path / "reflect.db"
    spans = tmp_path / "spans.jsonl"
    span = {
        "name": "PreToolUse",
        "traceId": "trace-3",
        "spanId": "span-5",
        "start_time_ns": 100,
        "end_time_ns": 200,
        "attributes": {
            "gen_ai.client.name": "cursor",
            "gen_ai.client.session_id": "sess-fail",
            "gen_ai.client.tool_name": "Shell",
        },
    }
    spans.write_text(json.dumps(span) + "\n", encoding="utf-8")

    def _raise_after_step_inserted(*_args, **_kwargs):
        raise RuntimeError("forced call insert failure")

    monkeypatch.setattr(normalize_mod, "_insert_call_record", _raise_after_step_inserted)

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        ingest_local_spans_file(conn, file_path=spans)

        assert normalize_pending_raw_events(conn) == {"processed": 0, "failed": 1, "skipped": 0}
        raw = conn.execute(
            "SELECT normalized_status, normalization_error FROM raw_events"
        ).fetchone()
        assert raw[0] == "failed"
        assert "forced call insert failure" in raw[1]
        assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
    finally:
        conn.close()
