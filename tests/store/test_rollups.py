import json

from reflect.store.ingest import ingest_local_spans_file
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.rollups import rebuild_rollups
from reflect.store.sqlite import connect_sqlite


def _write_spans(path):
    spans = [
        {
            "name": "UserPromptSubmit",
            "traceId": "trace-1",
            "spanId": "span-1",
            "start_time_ns": 1_700_000_000_000_000_000,
            "end_time_ns": 1_700_000_000_100_000_000,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-rollup",
                "gen_ai.client.hook.event": "UserPromptSubmit",
                "gen_ai.client.generation_id": "gen-1",
                "gen_ai.request.model": "claude-4.6-opus",
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 50,
            },
        },
        {
            "name": "UserPromptSubmit",
            "traceId": "trace-1",
            "spanId": "span-1-duplicate",
            "start_time_ns": 1_700_000_000_010_000_000,
            "end_time_ns": 1_700_000_000_110_000_000,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-rollup",
                "gen_ai.client.hook.event": "UserPromptSubmit",
                "gen_ai.client.generation_id": "gen-1",
                "gen_ai.request.model": "claude-4.6-opus",
            },
        },
        {
            "name": "UserPromptSubmit",
            "traceId": "trace-1",
            "spanId": "span-1-duplicate-2",
            "start_time_ns": 1_700_000_000_020_000_000,
            "end_time_ns": 1_700_000_000_120_000_000,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-rollup",
                "gen_ai.client.hook.event": "UserPromptSubmit",
                "gen_ai.client.generation_id": "gen-1",
                "gen_ai.request.model": "claude-4.6-opus",
            },
        },
        {
            "name": "PreToolUse",
            "traceId": "trace-1",
            "spanId": "span-2",
            "start_time_ns": 1_700_000_001_000_000_000,
            "end_time_ns": 1_700_000_001_250_000_000,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-rollup",
                "gen_ai.client.tool_name": "Read",
            },
        },
        {
            "name": "PostToolUseFailure",
            "traceId": "trace-1",
            "spanId": "span-3",
            "start_time_ns": 1_700_000_002_000_000_000,
            "end_time_ns": 1_700_000_002_050_000_000,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-rollup",
                "gen_ai.client.hook.event": "PostToolUseFailure",
                "gen_ai.client.tool_name": "Read",
                "gen_ai.client.tool_use_id": "tool-failed-1",
            },
        },
        {
            "name": "PostToolUseFailure",
            "traceId": "trace-1",
            "spanId": "span-3-duplicate",
            "start_time_ns": 1_700_000_002_000_000_000,
            "end_time_ns": 1_700_000_002_050_000_000,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-rollup",
                "gen_ai.client.hook.event": "PostToolUseFailure",
                "gen_ai.client.tool_name": "Read",
                "gen_ai.client.tool_use_id": "tool-failed-1",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(span) for span in spans) + "\n", encoding="utf-8")


def test_rebuild_rollups_from_canonical_tables(tmp_path):
    db = tmp_path / "reflect.db"
    spans = tmp_path / "spans.jsonl"
    _write_spans(spans)

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        ingest_local_spans_file(conn, file_path=spans)
        normalize_pending_raw_events(conn)

        result = rebuild_rollups(conn)
        second = rebuild_rollups(conn)

        assert result == {"session_rollups": 1, "daily_rollups": 1, "tool_rollups": 1}
        assert second == result
        session = conn.execute(
            """
            SELECT agent, prompt_count, tool_call_count, error_count, input_tokens, output_tokens
            FROM session_rollups
            WHERE session_id = 'sess-rollup'
            """
        ).fetchone()
        assert session == ("claude", 1, 3, 1, 100, 50)
        day = conn.execute("SELECT session_count, prompt_count, tool_call_count, error_count FROM daily_rollups").fetchone()
        assert day == (1, 1, 3, 1)
        tool = conn.execute(
            "SELECT tool_name, call_count, success_count, error_count, total_duration_ms FROM tool_rollups"
        ).fetchone()
        assert tool == ("Read", 2, 1, 1, 300)
    finally:
        conn.close()


def test_rebuild_rollups_uses_valid_end_time_when_start_is_epoch(tmp_path):
    db = tmp_path / "reflect.db"
    spans = tmp_path / "spans.jsonl"
    _write_spans(spans)

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        ingest_local_spans_file(conn, file_path=spans)
        normalize_pending_raw_events(conn)
        conn.execute(
            """
            UPDATE sessions
            SET started_at = '1970-01-01T00:00:00+00:00',
                ended_at = '2026-03-25T06:40:50+00:00'
            WHERE id = 'sess-rollup'
            """
        )

        rebuild_rollups(conn)

        assert conn.execute(
            "SELECT started_at FROM session_rollups WHERE session_id = 'sess-rollup'"
        ).fetchone()[0] == "2026-03-25T06:40:50+00:00"
        assert conn.execute("SELECT day FROM daily_rollups").fetchone()[0] == "2026-03-25"
    finally:
        conn.close()
