import json

from reflect.store.cursor_adapter import apply_cursor_transcript_usage_estimates
from reflect.store.ingest import (
    ingest_local_spans_file,
    ingest_native_session_file,
    ingest_otlp_logs_file,
    ingest_otlp_traces_file,
)
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.rollups import rebuild_rollups
from reflect.store.sqlite import connect_sqlite


def _write_otlp_file(path):
    payload = {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "gen_ai.client.name", "value": {"stringValue": "claude"}}]},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "UserPromptSubmit",
                                "traceId": "t1",
                                "spanId": "s1",
                                "parentSpanId": "",
                                "startTimeUnixNano": "100",
                                "endTimeUnixNano": "200",
                                "attributes": [{"key": "session.id", "value": {"stringValue": "sess-1"}}],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _write_spans_file(path):
    payload = {
        "name": "PreToolUse",
        "traceId": "t2",
        "spanId": "s2",
        "parentSpanId": "",
        "start_time_ns": 300,
        "end_time_ns": 400,
        "attributes": {
            "gen_ai.client.session_id": "sess-2",
            "gen_ai.client.tool_name": "Read",
        },
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _write_codex_logs_file(path):
    payload = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "codex_cli_rs"}}]
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "1000",
                                "attributes": [
                                    {"key": "event.name", "value": {"stringValue": "codex.user_prompt"}},
                                    {"key": "event.timestamp", "value": {"stringValue": "2026-03-24T10:00:01Z"}},
                                    {"key": "conversation.id", "value": {"stringValue": "codex-sess-1"}},
                                    {"key": "model", "value": {"stringValue": "gpt-5.5"}},
                                    {"key": "prompt", "value": {"stringValue": "[REDACTED]"}},
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _write_claude_logs_file(path):
    payload = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "claude-code"}}]
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "3000",
                                "attributes": [
                                    {"key": "event.name", "value": {"stringValue": "claude_code.api_request"}},
                                    {"key": "event.timestamp", "value": {"stringValue": "2026-05-28T14:47:57Z"}},
                                    {"key": "session.id", "value": {"stringValue": "claude-sess-1"}},
                                    {"key": "model", "value": {"stringValue": "claude-opus-4-6"}},
                                    {"key": "input_tokens", "value": {"stringValue": "9"}},
                                    {"key": "output_tokens", "value": {"stringValue": "7238"}},
                                    {"key": "cache_creation_tokens", "value": {"stringValue": "41530"}},
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _write_gemini_logs_file(path):
    payload = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "gemini-cli"}}]
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "2000",
                                "attributes": [
                                    {"key": "event.name", "value": {"stringValue": "gemini_cli.user_prompt"}},
                                    {"key": "event.timestamp", "value": {"stringValue": "2026-03-24T10:00:02Z"}},
                                    {"key": "session.id", "value": {"stringValue": "gemini-sess-1"}},
                                    {"key": "prompt_id", "value": {"stringValue": "prompt-1"}},
                                    {"key": "prompt_length", "value": {"intValue": "42"}},
                                ],
                            },
                            {
                                "timeUnixNano": "3000",
                                "attributes": [
                                    {"key": "event.name", "value": {"stringValue": "gemini_cli.api_response"}},
                                    {"key": "event.timestamp", "value": {"stringValue": "2026-03-24T10:00:04Z"}},
                                    {"key": "session.id", "value": {"stringValue": "gemini-sess-1"}},
                                    {"key": "model", "value": {"stringValue": "gemini-2.5-flash-lite"}},
                                    {"key": "input_token_count", "value": {"intValue": "100"}},
                                    {"key": "output_token_count", "value": {"intValue": "25"}},
                                    {"key": "cached_content_token_count", "value": {"intValue": "12"}},
                                    {"key": "thoughts_token_count", "value": {"intValue": "5"}},
                                    {"key": "duration_ms", "value": {"intValue": "750"}},
                                ],
                            },
                        ]
                    }
                ],
            }
        ]
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _write_codex_session_file(path):
    records = [
        {
            "timestamp": "2026-05-08T00:42:07.990Z",
            "type": "session_meta",
            "payload": {
                "id": "codex-native-sess-1",
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
                "content": [{"type": "input_text", "text": "inspect native sessions"}],
            },
        },
        {
            "timestamp": "2026-05-08T00:42:16.256Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call-1",
                "arguments": "{\"cmd\":\"git status\"}",
            },
        },
        {
            "timestamp": "2026-05-08T00:42:18.256Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "clean",
            },
        },
        {
            "timestamp": "2026-05-08T00:42:19.256Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 250,
                        "output_tokens": 80,
                        "reasoning_output_tokens": 12,
                    }
                },
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


def _write_cursor_session_file(path):
    records = [
        {
            "role": "user",
            "message": {"content": [{"type": "text", "text": "inspect cursor native tools"}]},
        },
        {
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I will inspect the repo."},
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
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


def test_ingest_otlp_traces_dedupes(tmp_path):
    db = tmp_path / "reflect.db"
    otlp = tmp_path / "traces.json"
    _write_otlp_file(otlp)

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        first = ingest_otlp_traces_file(conn, file_path=otlp)
        second = ingest_otlp_traces_file(conn, file_path=otlp)

        assert first == {"inserted": 1, "skipped": 0}
        assert second == {"inserted": 0, "skipped": 1}

        row = conn.execute("SELECT source_type, event_type, session_id, origin_kind, attrs_json FROM raw_events").fetchone()
        attrs = json.loads(row[4])
        assert row[:4] == ("otlp_traces_json", "UserPromptSubmit", "sess-1", "native_otlp_trace")
        assert attrs["reflect.telemetry.origin"] == "native_otlp_trace"
    finally:
        conn.close()


def test_ingest_otlp_logs_normalizes_codex_records(tmp_path):
    db = tmp_path / "reflect.db"
    logs = tmp_path / "otel-logs.json"
    _write_codex_logs_file(logs)

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        first = ingest_otlp_logs_file(conn, file_path=logs)
        second = ingest_otlp_logs_file(conn, file_path=logs)

        assert first == {"inserted": 1, "skipped": 0}
        assert second == {"inserted": 0, "skipped": 1}

        row = conn.execute(
            "SELECT source_type, event_type, session_id, origin_kind, attrs_json FROM raw_events"
        ).fetchone()
        attrs = json.loads(row[4])
        assert row[:4] == ("otlp_logs_json", "gen_ai.client.hook.UserPromptSubmit", "codex-sess-1", "native_otlp_log")
        assert attrs["gen_ai.client.name"] == "codex"
        assert attrs["gen_ai.request.model"] == "gpt-5.5"
        assert attrs["reflect.telemetry.origin"] == "native_otlp_log"
    finally:
        conn.close()


def test_ingest_otlp_logs_normalizes_claude_api_request(tmp_path):
    db = tmp_path / "reflect.db"
    logs = tmp_path / "otel-logs.json"
    _write_claude_logs_file(logs)

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        first = ingest_otlp_logs_file(conn, file_path=logs)

        assert first == {"inserted": 1, "skipped": 0}

        row = conn.execute(
            "SELECT source_type, event_type, session_id, origin_kind, attrs_json FROM raw_events"
        ).fetchone()
        attrs = json.loads(row[4])
        assert row[:4] == ("otlp_logs_json", "gen_ai.client.hook.Stop", "claude-sess-1", "native_otlp_log")
        assert attrs["gen_ai.client.name"] == "claude"
        assert attrs["gen_ai.request.model"] == "claude-opus-4-6"
        assert attrs["gen_ai.usage.output_tokens"] == 7238
        assert attrs["reflect.telemetry.origin"] == "native_otlp_log"
    finally:
        conn.close()


def test_ingest_native_codex_session_file(tmp_path):
    db = tmp_path / "reflect.db"
    session_file = tmp_path / "rollout-codex-native-sess-1.jsonl"
    _write_codex_session_file(session_file)

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        first = ingest_native_session_file(conn, file_path=session_file, agent="codex")
        second = ingest_native_session_file(conn, file_path=session_file, agent="codex")

        assert first == {"inserted": 6, "skipped": 0}
        assert second == {"inserted": 0, "skipped": 6}

        rows = conn.execute(
            """
            SELECT source_type, event_type, session_id, origin_kind, attrs_json
            FROM raw_events
            ORDER BY observed_at, event_type
            """
        ).fetchall()
        assert {row[0] for row in rows} == {"native_session"}
        assert {row[2] for row in rows} == {"codex-native-sess-1"}
        assert {row[3] for row in rows} == {"native_session"}
        attrs = [json.loads(row[4]) for row in rows]
        assert {attr["gen_ai.client.name"] for attr in attrs} == {"codex"}
        assert any(attr.get("gen_ai.client.tool_name") == "exec_command" for attr in attrs)
        assert any(attr.get("gen_ai.usage.input_tokens") == 750 for attr in attrs)
    finally:
        conn.close()


def test_ingest_native_cursor_session_file_extracts_tool_and_mcp_calls(tmp_path):
    db = tmp_path / "reflect.db"
    session_file = tmp_path / "cursor-native-sess-1.jsonl"
    _write_cursor_session_file(session_file)

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        first = ingest_native_session_file(conn, file_path=session_file, agent="cursor")
        second = ingest_native_session_file(conn, file_path=session_file, agent="cursor")

        assert first == {"inserted": 6, "skipped": 0}
        assert second == {"inserted": 0, "skipped": 6}

        assert normalize_pending_raw_events(conn) == {"processed": 6, "failed": 0, "skipped": 0}
        assert apply_cursor_transcript_usage_estimates(conn, [session_file]) == {
            "updated": 1,
            "skipped": 0,
            "missing": 0,
        }
        assert rebuild_rollups(conn) == {"session_rollups": 1, "daily_rollups": 1, "tool_rollups": 1}
        rollup = conn.execute(
            """
            SELECT agent, input_tokens, output_tokens, total_cost
            FROM session_rollups
            WHERE session_id = 'cursor-native-sess-1'
            """
        ).fetchone()
        assert rollup[0] == "cursor"
        assert rollup[1] > 0
        assert rollup[2] > 0
        assert rollup[3] == 0
        raw_attrs = conn.execute(
            """
            SELECT attrs_json
            FROM raw_events
            WHERE source_id = ?
            ORDER BY event_type
            """,
            (f"native_session:cursor:{session_file}",),
        ).fetchall()
        assert raw_attrs
        assert all("estimated_cursor_transcript" not in row[0] for row in raw_attrs)
        assert all("gen_ai.usage.input_tokens" not in row[0] for row in raw_attrs)
        llm_rows = conn.execute(
            """
            SELECT input_tokens, output_tokens, raw_attrs_json
            FROM llm_calls
            WHERE session_id = 'cursor-native-sess-1'
            ORDER BY operation_name
            """
        ).fetchall()
        assert not any(row[0] > 0 for row in llm_rows)
        assert not any(row[1] > 0 for row in llm_rows)
        assert not any("estimated_cursor_transcript" in row[2] for row in llm_rows)
        token_step = conn.execute(
            """
            SELECT type, summary, raw_attrs_json
            FROM steps
            WHERE session_id = 'cursor-native-sess-1'
              AND type = 'token_estimate'
            """
        ).fetchone()
        assert token_step[0] == "token_estimate"
        assert token_step[1] == "cursor.transcript.token_estimate"
        assert "estimated_cursor_transcript" in token_step[2]
        tool = conn.execute(
            """
            SELECT tool_name, input_preview_redacted
            FROM tool_calls
            WHERE tool_name = 'Shell'
            """
        ).fetchone()
        assert tool[0] == "Shell"
        assert "git status --short" in tool[1]
        mcp = conn.execute(
            """
            SELECT server_name, tool_name
            FROM mcp_calls
            WHERE server_name = 'jira'
            """
        ).fetchone()
        assert tuple(mcp) == ("jira", "search")
    finally:
        conn.close()


def test_ingest_otlp_logs_normalizes_gemini_records(tmp_path):
    db = tmp_path / "reflect.db"
    logs = tmp_path / "otel-logs.json"
    _write_gemini_logs_file(logs)

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        first = ingest_otlp_logs_file(conn, file_path=logs)
        second = ingest_otlp_logs_file(conn, file_path=logs)

        assert first == {"inserted": 2, "skipped": 0}
        assert second == {"inserted": 0, "skipped": 2}

        rows = conn.execute(
            """
            SELECT source_type, event_type, session_id, attrs_json
            FROM raw_events
            ORDER BY observed_at, event_type
            """
        ).fetchall()
        assert [row[1] for row in rows] == [
            "gen_ai.client.hook.UserPromptSubmit",
            "gen_ai.client.hook.Stop",
        ]
        attrs = [json.loads(row[3]) for row in rows]
        assert {row[2] for row in rows} == {"gemini-sess-1"}
        assert {attr["gen_ai.client.name"] for attr in attrs} == {"gemini"}
        assert attrs[1]["gen_ai.request.model"] == "gemini-2.5-flash-lite"
        assert attrs[1]["gen_ai.usage.input_tokens"] == 100
        assert attrs[1]["gen_ai.usage.output_tokens"] == 25
        assert attrs[1]["gen_ai.usage.cache_read.input_tokens"] == 12

        assert normalize_pending_raw_events(conn) == {"processed": 2, "failed": 0, "skipped": 0}
        assert rebuild_rollups(conn) == {"session_rollups": 1, "daily_rollups": 1, "tool_rollups": 0}
        session = conn.execute(
            """
            SELECT sr.agent, sr.prompt_count, sr.input_tokens, sr.output_tokens, sr.cache_read_tokens
            FROM session_rollups sr
            WHERE sr.session_id = 'gemini-sess-1'
            """
        ).fetchone()
        assert tuple(session) == ("gemini", 1, 100, 25, 12)
        llm_call = conn.execute(
            "SELECT provider, response_model FROM llm_calls WHERE response_model IS NOT NULL"
        ).fetchone()
        assert tuple(llm_call) == ("google", "gemini-2.5-flash-lite")
    finally:
        conn.close()


def test_ingest_local_spans_dedupes(tmp_path):
    db = tmp_path / "reflect.db"
    spans = tmp_path / "spans.jsonl"
    _write_spans_file(spans)

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        first = ingest_local_spans_file(conn, file_path=spans)
        second = ingest_local_spans_file(conn, file_path=spans)

        assert first == {"inserted": 1, "skipped": 0}
        assert second == {"inserted": 0, "skipped": 1}

        row = conn.execute(
            "SELECT source_type, event_type, session_id, attrs_json FROM raw_events"
        ).fetchone()
        assert row[:3] == ("local_spans_jsonl", "PreToolUse", "sess-2")
        assert json.loads(row[3])["gen_ai.client.tool_name"] == "Read"
    finally:
        conn.close()
