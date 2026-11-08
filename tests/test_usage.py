from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner

from reflect.core import main
from reflect.preparation_pipeline import prepare_usage_db
from reflect.store.migrate import load_migrations, migrate
from reflect.store.sqlite import connect_sqlite
from reflect.usage import UsageService

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def _write_codex_session(path, session_id: str, *, timestamp: str) -> None:
    path.write_text(
        json.dumps(
            {
                "timestamp": timestamp,
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": str(path.parent),
                    "model": "gpt-5.6-sol",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


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
          input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, reasoning_output_tokens, estimated_cost_usd,
          raw_attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, 'openai', 'gpt-5', 'gpt-5', 1000, 250, 100, 400, 50, 1.25, '{}', ?, ?)
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
        INSERT INTO tool_calls(
          id, step_id, session_id, tool_name, tool_type, status,
          raw_attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, 'mcp__browser__open', 'mcp', 'ok', '{}', ?, ?)
        """,
        (f"{session_id}-mcp", f"{session_id}-step", session_id, NOW.isoformat(), NOW.isoformat()),
    )
    mcp_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info('mcp_calls')")
    }
    if "step_id" in mcp_columns:
        conn.execute(
            """
            INSERT INTO mcp_calls(
              id, step_id, session_id, tool_call_id, server_name, tool_name,
              status, raw_attrs_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'browser', 'open', 'ok', '{}', ?, ?)
            """,
            (
                f"{session_id}-mcp-extension",
                f"{session_id}-step",
                session_id,
                f"{session_id}-mcp",
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO mcp_calls(
              tool_call_id, server_name, tool_name, created_at, updated_at
            ) VALUES (?, 'browser', 'open', ?, ?)
            """,
            (f"{session_id}-mcp", NOW.isoformat(), NOW.isoformat()),
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
    assert report.token_provenance.exact_sessions == 1
    assert report.token_provenance.estimated_sessions == 0
    assert report.token_provenance.unavailable_sessions == 0
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


def test_usage_labels_estimated_and_unavailable_token_sources(tmp_path):
    conn = _open_db(tmp_path / "reflect.db")
    try:
        _seed_session(conn, "session-estimated")
        conn.execute(
            """
            INSERT INTO agents(id, name, kind, raw_json, created_at, updated_at)
            VALUES ('cursor', 'cursor', 'desktop', '{}', ?, ?)
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
        conn.execute(
            """
            UPDATE sessions
            SET token_provenance = 'estimated_cursor_transcript'
            WHERE id = 'session-estimated'
            """
        )
        conn.execute(
            """
            UPDATE llm_calls
            SET input_tokens = 0, output_tokens = 0,
                cache_creation_input_tokens = 0, cache_read_input_tokens = 0,
                reasoning_output_tokens = 0
            WHERE session_id = 'session-estimated'
            """
        )
        wrong_agent = UsageService(conn, environ={}, cwd=tmp_path, now=NOW).report(
            session_id="session-estimated"
        )
        conn.execute(
            "UPDATE sessions SET agent_id = 'cursor' WHERE id = 'session-estimated'"
        )
        estimated = UsageService(conn, environ={}, cwd=tmp_path, now=NOW).report(
            session_id="session-estimated"
        )

        conn.execute(
            """
            UPDATE sessions
            SET input_tokens = 0, output_tokens = 0, cache_creation_tokens = 0,
                cache_read_tokens = 0, reasoning_tokens = 0,
                token_provenance = 'unavailable'
            WHERE id = 'session-estimated'
            """
        )
        unavailable = UsageService(conn, environ={}, cwd=tmp_path, now=NOW).report(
            session_id="session-estimated"
        )
    finally:
        conn.close()

    assert wrong_agent.token_provenance.estimated_sessions == 0
    assert estimated.token_provenance.estimated_sessions == 1
    assert estimated.token_provenance.sources == ["estimated_cursor_transcript"]
    assert any("transcript-derived" in item for item in estimated.limitations)
    assert unavailable.token_provenance.unavailable_sessions == 1
    assert unavailable.token_provenance.proxy_order == [
        "captured_output_size",
        "llm_call_count",
        "tool_call_count",
        "session_duration",
    ]
    assert any("inference-only proxies" in item for item in unavailable.limitations)


def test_identical_token_counts_do_not_deduplicate_distinct_requests(tmp_path):
    conn = _open_db(tmp_path / "reflect.db")
    try:
        _seed_session(conn)
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, summary,
              raw_attrs_json, created_at, updated_at
            ) VALUES ('duplicate-step', 'session-current', 3, 'llm_call', ?, 'ok',
                      'generation', '{}', ?, ?)
            """,
            (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
        conn.execute(
            """
            INSERT INTO llm_calls(
              id, step_id, session_id, provider, request_model, response_model,
              input_tokens, output_tokens, estimated_cost_usd,
              raw_attrs_json, created_at, updated_at
            ) VALUES ('duplicate-llm', 'duplicate-step', 'session-current', 'openai',
                      'gpt-5', 'gpt-5', 1000, 250, 0, '{}', ?, ?)
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
        report = UsageService(
            conn,
            environ={"CODEX_THREAD_ID": "session-current"},
            cwd=tmp_path,
            now=NOW,
        ).report()
    finally:
        conn.close()

    assert report.models[0].count == 2
    assert report.models[0].input_tokens == report.totals.input_tokens == 2000
    assert report.models[0].output_tokens == report.totals.output_tokens == 500


def test_usage_labels_token_bearing_zero_cost_sessions_as_unpriced(tmp_path):
    conn = _open_db(tmp_path / "reflect.db")
    try:
        _seed_session(conn)
        conn.execute(
            "UPDATE sessions SET estimated_cost_usd = 0 WHERE id = 'session-current'"
        )
        report = UsageService(
            conn,
            environ={"CODEX_THREAD_ID": "session-current"},
            cwd=tmp_path,
            now=NOW,
        ).report()
    finally:
        conn.close()

    assert any("Missing rates or models do not imply free usage" in limitation for limitation in report.limitations)


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


def test_current_session_usage_checks_all_runtime_ids_before_falling_back(tmp_path):
    conn = _open_db(tmp_path / "reflect.db")
    try:
        _seed_session(conn, "session-current")
        _seed_session(conn, "session-newer", started_at=NOW + timedelta(hours=1))
        report = UsageService(
            conn,
            environ={
                "REFLECT_SESSION_ID": "not-flushed",
                "CODEX_THREAD_ID": "session-current",
            },
            cwd=tmp_path / "outside",
            now=NOW,
        ).report()
    finally:
        conn.close()

    assert report.resolution == "environment:CODEX_THREAD_ID"
    assert report.session is not None and report.session.id == "session-current"


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
    for sid, stamp, inp, out, cost in conn.execute("SELECT id,started_at,input_tokens,output_tokens,estimated_cost_usd FROM sessions").fetchall():
        conn.execute("INSERT INTO steps(id,session_id,seq,type,started_at,status,raw_attrs_json,created_at,updated_at) VALUES(?,?,1,'llm_call',?,'ok','{}',?,?)", (sid+'-step',sid,stamp,stamp,stamp))
        conn.execute("INSERT INTO llm_calls(id,step_id,session_id,input_tokens,output_tokens,estimated_cost_usd,raw_attrs_json,created_at,updated_at) VALUES(?,?,?,?,?,?,'{}',?,?)", (sid+'-call',sid+'-step',sid,inp,out,cost,stamp,stamp))
        conn.execute("UPDATE steps SET summary='UserPromptSubmit' WHERE id=?", (sid+'-step',))
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
    assert report.totals.estimated_cost_usd == pytest.approx(5.05)
    assert all_time.totals.sessions == 506


def test_usage_cli_emits_json(tmp_path, monkeypatch):
    db_path = tmp_path / "reflect.db"
    conn = _open_db(db_path)
    try:
        _seed_session(conn)
    finally:
        conn.close()
    monkeypatch.setattr("reflect.core.prepare_usage_db", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(
        main,
        ["usage", "--session", "session-current", "--json", "--db-path", str(db_path)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scope"] == "session"
    assert payload["session"]["id"] == "session-current"
    assert payload["totals"]["estimated_cost_usd"] == 1.25


def test_refresh_command_explicitly_prepares_a_missing_snapshot(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "reflect.db"
    native_session = tmp_path / "rollout-session.jsonl"
    _write_codex_session(
        native_session,
        "session-refresh",
        timestamp="2026-07-30T10:00:00Z",
    )
    monkeypatch.setattr("reflect.core.default_otlp_traces", lambda: None)
    monkeypatch.setattr("reflect.core._default_spans_dir", lambda: tmp_path / "spans")
    monkeypatch.setattr(
        "reflect.preparation_pipeline._discover_rich_session_files",
        lambda: [("codex", native_session)],
    )

    result = CliRunner().invoke(
        main,
        [
            "refresh",
            "--json",
            "--db-path",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["sessions"] == 1
    assert "Opening the local telemetry store" in result.stderr
    assert db_path.exists()


def test_usage_refresh_skips_native_discovery_when_store_has_sessions(tmp_path, monkeypatch):
    db_path = tmp_path / "reflect.db"
    conn = _open_db(db_path)
    try:
        _seed_session(conn)
    finally:
        conn.close()
    monkeypatch.setattr(
        "reflect.preparation_pipeline._discover_rich_session_files",
        lambda: (_ for _ in ()).throw(AssertionError("native discovery should be skipped")),
    )

    prepare_usage_db(db_path, otlp_traces=None, include_native_sessions=False)


def test_usage_refresh_ingests_only_the_runtime_native_session(tmp_path, monkeypatch):
    db_path = tmp_path / "reflect.db"
    old_session = tmp_path / "rollout-session-old.jsonl"
    runtime_session = tmp_path / "rollout-session-runtime.jsonl"
    prefix_collision = tmp_path / "rollout-session-runtime-extra.jsonl"
    _write_codex_session(
        old_session,
        "session-old",
        timestamp="2026-07-18T10:00:00Z",
    )
    _write_codex_session(
        runtime_session,
        "session-runtime",
        timestamp="2026-07-19T10:00:00Z",
    )
    _write_codex_session(
        prefix_collision,
        "session-prefix-collision",
        timestamp="2026-07-19T09:00:00Z",
    )
    monkeypatch.setattr(
        "reflect.preparation_pipeline._discover_rich_session_files",
        lambda: [
            ("codex", old_session),
            ("codex", runtime_session),
            ("codex", prefix_collision),
        ],
    )
    monkeypatch.setattr("reflect.core.default_otlp_traces", lambda: None)
    monkeypatch.setenv("REFLECT_SESSION_ID", "session-stale")
    monkeypatch.setenv("CODEX_THREAD_ID", "session-runtime")

    result = CliRunner().invoke(
        main,
        ["usage", "--refresh", "--json", "--db-path", str(db_path)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["session"]["id"] == "session-runtime"
    assert "Reading local agent sessions..." in result.stderr
    assert "Normalizing usage telemetry..." in result.stderr
    assert "Usage refresh complete." in result.stderr
    conn = connect_sqlite(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT id FROM sessions").fetchone()[0] == "session-runtime"
    finally:
        conn.close()


def test_usage_requires_explicit_refresh_for_pending_rollup_rebuild(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "reflect.db"
    conn = connect_sqlite(db_path)
    try:
        for migration in load_migrations():
            if migration.version > 18:
                break
            conn.executescript(migration.sql)
            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
                VALUES (?, ?, '2026-07-26T00:00:00+00:00')
                """,
                (migration.version, migration.name),
            )
        conn.execute(
            """
            INSERT INTO agents(id, name, kind, raw_json, created_at, updated_at)
            VALUES ('codex', 'codex', 'cli', '{}', ?, ?)
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
        _seed_session(conn, "session-current")
        conn.execute(
            """
            INSERT INTO agents(id, name, kind, raw_json, created_at, updated_at)
            VALUES ('codex-desktop', 'Codex Desktop', 'native', '{}', ?, ?)
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
        conn.execute(
            """
            INSERT INTO sessions(
              id, agent_id, started_at, status, source_kind, source_ref,
              created_at, updated_at
            ) VALUES (
              'session-noisy', 'codex-desktop', ?, 'ok', 'otlp_traces_json',
              '/tmp/otel-traces.json', ?, ?
            )
            """,
            (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, raw_attrs_json,
              created_at, updated_at
            ) VALUES (
              'step-noisy', 'session-noisy', 1, 'FramedRead::poll_next', ?,
              'ok', '{"service.name":"Codex Desktop","code.module.name":"h2::codec"}',
              ?, ?
            )
            """,
            (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
        conn.execute("DELETE FROM daily_rollups")
        conn.execute("DELETE FROM tool_rollups")
        conn.execute(
            """
            INSERT INTO daily_rollups(
              day, agent, session_count, prompt_count, updated_at
            ) VALUES ('2026-07-19', 'Codex Desktop', 1, 99, ?)
            """,
            (NOW.isoformat(),),
        )
        conn.execute(
            """
            INSERT INTO tool_rollups(
              tool_name, agent, call_count, success_count, updated_at
            ) VALUES ('stale_tool', 'Codex Desktop', 99, 99, ?)
            """,
            (NOW.isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()
    conn = connect_sqlite(db_path)
    try:
        assert migrate(conn) == [19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48]
    finally:
        conn.close()
    monkeypatch.setattr("reflect.core.default_otlp_traces", lambda: None)
    monkeypatch.setattr("reflect.preparation_pipeline._discover_rich_session_files", lambda: [])

    read_result = CliRunner().invoke(
        main,
        ["usage", "--session", "session-current", "--json", "--db-path", str(db_path)],
    )

    assert read_result.exit_code == 1
    assert "usage rollups are stale" in read_result.output
    conn = connect_sqlite(db_path)
    try:
        assert conn.execute(
            """
            SELECT COUNT(*) FROM maintenance_tasks
            WHERE task = 'rebuild_rollups_after_codex_desktop_otel'
            """
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT tool_name FROM tool_rollups"
        ).fetchone()[0] == "stale_tool"
    finally:
        conn.close()

    refresh_result = CliRunner().invoke(
        main,
        [
            "usage",
            "--session",
            "session-current",
            "--refresh",
            "--json",
            "--db-path",
            str(db_path),
        ],
    )

    assert refresh_result.exit_code == 0, refresh_result.output
    conn = connect_sqlite(db_path)
    try:
        assert conn.execute(
            "SELECT agent FROM daily_rollups"
        ).fetchone()[0] == "codex"
        assert conn.execute(
            "SELECT tool_name FROM tool_rollups"
        ).fetchone()[0] == "exec_command"
        assert conn.execute(
            """
            SELECT COUNT(*) FROM maintenance_tasks
            WHERE task = 'rebuild_rollups_after_codex_desktop_otel'
            """
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_usage_rejects_structurally_incomplete_rollups(tmp_path):
    db_path = tmp_path / "reflect.db"
    conn = _open_db(db_path)
    try:
        _seed_session(conn)
        conn.execute(
            "DELETE FROM session_rollups WHERE session_id = 'session-current'"
        )
        conn.commit()
    finally:
        conn.close()

    result = CliRunner().invoke(
        main,
        [
            "usage",
            "--session",
            "session-current",
            "--json",
            "--db-path",
            str(db_path),
        ],
    )

    assert result.exit_code == 1
    assert "usage rollups are stale: 1 session(s) are missing rollups" in result.output


def test_usage_session_ignores_an_unrelated_missing_rollup(tmp_path):
    db_path = tmp_path / "reflect.db"
    conn = _open_db(db_path)
    try:
        _seed_session(conn, "session-current")
        _seed_session(conn, "session-unrelated")
        conn.execute(
            "DELETE FROM session_rollups WHERE session_id = 'session-unrelated'"
        )
        conn.commit()
    finally:
        conn.close()

    result = CliRunner().invoke(
        main,
        [
            "usage",
            "--session",
            "session-current",
            "--json",
            "--db-path",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["session"]["id"] == "session-current"
    assert payload["totals"]["sessions"] == 1


def test_usage_cli_rejects_conflicting_scopes():
    result = CliRunner().invoke(main, ["usage", "--session", "session", "--global"])

    assert result.exit_code == 2
    assert "cannot be used together" in result.output


def test_usage_cli_uses_period_option(tmp_path):
    db_path = tmp_path / "reflect.db"
    conn = _open_db(db_path)
    try:
        _seed_session(conn)
    finally:
        conn.close()

    runner = CliRunner()
    current = runner.invoke(
        main,
        ["usage", "--global", "--period", "all", "--json", "--db-path", str(db_path)],
    )
    assert current.exit_code == 0, current.output
    assert json.loads(current.stdout)["period"] == "all"


def test_period_activity_counts_events_not_lifetime_session_rollups(tmp_path, monkeypatch):
    from reflect.dashboard_queries import build_dashboard_payload

    conn = _open_db(tmp_path / "reflect.db")
    try:
        _seed_session(conn, "straddling", started_at=NOW-timedelta(days=2))
        _seed_session(conn, "recent", started_at=NOW-timedelta(hours=1))
        conn.execute("UPDATE steps SET seq=seq+10 WHERE session_id='recent'")
        for table in ("steps", "tool_calls", "llm_calls"):
            conn.execute(f"UPDATE {table} SET session_id='straddling' WHERE session_id='recent'")
        conn.execute("DELETE FROM sessions WHERE id='recent'")
        conn.execute("UPDATE sessions SET ended_at=? WHERE id='straddling'", ((NOW-timedelta(minutes=30)).isoformat(),))
        conn.execute("UPDATE steps SET summary='UserPromptSubmit' WHERE id IN ('straddling-step','recent-step')")
        conn.execute("UPDATE tool_calls SET status='error',input_hash='same-command' WHERE id='straddling-tool'")
        conn.execute("UPDATE tool_calls SET input_hash='same-command' WHERE id='recent-tool'")
        conn.commit()
        report = UsageService(conn, now=NOW).report(global_scope=True, period="day")
        assert report.totals.sessions == report.totals.prompts == report.totals.llm_calls == 1
        assert report.totals.tool_calls == 2
        assert report.totals.mcp_calls == report.totals.subagent_launches == 1
        assert report.totals.failures == report.totals.recovered_failures == 0
        assert report.totals.duration_ms == 84_600_000
        assert sum(item.count for item in report.tools) == 2
        assert report.totals.input_tokens == 1000
        # All-time remains compatible with the existing lifetime rollups.
        assert UsageService(conn, now=NOW).report(global_scope=True, period="all").totals.prompts == 2

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW

        monkeypatch.setattr("reflect.dashboard_queries.datetime", FrozenDatetime)
        payload = build_dashboard_payload(tmp_path / "reflect.db", range_name="24h",
            lazy_all_tabs=True, include_comparison=False)
        overview = payload["sqlite"]["overview"]
        assert overview["prompt_count"] == 1
        assert overview["tool_call_count"] == 2
        assert overview["failure_count"] == 0
        assert payload["sqlite"]["tabs"]["usage"]["mcp_calls"] == 1
        assert payload["sqlite"]["tabs"]["usage"]["subagent_launches"] == 1
    finally:
        conn.close()


def test_window_activity_covering_migration_preserves_deduplicated_facts(tmp_path, monkeypatch):
    previous = [migration for migration in load_migrations() if migration.version <= 41]
    with monkeypatch.context() as patcher:
        patcher.setattr("reflect.store.migrate.load_migrations", lambda: previous)
        conn = _open_db(tmp_path / "reflect.db")
    try:
        _seed_session(conn, "recent", started_at=NOW - timedelta(hours=1))
        conn.execute("""UPDATE steps SET summary='UserPromptSubmit',
            raw_attrs_json='{"gen_ai.client.turn_id":"same-turn"}' WHERE id='recent-step'""")
        conn.execute("UPDATE steps SET raw_attrs_json=json_set(raw_attrs_json,'$.output',?)",
                     ("large captured output " * 10_000,))
        conn.execute("""INSERT INTO steps(id,session_id,seq,type,started_at,ended_at,status,
            summary,raw_attrs_json,created_at,updated_at)
            SELECT 'duplicate-prompt',session_id,3,type,started_at,ended_at,status,
                summary,raw_attrs_json,created_at,updated_at FROM steps WHERE id='recent-step'""")
        conn.execute("""INSERT INTO steps(id,session_id,seq,type,started_at,ended_at,status,
            summary,raw_attrs_json,created_at,updated_at)
            SELECT 'inferred-launch',session_id,4,type,started_at,ended_at,status,
                summary,'{"reflect.time.provenance":"inferred"}',created_at,updated_at
                FROM steps WHERE id='recent-subagent'""")
        conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,status,
            raw_attrs_json,created_at,updated_at)
            SELECT 'subagent-tool',id,session_id,'spawn_agent','ok','{}',created_at,updated_at
                FROM steps WHERE id='recent-subagent'""")
        window = {"date_from": (NOW - timedelta(days=1)).isoformat(), "date_to": NOW.isoformat()}
        service = UsageService(conn, now=NOW)
        before = service.activity_totals(session_ids={"recent"}, **window)
        assert [getattr(before, key) for key in (
            "sessions", "prompts", "tool_calls", "failures", "recovered_failures",
            "duration_ms", "llm_calls", "mcp_calls", "subagent_launches",
        )] == [1, 1, 3, 0, 0, 300_000, 1, 1, 1]
        retained = conn.execute("SELECT * FROM steps ORDER BY id").fetchall()
        scope_sql = service._scope_cte("s.id=?", **window, materialize=True) + "SELECT * FROM scoped_steps ORDER BY id"
        scope_params = ["recent", window["date_from"], window["date_to"]]
        scope_before = conn.execute(scope_sql, scope_params).fetchall()
        steps_root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='steps'").fetchone()[0]

        def table_reads():
            bytecode = conn.execute("EXPLAIN " + scope_sql, scope_params).fetchall()
            table_cursors = {row[2] for row in bytecode
                             if row[1] == "OpenRead" and row[3] == steps_root and row[4] == 0}
            return [row for row in bytecode if row[1] == "Column" and row[2] in table_cursors]

        assert table_reads(), "The old index must reproduce the wide step-table reads"
        assert migrate(conn) == [42, 43, 44, 45, 46, 47, 48]
        plan = conn.execute("EXPLAIN QUERY PLAN " + scope_sql, scope_params).fetchall()
        assert any("COVERING INDEX idx_steps_measurement_times" in row[-1] for row in plan)
        assert table_reads() == []
        assert conn.execute(scope_sql, scope_params).fetchall() == scope_before
        assert conn.execute("SELECT * FROM steps ORDER BY id").fetchall() == retained
        assert service.activity_totals(session_ids={"recent"}, **window) == before
        assert migrate(conn) == []
        assert service.activity_totals(session_ids={"recent"}, **window) == before
    finally:
        conn.close()


@pytest.mark.parametrize("state", ["ready", "preparing", "historical_pending"])
def test_cli_and_mcp_preserve_usage_and_withhold_pending_insights(tmp_path, monkeypatch, state):
    from reflect import mcp
    from reflect.store.pending_refresh import PendingRefreshState
    from reflect.store.sqlite import mark_snapshot

    db = tmp_path / "reflect.db"
    conn = _open_db(db)
    try:
        _seed_session(conn)
        if state != "ready":
            pending = PendingRefreshState(conn)
            pending.mark({"session-current"}, phases=("graph", "improvements"))
            if state == "historical_pending":
                pending.defer({"session-current"}, phases=("graph", "improvements"))
        mark_snapshot(conn, ready=True)
    finally:
        conn.close()
    reads = []

    def insights(*args, **kwargs):
        reads.append(True)
        return {"session-current": [{"id": "retained-insight"}]}

    monkeypatch.setattr("reflect.improvements.session_insights.read_session_insights", insights)
    monkeypatch.setenv("REFLECT_DB_PATH", str(db))
    result = CliRunner().invoke(main, ["usage", "--session", "session-current", "--json", "--db-path", str(db)])
    assert result.exit_code == 0, result.output
    reports = [json.loads(result.output), mcp.reflect_usage(session_id="session-current")]
    for report in reports:
        assert report["analysis_state"] == state
        assert report["totals"]["input_tokens"] == 1000
        assert report["totals"]["estimated_cost_usd"] == 1.25
        assert bool(report["insights"]) is (state == "ready")
        assert report["analysis_coverage"]["improvements"]["pending_sessions"] == (state != "ready")
        if state != "ready":
            assert any("Derived analysis" in limitation for limitation in report["limitations"])
    assert len(reads) == (2 if state == "ready" else 0)
