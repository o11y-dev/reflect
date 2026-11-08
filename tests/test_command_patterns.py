import json
import sqlite3
from collections import Counter

import pytest

from reflect.command_patterns import command_pattern, command_projection, extract_command


@pytest.mark.parametrize(("command", "expected"), [
    ("rtk --project /tmp/a memory sync --format json", "rtk memory sync"),
    ("poetry run pytest tests/test_a.py -q", "pytest <path> -q"),
    ("TOKEN=secret uv run python -m pytest tests/test_b.py -q", "pytest <path> -q"),
    ("git -C /tmp/a diff --stat", "git diff --stat"),
    ("git -C /tmp/b diff --stat", "git diff --stat"),
    ("glab mr view 123 --repo example/project", "glab mr view <arg> --repo <value>"),
    ("glab mr view 456 --repo other/project", "glab mr view <arg> --repo <value>"),
    ("gh pr list --state open", "gh pr list --state <value>"),
    ("npm run build", "npm run build"),
    ("python -m pip install package", "python -m pip install <arg>"),
    ("python -c 'print(1)'", "python -c <code>"),
    ("python scripts/check.py", "python <script>"),
    ("rtk memory sync && git status", "rtk memory sync && git status"),
    ("git status\ngit diff --stat", "git status ; git diff --stat"),
    ("cd /tmp/repo && git diff --stat", "cd <path> && git diff --stat"),
    ("bash -lc 'git status && git diff --stat'", "git status && git diff --stat"),
    ("rg '|' tests/a.py", "rg <arg> <path>"),
    ("python check.py > /tmp/check.log 2>&1", "python <script> > <path> 2>&1"),
])
def test_command_templates_preserve_actions_and_remove_operand_noise(command, expected):
    assert command_pattern(command) == expected


def test_current_command_input_precedes_stale_decision_preview():
    attrs = {"gen_ai.client.tool.input": json.dumps({"cmd": "git status\ngit diff"})}
    assert extract_command(json.dumps(attrs), '{"decision":"approved"}') == "git status\ngit diff"


def test_command_argv_is_extracted_and_invalid_envelopes_are_not_commands():
    assert extract_command("{}", '{"command":["bash","-lc","git status"]}') == "bash -lc 'git status'"
    assert extract_command("{}", '{"cmd":"git status"') == ""


def test_opaque_scripts_are_bounded_and_exec_javascript_is_not_shell(monkeypatch):
    def unexpected_parse(_command):
        raise AssertionError("oversized inputs must not reach shell parsing")

    monkeypatch.setattr("reflect.command_patterns.shlex.split", unexpected_parse)
    payload = "python -c '" + "x" * (1024 * 1024) + "'"
    first = command_pattern(payload)
    assert first.startswith("shell script [")
    assert first != command_pattern(payload + "x")
    assert command_projection("exec", {"command": "const result = " + payload}, payload) == (None, False)


def test_display_sanitizer_does_not_rewrite_relative_paths():
    from reflect.utils import _sanitize_command_display

    assert _sanitize_command_display("pytest tests/test_a.py") == "pytest tests/test_a.py"
    assert _sanitize_command_display("cat /tmp/private.txt") == "cat <tmp>/private.txt"


def test_sql_command_evidence_grouping_and_totals_are_scoped():
    from reflect.views.report_tabs import _command_summary

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tool_calls (id INTEGER PRIMARY KEY, session_id, tool_name, input_preview_redacted, raw_attrs_json, input_hash, command_template, is_shell_call INTEGER DEFAULT 0)")
    rows = [
        ("one", "exec_command", '{"decision":"approved"}', {"gen_ai.client.tool.input": '{"cmd":"git diff --stat"}'}),
        ("one", "exec_command", '{"decision":"approved"}', {"gen_ai.client.tool.input": '{"cmd":"git status"}'}),
        ("one", "run_shell_command", '{"command":"git status"}', {}),
        ("one", "git status", None, {"gen_ai.client.command": "git status"}),
        ("one", "exec", "{}", {"command": "const result = await tools.exec_command({cmd: request.cmd});"}),
        ("one", "exec", '{"cmd":"git status"}', {}),
        ("one", "tools.exec_command", '{"cmd":"git status"}', {}),
        ("one", "terminal.run_shell_command", '{"command":"git status"}', {}),
        ("one", "Bash", None, {}),
        ("one", "Read", '{"cmd":"git status"}', {}),
        ("two", "functions.exec_command", '{"cmd":"poetry run pytest"}', {}),
    ]
    conn.executemany("INSERT INTO tool_calls (session_id,tool_name,input_preview_redacted,raw_attrs_json,input_hash) VALUES (?, ?, ?, ?, 'same-old-hash')", [
        (session, name, preview, json.dumps(attrs)) for session, name, preview, attrs in rows
    ])
    from reflect.store.command_patterns import refresh_command_projection
    refresh_command_projection(conn)
    assert refresh_command_projection(conn) == 0
    assert _command_summary(conn, ["one"]) == (Counter({"git status": 6, "git diff --stat": 1}), 8)
    assert _command_summary(conn, ["two"]) == (Counter({"pytest": 1}), 1)
    assert _command_summary(conn, []) == (Counter(), 0)
    conn.close()


def test_normalization_refreshes_command_after_decision_without_changing_preview(tmp_path):
    from reflect.store.command_patterns import refresh_command_projection
    from reflect.store.ingest import ingest_local_spans_file
    from reflect.store.migrate import migrate
    from reflect.store.normalize import normalize_pending_raw_events
    from reflect.store.sqlite import connect_sqlite

    conn = connect_sqlite(tmp_path / "commands.db")
    migrate(conn)
    base = {"gen_ai.client.name": "codex", "gen_ai.client.session_id": "session",
            "gen_ai.client.tool_name": "exec_command", "gen_ai.tool.call.id": "call"}
    for index, (event, payload) in enumerate([
        ("PreToolUse", {"decision": "approved"}),
        ("PostToolUse", {"cmd": "poetry run pytest tests/a.py -q"}),
    ]):
        path = tmp_path / f"spans-{index}.jsonl"
        path.write_text(json.dumps({"name": event, "spanId": str(index), "traceId": "trace",
                                   "start_time_ns": 100 + index, "end_time_ns": 101 + index,
                                   "attributes": {**base, "gen_ai.client.hook.event": event,
                                                  "gen_ai.client.tool.input": json.dumps(payload)}}) + "\n")
        ingest_local_spans_file(conn, file_path=path)
        assert normalize_pending_raw_events(conn)["failed"] == 0
        row = conn.execute("SELECT input_preview_redacted, command_template, is_shell_call FROM tool_calls").fetchone()
        assert json.loads(row[0]) == {"decision": "approved"}
        assert tuple(row[1:]) == (None if index == 0 else "pytest <path> -q", 1)
    before = conn.total_changes
    assert refresh_command_projection(conn) == 0
    assert conn.total_changes == before
    conn.close()
