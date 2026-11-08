import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from test_workflow_milestones import _seed

from reflect import mcp
from reflect.preparation_pipeline import prepare_sql_report_db
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite, mark_snapshot
from reflect.task_inbox import TaskEventInbox
from reflect.task_runs import MCPSelectedSkillRef, TaskStart


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "reflect.db"
    with closing(connect_sqlite(path)) as conn:
        migrate(conn)
        mark_snapshot(conn, ready=False)
    monkeypatch.setenv("REFLECT_DB_PATH", str(path))
    return path


@contextmanager
def writer_lock(db):
    process = subprocess.Popen(
        [sys.executable, "-u", "-c",
         "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN IMMEDIATE'); "
         "print('locked',flush=True); sys.stdin.readline(); c.rollback()", str(db)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        yield
    finally:
        process.communicate("release\n", timeout=10)
        assert process.returncode == 0


def start(db, **kwargs):
    return TaskStart.capture(
        question="private question must not enter the inbox", workspace_path=db.parent,
        task_file_path=kwargs.pop("task_file_path", None), workflow_id=None,
        selected_skills=kwargs.pop("selected_skills", []), selected_memories=[],
        **kwargs,
    )


def test_busy_mcp_accepts_start_and_completion_then_restart_replays(db, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "wrong-server-environment")
    contract = db.parent / "task.md"
    contract.write_text("original contract")
    with writer_lock(db):
        answer = mcp.reflect_context(
            "private question must not enter the inbox", path=str(db.parent), task_file=str(contract),
            runtime_session_id="caller-session", runtime_agent="codex",
        )
        task_id = answer["task_run_id"]
        assert answer["tracking_state"] == "accepted_pending"
        assert answer["runtime_session_id"] == "caller-session"
        completed = mcp.reflect_complete(task_id, "success", verification_passed=True,
                                         runtime_session_id="caller-session", runtime_agent="codex")
        assert completed["tracking_state"] == "accepted_pending"
        assert mcp.reflect_task_status(task_id)["status"] == "completed"
        assert mcp.reflect_complete(task_id, "success", verification_passed=True)["idempotent"]
        health = TaskEventInbox(db).health()
        assert health["pending_count"] == 2
        assert health["oldest_pending_age_seconds"] >= 0
    contract.write_text("changed before replay")
    inbox = TaskEventInbox(db)  # A fresh process/object needs no in-memory state.
    assert "private question" not in inbox._path(task_id).read_text()
    assert inbox._path(task_id).stat().st_mode & 0o777 == 0o600
    inbox.drain()
    status = inbox.status(task_id)
    assert status.tracking_state == "applied"
    assert status.completed_at == completed["completed_at"]
    assert status.runtime_session_id == "caller-session"
    assert status.task_contract_hash == hashlib.sha256(b"original contract").hexdigest()
    assert inbox.health()["pending_count"] == 0


def test_refresh_drains_pending_tasks(db):
    inbox = TaskEventInbox(db)
    task = start(db)
    inbox.start(task)
    inbox.accept(task.task_run_id, "complete", {"outcome": "success"})
    prepare_sql_report_db(db, otlp_traces=None, include_native_sessions=False,
                          spans_dir=db.parent / "no-spans")
    assert inbox.status(task.task_run_id).tracking_state == "applied"
    assert inbox.status(task.task_run_id).status == "completed"


def test_crash_after_commit_before_cleanup_replays_once(db, monkeypatch):
    inbox = TaskEventInbox(db)
    task = start(db)
    inbox.start(task)
    inbox.accept(task.task_run_id, "complete", {"outcome": "success"})
    original = Path.unlink
    def interrupted(path, *args, **kwargs):
        if path == inbox._path(task.task_run_id):
            raise OSError("simulated crash after commit")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupted)
        with pytest.raises(OSError, match="simulated crash"):
            inbox.drain()
    TaskEventInbox(db).drain()
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM mcp_task_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_event_receipts").fetchone()[0] == 2


def test_projection_and_receipt_roll_back_together(db, monkeypatch):
    inbox = TaskEventInbox(db)
    task = start(db)
    inbox.start(task)
    apply = inbox._apply
    def interrupt(*args):
        apply(*args)
        raise RuntimeError("interrupted before receipt")
    with monkeypatch.context() as patch:
        patch.setattr(inbox, "_apply", interrupt)
        inbox.drain()
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM mcp_task_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_event_receipts").fetchone()[0] == 0
    assert inbox.health()["projection_errors"]
    inbox.drain()
    assert inbox.status(task.task_run_id).tracking_state == "applied"


def test_conflicting_and_concurrent_completions(db):
    inbox = TaskEventInbox(db)
    task = start(db, runtime_session_id="caller", runtime_agent="codex")
    inbox.start(task)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: TaskEventInbox(db).accept(
            task.task_run_id, "complete", {"outcome": "success"}), range(2)))
    assert sorted(result["idempotent"] for result in results) == [False, True]
    assert inbox.health()["pending_count"] == 2
    with pytest.raises(RuntimeError, match="already completed"):
        inbox.accept(task.task_run_id, "complete", {"outcome": "failure"})
    with pytest.raises(ValueError, match="identity conflicts"):
        inbox.accept(task.task_run_id, "complete", {"outcome": "success", "runtime_session_id": "other"})


def test_disk_failure_never_acknowledges_acceptance(db, monkeypatch):
    inbox = TaskEventInbox(db)
    task = start(db)
    def full(*args):
        raise OSError("disk full")
    monkeypatch.setattr(inbox, "_write", full)
    with pytest.raises(OSError, match="disk full"):
        inbox.start(task)
    assert not inbox._path(task.task_run_id).exists()


def test_milestone_survives_pending_start_and_projects_before_completion(db):
    with closing(connect_sqlite(db)) as conn:
        _seed(conn)
        conn.execute("DELETE FROM mcp_task_runs WHERE id='task-run'")
        conn.commit()
    inbox = TaskEventInbox(db)
    task = start(db, runtime_session_id="session", runtime_agent="codex", selected_skills=[
        MCPSelectedSkillRef(skill_id="skill", version_id="skill-version", slug="proven-review")])
    inbox.start(task)
    payload = {"skill_version_id": "skill-version", "milestone_id": "observe",
               "state": "completed", "idempotency_key": "observe", "evidence": {}}
    with writer_lock(db):
        recorded = inbox.accept(task.task_run_id, "milestone", payload)
        assert recorded["created"]
        assert not inbox.accept(task.task_run_id, "milestone", payload)["created"]
        with pytest.raises(ValueError, match="different milestone"):
            inbox.accept(task.task_run_id, "milestone", {**payload, "state": "failed"})
        inbox.accept(task.task_run_id, "complete", {"outcome": "success"})
    inbox.drain()
    status = inbox.status(task.task_run_id)
    assert status.tracking_state == "applied"
    assert status.milestone_count == 1
    assert status.link_state == "linked"
    with closing(connect_sqlite(db)) as conn:
        event = conn.execute("SELECT details_json, created_at FROM improvement_events WHERE event_type='workflow_milestone'").fetchone()
        assert json.loads(event[0])["corroborated"] is False
        assert event[1] == recorded["recorded_at"]


def test_missing_identity_is_visible_and_late_identity_can_be_supplied(db):
    inbox = TaskEventInbox(db)
    task = start(db)
    inbox.start(task)
    assert inbox.status(task.task_run_id).link_state == "no_runtime_session"
    inbox.accept(task.task_run_id, "complete", {"outcome": "success"})
    inbox.accept(task.task_run_id, "complete", {"outcome": "success", "runtime_session_id": "late-id", "runtime_agent": "codex"})
    inbox.drain()
    assert inbox.status(task.task_run_id).runtime_session_id == "late-id"


def test_invalid_event_does_not_block_other_tasks_and_health_reports_it(db):
    from reflect.store.doctor import inspect_database

    inbox = TaskEventInbox(db)
    broken, healthy = start(db), start(db)
    inbox.start(broken)
    inbox.start(healthy)
    data = json.loads(inbox._path(broken.task_run_id).read_text())
    data["events"][0]["kind"] = "invalid"
    inbox._write(inbox._path(broken.task_run_id), data)
    inbox.drain()
    assert inbox.status(healthy.task_run_id).tracking_state == "applied"
    assert inbox.status(broken.task_run_id).projection_error == "Unknown task event"
    with closing(connect_sqlite(db)) as conn:
        health = inspect_database(conn)
        assert health["ok"] is False
        assert health["task_inbox"]["pending_count"] == 1


def test_milestone_retry_after_late_session_link_is_idempotent(db):
    with closing(connect_sqlite(db)) as conn:
        _seed(conn)
        conn.execute("DELETE FROM mcp_task_runs WHERE id='task-run'")
        conn.commit()
    inbox = TaskEventInbox(db)
    task = start(db, selected_skills=[
        MCPSelectedSkillRef(skill_id="skill", version_id="skill-version", slug="proven-review")])
    inbox.start(task)
    inbox.drain()
    payload = {"skill_version_id": "skill-version", "milestone_id": "observe",
               "state": "completed", "idempotency_key": "observe", "evidence": {}}
    inbox.accept(task.task_run_id, "milestone", payload)
    inbox.drain()
    inbox.accept(task.task_run_id, "complete", {"outcome": "success", "runtime_session_id": "session", "runtime_agent": "codex"})
    inbox.drain()
    assert inbox.status(task.task_run_id).link_state == "linked"
    assert inbox.accept(task.task_run_id, "milestone", payload)["created"] is False


def test_two_sessions_in_one_workspace_remain_distinct(db):
    inbox = TaskEventInbox(db)
    tasks = [start(db, runtime_session_id=session, runtime_agent="codex") for session in ["first", "second"]]
    for task in tasks:
        inbox.start(task)
        inbox.accept(task.task_run_id, "complete", {"outcome": "success"})
    inbox.drain()
    assert [inbox.status(task.task_run_id).runtime_session_id for task in tasks] == ["first", "second"]
