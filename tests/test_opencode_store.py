from __future__ import annotations

import json
import sqlite3

from reflect.conversation_adapters import DEFAULT_CONVERSATION_ADAPTERS
from reflect.opencode_store import OpenCodeSessionStore
from reflect.parsing import _discover_rich_session_files, _iter_opencode_session_spans
from reflect.store.ingest import ingest_native_session_file
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.sqlite import connect_sqlite


def _write_opencode_store(path, *, directory="/work/repo", tool_name="bash"):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE session (
              id TEXT PRIMARY KEY,
              parent_id TEXT,
              directory TEXT NOT NULL,
              title TEXT NOT NULL,
              time_created INTEGER NOT NULL,
              time_updated INTEGER NOT NULL
            );
            CREATE TABLE message (
              id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              time_created INTEGER NOT NULL,
              data TEXT NOT NULL
            );
            CREATE TABLE part (
              id TEXT PRIMARY KEY,
              message_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              time_created INTEGER NOT NULL,
              data TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
            (
                "open-session-1",
                "parent-session",
                directory,
                "Repair telemetry",
                1_780_000_000_000,
                1_780_000_003_000,
            ),
        )
        messages = [
            (
                "message-user",
                "open-session-1",
                1_780_000_000_100,
                {"role": "user", "agent": "build"},
            ),
            (
                "message-assistant",
                "open-session-1",
                1_780_000_001_000,
                {
                    "role": "assistant",
                    "agent": "build",
                    "modelID": "claude-sonnet",
                    "providerID": "litellm",
                },
            ),
        ]
        conn.executemany(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            [(row[0], row[1], row[2], json.dumps(row[3])) for row in messages],
        )
        parts = [
            (
                "part-prompt",
                "message-user",
                "open-session-1",
                1_780_000_000_101,
                {"type": "text", "text": "Fix the telemetry adapter"},
            ),
            (
                "part-response",
                "message-assistant",
                "open-session-1",
                1_780_000_001_100,
                {"type": "text", "text": "Implemented the adapter"},
            ),
            (
                "part-tool",
                "message-assistant",
                "open-session-1",
                1_780_000_001_200,
                {
                    "type": "tool",
                    "tool": tool_name,
                    "callID": "tool-call-1",
                    "state": {
                        "status": "completed",
                        "input": {"command": "pytest -q"},
                        "output": "1 passed",
                        "time": {
                            "start": 1_780_000_001_200,
                            "end": 1_780_000_002_200,
                        },
                    },
                },
            ),
            (
                "part-finish",
                "message-assistant",
                "open-session-1",
                1_780_000_002_300,
                {
                    "type": "step-finish",
                    "tokens": {
                        "input": 10,
                        "output": 5,
                        "reasoning": 2,
                        "cache": {"read": 20, "write": 3},
                    },
                    "cost": 0.25,
                },
            ),
        ]
        conn.executemany(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            [(row[0], row[1], row[2], row[3], json.dumps(row[4])) for row in parts],
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_opencode_store_exposes_typed_session_records(tmp_path):
    source = _write_opencode_store(tmp_path / "opencode.db")

    session = OpenCodeSessionStore(source).load("open-session-1")[0]

    assert session.directory == "/work/repo"
    assert session.parent_id == "parent-session"
    assert [message.role for message in session.messages] == ["user", "assistant"]
    assert session.messages[1].model == "claude-sonnet"
    assert [part.type for part in session.messages[1].parts] == [
        "text",
        "tool",
        "step-finish",
    ]


def test_opencode_store_loads_records_after_composite_cursor(tmp_path):
    source = _write_opencode_store(tmp_path / "opencode.db")
    conn = sqlite3.connect(source)
    try:
        conn.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
            (
                "open-session-2",
                None,
                "/work/repo",
                "Same timestamp",
                1_780_000_000_500,
                1_780_000_003_000,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    sessions = OpenCodeSessionStore(source).load(
        after_updated_ms=1_780_000_003_000,
        after_session_id="open-session-1",
    )

    assert [session.id for session in sessions] == ["open-session-2"]


def test_opencode_native_spans_preserve_usage_tools_and_lineage(tmp_path):
    source = _write_opencode_store(tmp_path / "opencode.db")

    spans = list(_iter_opencode_session_spans(source))

    prompt = next(span for span in spans if span["attributes"].get("gen_ai.client.prompt"))
    response = next(span for span in spans if span["attributes"].get("gen_ai.client.output"))
    tool_end = next(
        span
        for span in spans
        if span["attributes"].get("gen_ai.client.hook.event") == "PostToolUse"
    )
    assert prompt["attributes"]["gen_ai.client.name"] == "opencode"
    assert response["attributes"]["gen_ai.request.model"] == "claude-sonnet"
    assert response["attributes"]["gen_ai.usage.input_tokens"] == 10
    assert response["attributes"]["gen_ai.usage.cache_read.input_tokens"] == 20
    assert response["attributes"]["gen_ai.client.parent_session_id"] == "parent-session"
    assert tool_end["attributes"]["gen_ai.tool.call.id"] == "tool-call-1"
    assert tool_end["end_time_ns"] - tool_end["start_time_ns"] == 1_000_000_000


def test_opencode_mcp_identity_uses_configured_server_prefix(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "opencode.json").write_text(
        json.dumps({"mcp": {"reflect": {"type": "local"}}}),
        encoding="utf-8",
    )
    source = _write_opencode_store(
        tmp_path / "opencode.db",
        directory=str(workspace),
        tool_name="reflect_reflect_context",
    )

    tool = next(
        span
        for span in _iter_opencode_session_spans(source)
        if span["attributes"].get("gen_ai.client.hook.event") == "PreToolUse"
    )

    assert tool["attributes"]["gen_ai.client.mcp_server"] == "reflect"
    assert tool["attributes"]["gen_ai.client.mcp_tool"] == "reflect_context"
    assert tool["attributes"]["gen_ai.client.tool_use_id"] == "tool-call-1"


def test_opencode_native_store_normalizes_to_canonical_usage(tmp_path):
    source = _write_opencode_store(tmp_path / "opencode.db")
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        conn.execute(
            """
            INSERT INTO agents(id, name, raw_json, created_at, updated_at)
            VALUES ('claude', 'claude', '{}', '2026-01-01', '2026-01-01')
            """
        )
        conn.execute(
            """
            INSERT INTO sessions(
              id, agent_id, started_at, status, source_kind, source_ref,
              created_at, updated_at
            ) VALUES (
              'open-session-1', 'claude', '2026-01-01', 'unknown',
              'otlp_traces_json', '/tmp/traces.json', '2026-01-01', '2026-01-01'
            )
            """
        )

        result = ingest_native_session_file(conn, file_path=source, agent="opencode")
        normalize_pending_raw_events(conn)

        assert result == {
            "inserted": 6,
            "skipped": 0,
            "unchanged": 0,
            "scanned_sessions": 1,
        }
        session = conn.execute(
            """
            SELECT a.name, s.input_tokens, s.output_tokens,
                   s.cache_creation_tokens, s.cache_read_tokens, s.reasoning_tokens
            FROM sessions s
            JOIN agents a ON a.id = s.agent_id
            WHERE s.id = 'open-session-1'
            """
        ).fetchone()
        assert tuple(session) == ("opencode", 10, 5, 3, 20, 2)
        tool = conn.execute(
            """
            SELECT tool_name, status, duration_ms
            FROM tool_calls
            WHERE session_id = 'open-session-1'
            """
        ).fetchone()
        assert tuple(tool) == ("bash", "ok", 1000)
    finally:
        conn.close()


def test_opencode_ingestion_detects_sessions_appended_in_sqlite_wal(tmp_path):
    source = _write_opencode_store(tmp_path / "opencode.db")
    reflect_conn = connect_sqlite(tmp_path / "reflect.db")
    writer = sqlite3.connect(source)
    try:
        migrate(reflect_conn)
        writer.execute("PRAGMA journal_mode = WAL")
        first = ingest_native_session_file(
            reflect_conn,
            file_path=source,
            agent="opencode",
            skip_unchanged=True,
        )
        writer.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
            (
                "open-session-wal",
                None,
                "/work/repo",
                "Appended session",
                1_780_000_004_000,
                1_780_000_005_000,
            ),
        )
        writer.commit()

        second = ingest_native_session_file(
            reflect_conn,
            file_path=source,
            agent="opencode",
            skip_unchanged=True,
        )

        third = ingest_native_session_file(
            reflect_conn,
            file_path=source,
            agent="opencode",
            skip_unchanged=True,
        )

        assert first == {
            "inserted": 6,
            "skipped": 0,
            "unchanged": 0,
            "scanned_sessions": 1,
        }
        assert second == {
            "inserted": 2,
            "skipped": 0,
            "unchanged": 0,
            "scanned_sessions": 1,
        }
        assert third == {
            "inserted": 0,
            "skipped": 0,
            "unchanged": 1,
            "scanned_sessions": 0,
        }
    finally:
        writer.close()
        reflect_conn.close()


def test_opencode_decoder_change_replays_from_the_start(tmp_path):
    source = _write_opencode_store(tmp_path / "opencode.db")
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        first = ingest_native_session_file(
            conn,
            file_path=source,
            agent="opencode",
            skip_unchanged=True,
        )
        conn.execute(
            "UPDATE source_ingestion_state SET decoder_version = decoder_version - 1"
        )
        conn.commit()

        replay = ingest_native_session_file(
            conn,
            file_path=source,
            agent="opencode",
            skip_unchanged=True,
        )

        assert first["scanned_sessions"] == 1
        assert replay == {
            "inserted": 0,
            "skipped": 6,
            "unchanged": 0,
            "scanned_sessions": 1,
        }
    finally:
        conn.close()


def test_opencode_ingestion_rescans_only_an_updated_session(tmp_path):
    source = _write_opencode_store(tmp_path / "opencode.db")
    reflect_conn = connect_sqlite(tmp_path / "reflect.db")
    writer = sqlite3.connect(source)
    try:
        migrate(reflect_conn)
        ingest_native_session_file(
            reflect_conn,
            file_path=source,
            agent="opencode",
            skip_unchanged=True,
        )
        writer.execute(
            "UPDATE session SET time_updated = ? WHERE id = ?",
            (1_780_000_004_000, "open-session-1"),
        )
        writer.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            (
                "message-follow-up",
                "open-session-1",
                1_780_000_003_500,
                json.dumps({"role": "user", "agent": "build"}),
            ),
        )
        writer.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            (
                "part-follow-up",
                "message-follow-up",
                "open-session-1",
                1_780_000_003_501,
                json.dumps({"type": "text", "text": "Verify the fix"}),
            ),
        )
        writer.commit()

        result = ingest_native_session_file(
            reflect_conn,
            file_path=source,
            agent="opencode",
            skip_unchanged=True,
        )
        cursor = reflect_conn.execute(
            "SELECT record_cursor_time, record_cursor_id FROM source_ingestion_state"
        ).fetchone()

        assert result["scanned_sessions"] == 1
        assert result["inserted"] == 2
        assert tuple(cursor) == (1_780_000_004_000, "open-session-1")
    finally:
        writer.close()
        reflect_conn.close()


def test_opencode_conversation_adapter_reads_selected_session(tmp_path):
    source = _write_opencode_store(tmp_path / "opencode.db")

    transcript = DEFAULT_CONVERSATION_ADAPTERS.load(
        "open-session-1",
        "opencode",
        source,
    )

    assert [event.type for event in transcript.events] == [
        "prompt",
        "response",
        "tool_call",
        "tool_result",
    ]
    response = transcript.events[1]
    assert response.model == "claude-sonnet"
    assert response.input_tokens == 10
    assert response.output_tokens == 5
    assert transcript.events[-1].duration_ms == 1000


def test_opencode_store_discovery_honors_xdg_data_home(tmp_path, monkeypatch):
    source = _write_opencode_store(tmp_path / "opencode" / "opencode.db")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    assert ("opencode", source) in _discover_rich_session_files()
