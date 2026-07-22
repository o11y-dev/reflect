from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from click.testing import CliRunner

from reflect.core import _prepare_usage_db, main
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite
from reflect.usage import UsageService

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def _open_db(path):
    conn = connect_sqlite(path)
    migrate(conn)
    conn.execute(
        """
        INSERT INTO agents(id, name, kind, raw_json, created_at, updated_at)
        VALUES ('codex', 'codex', 'cli', '{}', ?, ?)
        """,
        (NOW.isoformat(), NOW.isoformat()),
    )
    return conn


def _seed_session(conn, session_id: str = "session-current", *, started_at: datetime = NOW) -> None:
    ended_at = started_at + timedelta(minutes=5)
    conn.execute(
        """
        INSERT INTO workspaces(
          id, root_path, path_hash, label, source_key, confidence,
          raw_json, created_at, updated_at
        ) VALUES ('workspace', '/work/reflect', 'workspace-hash', 'reflect', 'test', 1, '{}', ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        """
        INSERT INTO sessions(
          id, agent_id, workspace_id, started_at, ended_at, status, title,
          failure_count, recovered_failure_count, input_tokens, output_tokens,
          cache_creation_tokens, cache_read_tokens, reasoning_tokens,
          estimated_cost_usd, created_at, updated_at
        ) VALUES (?, 'codex', 'workspace', ?, ?, 'ok', 'Usage test', 1, 1,
                  1000, 250, 100, 400, 50, 1.25, ?, ?)
        """,
        (session_id, started_at.isoformat(), ended_at.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        """
        INSERT INTO session_rollups(
          session_id, agent, started_at, ended_at, duration_ms, prompt_count,
          tool_call_count, error_count, input_tokens, output_tokens,
          cache_read_tokens, cache_write_tokens, total_cost, updated_at
        ) VALUES (?, 'codex', ?, ?, 300000, 2, 1, 1, 1000, 250, 400, 100, 1.25, ?)
        """,
        (session_id, started_at.isoformat(), ended_at.isoformat(), NOW.isoformat()),
    )
    conn.executemany(
        """
        INSERT INTO steps(
          id, session_id, seq, type, started_at, ended_at, duration_ms,
          status, summary, raw_attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 10, 'ok', ?, '{}', ?, ?)
        """,
        [
            (
                f"{session_id}-step",
                session_id,
                1,
                "llm_call",
                started_at.isoformat(),
                ended_at.isoformat(),
                "generation",
                NOW.isoformat(),
                NOW.isoformat(),
            ),
            (
                f"{session_id}-subagent",
                session_id,
                2,
                "subagent_start",
                started_at.isoformat(),
                ended_at.isoformat(),
                "SubagentStart",
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        ],
    )
    conn.execute(
        """
        INSERT INTO llm_calls(
          id, step_id, session_id, provider, request_model, response_model,
          input_tokens, output_tokens, estimated_cost_usd,
          raw_attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, 'openai', 'gpt-5', 'gpt-5', 1000, 250, 1.25, '{}', ?, ?)
        """,
        (f"{session_id}-llm", f"{session_id}-step", session_id, NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        """
        INSERT INTO tool_calls(
          id, step_id, session_id, tool_name, status, raw_attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, 'exec_command', 'ok', '{}', ?, ?)
        """,
        (f"{session_id}-tool", f"{session_id}-step", session_id, NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        """
        INSERT INTO mcp_calls(
          id, step_id, session_id, server_name, tool_name, status,
          raw_attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, 'browser', 'open', 'ok', '{}', ?, ?)
        """,
        (f"{session_id}-mcp", f"{session_id}-step", session_id, NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()


def test_current_session_usage_uses_runtime_session_id(tmp_path):
    conn = _open_db(tmp_path / "reflect.db")
    try:
        _seed_session(conn)
        report = UsageService(
            conn,
            environ={"CODEX_THREAD_ID": "session-current"},
            cwd=tmp_path,
            now=NOW,
        ).report()
    finally:
        conn.close()

    assert report.resolution == "environment:CODEX_THREAD_ID"
    assert report.session is not None and report.session.id == "session-current"
    assert report.totals.model_dump() == {
        "sessions": 1,
        "prompts": 2,
        "llm_calls": 1,
        "tool_calls": 1,
        "mcp_calls": 1,
        "subagent_launches": 1,
        "failures": 1,
        "recovered_failures": 1,
        "duration_ms": 300000,
        "input_tokens": 1000,
        "output_tokens": 250,
        "cache_creation_tokens": 100,
        "cache_read_tokens": 400,
        "reasoning_tokens": 50,
        "estimated_cost_usd": 1.25,
    }
    assert report.models[0].name == "gpt-5"
    assert report.tools[0].name == "exec_command"


def test_current_session_usage_labels_workspace_fallback(tmp_path):
    conn = _open_db(tmp_path / "reflect.db")
    try:
        _seed_session(conn)
        report = UsageService(
            conn,
            environ={"CODEX_THREAD_ID": "not-flushed"},
            cwd=tmp_path / "outside",
            now=NOW,
        ).report()
    finally:
        conn.close()

    assert report.resolution == "inferred_agent"
    assert report.session is not None and report.session.id == "session-current"
    assert report.limitations and "not present" in report.limitations[0]


def test_current_session_usage_prefers_matching_workspace(tmp_path):
    conn = _open_db(tmp_path / "reflect.db")
    try:
        _seed_session(conn)
        report = UsageService(
            conn,
            environ={"CODEX_THREAD_ID": "not-flushed"},
            cwd=tmp_path,
            now=NOW,
        )
        conn.execute("UPDATE workspaces SET root_path = ? WHERE id = 'workspace'", (str(tmp_path),))
        conn.commit()
        resolved = report.report()
    finally:
        conn.close()

    assert resolved.resolution == "inferred_workspace"
    assert resolved.session is not None and resolved.session.id == "session-current"


def test_global_usage_is_not_limited_to_500_sessions(tmp_path):
    conn = _open_db(tmp_path / "reflect.db")
    started_at = NOW - timedelta(hours=1)
    session_rows = []
    rollup_rows = []
    for index in range(505):
        session_id = f"session-{index:03d}"
        session_rows.append(
            (
                session_id,
                started_at.isoformat(),
                10,
                2,
                0.01,
                NOW.isoformat(),
                NOW.isoformat(),
            )
        )
        rollup_rows.append(
            (
                session_id,
                started_at.isoformat(),
                1,
                10,
                2,
                0.01,
                NOW.isoformat(),
            )
        )
    conn.executemany(
        """
        INSERT INTO sessions(
          id, agent_id, started_at, status, input_tokens, output_tokens,
          estimated_cost_usd, created_at, updated_at
        ) VALUES (?, 'codex', ?, 'ok', ?, ?, ?, ?, ?)
        """,
        session_rows,
    )
    conn.executemany(
        """
        INSERT INTO session_rollups(
          session_id, agent, started_at, duration_ms, prompt_count,
          tool_call_count, error_count, input_tokens, output_tokens,
          cache_read_tokens, cache_write_tokens, total_cost, updated_at
        ) VALUES (?, 'codex', ?, 1000, ?, 0, 0, ?, ?, 0, 0, ?, ?)
        """,
        rollup_rows,
    )
    old_started_at = NOW - timedelta(days=90)
    conn.execute(
        """
        INSERT INTO sessions(
          id, agent_id, started_at, status, input_tokens, output_tokens,
          estimated_cost_usd, created_at, updated_at
        ) VALUES ('session-old', 'codex', ?, 'ok', 99, 9, 0.09, ?, ?)
        """,
        (old_started_at.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        """
        INSERT INTO session_rollups(
          session_id, agent, started_at, duration_ms, prompt_count,
          tool_call_count, error_count, input_tokens, output_tokens,
          cache_read_tokens, cache_write_tokens, total_cost, updated_at
        ) VALUES ('session-old', 'codex', ?, 1000, 1, 0, 0, 99, 9, 0, 0, 0.09, ?)
        """,
        (old_started_at.isoformat(), NOW.isoformat()),
    )
    conn.commit()
    try:
        service = UsageService(conn, environ={}, cwd=tmp_path, now=NOW)
        report = service.report(
            global_scope=True,
            period="week",
        )
        all_time = service.report(global_scope=True, period="all")
    finally:
        conn.close()

    assert report.totals.sessions == 505
    assert report.totals.prompts == 505
    assert report.totals.input_tokens == 5050
    assert report.totals.estimated_cost_usd == 5.05
    assert all_time.totals.sessions == 506


def test_usage_cli_emits_json(tmp_path, monkeypatch):
    db_path = tmp_path / "reflect.db"
    conn = _open_db(db_path)
    try:
        _seed_session(conn)
    finally:
        conn.close()
    monkeypatch.setattr("reflect.core._prepare_usage_db", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(
        main,
        ["usage", "--session", "session-current", "--json", "--db-path", str(db_path)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scope"] == "session"
    assert payload["session"]["id"] == "session-current"
    assert payload["totals"]["estimated_cost_usd"] == 1.25


def test_usage_refresh_skips_native_discovery_when_store_has_sessions(tmp_path, monkeypatch):
    db_path = tmp_path / "reflect.db"
    conn = _open_db(db_path)
    try:
        _seed_session(conn)
    finally:
        conn.close()
    monkeypatch.setattr(
        "reflect.core._discover_rich_session_files",
        lambda: (_ for _ in ()).throw(AssertionError("native discovery should be skipped")),
    )

    _prepare_usage_db(db_path, otlp_traces=None, include_native_sessions=False)


def test_usage_cli_rejects_conflicting_scopes():
    result = CliRunner().invoke(main, ["usage", "--session", "session", "--global"])

    assert result.exit_code == 2
    assert "cannot be used together" in result.output
