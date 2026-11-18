import json

import pytest

from reflect.store.git_activity import git_activity, operation_for_tool, refresh_git_activity
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite

URL = "https://git.example.test/group/project/-/merge_requests/42"


def cli(command, output=None, **attrs):
    return {"tool_name": "exec_command", "input_preview_redacted": json.dumps({"cmd": command}), "output_preview_redacted": json.dumps(output) if isinstance(output, dict) else output, "raw_attrs_json": json.dumps(attrs), "repository": "group/project"}


@pytest.mark.parametrize("prefix,suffix", [
    ("python - <<'PY'\nvalue = '", "'\nPY"),
    ("node -e '", "'"),
    ("git commit -m '", "' && false"),
    ("glab mr create --description '", "' --dry-run"),
])
def test_oversized_commands_are_unclassified_without_tokenizing(monkeypatch, prefix, suffix):
    def forbidden(*args, **kwargs):
        raise AssertionError("Oversized command entered shlex")

    monkeypatch.setattr("reflect.store.git_activity.shlex.shlex", forbidden)
    command = prefix + "x" * 1_000_000 + suffix
    assert operation_for_tool(cli(command, {"exit_code": 0, "output": URL})) is None


def test_non_git_executable_does_not_consume_script_arguments(monkeypatch):
    def lexer(*args, **kwargs):
        class FirstTokenOnly:
            def __iter__(self):
                return self

            def __next__(self):
                if hasattr(self, "seen"):
                    raise AssertionError("Non-Git command consumed script arguments")
                self.seen = True
                return "python"

        return FirstTokenOnly()

    monkeypatch.setattr("reflect.store.git_activity.shlex.shlex", lexer)
    assert operation_for_tool(cli("python -c 'large embedded script'")) is None


@pytest.mark.parametrize("command", ["glab mr create --title title", "glab api --method POST projects/group%2Fproject/merge_requests", "glab api projects/group%2Fproject/merge_requests --field title=title"])
def test_glab_create_requires_its_terminal_result(command):
    assert operation_for_tool(cli(command)).state == "attempted"
    result = operation_for_tool(cli(command, {"exit_code": 0, "output": URL}))
    assert (result.operation, result.state, result.artifact_id) == ("create", "confirmed", "42")


@pytest.mark.parametrize("command", ["glab mr view 42", "glab mr list", "glab mr diff 42", "glab api projects/group%2Fproject/merge_requests/42", "gh pr view 42", "glab mr create --help", "git push --dry-run"])
def test_reads_and_dry_runs_do_not_create_artifacts(command):
    result = operation_for_tool(cli(command, {"exit_code": 0, "output": URL}))
    assert result.operation == "read"


def test_nested_tool_name_and_result_confirm_mcp_create():
    row = {"tool_name": "mcp__gitlab_mcpcreate_merge_request", "input_preview_redacted": '{"project_id":"group/project"}', "output_preview_redacted": json.dumps({"content": [{"type": "text", "text": json.dumps({"id": 123, "iid": 42, "web_url": URL})}]})}
    result = operation_for_tool(row)
    assert (result.operation, result.state, result.source) == ("create", "confirmed", "mcp")


def test_merge_confirmation_does_not_follow_a_read_or_auto_merge():
    merged = {"exit_code": 0, "output": json.dumps({"state": "merged", "web_url": URL})}
    assert operation_for_tool(cli("glab mr merge 42", merged)).state == "confirmed"
    assert operation_for_tool(cli("glab mr view 42", merged)).operation == "read"
    assert operation_for_tool(cli("glab mr merge 42 --when-pipeline-succeeds", merged)).operation == "schedule_merge"
    assert operation_for_tool(cli("glab mr merge 42", {"exit_code": 0, "output": "scheduled"})).state == "unresolved"


def test_failure_and_compound_wrapper_success_are_not_confirmed_mutations():
    assert operation_for_tool(cli("glab mr create", {"exit_code": 1, "output": URL})).state == "failed"
    assert operation_for_tool(cli("glab mr create; true", {"exit_code": 0, "output": URL})).state == "unresolved"
    assert operation_for_tool(cli("glab mr create && echo done", {"exit_code": 0, "output": URL})).state == "unresolved"
    assert operation_for_tool(cli("git commit -m done && git push", {"exit_code": 1, "output": "push failed"})).state == "unresolved"
    assert operation_for_tool(cli("echo 'glab mr create'", {"exit_code": 0, "output": URL})) is None


def test_merge_result_without_host_confirms_operation_but_not_distinct_identity():
    row = {"tool_name": "merge_pull_request", "input_preview_redacted": '{"owner":"team","repo":"project","pull_number":42}', "output_preview_redacted": '{"merged":true,"sha":"abc"}'}
    result = operation_for_tool(row)
    assert result.state == "confirmed"
    assert result.artifact_key is None
    row["output_preview_redacted"] = json.dumps({"merged": True, "already_merged": True, "html_url": "https://github.example.test/team/project/pull/42"})
    assert operation_for_tool(row).state == "unresolved"


def test_commit_requires_identity_and_short_sha_does_not_claim_global_uniqueness():
    short = operation_for_tool(cli("git commit -m done", {"exit_code": 0, "output": "[main abcdef1] done"}))
    assert short.state == "confirmed" and short.artifact_key is None
    sha = "abcdef1234" * 4
    full = operation_for_tool(cli("git commit -m done", {"exit_code": 0, "output": f"[main {sha}] done"}))
    assert full.artifact_key == f"group/project@{sha}"
    assert operation_for_tool(cli("git commit -m done", {"exit_code": 0, "output": "nothing to commit"})).state == "unresolved"


def add_call(conn, session, call_id, command, *, at="2026-11-01T12:00:00Z", result=None, attrs=None):
    conn.execute("INSERT OR IGNORE INTO sessions(id,started_at,created_at,updated_at) VALUES(?,?,?,?)", (session, at, at, at))
    conn.execute("INSERT INTO steps(id,session_id,seq,type,started_at,created_at,updated_at) VALUES(?,?,?,'tool_call',?,?,?)", (call_id, session, int(call_id[-1]), at, at, at))
    conn.execute("INSERT INTO tool_calls(id,step_id,session_id,logical_call_id,tool_name,input_preview_redacted,output_preview_redacted,raw_attrs_json,created_at,updated_at) VALUES(?,?,?,?,'exec_command',?,?,?,?,?)", (call_id, call_id, session, call_id, json.dumps({"cmd": command}), json.dumps(result or {"exit_code": 0, "output": URL}), json.dumps(attrs or {}), at, at))


def test_refresh_is_idempotent_scoped_and_artifact_counts_precede_pagination(tmp_path):
    conn = connect_sqlite(tmp_path / "test.db")
    migrate(conn)
    add_call(conn, "s1", "call1", "glab mr create")
    add_call(conn, "s2", "call2", "glab mr create")
    add_call(conn, "s1", "call3", "glab mr view 42")
    assert refresh_git_activity(conn) == {"operations": 3}
    refresh_git_activity(conn, session_ids={"s1"})
    result = git_activity(conn, limit=1)
    assert result["total_operations"] == 3 and len(result["operations"]) == 1
    assert result["created_merge_requests"] == 1
    assert git_activity(conn, session_ids={"s2"})["total_operations"] == 1
    assert git_activity(conn, session_ids=set())["total_operations"] == 0
    conn.execute("UPDATE tool_calls SET input_preview_redacted='{}' WHERE id='call1'")
    refresh_git_activity(conn, session_ids={"s1"})
    assert git_activity(conn)["total_operations"] == 2
    assert git_activity(conn)["created_merge_requests"] == 1
    conn.execute("UPDATE evidence SET session_id=NULL WHERE session_id='s2'")
    assert git_activity(conn)["created_merge_requests"] == 0
    conn.close()


def test_event_window_and_undated_coverage(tmp_path):
    conn = connect_sqlite(tmp_path / "test.db")
    migrate(conn)
    add_call(conn, "s1", "call1", "glab mr create", at="2026-11-01T00:00:00+02:00")
    add_call(conn, "s1", "call2", "glab mr create", at="2026-11-01T12:00:00Z")
    add_call(conn, "s1", "call3", "glab mr create", attrs={"reflect.time.provenance": "synthetic"})
    refresh_git_activity(conn)
    result = git_activity(conn, date_from="2026-11-01T00:00:00Z", date_to="2026-11-02T00:00:00Z")
    assert result["total_operations"] == 1
    assert result["undated_operations"] == 1
    conn.close()


@pytest.mark.parametrize("command", [
    "cd '/project path' && git add . && git commit -m 'quoted; git push'",
    "git add . 2>&1 && command git commit -m 'quoted; git push'",
    "cd /project; env GIT_OPTION=test rtk git add . | head; git commit -m 'quoted; git push'",
])
def test_command_chain_keeps_each_git_observation_without_claiming_results(tmp_path, command):
    conn = connect_sqlite(tmp_path / "git.db")
    migrate(conn)
    add_call(conn, "s1", "call1", command,
             result={"exit_code": 0, "output": "[main abcdef1] commit"})
    add_call(conn, "s1", "call2", "cd /project && echo 'git push'")
    add_call(conn, "s1", "call3", "cd /project && git status # && git push")
    add_call(conn, "s1", "call4", "if true; then git push; git commit; fi")
    refresh_git_activity(conn, session_ids={"s1"})
    result = git_activity(conn, session_ids={"s1"})
    assert {row["operation"] for row in result["operations"]} == {"add", "commit"}
    assert result["states"] == {"unresolved": 2}
    assert result["distinct_commits"] == 0
    assert all(row["repository"] is None and row["artifact_id"] is None for row in result["operations"])
    refresh_git_activity(conn, session_ids={"s1"})
    assert git_activity(conn, session_ids={"s1"})["total_operations"] == 2
    conn.close()


@pytest.mark.parametrize("command", ["rtk gh pr view 42", "command gh pr view 42", "env PAGER=cat gh pr view 42"])
def test_known_wrappers_leave_a_single_command_result_attributable(command):
    op = operation_for_tool(cli(command, {"exit_code": 0}))
    assert (op.provider, op.operation, op.state) == ("github", "read", "confirmed")


def test_wrapped_git_operations_keep_separate_results_and_canonical_owner(tmp_path):
    conn = connect_sqlite(tmp_path / 'test.db')
    migrate(conn)
    add_call(conn, 's1', 'call1', 'unused')
    code = 'text(await tools.exec_command({cmd:"git status"})); text(await tools.exec_command({cmd:"git commit -m done"}));'
    results = '\n'.join(json.dumps(item) for item in [
        {'exit_code':0,'output':'clean'}, {'exit_code':1,'output':'commit failed'}])
    conn.execute("UPDATE tool_calls SET tool_name='exec',input_preview_redacted=?,output_preview_redacted=?", (code,results))
    assert refresh_git_activity(conn)['operations'] == 2
    operations = git_activity(conn)['operations']
    assert {(o['operation'],o['state']) for o in operations} == {('status','confirmed'),('commit','failed')}
    assert {o['tool_call_id'] for o in operations} == {'call1'}
    assert git_activity(conn)['distinct_commits'] == 0
    assert refresh_git_activity(conn)['operations'] == 2
    conn.close()


def test_gitlab_mr_iid_is_preserved_for_captured_mcp_reads():
    result = operation_for_tool({'tool_name':'mcp__gitlab_mcp__get_merge_request_details',
        'input_preview_redacted':'{"project_id":123,"mr_iid":42}'})
    assert result.artifact_id == '42'
    assert result.repository == '123'
    assert result.state == 'attempted'


def test_captured_input_migration_repairs_git_without_queuing_raw_replay(tmp_path):
    conn = connect_sqlite(tmp_path / 'migration.db')
    migrate(conn)
    add_call(conn, 's1', 'call1', 'unused')
    conn.execute("UPDATE tool_calls SET tool_name='exec',input_preview_redacted=?,output_preview_redacted=?", (
        'text(await tools.exec_command({cmd:"git status"}));', json.dumps({'exit_code':0,'output':'clean'})))
    conn.execute('DELETE FROM pending_refresh')
    conn.execute('DELETE FROM schema_migrations WHERE version=56')
    assert migrate(conn) == [56]
    assert git_activity(conn)['operations'][0]['operation'] == 'status'
    assert conn.execute('SELECT phase FROM pending_refresh').fetchall() == [('improvements',)]
    assert migrate(conn) == []
    conn.close()
