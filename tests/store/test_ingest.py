import json

from reflect.store.ingest import (
    ingest_local_spans_file,
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

        row = conn.execute("SELECT source_type, event_type, session_id FROM raw_events").fetchone()
        assert row == ("otlp_traces_json", "UserPromptSubmit", "sess-1")
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
            "SELECT source_type, event_type, session_id, attrs_json FROM raw_events"
        ).fetchone()
        attrs = json.loads(row[3])
        assert row[:3] == ("otlp_logs_json", "gen_ai.client.hook.UserPromptSubmit", "codex-sess-1")
        assert attrs["gen_ai.client.name"] == "codex"
        assert attrs["gen_ai.request.model"] == "gpt-5.5"
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
        assert tuple(session) == ("gemini", 2, 100, 25, 12)
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
