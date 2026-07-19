"""Tests for Click CLI argument parsing and invocation."""

import io
import json
import os
import shutil
import sqlite3
import subprocess
import tomllib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest
from click.testing import CliRunner
from conftest import make_span, wrap_otlp

import reflect.core as core
from reflect.core import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def otlp_file(tmp_path):
    spans = [make_span("UserPromptSubmit", input_tokens=100, output_tokens=50)]
    p = tmp_path / "traces.json"
    p.write_text(wrap_otlp(spans) + "\n")
    return p


@pytest.fixture(autouse=True)
def _disable_pricing_network(monkeypatch):
    from reflect import pricing as pricing_mod

    def _raise(*_args, **_kwargs):
        raise RuntimeError("pricing network disabled in tests")

    monkeypatch.setattr(pricing_mod, "_fetch_json_url", _raise)


class TestHelp:
    def test_help(self, runner):
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output

    def test_setup_help(self, runner):
        result = runner.invoke(main, ["setup", "--help"])
        assert result.exit_code == 0
        assert "setup" in result.output.lower() or "Usage" in result.output

    def test_doctor_help(self, runner):
        result = runner.invoke(main, ["doctor", "--help"])
        assert result.exit_code == 0
        assert "doctor" in result.output.lower() or "Usage" in result.output

    def test_update_help(self, runner):
        result = runner.invoke(main, ["update", "--help"])
        assert result.exit_code == 0
        assert "update" in result.output.lower() or "Usage" in result.output

    def test_memory_help(self, runner):
        result = runner.invoke(main, ["memory", "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output

    def test_skills_help(self, runner):
        result = runner.invoke(main, ["skills", "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output

    def test_usage_help(self, runner):
        result = runner.invoke(main, ["usage", "--help"])
        assert result.exit_code == 0
        assert "--refresh" in result.output
        assert "--global" in result.output

    def test_db_doctor_help(self, runner):
        result = runner.invoke(main, ["db", "doctor", "--help"])
        assert result.exit_code == 0
        assert "doctor" in result.output.lower() or "Usage" in result.output

    def test_db_doctor_reports_healthy_store(self, runner, tmp_path):
        db_path = tmp_path / "reflect.db"
        migrate_result = runner.invoke(main, ["db", "migrate", "--db-path", str(db_path)])
        assert migrate_result.exit_code == 0

        result = runner.invoke(main, ["db", "doctor", "--db-path", str(db_path)])

        assert result.exit_code == 0
        assert "SQLite store health: ok" in result.output
        assert "Foreign keys: ok" in result.output

    def test_db_doctor_fails_for_pending_migrations(self, runner, tmp_path):
        db_path = tmp_path / "reflect.db"

        result = runner.invoke(main, ["db", "doctor", "--db-path", str(db_path)])

        assert result.exit_code != 0
        assert "Pending migrations: 1, 2, 3, 4, 5, 6" in result.output
        assert "SQLite store health: needs attention" in result.output


    def test_skill_drift_checks_all_packaged_reflect_helpers(self, tmp_path):
        global_root = tmp_path / "skills"
        global_root.mkdir()
        source = core._bundled_reflect_skill_dir()
        assert source is not None
        shutil.copytree(source, global_root / "reflect")

        issue = core._detect_skill_drift(
            [{"name": "OpenAI Codex CLI", "detected": True, "global_path": str(global_root)}]
        )

        assert issue is not None
        assert "OpenAI Codex CLI/reflect-skills" in issue["summary"]
        assert "OpenAI Codex CLI/reflect-usage" in issue["summary"]

    def test_ingest_requires_one_source(self, runner, tmp_path):
        db_path = tmp_path / "reflect.db"

        result = runner.invoke(main, ["ingest", "--db-path", str(db_path)])

        assert result.exit_code != 0
        assert "Pass exactly one of --otlp or --spans-file" in result.output

    def test_ingest_spans_file(self, runner, tmp_path):
        db_path = tmp_path / "reflect.db"
        spans_file = tmp_path / "spans.jsonl"
        spans_file.write_text(json.dumps({
            "name": "PreToolUse",
            "traceId": "trace-1",
            "spanId": "span-1",
            "start_time_ns": 100,
            "end_time_ns": 200,
            "attributes": {"gen_ai.client.session_id": "sess-cli"},
        }) + "\n")

        result = runner.invoke(main, [
            "ingest",
            "--db-path", str(db_path),
            "--spans-file", str(spans_file),
        ])

        assert result.exit_code == 0
        assert "inserted=1" in result.output

    def test_ingest_spans_file_refreshes_cost_aliases(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        db_path = tmp_path / "reflect.db"
        spans_file = tmp_path / "spans.jsonl"
        spans_file.write_text(json.dumps(make_span(
            "UserPromptSubmit",
            model="claude-4-sonnet-20250514",
            session="sess-cost-ingest",
            input_tokens=100,
            output_tokens=50,
        )) + "\n")

        with patch.dict(os.environ, {"REFLECT_HOME": str(reflect_home)}, clear=False):
            result = runner.invoke(main, [
                "ingest",
                "--db-path", str(db_path),
                "--spans-file", str(spans_file),
            ])

        assert result.exit_code == 0
        alias_payload = json.loads((reflect_home / "config" / "model-aliases.json").read_text())
        assert alias_payload["aliases"]["claude-4-sonnet-20250514"] == "claude-sonnet-4"
        with sqlite3.connect(db_path) as conn:
            cost = conn.execute("SELECT estimated_cost_usd FROM llm_calls").fetchone()[0]
            session_cost = conn.execute(
                "SELECT estimated_cost_usd FROM sessions WHERE id = 'sess-cost-ingest'"
            ).fetchone()[0]
        assert cost > 0
        assert session_cost == cost

    def test_reprice_preserves_session_tokens_when_llm_rows_have_no_usage(self, tmp_path):
        from reflect.store.migrate import migrate
        from reflect.store.sqlite import connect_sqlite

        db_path = tmp_path / "reflect.db"
        conn = connect_sqlite(db_path)
        try:
            migrate(conn)
            now = "2026-01-01T00:00:00+00:00"
            conn.execute(
                """
                INSERT INTO agents(id, name, raw_json, created_at, updated_at)
                VALUES ('agent-cursor', 'cursor', '{}', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO sessions(
                  id, agent_id, started_at, ended_at, status, input_tokens, output_tokens,
                  created_at, updated_at
                ) VALUES ('sess-cursor-estimate', 'agent-cursor', ?, ?, 'ok', 123, 456, ?, ?)
                """,
                (now, now, now, now),
            )
            conn.execute(
                """
                INSERT INTO steps(
                  id, session_id, seq, type, started_at, ended_at, status, summary,
                  raw_attrs_json, created_at, updated_at
                ) VALUES ('step-zero-llm', 'sess-cursor-estimate', 0, 'llm_call', ?, ?, 'ok', 'Stop',
                  '{"gen_ai.request.model":"gpt-4o-mini"}', ?, ?)
                """,
                (now, now, now, now),
            )
            conn.execute(
                """
                INSERT INTO llm_calls(
                  id, step_id, session_id, request_model, operation_name,
                  input_tokens, output_tokens, raw_attrs_json, created_at, updated_at
                ) VALUES ('llm-zero-usage', 'step-zero-llm', 'sess-cursor-estimate', 'gpt-4o-mini', 'Stop',
                  0, 0, '{"gen_ai.request.model":"gpt-4o-mini"}', ?, ?)
                """,
                (now, now),
            )
            conn.commit()

            core._reprice_sql_store(conn)

            tokens = conn.execute(
                """
                SELECT input_tokens, output_tokens, estimated_cost_usd
                FROM sessions
                WHERE id = 'sess-cursor-estimate'
                """
            ).fetchone()
        finally:
            conn.close()

        assert tokens[0] == 123
        assert tokens[1] == 456
        assert tokens[2] > 0

    def test_reprice_uses_cursor_parent_hook_model_for_native_subagent_tokens(self, tmp_path):
        from reflect.store.migrate import migrate
        from reflect.store.sqlite import connect_sqlite

        db_path = tmp_path / "reflect.db"
        conn = connect_sqlite(db_path)
        try:
            migrate(conn)
            now = "2026-01-01T00:00:00+00:00"
            conn.execute(
                """
                INSERT INTO agents(id, name, raw_json, created_at, updated_at)
                VALUES ('agent-cursor', 'cursor', '{}', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO sessions(id, agent_id, started_at, ended_at, status, created_at, updated_at)
                VALUES ('parent-cursor-session', 'agent-cursor', ?, ?, 'ok', ?, ?)
                """,
                (now, now, now, now),
            )
            conn.execute(
                """
                INSERT INTO steps(
                  id, session_id, seq, type, started_at, ended_at, status, summary,
                  raw_attrs_json, created_at, updated_at
                ) VALUES ('step-parent-model', 'parent-cursor-session', 0, 'llm_call', ?, ?, 'ok', 'Stop',
                  '{"gen_ai.request.model":"gpt-4o-mini"}', ?, ?)
                """,
                (now, now, now, now),
            )
            conn.execute(
                """
                INSERT INTO sessions(
                  id, agent_id, started_at, ended_at, status, input_tokens, output_tokens,
                  source_kind, source_ref, created_at, updated_at
                ) VALUES (
                  'child-cursor-subagent', 'agent-cursor', ?, ?, 'ok', 1000, 500,
                  'native_session',
                  'native_session:cursor:/tmp/project/agent-transcripts/parent-cursor-session/subagents/child-cursor-subagent.jsonl',
                  ?, ?
                )
                """,
                (now, now, now, now),
            )
            conn.commit()

            core._reprice_sql_store(conn)

            child = conn.execute(
                """
                SELECT input_tokens, output_tokens, estimated_cost_usd
                FROM sessions
                WHERE id = 'child-cursor-subagent'
                """
            ).fetchone()
        finally:
            conn.close()

        assert child[0] == 1000
        assert child[1] == 500
        assert child[2] > 0

    def test_reprice_can_scope_updates_to_changed_sessions(self, tmp_path):
        from reflect.store.migrate import migrate
        from reflect.store.sqlite import connect_sqlite

        db_path = tmp_path / "reflect.db"
        conn = connect_sqlite(db_path)
        try:
            migrate(conn)
            now = "2026-01-01T00:00:00+00:00"
            conn.execute(
                """
                INSERT INTO agents(id, name, raw_json, created_at, updated_at)
                VALUES ('agent-scope', 'codex', '{}', ?, ?)
                """,
                (now, now),
            )
            for index, session_id in enumerate(("sess-changed", "sess-untouched")):
                step_id = f"step-{index}"
                conn.execute(
                    """
                    INSERT INTO sessions(
                      id, agent_id, started_at, ended_at, status, created_at, updated_at
                    ) VALUES (?, 'agent-scope', ?, ?, 'ok', ?, ?)
                    """,
                    (session_id, now, now, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO steps(
                      id, session_id, seq, type, started_at, ended_at, status,
                      raw_attrs_json, created_at, updated_at
                    ) VALUES (?, ?, 0, 'llm_call', ?, ?, 'ok',
                      '{"gen_ai.request.model":"gpt-4o-mini"}', ?, ?)
                    """,
                    (step_id, session_id, now, now, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO llm_calls(
                      id, step_id, session_id, request_model, input_tokens, output_tokens,
                      raw_attrs_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'gpt-4o-mini', 100, 50, '{}', ?, ?)
                    """,
                    (f"llm-{index}", step_id, session_id, now, now),
                )
            conn.commit()

            core._reprice_sql_store(conn, session_ids={"sess-changed"})

            costs = dict(conn.execute(
                "SELECT session_id, estimated_cost_usd FROM llm_calls ORDER BY session_id"
            ).fetchall())
            session_costs = dict(conn.execute(
                "SELECT id, estimated_cost_usd FROM sessions ORDER BY id"
            ).fetchall())
        finally:
            conn.close()

        assert costs["sess-changed"] > 0
        assert costs["sess-untouched"] == 0
        assert session_costs["sess-changed"] > 0
        assert session_costs["sess-untouched"] == 0

    def test_db_ingest_spans_alias(self, runner, tmp_path):
        db_path = tmp_path / "reflect.db"
        spans_file = tmp_path / "spans.jsonl"
        spans_file.write_text(json.dumps({
            "name": "SessionStart",
            "traceId": "trace-2",
            "spanId": "span-2",
            "start_time_ns": 100,
            "end_time_ns": 200,
            "attributes": {"session.id": "sess-db-cli"},
        }) + "\n")

        result = runner.invoke(main, [
            "db", "ingest-spans",
            "--db-path", str(db_path),
            "--spans-file", str(spans_file),
        ])

        assert result.exit_code == 0
        assert "inserted=1" in result.output

    def test_db_normalize(self, runner, tmp_path):
        db_path = tmp_path / "reflect.db"
        spans_file = tmp_path / "spans.jsonl"
        spans_file.write_text(json.dumps({
            "name": "UserPromptSubmit",
            "traceId": "trace-3",
            "spanId": "span-3",
            "start_time_ns": 100,
            "end_time_ns": 200,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-normalize",
                "gen_ai.request.model": "claude-4.6-opus",
            },
        }) + "\n")
        ingest_result = runner.invoke(main, [
            "ingest",
            "--db-path", str(db_path),
            "--spans-file", str(spans_file),
        ])
        assert ingest_result.exit_code == 0

        result = runner.invoke(main, ["db", "normalize", "--db-path", str(db_path)])

        assert result.exit_code == 0
        assert "processed=0" in result.output

    def test_memory_sync_lists_and_searches_instructions(self, runner, tmp_path):
        db_path = tmp_path / "reflect.db"
        workspace = tmp_path / "repo"
        home = tmp_path / "home"
        workspace.mkdir()
        home.mkdir()
        (workspace / "AGENTS.md").write_text("# project\n", encoding="utf-8")
        (workspace / ".github" / "instructions").mkdir(parents=True)
        (workspace / ".github" / "instructions" / "workflow.instructions.md").write_text("# workflow\n", encoding="utf-8")
        (home / ".cursor" / "plans").mkdir(parents=True)
        (home / ".cursor" / "plans" / "workflow.plan.md").write_text("# cursor plan\n", encoding="utf-8")
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "CLAUDE.md").write_text("# user\n", encoding="utf-8")

        with patch.dict(os.environ, {"HOME": str(home)}, clear=False):
            result = runner.invoke(
                main,
                ["memory", "sync", str(workspace), "--db-path", str(db_path)],
            )

        assert result.exit_code == 0
        assert "Synced memories" in result.output
        assert "discovered=4" in result.output

        list_result = runner.invoke(
            main,
            ["memory", "list", str(workspace), "--db-path", str(db_path), "--json"],
        )
        assert list_result.exit_code == 0
        assert "AGENTS.md" in list_result.output

        search_result = runner.invoke(
            main,
            ["memory", "search", "workflow", str(workspace), "--db-path", str(db_path), "--json"],
        )
        assert search_result.exit_code == 0
        assert "workflow.instructions.md" in search_result.output

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT scope, type, source, provider FROM memories ORDER BY type, scope"
            ).fetchall()
        finally:
            conn.close()
        assert ("project", "agent_instruction", "filesystem_instruction_scan", "local_sqlite") in rows
        assert ("path", "copilot_instruction", "filesystem_instruction_scan", "local_sqlite") in rows
        assert ("user", "claude_memory", "filesystem_instruction_scan", "local_sqlite") in rows
        assert ("user", "cursor_plan", "filesystem_instruction_scan", "local_sqlite") in rows

    def test_db_rebuild_graph(self, runner, tmp_path):
        db_path = tmp_path / "reflect.db"
        spans_file = tmp_path / "spans.jsonl"
        spans_file.write_text(json.dumps({
            "name": "PreToolUse",
            "traceId": "trace-4",
            "spanId": "span-4",
            "start_time_ns": 100,
            "end_time_ns": 200,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-graph-cli",
                "gen_ai.client.tool_name": "Read",
            },
        }) + "\n")
        assert runner.invoke(main, [
            "ingest",
            "--db-path", str(db_path),
            "--spans-file", str(spans_file),
        ]).exit_code == 0
        assert runner.invoke(main, ["db", "normalize", "--db-path", str(db_path)]).exit_code == 0

        result = runner.invoke(main, ["db", "rebuild-graph", "--db-path", str(db_path)])

        assert result.exit_code == 0
        assert "Rebuilt graph" in result.output
        assert "nodes=" in result.output

    def test_db_rebuild_rollups(self, runner, tmp_path):
        db_path = tmp_path / "reflect.db"
        spans_file = tmp_path / "spans.jsonl"
        spans_file.write_text(json.dumps({
            "name": "PreToolUse",
            "traceId": "trace-5",
            "spanId": "span-5",
            "start_time_ns": 100,
            "end_time_ns": 200,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-rollup-cli",
                "gen_ai.client.tool_name": "Read",
            },
        }) + "\n")
        assert runner.invoke(main, [
            "ingest",
            "--db-path", str(db_path),
            "--spans-file", str(spans_file),
        ]).exit_code == 0
        assert runner.invoke(main, ["db", "normalize", "--db-path", str(db_path)]).exit_code == 0

        result = runner.invoke(main, ["db", "rebuild-rollups", "--db-path", str(db_path)])

        assert result.exit_code == 0
        assert "Rebuilt rollups" in result.output
        assert "sessions=1" in result.output


class TestBrowserMode:
    def test_foreground_opens_browser_report(self, runner, otlp_file, tmp_path):
        with patch("reflect.core._start_publish_server") as mock_server, \
             patch("reflect.core._render_terminal") as mock_render:
            db_path = tmp_path / "reflect.db"
            result = runner.invoke(main, [
                "--foreground",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
                "--db-path", str(db_path),
            ])
            assert result.exit_code == 0
            mock_server.assert_called_once()
            mock_render.assert_not_called()
            assert mock_server.call_args.kwargs["db_path"] == db_path

    @pytest.mark.parametrize("flag", ["--terminal", "--no-terminal", "--sql-only"])
    def test_removed_legacy_flags_fail(self, runner, flag):
        result = runner.invoke(main, [flag])
        assert result.exit_code != 0
        assert "No such option" in result.output


class TestBrowserReportCommandSurface:
    def test_report_subcommand_is_removed(self, runner):
        result = runner.invoke(main, ["report", "--help"])
        assert result.exit_code != 0
        assert "No such command" in result.output

    def test_foreground_command_starts_server(self, runner, otlp_file, tmp_path):
        with patch("reflect.core._start_publish_server") as mock_server:
            db_path = tmp_path / "reflect.db"
            result = runner.invoke(main, [
                "--foreground",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
                "--db-path", str(db_path),
            ])
        assert result.exit_code == 0
        assert "REFLECT" in result.output
        assert mock_server.call_args.kwargs["db_path"] == db_path
        assert mock_server.call_args.kwargs["sql_only"] is False
        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM session_rollups").fetchone()[0] > 0
        finally:
            conn.close()

    def test_existing_snapshot_refreshes_in_background(self, runner, otlp_file, tmp_path):
        db_path = tmp_path / "reflect.db"
        core._prepare_sql_report_db(
            db_path,
            otlp_traces=otlp_file,
            include_native_sessions=False,
        )
        with patch("reflect.core._start_publish_server") as mock_server, \
             patch("reflect.core._prepare_sql_report_db", return_value={"refreshed": True}) as mock_prepare:
            result = runner.invoke(main, [
                "--foreground",
                "--otlp-traces", str(otlp_file),
                "--db-path", str(db_path),
            ])
            assert result.exit_code == 0
            assert "refreshing telemetry in the background" in result.output
            mock_prepare.assert_not_called()
            worker = mock_server.call_args.kwargs["preparation_worker"]
            assert worker is not None
            assert worker.start() is True
            assert worker.wait(timeout=2) is True
            mock_prepare.assert_called_once()

    def test_foreground_report_ingests_inferred_otlp_logs(self, runner, tmp_path):
        otlp_file = tmp_path / "otel-traces.json"
        otlp_file.write_text(json.dumps({"resourceSpans": []}) + "\n", encoding="utf-8")
        (tmp_path / "otel-logs.json").write_text(json.dumps({
            "resourceLogs": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "codex_cli_rs"}},
                    ],
                },
                "scopeLogs": [{
                    "logRecords": [{
                        "timeUnixNano": "1000",
                        "attributes": [
                            {"key": "event.name", "value": {"stringValue": "codex.user_prompt"}},
                            {"key": "event.timestamp", "value": {"stringValue": "2026-03-24T10:00:01Z"}},
                            {"key": "conversation.id", "value": {"stringValue": "codex-sql-log-session"}},
                            {"key": "model", "value": {"stringValue": "gpt-5.5"}},
                            {"key": "prompt", "value": {"stringValue": "[REDACTED]"}},
                        ],
                    }],
                }],
            }],
        }) + "\n", encoding="utf-8")

        with patch("reflect.core._start_publish_server"):
            db_path = tmp_path / "reflect.db"
            result = runner.invoke(main, [
                "--foreground",
                "--otlp-traces", str(otlp_file),
                "--db-path", str(db_path),
            ])

        assert result.exit_code == 0
        assert "REFLECT" in result.output
        assert "Otlp Traces" in result.output
        assert "Otlp Logs" in result.output
        assert "codex" in result.output
        assert "1 native / 0 hook" in result.output
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                """
                SELECT a.name, s.id, sr.prompt_count
                FROM sessions s
                JOIN agents a ON a.id = s.agent_id
                JOIN session_rollups sr ON sr.session_id = s.id
                """
            ).fetchone()
            assert row == ("codex", "codex-sql-log-session", 1)
        finally:
            conn.close()

    def test_report_summary_breaks_native_sessions_down_by_agent(self, runner, tmp_path):
        otlp_file = tmp_path / "otel-traces.json"
        otlp_file.write_text(json.dumps({"resourceSpans": []}) + "\n", encoding="utf-8")
        cursor_file = tmp_path / "cursor-native-session.jsonl"
        cursor_file.write_text(
            "\n".join([
                json.dumps({
                    "role": "user",
                    "message": {"content": [{"type": "text", "text": "inspect native cursor"}]},
                }),
                json.dumps({
                    "role": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "I will inspect it."},
                            {
                                "type": "tool_use",
                                "name": "CallMcpTool",
                                "input": {"server": "jira", "toolName": "search"},
                            },
                        ],
                    },
                }),
            ])
            + "\n",
            encoding="utf-8",
        )

        with patch("reflect.core._start_publish_server"), \
             patch("reflect.core._default_otlp_traces", return_value=otlp_file), \
             patch("reflect.core._discover_rich_session_files", return_value=[("cursor", cursor_file)]):
            db_path = tmp_path / "reflect.db"
            result = runner.invoke(main, [
                "--foreground",
                "--db-path", str(db_path),
            ])

        assert result.exit_code == 0
        assert "Native Sessions" in result.output
        assert "cursor" in result.output
        assert "native /" in result.output
        assert "hook event(s)" in result.output

    def test_report_reprices_token_rows_with_session_model_hint(self, runner, tmp_path):
        session_id = "copilot-priced-session"
        model_hint = make_span(
            "PostToolUse",
            agent="copilot",
            model="gpt-4o",
            tool="view",
            session=session_id,
        )
        token_row = make_span(
            "SessionEnd",
            agent="copilot",
            model="gpt-4o",
            session=session_id,
            input_tokens=1000,
            output_tokens=100,
        )
        token_row["attributes"].pop("gen_ai.request.model", None)
        otlp_file = tmp_path / "copilot-traces.json"
        otlp_file.write_text(wrap_otlp([model_hint, token_row], agent="copilot") + "\n", encoding="utf-8")

        with patch("reflect.core._start_publish_server"):
            db_path = tmp_path / "reflect.db"
            result = runner.invoke(main, [
                "--foreground",
                "--otlp-traces", str(otlp_file),
                "--db-path", str(db_path),
            ])

        assert result.exit_code == 0
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                """
                SELECT response_model, estimated_cost_usd
                FROM llm_calls
                WHERE session_id = ?
                  AND input_tokens > 0
                """,
                (session_id,),
            ).fetchone()
            assert row[0] == "gpt-4o"
            assert row[1] > 0
            assert conn.execute(
                "SELECT total_cost FROM session_rollups WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0] > 0
        finally:
            conn.close()

    def test_report_with_output_saves_markdown(self, runner, otlp_file, tmp_path):
        with patch("reflect.core._start_publish_server"), \
             patch("reflect.core.render_report") as mock_report:
            mock_report.return_value = "# report"
            db_path = tmp_path / "reflect.db"
            result = runner.invoke(main, [
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
                "--db-path", str(db_path),
                "--output", str(tmp_path / "report.md"),
            ])
        assert result.exit_code == 0
        mock_report.assert_called_once()

    def test_deprecated_dashboard_artifact_remains_compatible_until_removal(
        self, runner, otlp_file, tmp_path
    ):
        artifact_path = tmp_path / "docs" / "reports" / "latest.json"
        with patch("reflect.core._start_publish_server"):
            db_path = tmp_path / "reflect.db"
            result = runner.invoke(main, [
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
                "--db-path", str(db_path),
                "--dashboard-artifact", str(artifact_path),
            ])
        assert result.exit_code == 0
        assert artifact_path.exists()
        assert "agents" in json.loads(artifact_path.read_text())


_FAKE_SKILLS = [
    {"name": "debug-loop", "description": "Iterative debug workflow", "content": "## Steps\n1. Do the thing"},
    {"name": "context-reset", "description": "Clear and re-establish scope", "content": "## Steps\n1. Reset"},
    {"name": "test-first", "description": "Write tests before fixing", "content": "## Steps\n1. Test"},
]
_R = lambda code, out, err="": type("R", (), {"returncode": code, "stdout": out, "stderr": err})()  # noqa: E731


class TestSkillsSubcommand:
    @pytest.fixture(autouse=True)
    def _isolated_skills_db(self, tmp_path):
        parameter = next(
            item for item in main.commands["skills"].params if item.name == "db_path"
        )
        original = parameter.default
        parameter.default = tmp_path / "skills-reflect.db"
        try:
            yield
        finally:
            parameter.default = original

    def _agent_fixture(self, skill_dest, fake_skills=None):
        return [{
            "name": "Claude Code",
            "detected": True,
            "global_path": str(skill_dest),
        }]

    def test_skills_stages_all_with_yes_without_installing(self, runner, otlp_file, tmp_path):
        skill_dest = tmp_path / "skills"
        db_path = tmp_path / "reflect.db"
        fake_output = json.dumps([_FAKE_SKILLS[0]])
        with patch("subprocess.run", return_value=_R(0, fake_output)), \
             patch("reflect.core._detect_agents", return_value=self._agent_fixture(skill_dest)):
            result = runner.invoke(main, [
                "skills", "--yes", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
                "--db-path", str(db_path),
            ])
        assert result.exit_code == 0
        assert not skill_dest.exists()
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                """
                SELECT status,
                       json_extract(content_json, '$.slug'),
                       json_extract(content_json, '$.source.kind'),
                       json_extract(content_json, '$.source.agent'),
                       json_extract(content_json, '$.suggested_artifact')
                FROM workflow_candidates
                """
            ).fetchone()
        finally:
            conn.close()
        assert row == ("pending", "debug-loop", "agent_authored", "claude", "skill")
        assert "Nothing was installed" in result.output

    def test_skills_passes_evidence_bundle_to_agent(self, runner, otlp_file, tmp_path):
        fake_output = json.dumps([_FAKE_SKILLS[0]])
        with patch("subprocess.run", return_value=_R(0, fake_output)) as mock_run, \
             patch("reflect.core._detect_agents", return_value=[]):
            result = runner.invoke(main, [
                "skills", "--yes", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])

        assert result.exit_code == 0
        prompt = mock_run.call_args[0][0][-1]
        assert "Evidence JSON (authoritative):" in prompt
        assert '"schema_version": 1' in prompt
        assert '"selection_policy"' in prompt
        assert '"sessions"' in prompt

    def test_skills_attaches_sql_graph_evidence_when_available(self, runner, otlp_file, tmp_path):
        fake_output = json.dumps([_FAKE_SKILLS[0]])
        sql_bundle = {
            "schema_version": 1,
            "selection_policy": {"session_limit": 12, "deep_context_limit": 0, "evidence_source": "sql"},
            "summary": {
                "total_sessions_seen": 1,
                "included_sessions": 1,
                "deep_context_sessions": 0,
                "average_quality_score": 72.0,
                "recurring_tool_flows": [],
                "recurring_shell_commands": [],
                "recurring_recovery_chains": [],
                "recurring_improvement_targets": [],
            },
            "sessions": [
                {
                    "id": "sess-default-001",
                    "short_id": "sess-def",
                    "rank": 1,
                    "signal_score": 10.0,
                    "refs": {"session": "session://sess-default-001", "telemetry": "telemetry://sess-default-001"},
                    "agent": "claude",
                    "model": "claude-sonnet",
                    "event_count": 4,
                    "quality_score": 72.0,
                    "goal_completed": True,
                    "recovered_failures": 0,
                    "token_usage": {"input": 100, "output": 50, "total": 150},
                    "score_signals": {"tool_uses": 2, "tool_failures": 0, "tool_loops": 0},
                    "tool_flow": ["Read", "Edit"],
                    "shell_cmds": [],
                    "prompts": ["Fix skills extraction"],
                    "error_recovery": [],
                    "improvement_targets": [],
                }
            ],
            "graph_evidence": {
                "source": "sql-graph",
                "scoped_session_count": 1,
                "recurring_patterns": [
                    {
                        "id": "graph-01",
                        "edge_kind": "used_skill",
                        "source": {"kind": "Session", "label": "Session"},
                        "target": {"kind": "Skill", "label": "reflect-skills"},
                        "count": 2,
                        "session_support": 1,
                        "session_ids": ["sess-default-001"],
                    }
                ],
                "skill_clusters": [],
                "subagent_clusters": [],
            },
        }
        with patch("subprocess.run", return_value=_R(0, fake_output)) as mock_run, \
             patch("reflect.core._detect_agents", return_value=[]), \
             patch("reflect.core._prepare_sql_report_db"), \
             patch("reflect.core._build_skill_evidence_bundle_from_sql", return_value=sql_bundle):
            result = runner.invoke(main, [
                "skills", "--yes", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
                "--db-path", str(tmp_path / "reflect.db"),
            ])

        assert result.exit_code == 0
        prompt = mock_run.call_args[0][0][-1]
        assert '"graph_evidence"' in prompt
        assert '"source": "sql-graph"' in prompt

    def test_skills_partial_selection_stages_only_selected(self, runner, otlp_file, tmp_path):
        """User selects only skill #1 from a list of 3."""
        skill_dest = tmp_path / "skills"
        db_path = tmp_path / "reflect.db"
        fake_output = json.dumps(_FAKE_SKILLS)
        with patch("subprocess.run", return_value=_R(0, fake_output)), \
             patch("reflect.core._detect_agents", return_value=self._agent_fixture(skill_dest)):
            # --yes is NOT passed; input "1" selects only the first skill, then "y" confirms
            result = runner.invoke(main, [
                "skills", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
                "--db-path", str(db_path),
            ], input="1\n")
        assert result.exit_code == 0
        assert not skill_dest.exists()
        conn = sqlite3.connect(db_path)
        try:
            slugs = {
                row[0]
                for row in conn.execute("SELECT json_extract(content_json, '$.slug') FROM workflow_candidates")
            }
        finally:
            conn.close()
        assert slugs == {"debug-loop"}

    def test_skills_does_not_select_or_write_agent_install_targets(self, runner, otlp_file, tmp_path):
        skill_output = json.dumps([_FAKE_SKILLS[0]])
        first_dest = tmp_path / "agent-one"
        second_dest = tmp_path / "agent-two"
        agents = [
            {"name": "Claude Code", "detected": True, "global_path": str(first_dest)},
            {"name": "Cursor", "detected": True, "global_path": str(second_dest)},
        ]
        with patch("subprocess.run", return_value=_R(0, skill_output)), \
             patch("reflect.core._detect_agents", return_value=agents), \
             patch("reflect.core._select_skills", return_value=[_FAKE_SKILLS[0]]), \
             patch("reflect.core._select_skill_install_agents") as selector:
            result = runner.invoke(main, [
                "skills", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
                "--db-path", str(tmp_path / "reflect.db"),
            ])

        assert result.exit_code == 0
        assert not selector.called
        assert not (first_dest / "debug-loop" / "SKILL.md").exists()
        assert not (second_dest / "debug-loop" / "SKILL.md").exists()

    def test_skills_gemini_uses_p_flag(self, runner, otlp_file, tmp_path):
        """gemini agent uses -p flag, not --print."""
        fake_output = json.dumps([_FAKE_SKILLS[0]])
        with patch("subprocess.run", return_value=_R(0, fake_output)) as mock_run, \
             patch("reflect.core._detect_agents", return_value=[]):
            runner.invoke(main, [
                "skills", "--yes", "--agent", "gemini",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gemini"
        assert "-p" in cmd
        assert "--print" not in cmd

    def test_skills_codex_uses_exec_subcommand(self, runner, otlp_file, tmp_path):
        """codex uses the exec subcommand, not the interactive CLI's unsupported --print flag."""
        fake_output = json.dumps([_FAKE_SKILLS[0]])
        with patch("subprocess.run", return_value=_R(0, fake_output)) as mock_run, \
             patch("reflect.core._detect_agents", return_value=[]):
            runner.invoke(main, [
                "skills", "--yes", "--agent", "codex",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        cmd = mock_run.call_args[0][0]
        assert cmd[:2] == ["codex", "exec"]
        assert "--print" not in cmd

    def test_skills_cursor_agent_uses_headless_trusted_ask_mode(self, runner, otlp_file, tmp_path):
        """cursor-agent uses print mode with trust and read-only ask mode for headless extraction."""
        fake_output = json.dumps([_FAKE_SKILLS[0]])
        with patch("subprocess.run", return_value=_R(0, fake_output)) as mock_run, \
             patch("reflect.core._detect_agents", return_value=[]):
            runner.invoke(main, [
                "skills", "--yes", "--agent", "cursor-agent",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "cursor-agent"
        assert "--print" in cmd
        assert "--trust" in cmd
        assert "--mode" in cmd
        assert "ask" in cmd

    def test_skills_auto_detection_skips_broken_cursor_agent(self, runner, otlp_file, tmp_path):
        fake_output = json.dumps([_FAKE_SKILLS[0]])

        def fake_which(binary):
            return f"/usr/bin/{binary}" if binary in {"cursor-agent", "codex"} else None

        def fake_run(cmd, *_args, **_kwargs):
            if cmd[:3] == ["cursor-agent", "status", "--format"]:
                return _R(139, "", "ERROR: SecItemCopyMatching failed -50")
            return _R(0, fake_output)

        with patch("subprocess.run", side_effect=fake_run) as mock_run, \
             patch("reflect.core._detect_agents", return_value=[]), \
             patch("reflect.core.shutil.which", side_effect=fake_which):
            result = runner.invoke(main, [
                "skills", "--yes",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])

        assert result.exit_code == 0
        extraction_cmd = mock_run.call_args[0][0]
        assert extraction_cmd[:2] == ["codex", "exec"]

    def test_skills_agent_failure_truncates_noisy_stderr(self, runner, otlp_file, tmp_path):
        noisy_error = "ERROR: SecItemCopyMatching failed -50\n" + ("minified-js" * 500)
        with patch("subprocess.run", return_value=_R(1, "", noisy_error)), \
             patch("reflect.core._detect_agents", return_value=[]):
            result = runner.invoke(main, [
                "skills", "--yes", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])

        assert result.exit_code != 0
        assert "truncated" in result.output
        assert len(result.output) < 2500

    def test_skills_copilot_uses_prompt_flag(self, runner, otlp_file, tmp_path):
        """copilot uses --prompt flag, not --print."""
        fake_output = json.dumps([_FAKE_SKILLS[0]])
        with patch("subprocess.run", return_value=_R(0, fake_output)) as mock_run, \
             patch("reflect.core._detect_agents", return_value=[]):
            runner.invoke(main, [
                "skills", "--yes", "--agent", "copilot",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "copilot"
        assert "--prompt" in cmd
        assert "--print" not in cmd

    def test_skills_opencode_uses_run_subcommand(self, runner, otlp_file, tmp_path):
        """opencode uses 'run' subcommand, not --print."""
        fake_output = json.dumps([_FAKE_SKILLS[0]])
        with patch("subprocess.run", return_value=_R(0, fake_output)) as mock_run, \
             patch("reflect.core._detect_agents", return_value=[]):
            runner.invoke(main, [
                "skills", "--yes", "--agent", "opencode",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "opencode"
        assert "run" in cmd
        assert "--print" not in cmd

    def test_skills_autodetect_picks_first_available(self, runner, otlp_file, tmp_path):
        """With no --agent, auto-detection uses the first CLI found by shutil.which."""
        fake_output = json.dumps([_FAKE_SKILLS[0]])
        with patch("subprocess.run", return_value=_R(0, fake_output)) as mock_run, \
             patch("reflect.core._detect_agents", return_value=[]), \
             patch("reflect.core.shutil.which", side_effect=lambda b: "/usr/bin/gemini" if b == "gemini" else None):
            runner.invoke(main, [
                "skills", "--yes",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gemini"
        assert "-p" in cmd

    def test_skills_no_agent_available_exits(self, runner, otlp_file, tmp_path):
        with patch("reflect.core.shutil.which", return_value=None):
            result = runner.invoke(main, [
                "skills",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        assert result.exit_code != 0

    def test_skills_agent_failure_exits_nonzero(self, runner, otlp_file, tmp_path):
        with patch("subprocess.run", return_value=_R(1, "", "error")), \
             patch("reflect.core._detect_agents", return_value=[]):
            result = runner.invoke(main, [
                "skills", "--yes", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        assert result.exit_code != 0

    def test_skills_bad_json_exits_nonzero(self, runner, otlp_file, tmp_path):
        with patch("subprocess.run", return_value=_R(0, "not json")), \
             patch("reflect.core._detect_agents", return_value=[]):
            result = runner.invoke(main, [
                "skills", "--yes", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        assert result.exit_code != 0

    def test_skills_strips_json_fences(self, runner, otlp_file, tmp_path):
        """Agent output wrapped in ```json fences is parsed correctly."""
        skill_dest = tmp_path / "skills"
        fenced_output = '```json\n' + json.dumps([_FAKE_SKILLS[0]]) + '\n```'
        with patch("subprocess.run", return_value=_R(0, fenced_output)), \
             patch("reflect.core._detect_agents", return_value=self._agent_fixture(skill_dest)):
            result = runner.invoke(main, [
                "skills", "--yes", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        assert result.exit_code == 0
        assert not skill_dest.exists()

    def test_skills_strips_plain_fences(self, runner, otlp_file, tmp_path):
        """Agent output wrapped in ``` fences (no language tag) is parsed correctly."""
        skill_dest = tmp_path / "skills"
        fenced_output = '```\n' + json.dumps([_FAKE_SKILLS[0]]) + '\n```'
        with patch("subprocess.run", return_value=_R(0, fenced_output)), \
             patch("reflect.core._detect_agents", return_value=self._agent_fixture(skill_dest)):
            result = runner.invoke(main, [
                "skills", "--yes", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        assert result.exit_code == 0
        assert not skill_dest.exists()

    def test_skills_accepts_trailing_text_after_json(self, runner, otlp_file, tmp_path):
        """Valid JSON followed by trailing prose should still parse."""
        skill_dest = tmp_path / "skills"
        noisy_output = json.dumps([_FAKE_SKILLS[0]]) + "\n\nI found one strong candidate skill."
        with patch("subprocess.run", return_value=_R(0, noisy_output)), \
             patch("reflect.core._detect_agents", return_value=self._agent_fixture(skill_dest)):
            result = runner.invoke(main, [
                "skills", "--yes", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        assert result.exit_code == 0
        assert not skill_dest.exists()


def test_strip_json_fences_variants():
    from reflect.core import _load_extracted_skills, _strip_json_fences

    raw = '[{"name": "x"}]'

    # No fences -- returns as-is
    assert _strip_json_fences(raw) == raw

    # ```json fences
    assert _strip_json_fences(f'```json\n{raw}\n```') == raw

    # ``` fences (no language tag)
    assert _strip_json_fences(f'```\n{raw}\n```') == raw

    # Leading/trailing whitespace around fences
    assert _strip_json_fences(f'  \n```json\n{raw}\n```\n  ') == raw

    # Prose before/after fences (only content between fences extracted)
    assert _strip_json_fences(f'Here is the JSON:\n```json\n{raw}\n```\nDone.') == raw

    # Backticks inside JSON strings are not treated as closing fence
    with_backticks = '[{"name": "x", "content": "```python\\nprint()\\n```"}]'
    fenced = f'```json\n{with_backticks}\n```'
    assert _strip_json_fences(fenced) == with_backticks

    # Valid JSON with trailing prose still parses
    parsed = _load_extracted_skills(raw + "\nDone.")
    assert parsed == [{"name": "x"}]


class TestNoDataNoCrash:
    def test_empty_dirs_no_crash(self, runner, tmp_path):
        with patch("reflect.core._start_publish_server"), \
             patch("reflect.core._default_otlp_traces", return_value=None), \
             patch("reflect.core._discover_rich_session_files", return_value=[]):
            result = runner.invoke(main, [
                "--foreground",
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
                "--db-path", str(tmp_path / "reflect.db"),
            ])
            assert result.exit_code == 0


class TestUpdateAdvisor:
    def test_foreground_run_surfaces_startup_notice(self, runner, otlp_file, tmp_path):
        with patch("reflect.core._start_publish_server"), \
             patch("reflect.core._build_startup_update_notice", return_value="v9.9.9 is available. Run reflect doctor for details."):
            result = runner.invoke(main, [
                "--foreground",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
                "--db-path", str(tmp_path / "reflect.db"),
            ])
        assert result.exit_code == 0
        assert "reflect notice:" in result.output
        assert "v9.9.9 is available" in result.output

    def test_doctor_renders_update_advisor(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        (reflect_home / "state").mkdir(parents=True)
        (hook_home).mkdir(parents=True)
        advisor = {
            "release": {
                "current_version": "1.0.0",
                "latest_version": "1.1.0",
                "checked_at": "2025-01-01T00:00:00Z",
                "update_available": True,
                "source": "remote",
            },
            "local_issues": [
                {
                    "component": "Reflect skill copies",
                    "summary": "Global skill distribution is out of date for Claude Code.",
                    "remediation": "Run reflect setup to refresh global installed skill copies.",
                }
            ],
        }
        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core._collect_update_advisor", return_value=advisor):
            result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "Update advisor" in result.output
        assert "update available" in result.output
        assert "1.1.0" in result.output

    def test_doctor_reports_pricing_status(self, runner, tmp_path, monkeypatch):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        (reflect_home / "state").mkdir(parents=True)
        hook_home.mkdir(parents=True)
        advisor = {
            "release": {
                "current_version": "1.0.0",
                "latest_version": None,
                "checked_at": None,
                "update_available": False,
                "source": "unknown",
            },
            "local_issues": [],
        }

        from reflect import pricing as pricing_mod

        def _fake_fetch(_url: str, _timeout: float, api_key: str = ""):
            return {
                "gpt-4o": {
                    "input_cost_per_token": 1.0,
                    "output_cost_per_token": 2.0,
                }
            }

        monkeypatch.setattr(pricing_mod, "_fetch_json_url", _fake_fetch)

        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core._collect_update_advisor", return_value=advisor), \
             patch.dict(os.environ, {"REFLECT_HOME": str(reflect_home)}, clear=False):
            result = runner.invoke(main, ["doctor"])

        assert result.exit_code == 0
        assert "Pricing" in result.output
        assert "live" in result.output
        assert "gpt-4o" in result.output

    def test_doctor_cost_appends_aliases_without_overwriting(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        alias_path = reflect_home / "config" / "model-aliases.json"
        alias_path.parent.mkdir(parents=True)
        alias_path.write_text(json.dumps({
            "aliases": {
                "claude-4-sonnet-20250514": "manual-target"
            }
        }))
        db_path = tmp_path / "reflect.db"
        from reflect.store.migrate import migrate
        from reflect.store.sqlite import connect_sqlite

        now = "2026-05-26T00:00:00+00:00"
        conn = connect_sqlite(db_path)
        try:
            migrate(conn)
            for suffix, model in [
                ("manual", "claude-4-sonnet-20250514"),
                ("new", "claude-4-5-sonnet-20250929"),
            ]:
                conn.execute(
                    """
                    INSERT INTO sessions(id, started_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (f"sess-{suffix}", now, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO steps(id, session_id, seq, type, started_at, raw_attrs_json, created_at, updated_at)
                    VALUES (?, ?, 1, 'llm', ?, '{}', ?, ?)
                    """,
                    (f"step-{suffix}", f"sess-{suffix}", now, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO llm_calls(
                      id, step_id, session_id, request_model, input_tokens, output_tokens, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 100, 50, ?, ?)
                    """,
                    (f"llm-{suffix}", f"step-{suffix}", f"sess-{suffix}", model, now, now),
                )
            conn.commit()
        finally:
            conn.close()

        with patch.dict(os.environ, {"REFLECT_HOME": str(reflect_home)}, clear=False):
            result = runner.invoke(main, ["doctor", "cost", "--db-path", str(db_path)])

        assert result.exit_code == 0
        payload = json.loads(alias_path.read_text())
        assert payload["aliases"]["claude-4-sonnet-20250514"] == "manual-target"
        assert payload["aliases"]["claude-4-5-sonnet-20250929"] == "claude-sonnet-4-5"
        assert "New aliases" in result.output
        assert "1" in result.output

    def test_prepare_sql_report_db_repairs_provenance_before_summary_breakdown(self, tmp_path, monkeypatch):
        from reflect.store.migrate import migrate
        from reflect.store.sqlite import connect_sqlite

        db_path = tmp_path / "reflect.db"
        otlp_traces = tmp_path / "otel-traces.json"
        otlp_traces.write_text("{}")

        conn = connect_sqlite(db_path)
        try:
            migrate(conn)
            conn.execute(
                """
                INSERT INTO raw_events(
                  id, source_id, source_type, event_type, trace_id, span_id, parent_span_id,
                  session_id, observed_at, received_at, attrs_json, body_json,
                  normalized_status, normalization_error, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "raw-legacy-hook-trace",
                    str(otlp_traces),
                    "otlp_traces_json",
                    "tool_call",
                    "trace-1",
                    "span-1",
                    "",
                    "sess-1",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:01+00:00",
                    json.dumps(
                        {
                            "gen_ai.client.name": "cursor",
                            "gen_ai.client.hook.event": "PreToolUse",
                            "session.id": "sess-1",
                        },
                        sort_keys=True,
                    ),
                    "{}",
                    "pending",
                    None,
                    "legacy-hash",
                    "2026-01-01T00:00:01+00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setattr("reflect.store.ingest.ingest_otlp_traces_file", lambda *_args, **_kwargs: {"inserted": 0, "skipped": 1})
        monkeypatch.setattr("reflect.store.graph_normalize.rebuild_graph", lambda *_args, **_kwargs: {"sessions": 0, "transitions": 0})
        monkeypatch.setattr("reflect.store.rollups.rebuild_rollups", lambda *_args, **_kwargs: {"session_rollups": 0})
        monkeypatch.setattr(core, "_ensure_sql_costs", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(core, "_infer_otlp_logs_file", lambda *_args, **_kwargs: None)

        result = core._prepare_sql_report_db(db_path, otlp_traces=otlp_traces, include_native_sessions=False)

        counts = result["ingest_sources"]["otlp_traces"]["agents"]["cursor"]
        assert counts["events"] == 1
        assert counts["native_events"] == 0
        assert counts["hook_events"] == 1

    def test_prepare_sql_report_db_applies_cursor_adapter_before_rollups(self, tmp_path, monkeypatch):
        from reflect.store.sqlite import connect_sqlite

        db_path = tmp_path / "reflect.db"
        session_file = tmp_path / "cursor-native-sess-1.jsonl"
        session_file.write_text(
            "\n".join([
                json.dumps({
                    "role": "user",
                    "message": {"content": [{"type": "text", "text": "summarize this failing test output"}]},
                }),
                json.dumps({
                    "role": "assistant",
                    "message": {"content": [{"type": "text", "text": "The failing assertion is in the adapter path."}]},
                }),
            ])
            + "\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(core, "_discover_rich_session_files", lambda: [("cursor", session_file)])
        monkeypatch.setattr("reflect.store.graph_normalize.rebuild_graph", lambda *_args, **_kwargs: {"sessions": 0})
        monkeypatch.setattr(core, "_ensure_sql_costs", lambda *_args, **_kwargs: None)

        result = core._prepare_sql_report_db(db_path, otlp_traces=None, include_native_sessions=True)

        assert result["cursor_adapter"] == {"updated": 1, "skipped": 0, "missing": 0}
        assert result["rollups"] == {"session_rollups": 1, "daily_rollups": 1, "tool_rollups": 0}
        conn = connect_sqlite(db_path)
        try:
            rollup = conn.execute(
                """
                SELECT input_tokens, output_tokens
                FROM session_rollups
                WHERE session_id = 'cursor-native-sess-1'
                """
            ).fetchone()
            assert rollup[0] > 0
            assert rollup[1] > 0
            raw_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM raw_events
                WHERE attrs_json LIKE '%estimated_cursor_transcript%'
                   OR attrs_json LIKE '%gen_ai.usage.input_tokens%'
                """
            ).fetchone()[0]
            assert raw_count == 0
        finally:
            conn.close()

    def test_prepare_sql_report_db_reuses_derived_state_for_unchanged_sources(
        self,
        tmp_path,
        monkeypatch,
        otlp_file,
    ):
        from reflect.store.migrate import migrate
        from reflect.store.sqlite import connect_sqlite

        db_path = tmp_path / "reflect.db"
        otlp_traces = otlp_file
        conn = connect_sqlite(db_path)
        try:
            migrate(conn)
            conn.execute(
                """
                INSERT INTO source_ingestion_state(
                  source_id, source_type, size_bytes, modified_ns, updated_at
                ) VALUES (?, 'otlp_traces_json', ?, ?, '2026-01-01T00:00:00+00:00')
                """,
                (
                    str(otlp_traces),
                    otlp_traces.stat().st_size,
                    otlp_traces.stat().st_mtime_ns,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setattr(core, "_infer_otlp_logs_file", lambda *_args, **_kwargs: None)

        def fail_if_rebuilt(*_args, **_kwargs):
            raise AssertionError("unchanged preparation should reuse derived state")

        monkeypatch.setattr(core, "_ensure_sql_costs", fail_if_rebuilt)
        monkeypatch.setattr("reflect.store.graph_normalize.rebuild_graph", fail_if_rebuilt)
        monkeypatch.setattr("reflect.store.rollups.rebuild_rollups", fail_if_rebuilt)

        result = core._prepare_sql_report_db(
            db_path,
            otlp_traces=otlp_traces,
            include_native_sessions=False,
        )

        assert result["ingest_sources"]["otlp_traces"]["unchanged"] == 1
        assert result["normalize"] == {"processed": 0, "failed": 0, "skipped": 0}
        assert result["graph"]["skipped"] == 1
        assert result["rollups"]["skipped"] == 1

    def test_prepare_sql_report_db_refreshes_only_changed_session_rollups(
        self,
        tmp_path,
        monkeypatch,
    ):
        db_path = tmp_path / "reflect.db"
        otlp_traces = tmp_path / "otel-traces.json"
        initial_span = make_span(
            "UserPromptSubmit",
            session="sess-incremental",
            input_tokens=100,
            output_tokens=50,
        )
        otlp_traces.write_text(wrap_otlp([initial_span]) + "\n", encoding="utf-8")
        monkeypatch.setattr(core, "_infer_otlp_logs_file", lambda *_args, **_kwargs: None)

        core._prepare_sql_report_db(
            db_path,
            otlp_traces=otlp_traces,
            include_native_sessions=False,
        )
        changed_span = make_span(
            "PreToolUse",
            session="sess-incremental",
            tool="Read",
            start_ns=initial_span["start_time_ns"] + 1_000_000_000,
        )
        otlp_traces.write_text(
            wrap_otlp([initial_span, changed_span]) + "\n",
            encoding="utf-8",
        )

        result = core._prepare_sql_report_db(
            db_path,
            otlp_traces=otlp_traces,
            include_native_sessions=False,
        )

        assert result["ingest"]["inserted"] == 1
        assert result["normalize"]["processed"] == 1
        assert result["graph"]["refreshed_sessions"] == 1
        assert result["rollups"]["refreshed_sessions"] == 1

    def test_update_apply_uses_pipx_upgrade(self, runner):
        advisor = {
            "release": {
                "current_version": "1.0.0",
                "latest_version": "1.1.0",
                "checked_at": "2025-01-01T00:00:00Z",
                "update_available": True,
                "source": "remote",
            },
            "local_issues": [],
        }
        with patch("reflect.core._collect_update_advisor", return_value=advisor), \
             patch("reflect.core.shutil.which", return_value="/usr/local/bin/pipx"), \
             patch("reflect.core.subprocess.check_call") as mock_check_call:
            result = runner.invoke(main, ["update", "--apply"])
        assert result.exit_code == 0
        assert mock_check_call.call_args_list == [
            call(["/usr/local/bin/pipx", "upgrade", "o11y-reflect"]),
            call(["/usr/local/bin/pipx", "upgrade", "opentelemetry-hooks"]),
        ]
        assert "Package upgrades finished." in result.output

    def test_update_apply_upgrades_hooks_even_without_new_reflect_release(self, runner):
        advisor = {
            "release": {
                "current_version": "1.1.0",
                "latest_version": "1.1.0",
                "checked_at": "2025-01-01T00:00:00Z",
                "update_available": False,
                "source": "remote",
            },
            "local_issues": [],
        }
        with patch("reflect.core._collect_update_advisor", return_value=advisor), \
             patch("reflect.core.shutil.which", return_value="/usr/local/bin/pipx"), \
             patch("reflect.core.subprocess.check_call") as mock_check_call:
            result = runner.invoke(main, ["update", "--apply"])
        assert result.exit_code == 0
        assert mock_check_call.call_args_list == [
            call(["/usr/local/bin/pipx", "upgrade", "o11y-reflect"]),
            call(["/usr/local/bin/pipx", "upgrade", "opentelemetry-hooks"]),
        ]

    def test_release_update_status_uses_cache_when_fresh(self, tmp_path):
        cache_path = tmp_path / "update-check.json"
        cache_path.write_text(json.dumps({
            "latest_version": "1.2.0",
            "checked_at": "2026-04-06T12:00:00Z",
        }))
        fake_now = datetime.fromisoformat("2026-04-06T13:00:00+00:00")

        with patch("reflect.core._UPDATE_CACHE_PATH", cache_path), \
             patch("reflect.core._current_reflect_version", return_value="1.0.0"), \
             patch("reflect.core._fetch_latest_reflect_version") as mock_fetch, \
             patch("reflect.core.datetime") as mock_datetime:
            mock_datetime.now.return_value = fake_now
            mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
            status = core._release_update_status(allow_remote=True)

        assert status["latest_version"] == "1.2.0"
        assert status["update_available"] is True
        assert status["source"] == "cache"
        mock_fetch.assert_not_called()

    def test_startup_notice_ignores_hook_wiring_only(self):
        advisor = {
            "release": {
                "current_version": "1.0.0",
                "latest_version": None,
                "checked_at": None,
                "update_available": False,
                "source": "unknown",
            },
            "local_issues": [
                {
                    "component": "Hook wiring",
                    "summary": "Claude Code hooks are incomplete.",
                    "remediation": "Run reflect setup.",
                }
            ],
        }

        assert core._build_startup_update_notice(advisor) is None

    def test_release_update_status_fetches_and_saves_remote_version(self, tmp_path):
        cache_path = tmp_path / "update-check.json"
        fake_now = datetime.fromisoformat("2026-04-06T13:00:00+00:00")

        with patch("reflect.core._UPDATE_CACHE_PATH", cache_path), \
             patch("reflect.core._current_reflect_version", return_value="1.0.0"), \
             patch("reflect.core._fetch_latest_reflect_version", return_value="1.3.0"), \
             patch("reflect.core.datetime") as mock_datetime:
            mock_datetime.now.return_value = fake_now
            mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
            status = core._release_update_status(allow_remote=True)

        saved = json.loads(cache_path.read_text())
        assert status["latest_version"] == "1.3.0"
        assert status["source"] == "remote"
        assert saved["latest_version"] == "1.3.0"

    def test_detect_hook_drift_reports_missing_config(self, tmp_path):
        hook_home = tmp_path / ".otel-hook-home"
        hook_home.mkdir()

        with patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core._claude_hooks_registered", return_value=False):
            drift = core._detect_hook_drift()

        assert drift is not None
        assert drift["component"] == "Hook wiring"
        assert "missing" in drift["summary"]
        assert "reflect setup" in drift["remediation"]

    def test_detect_hook_drift_returns_none_when_otel_hook_not_installed(self, tmp_path):
        hook_home = tmp_path / ".otel-hook-home"
        hook_home.mkdir()

        with patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value=None):
            drift = core._detect_hook_drift()

        assert drift is None

    def test_detect_hook_drift_skips_when_otel_hook_not_installed_despite_config(self, tmp_path):
        hook_home = tmp_path / ".otel-hook-home"
        hook_home.mkdir()
        (hook_home / "otel_config.json").write_text('{"IDE_OTEL_LOCAL_SPANS": "true"}')

        with patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value=None):
            drift = core._detect_hook_drift()

        assert drift is None

    def test_detect_hook_drift_returns_none_when_fully_configured(self, tmp_path):
        hook_home = tmp_path / ".otel-hook-home"
        hook_home.mkdir()
        (hook_home / "otel_config.json").write_text('{"IDE_OTEL_LOCAL_SPANS": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}')

        with patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core._claude_hooks_registered", return_value=True):
            drift = core._detect_hook_drift()

        assert drift is None

    def test_detect_hook_drift_reports_missing_endpoint(self, tmp_path):
        hook_home = tmp_path / ".otel-hook-home"
        hook_home.mkdir()
        (hook_home / "otel_config.json").write_text('{"IDE_OTEL_LOCAL_SPANS": "true"}')

        with patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core._claude_hooks_registered", return_value=True):
            drift = core._detect_hook_drift()

        assert drift is not None
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in drift["summary"]

    def test_detect_hook_drift_reports_missing_protocol(self, tmp_path):
        hook_home = tmp_path / ".otel-hook-home"
        hook_home.mkdir()
        (hook_home / "otel_config.json").write_text(
            '{"IDE_OTEL_LOCAL_SPANS": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317"}'
        )

        with patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core._claude_hooks_registered", return_value=True):
            drift = core._detect_hook_drift()

        assert drift is not None
        assert "OTEL_EXPORTER_OTLP_PROTOCOL" in drift["summary"]

    def test_detect_hook_drift_reports_unsupported_protocol(self, tmp_path):
        hook_home = tmp_path / ".otel-hook-home"
        hook_home.mkdir()
        (hook_home / "otel_config.json").write_text(
            '{"IDE_OTEL_LOCAL_SPANS": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317", "OTEL_EXPORTER_OTLP_PROTOCOL": "json"}'
        )

        with patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core._claude_hooks_registered", return_value=True):
            drift = core._detect_hook_drift()

        assert drift is not None
        assert "unsupported value" in drift["summary"]

class TestDoctor:
    def test_doctor_reports_managed_report_server_status(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        (reflect_home / "state").mkdir(parents=True)
        hook_home.mkdir(parents=True)
        advisor = {
            "release": {
                "current_version": "1.0.0",
                "latest_version": None,
                "checked_at": None,
                "update_available": False,
                "source": "unknown",
            },
            "local_issues": [],
        }
        report_status = SimpleNamespace(
            running=True,
            pid=4321,
            port_in_use=True,
            url="http://127.0.0.1:8877/?report=api/data",
        )
        daemon = SimpleNamespace(status=lambda: report_status)
        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core._collect_update_advisor", return_value=advisor), \
             patch("reflect.core._report_server_daemon", return_value=daemon):
            result = runner.invoke(main, ["doctor"])

        assert result.exit_code == 0
        assert "report server" in result.output
        assert "running (PID 4321)" in result.output
        assert "127.0.0.1:8877" in result.output

    def test_doctor_reports_detected_agents_and_files(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        (reflect_home / "state").mkdir(parents=True)
        (reflect_home / "state" / "local_spans").mkdir(parents=True)
        (reflect_home / "state" / "sessions").mkdir(parents=True)
        (reflect_home / "state" / "local_spans" / "s1.jsonl").write_text("{}\n")
        (reflect_home / "state" / "sessions" / "s1.json").write_text("{}\n")
        otlp_file = reflect_home / "state" / "otel-traces.json"
        otlp_file.write_text(wrap_otlp([make_span("UserPromptSubmit")]) + "\n")
        (reflect_home / "state" / "otel-logs.json").write_text('{"resourceLogs":[]}\n')
        (hook_home).mkdir(parents=True)
        (hook_home / "otel_config.json").write_text("{}\n")
        gemini_home = tmp_path / ".gemini"
        gemini_home.mkdir()
        advisor = {
            "release": {
                "current_version": "1.0.0",
                "latest_version": None,
                "checked_at": None,
                "update_available": False,
                "source": "unknown",
            },
            "local_issues": [],
        }
        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core._collect_update_advisor", return_value=advisor), \
             patch.dict(os.environ, {"GEMINI_DIR": str(gemini_home)}, clear=False):
            result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "reflect doctor" in result.output
        assert "Telemetry files" in result.output
        assert "Detected agent homes" in result.output
        assert "Support matrix" in result.output
        assert "Gemini CLI" in result.output
        assert "Use native telemetry first" in result.output

    def test_doctor_support_matrix_marks_planned_agents(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        (reflect_home / "state").mkdir(parents=True)
        hook_home.mkdir(parents=True)
        advisor = {
            "release": {
                "current_version": "1.0.0",
                "latest_version": None,
                "checked_at": None,
                "update_available": False,
                "source": "unknown",
            },
            "local_issues": [],
        }
        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core._collect_update_advisor", return_value=advisor), \
             patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=False):
            result = runner.invoke(main, ["doctor"])

        assert result.exit_code == 0
        assert "Antigravity" in result.output
        assert "OpenClaw" in result.output
        assert "Windsurf" not in result.output
        assert "Planned" in result.output

    def test_doctor_otlp_logs_waiting_when_otel_hook_installed(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        (reflect_home / "state").mkdir(parents=True)
        (reflect_home / "state" / "local_spans").mkdir(parents=True)
        (reflect_home / "state" / "sessions").mkdir(parents=True)
        hook_home.mkdir(parents=True)
        advisor = {
            "release": {
                "current_version": "1.0.0",
                "latest_version": None,
                "checked_at": None,
                "update_available": False,
                "source": "unknown",
            },
            "local_issues": [],
        }
        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core._collect_update_advisor", return_value=advisor), \
             patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=False):
            result = runner.invoke(main, ["doctor"])

        assert result.exit_code == 0
        assert "waiting" in result.output

    def test_doctor_otlp_logs_missing_when_otel_hook_not_installed(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        (reflect_home / "state").mkdir(parents=True)
        (reflect_home / "state" / "local_spans").mkdir(parents=True)
        (reflect_home / "state" / "sessions").mkdir(parents=True)
        hook_home.mkdir(parents=True)
        advisor = {
            "release": {
                "current_version": "1.0.0",
                "latest_version": None,
                "checked_at": None,
                "update_available": False,
                "source": "unknown",
            },
            "local_issues": [],
        }
        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value=None), \
             patch("reflect.core._collect_update_advisor", return_value=advisor), \
             patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=False):
            result = runner.invoke(main, ["doctor"])

        assert result.exit_code == 0
        # Without otel-hook, OTLP logs should show "missing" not "waiting"
        assert "waiting" not in result.output

    def test_doctor_shows_native_agent_telemetry_status(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        home_dir = tmp_path / "home"
        claude_settings = home_dir / ".claude" / "settings.json"
        claude_settings.parent.mkdir(parents=True)
        claude_settings.write_text(json.dumps({
            "env": {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_METRICS_EXPORTER": "otlp",
                "OTEL_LOGS_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            }
        }))
        (reflect_home / "state").mkdir(parents=True)
        hook_home.mkdir(parents=True)
        (hook_home / "otel_config.json").write_text(json.dumps({
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        }))
        advisor = {
            "release": {
                "current_version": "1.0.0",
                "latest_version": None,
                "checked_at": None,
                "update_available": False,
                "source": "unknown",
            },
            "local_issues": [],
        }
        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core._collect_update_advisor", return_value=advisor), \
             patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            result = runner.invoke(main, ["doctor"])

        assert result.exit_code == 0
        assert "Native agent telemetry" in result.output
        assert "Claude Code" in result.output
        assert "Native OTel" in result.output
        assert "Traces" in result.output
        assert "Status details" in result.output
        assert "incomplete" in result.output


class TestSetup:
    @pytest.fixture(autouse=True)
    def _do_not_start_real_gateway(self):
        """Setup tests must never leave detached OTLP daemons behind."""
        with patch("reflect.gateway._is_running", return_value=12345):
            yield

    def test_setup_surfaces_detected_agent_guidance(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        home_dir = tmp_path / "home"
        gemini_home = tmp_path / ".gemini"
        gemini_home.mkdir()
        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core.subprocess.check_call"), \
             patch("reflect.core._distribute_skills"), \
             patch.dict(os.environ, {"HOME": str(home_dir), "GEMINI_DIR": str(gemini_home)}, clear=False):
            result = runner.invoke(main, ["setup"])
        assert result.exit_code == 0
        assert "native OTel" in result.output
        assert "Gemini" in result.output

    def test_setup_can_select_single_agent_non_interactively(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        home_dir = tmp_path / "home"
        claude_home = home_dir / ".claude"
        cursor_home = home_dir / ".cursor"
        claude_home.mkdir(parents=True)
        cursor_home.mkdir(parents=True)

        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core.subprocess.check_call") as check_call, \
             patch("reflect.core._distribute_skills") as distribute_skills, \
             patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            result = runner.invoke(main, ["setup", "--agent", "Claude Code"])

        assert result.exit_code == 0
        check_call.assert_any_call(
            ["/usr/bin/otel-hook", "setup", "--global", "--agent", "claude"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert not any(
            isinstance(call.args[0], list) and "--agent" in call.args[0] and "cursor" in call.args[0]
            for call in check_call.call_args_list
        )
        assert distribute_skills.call_args.kwargs["selected_agent_names"] == {"claude-code"}

    def test_setup_accepts_codex_alias_for_codex_cli(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        home_dir = tmp_path / "home"
        codex_home = home_dir / ".codex"
        codex_home.mkdir(parents=True)

        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core.subprocess.check_call"), \
             patch("reflect.core._distribute_skills") as distribute_skills, \
             patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            result = runner.invoke(main, ["setup", "--agent", "codex"])

        assert result.exit_code == 0
        assert distribute_skills.call_args.kwargs["selected_agent_names"] == {"openai-codex-cli"}

    def test_setup_local_agent_is_explicit_opt_in(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        home_dir = tmp_path / "home"
        claude_home = home_dir / ".claude"
        claude_home.mkdir(parents=True)

        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core.subprocess.check_call") as check_call, \
             patch("reflect.core._distribute_skills") as distribute_skills, \
             patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            result = runner.invoke(main, ["setup", "--agent", "Claude Code", "--local-agent", "Claude Code"])

        assert result.exit_code == 0
        check_call.assert_any_call(
            ["/usr/bin/otel-hook", "setup", "--no-global", "--agent", "claude", "--cwd", str(Path.cwd())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert distribute_skills.call_args.kwargs["local_agent_names"] == {"claude-code"}

    def test_setup_agent_selection_uses_interactive_picker(self):
        agents = [
            {"name": "Claude Code", "detected": True, "support_status": "Implemented"},
            {"name": "Cursor", "detected": True, "support_status": "Implemented"},
        ]
        with patch("reflect.core._detect_agents", return_value=agents), \
             patch("reflect.core.sys.stdin.isatty", return_value=True), \
             patch("reflect.core._interactive_pick", return_value=[1]) as picker:
            selected = core._resolve_setup_agent_selection(
                core.Console(file=io.StringIO()),
                agent_names=(),
                all_agents=False,
            )

        assert selected == {"cursor"}
        assert "Claude Code (Implemented)" in picker.call_args.args[0]

    def test_setup_writes_agent_env_files_and_backups(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        home_dir = tmp_path / "home"
        claude_home = home_dir / ".claude"
        copilot_home = home_dir / ".copilot"
        gemini_home = home_dir / ".gemini"
        vscode_settings = home_dir / "Library" / "Application Support" / "Code" / "User"

        claude_home.mkdir(parents=True)
        copilot_home.mkdir(parents=True)
        gemini_home.mkdir(parents=True)
        hook_home.mkdir(parents=True)
        vscode_settings.mkdir(parents=True)

        (claude_home / "settings.json").write_text('{"hooks":{}}\n')
        (gemini_home / "settings.json").write_text('{"telemetry":{"enabled":false,"outfile":".gemini/telemetry.log"}}\n')
        (hook_home / "otel_config.json").write_text('{"OTEL_EXPORTER_OTLP_ENDPOINT":"http://localhost:4317","OTEL_EXPORTER_OTLP_PROTOCOL":"grpc"}\n')
        (vscode_settings / "settings.json").write_text('{"github.copilot.chat.otel.enabled":false}\n')

        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core.subprocess.check_call"), \
             patch("reflect.core._distribute_skills"), \
             patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            result = runner.invoke(main, ["setup"])

        assert result.exit_code == 0

        hook_backup_dir = reflect_home / "agents" / "opentelemetry-hooks" / "config-snapshots"
        claude_backup_dir = reflect_home / "agents" / "claude-code" / "config-snapshots"
        copilot_backup_dir = reflect_home / "agents" / "github-copilot" / "config-snapshots"
        gemini_backup_dir = reflect_home / "agents" / "gemini-cli" / "config-snapshots"

        # Claude Code: native OTel env block written to settings.json
        claude_settings = json.loads((claude_home / "settings.json").read_text())
        assert claude_settings["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
        assert claude_settings["env"]["OTEL_METRICS_EXPORTER"] == "otlp"
        assert claude_settings["env"]["OTEL_LOGS_EXPORTER"] == "otlp"

        # Gemini: native OTel settings written to settings.json
        gemini_settings = json.loads((gemini_home / "settings.json").read_text())
        assert gemini_settings["telemetry"]["enabled"] is True
        assert gemini_settings["telemetry"]["target"] == "local"
        assert gemini_settings["telemetry"]["useCollector"] is True
        assert gemini_settings["telemetry"]["otlpEndpoint"] == "http://localhost:4317"
        assert gemini_settings["telemetry"]["otlpProtocol"] == "grpc"
        assert gemini_settings["telemetry"]["logPrompts"] is False
        assert "outfile" not in gemini_settings["telemetry"]

        # Copilot VS Code: otel.* keys + CLI env vars written to settings.json
        copilot_settings = json.loads((vscode_settings / "settings.json").read_text())
        assert copilot_settings["github.copilot.chat.otel.enabled"] is True
        assert copilot_settings["github.copilot.chat.otel.otlpEndpoint"] == "http://localhost:4318"
        assert copilot_settings["github.copilot.chat.otel.exporterType"] == "otlp-http"
        assert copilot_settings["github.copilot.chat.otel.captureContent"] is False
        assert copilot_settings["env"]["COPILOT_OTEL_ENABLED"] == "true"
        assert copilot_settings["env"]["COPILOT_OTEL_OTLP_ENDPOINT"] == "http://localhost:4318"

        # Config snapshots created
        assert hook_backup_dir.exists()
        assert any(hook_backup_dir.iterdir())
        assert claude_backup_dir.exists()
        assert any(claude_backup_dir.iterdir())
        assert gemini_backup_dir.exists()
        assert any(gemini_backup_dir.iterdir())
        assert copilot_backup_dir.exists()
        assert any(copilot_backup_dir.iterdir())

    def test_distribute_skills_includes_reflect_skills_helper(self, tmp_path):
        from rich.console import Console

        console = Console(file=io.StringIO())
        global_skill_dir = tmp_path / "global-skills"
        (global_skill_dir / "skills").mkdir(parents=True)
        (global_skill_dir / "skills" / "SKILL.md").write_text("# legacy\n", encoding="utf-8")
        agent = {
            "name": "Claude Code",
            "detected": True,
            "global_path": str(global_skill_dir),
        }

        with patch("reflect.core._detect_agents", return_value=[agent]), \
             patch("reflect.core._fetch_opentelemetry_skill", return_value=None), \
             patch("reflect.core.Path.cwd", return_value=tmp_path):
            core._distribute_skills(console)

        assert (global_skill_dir / "reflect" / "SKILL.md").exists()
        assert (global_skill_dir / "reflect-skills" / "SKILL.md").exists()
        assert (global_skill_dir / "reflect-usage" / "SKILL.md").exists()
        assert not (global_skill_dir / "reflect-loops").exists()
        assert not (global_skill_dir / "skills").exists()
        assert not (tmp_path / ".claude" / "skills" / "reflect" / "SKILL.md").exists()

    def test_distribute_skills_dedupes_shared_global_paths(self, tmp_path):
        from rich.console import Console

        console = Console(file=io.StringIO())
        shared_skill_dir = tmp_path / "shared-skills"
        agents = [
            {
                "name": "Cursor",
                "detected": True,
                "global_path": str(shared_skill_dir),
                "local_skill_path": ".agents/skills/",
            },
            {
                "name": "Cline",
                "detected": True,
                "global_path": str(shared_skill_dir),
                "local_skill_path": ".agents/skills/",
            },
        ]

        with patch("reflect.core._detect_agents", return_value=agents), \
             patch("reflect.core._fetch_opentelemetry_skill", return_value=None):
            core._distribute_skills(console)

        assert (shared_skill_dir / "reflect" / "SKILL.md").exists()
        assert (shared_skill_dir / "reflect-skills" / "SKILL.md").exists()
        assert (shared_skill_dir / "reflect-usage" / "SKILL.md").exists()
        assert not (shared_skill_dir / "reflect-loops").exists()
        assert "already populated" in console.file.getvalue()

    def test_distribute_skills_can_opt_into_local_project_path(self, tmp_path):
        from rich.console import Console

        console = Console(file=io.StringIO())
        global_skill_dir = tmp_path / "global-skills"
        (tmp_path / ".claude" / "skills").mkdir(parents=True)
        (tmp_path / ".claude" / "skills" / "SKILL.md").write_text("# legacy\n", encoding="utf-8")
        agent = {
            "name": "Claude Code",
            "detected": True,
            "global_path": str(global_skill_dir),
            "local_skill_path": ".claude/skills/",
        }

        with patch("reflect.core._detect_agents", return_value=[agent]), \
             patch("reflect.core._fetch_opentelemetry_skill", return_value=None), \
             patch("reflect.core.Path.cwd", return_value=tmp_path):
            core._distribute_skills(console, local_agent_names={"claude-code"})

        assert (global_skill_dir / "reflect" / "SKILL.md").exists()
        assert (global_skill_dir / "reflect-skills" / "SKILL.md").exists()
        assert (global_skill_dir / "reflect-usage" / "SKILL.md").exists()
        assert not (global_skill_dir / "reflect-loops").exists()
        assert (tmp_path / ".claude" / "skills" / "reflect" / "SKILL.md").exists()
        assert (tmp_path / ".claude" / "skills" / "reflect-skills" / "SKILL.md").exists()
        assert (tmp_path / ".claude" / "skills" / "reflect-usage" / "SKILL.md").exists()
        assert not (tmp_path / ".claude" / "skills" / "skills").exists()

    def test_distribute_skills_writes_codex_global_path(self, tmp_path):
        from rich.console import Console

        console = Console(file=io.StringIO())
        codex_skill_dir = tmp_path / ".agents" / "skills"
        agent = {
            "name": "OpenAI Codex CLI",
            "detected": True,
            "global_path": str(codex_skill_dir),
            "local_skill_path": ".agents/skills/",
        }

        with patch("reflect.core._detect_agents", return_value=[agent]), \
             patch("reflect.core._fetch_opentelemetry_skill", return_value=None):
            core._distribute_skills(console, selected_agent_names={"openai-codex-cli"})

        assert (codex_skill_dir / "reflect" / "SKILL.md").exists()
        assert (codex_skill_dir / "reflect-skills" / "SKILL.md").exists()
        assert (codex_skill_dir / "reflect-usage" / "SKILL.md").exists()
        assert not (codex_skill_dir / "reflect-loops").exists()

    def test_codex_uses_the_current_shared_agent_skill_roots(self):
        codex = next(agent for agent in core._AGENT_SPECS if agent["name"] == "OpenAI Codex CLI")

        assert codex["global_path"] == "~/.agents/skills/"
        assert codex["local_skill_path"] == ".agents/skills/"


    def test_setup_seeds_config_from_example_on_fresh_install(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        home_dir = tmp_path / "home"
        hook_home.mkdir(parents=True)

        example_config = {
            "_comment_endpoint": "Set your OTLP endpoint here",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        }
        (hook_home / "otel_config.example.json").write_text(
            json.dumps(example_config, indent=2) + "\n"
        )

        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core.subprocess.check_call"), \
             patch("reflect.core._distribute_skills"), \
             patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            result = runner.invoke(main, ["setup"])

        assert result.exit_code == 0

        config_path = hook_home / "otel_config.json"
        assert config_path.exists(), "otel_config.json should be written on fresh install"

        written = json.loads(config_path.read_text())
        # Sentinel key from example must be preserved
        assert "_comment_endpoint" in written, "Example sentinel key must be seeded into config"
        assert written["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4317"
        # IDE_OTEL_LOCAL_SPANS must be forced to "true"
        assert written["IDE_OTEL_LOCAL_SPANS"] == "true"
        assert "IDE_OTEL_CAPTURE_TEXT" not in written

    def test_setup_can_opt_into_masked_text_capture(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        home_dir = tmp_path / "home"
        hook_home.mkdir(parents=True)

        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core.subprocess.check_call"), \
             patch("reflect.core._distribute_skills"), \
             patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            result = runner.invoke(main, ["setup", "--capture-text"])

        assert result.exit_code == 0
        written = json.loads((hook_home / "otel_config.json").read_text())
        assert written["IDE_OTEL_CAPTURE_TEXT"] == "true"
        assert written["IDE_OTEL_MASK_PROMPTS"] == "true"
        assert written["IDE_OTEL_TEXT_MAX_CHARS"] == "4000"

    def test_setup_can_disable_masking_and_set_text_capture_limit(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        home_dir = tmp_path / "home"
        hook_home.mkdir(parents=True)

        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core.subprocess.check_call"), \
             patch("reflect.core._distribute_skills"), \
             patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            result = runner.invoke(main, [
                "setup",
                "--capture-text",
                "--no-mask-captured-text",
                "--text-max-chars",
                "1200",
            ])

        assert result.exit_code == 0
        written = json.loads((hook_home / "otel_config.json").read_text())
        assert written["IDE_OTEL_CAPTURE_TEXT"] == "true"
        assert written["IDE_OTEL_MASK_PROMPTS"] == "false"
        assert written["IDE_OTEL_TEXT_MAX_CHARS"] == "1200"

    def test_setup_text_capture_mode_masked_is_scriptable(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        home_dir = tmp_path / "home"
        hook_home.mkdir(parents=True)

        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core.subprocess.check_call"), \
             patch("reflect.core._distribute_skills"), \
             patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            result = runner.invoke(main, ["setup", "--text-capture-mode", "masked"])

        assert result.exit_code == 0
        written = json.loads((hook_home / "otel_config.json").read_text())
        assert written["IDE_OTEL_CAPTURE_TEXT"] == "true"
        assert written["IDE_OTEL_MASK_PROMPTS"] == "true"

    def test_setup_can_explicitly_disable_text_capture(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        home_dir = tmp_path / "home"
        hook_home.mkdir(parents=True)
        (hook_home / "otel_config.json").write_text(
            json.dumps({"IDE_OTEL_CAPTURE_TEXT": "true"}) + "\n"
        )

        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core.subprocess.check_call"), \
             patch("reflect.core._distribute_skills"), \
             patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            result = runner.invoke(main, ["setup", "--no-capture-text"])

        assert result.exit_code == 0
        written = json.loads((hook_home / "otel_config.json").read_text())
        assert written["IDE_OTEL_CAPTURE_TEXT"] == "false"


class TestNativeOtelConfig:
    """Unit tests for per-agent native OTel configuration functions."""

    HOOK_CFG = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    }
    HOOK_CFG_CAPTURE_TEXT = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        "IDE_OTEL_CAPTURE_TEXT": "true",
    }

    def _console(self):
        from rich.console import Console
        return Console(file=io.StringIO())

    # ------------------------------------------------------------------
    # Claude Code
    # ------------------------------------------------------------------

    def test_claude_native_otel_creates_env_block(self, tmp_path):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text('{"hooks":{}}\n')

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_claude_native_otel(self._console(), self.HOOK_CFG)

        result = json.loads(settings_file.read_text())
        assert result["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
        assert result["env"]["OTEL_METRICS_EXPORTER"] == "otlp"
        assert result["env"]["OTEL_LOGS_EXPORTER"] == "otlp"
        assert result["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4317"
        assert result["env"]["OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"] == "cumulative"

    def test_claude_native_otel_idempotent(self, tmp_path):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(json.dumps({
            "env": {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_METRICS_EXPORTER": "otlp",
                "OTEL_LOGS_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
                "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative",
            }
        }))
        content_before = settings_file.read_text()

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_claude_native_otel(self._console(), self.HOOK_CFG)

        assert settings_file.read_text() == content_before

    def test_claude_native_otel_creates_file_if_missing(self, tmp_path):
        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_claude_native_otel(self._console(), self.HOOK_CFG)

        settings_file = tmp_path / ".claude" / "settings.json"
        assert settings_file.exists()
        result = json.loads(settings_file.read_text())
        assert result["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"

    def test_claude_native_otel_read_error(self, tmp_path):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text("not valid json {{{{")

        with patch("reflect.core.Path.home", return_value=tmp_path):
            # Should not raise
            core._configure_claude_native_otel(self._console(), self.HOOK_CFG)

    # ------------------------------------------------------------------
    # Copilot CLI
    # ------------------------------------------------------------------

    def test_copilot_cli_native_otel_writes_env_block(self, tmp_path):
        vscode = tmp_path / ".config" / "Code" / "User"
        vscode.mkdir(parents=True)
        (vscode / "settings.json").write_text('{"github.copilot.chat.otel.enabled": true}\n')

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_copilot_cli_native_otel(self._console(), self.HOOK_CFG)

        result = json.loads((vscode / "settings.json").read_text())
        assert result["env"]["COPILOT_OTEL_ENABLED"] == "true"
        assert result["env"]["COPILOT_OTEL_OTLP_ENDPOINT"] == "http://localhost:4318"

    def test_copilot_cli_native_otel_no_settings_file(self, tmp_path):
        from rich.console import Console

        stream = io.StringIO()
        console = Console(file=stream)
        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_copilot_cli_native_otel(console, self.HOOK_CFG)

        output = stream.getvalue()
        assert "Skipped Copilot CLI OTel env vars" in output
        assert "settings.json" in output

    def test_copilot_native_otel_no_settings_file_surfaces_skip(self, tmp_path):
        from rich.console import Console

        stream = io.StringIO()
        console = Console(file=stream)
        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_copilot_native_otel(console, self.HOOK_CFG)

        output = stream.getvalue()
        assert "Skipped native Copilot OTel" in output
        assert "settings.json" in output

    def test_copilot_cli_native_otel_idempotent(self, tmp_path):
        vscode = tmp_path / ".config" / "Code" / "User"
        vscode.mkdir(parents=True)
        settings = {
            "env": {
                "COPILOT_OTEL_ENABLED": "true",
                "COPILOT_OTEL_OTLP_ENDPOINT": "http://localhost:4318",
            }
        }
        (vscode / "settings.json").write_text(json.dumps(settings))
        content_before = (vscode / "settings.json").read_text()

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_copilot_cli_native_otel(self._console(), self.HOOK_CFG)

        assert (vscode / "settings.json").read_text() == content_before

    # ------------------------------------------------------------------
    # Codex CLI
    # ------------------------------------------------------------------

    def test_codex_native_otel_creates_config(self, tmp_path):
        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG)

        config_path = tmp_path / ".codex" / "config.toml"
        config = config_path.read_text()
        parsed = tomllib.loads(config)
        assert "[otel]" in config
        assert parsed["otel"]["exporter"]["otlp-grpc"]["endpoint"] == "http://localhost:4317"
        assert parsed["otel"]["trace_exporter"]["otlp-grpc"]["endpoint"] == "http://localhost:4317"
        assert parsed["otel"]["log_user_prompt"] is False

    def test_codex_native_otel_enables_prompt_logging_when_text_capture_is_enabled(self, tmp_path):
        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG_CAPTURE_TEXT)

        parsed = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text())
        assert parsed["otel"]["log_user_prompt"] is True

    def test_codex_native_otel_appends_to_existing_config(self, tmp_path):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("[model]\nname = \"o3\"\n")

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG)

        config = (codex_dir / "config.toml").read_text()
        assert "[model]" in config  # existing section preserved
        assert "[otel]" in config
        assert "trace_exporter = { otlp-grpc" in config
        assert "log_user_prompt = false" in config

    def test_codex_native_otel_already_configured(self, tmp_path):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        config_text = (
            '[otel]\nexporter = { otlp-grpc = { endpoint = "http://localhost:4317" } }\n'
            'trace_exporter = { otlp-grpc = { endpoint = "http://localhost:4317" } }\n'
            "log_user_prompt = false\n"
        )
        (codex_dir / "config.toml").write_text(config_text)
        content_before = (codex_dir / "config.toml").read_text()

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG)

        assert (codex_dir / "config.toml").read_text() == content_before

    def test_codex_native_otel_replaces_incomplete_section(self, tmp_path):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            "[model]\nname = \"o3\"\n\n[otel]\ntraces_exporter = \"otlp\"\n"
        )

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG)

        parsed = tomllib.loads((codex_dir / "config.toml").read_text())
        assert parsed["model"]["name"] == "o3"
        assert parsed["otel"]["exporter"]["otlp-grpc"]["endpoint"] == "http://localhost:4317"
        assert parsed["otel"]["trace_exporter"]["otlp-grpc"]["endpoint"] == "http://localhost:4317"
        assert "traces_exporter" not in parsed["otel"]

    def test_codex_native_otel_replaces_mid_file_section_without_clobbering_following_sections(self, tmp_path):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            "[model]\nname = \"o3\"\n\n"
            "[otel]\ntraces_exporter = \"console\"\n\n"
            "[projects]\n\"/tmp/demo\" = {trust_level = \"trusted\"}\n"
        )

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG)

        updated = (codex_dir / "config.toml").read_text()
        parsed = tomllib.loads(updated)
        assert parsed["otel"]["trace_exporter"]["otlp-grpc"]["endpoint"] == "http://localhost:4317"
        assert parsed["projects"]["/tmp/demo"]["trust_level"] == "trusted"
        assert "\n\n[projects]\n" in updated

    def test_codex_native_otel_migrates_legacy_keys_and_preserves_user_settings(self, tmp_path):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            '[otel]\nexporter = {otlp-grpc = {endpoint = "http://localhost:4317"}}\n'
            'traces_exporter = "otlp"\n'
            'traces_endpoint = "http://localhost:4317"\n'
            'logs_exporter = "otlp"\n'
            'logs_endpoint = "http://localhost:4317"\n'
            'log_user_prompt = false\n'
            'environment = "local-dev"\n'
            'metrics_exporter = "none"\n'
        )

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG)

        parsed = tomllib.loads((codex_dir / "config.toml").read_text())
        assert parsed["otel"]["environment"] == "local-dev"
        assert parsed["otel"]["metrics_exporter"] == "none"
        assert parsed["otel"]["trace_exporter"]["otlp-grpc"]["endpoint"] == "http://localhost:4317"
        assert not ({"traces_exporter", "traces_endpoint", "logs_exporter", "logs_endpoint"} & parsed["otel"].keys())

    def test_codex_native_otel_read_error(self, tmp_path):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("[[[[invalid toml")

        with patch("reflect.core.Path.home", return_value=tmp_path):
            # Should not raise
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG)

    def test_native_otel_status_reports_incomplete_claude_config(self, tmp_path):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(json.dumps({
            "env": {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_METRICS_EXPORTER": "otlp",
                "OTEL_LOGS_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            }
        }))

        with patch("reflect.core.Path.home", return_value=tmp_path):
            statuses = core._collect_native_otel_statuses(self.HOOK_CFG)

        claude = next(status for status in statuses if status["agent"] == "Claude Code")
        assert claude["status"] == "incomplete"
        assert "OTEL_EXPORTER_OTLP_PROTOCOL" in claude["details"]

    def test_native_otel_status_reports_ready_codex_config(self, tmp_path):
        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG)
            statuses = core._collect_native_otel_statuses(self.HOOK_CFG)

        codex = next(status for status in statuses if status["agent"] == "OpenAI Codex CLI")
        assert codex["status"] == "ready"
        assert "trace/log OTLP exporters" in codex["details"]
        assert "raw user prompt export stays disabled" in codex["details"]

    def test_native_otel_status_reports_enabled_codex_prompt_capture(self, tmp_path):
        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG_CAPTURE_TEXT)
            statuses = core._collect_native_otel_statuses(self.HOOK_CFG_CAPTURE_TEXT)

        codex = next(status for status in statuses if status["agent"] == "OpenAI Codex CLI")
        assert codex["status"] == "ready"
        assert "raw user prompt export is enabled" in codex["details"]

    def test_native_otel_status_reports_unreadable_codex_config(self, tmp_path):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("[[[[invalid toml")

        with patch("reflect.core.Path.home", return_value=tmp_path):
            statuses = core._collect_native_otel_statuses(self.HOOK_CFG)

        codex = next(status for status in statuses if status["agent"] == "OpenAI Codex CLI")
        assert codex["status"] == "unreadable"
        assert "Failed to read config.toml" in codex["details"]
