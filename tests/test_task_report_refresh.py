import json
from contextlib import closing

import pytest

from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite
from reflect.task_inbox import TaskEventInbox
from reflect.task_runs import MCPTaskRunService, TaskReportRefreshState, TaskStart

OLD = "2000-01-01T00:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "reflect.db"
    with closing(connect_sqlite(path)) as conn:
        migrate(conn)
        for session_id in ("old-session", "new-session"):
            conn.execute(
                """INSERT INTO sessions(id, started_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?)""", (session_id, OLD, OLD, OLD),
            )
        task = TaskStart.capture(
            question="test", workspace_path=tmp_path, task_file_path=None,
            workflow_id=None, selected_skills=[], selected_memories=[],
            runtime_session_id="old-session", runtime_agent="codex",
        ).model_copy(update={"started_at": OLD})
        MCPTaskRunService(conn).apply_start(task)
        state = TaskReportRefreshState(conn)
        yield conn, state, task, path


def checkpoint(conn, state):
    state.mark_current(state.inspect())
    conn.commit()


@pytest.mark.parametrize("stored", [None, "bad-json", "[]", '{"task":{}}', '{"task":null}'])
def test_missing_or_corrupt_checkpoint_requests_one_full_refresh(store, stored):
    conn, state, _, _ = store
    if stored is not None:
        conn.execute(
            "INSERT INTO store_metadata(key, value, updated_at) VALUES (?, ?, ?)",
            (state.METADATA_KEY, stored, OLD),
        )
    inputs = state.inspect()
    assert inputs.requires_full_refresh
    assert inputs.session_ids == {"old-session"}
    state.mark_current(inputs)
    assert not state.inspect().requires_full_refresh
    assert not state.inspect().session_ids


def test_direct_completion_with_old_event_time_is_changed_without_receipt(store):
    conn, state, task, _ = store
    checkpoint(conn, state)
    MCPTaskRunService(conn).complete(
        task.task_run_id, outcome="success", verification_passed=True, completed_at=OLD,
    )
    assert not conn.execute("SELECT 1 FROM task_event_receipts").fetchone()
    assert state.inspect().session_ids == {"old-session"}
    checkpoint(conn, state)
    assert not state.inspect().session_ids


def test_old_queued_completion_is_seen_after_projection(store, monkeypatch):
    conn, state, task, path = store
    checkpoint(conn, state)
    inbox = TaskEventInbox(path)
    with monkeypatch.context() as patch:
        patch.setattr("reflect.task_inbox.utc_now", lambda: OLD)
        inbox.accept(task.task_run_id, "complete", {"outcome": "partial"})
    assert not state.inspect().session_ids
    inbox.drain(conn=conn)
    assert conn.execute(
        "SELECT completed_at FROM mcp_task_runs WHERE id = ?", (task.task_run_id,),
    ).fetchone()[0] == OLD
    assert state.inspect().session_ids == {"old-session"}


def test_captured_checkpoint_does_not_swallow_concurrent_report(store):
    conn, state, task, path = store
    checkpoint(conn, state)
    captured = state.inspect()
    assert not captured.session_ids
    with closing(connect_sqlite(path)) as other:
        MCPTaskRunService(other).complete(task.task_run_id, outcome="failure", completed_at=OLD)
    state.mark_current(captured)
    conn.commit()
    assert state.inspect().session_ids == {"old-session"}


def test_derived_linkage_and_bookkeeping_do_not_dirty_report(store):
    conn, state, task, _ = store
    checkpoint(conn, state)
    conn.execute(
        """UPDATE mcp_task_runs SET execution_unit_id = NULL, updated_at = ?,
           session_linked_at = ?, session_outcome_recorded = 1,
           skill_usage_recorded_count = 3, memory_exposure_recorded_count = 2
           WHERE id = ?""", ("2099-01-01", "2099-01-01", task.task_run_id),
    )
    assert not state.inspect().session_ids


@pytest.mark.parametrize("new_session", ["new-session", None])
def test_moved_or_retracted_runtime_identity_retains_old_scope(store, new_session):
    conn, state, task, _ = store
    checkpoint(conn, state)
    conn.execute("DELETE FROM execution_units WHERE mcp_task_run_id = ?", (task.task_run_id,))
    conn.execute(
        "UPDATE mcp_task_runs SET runtime_session_id = ? WHERE id = ?", (new_session, task.task_run_id),
    )
    assert state.inspect().session_ids == ({"old-session", new_session} if new_session else {"old-session"})


def test_deleted_report_keeps_previous_session_scope(store):
    conn, state, task, _ = store
    checkpoint(conn, state)
    conn.execute("DELETE FROM mcp_task_runs WHERE id = ?", (task.task_run_id,))
    assert state.inspect().session_ids == {"old-session"}
    checkpoint(conn, state)
    assert not state.inspect().session_ids


def test_identity_conflict_without_updated_timestamp_is_detected(store):
    conn, state, task, _ = store
    checkpoint(conn, state)
    conn.execute(
        "UPDATE mcp_task_runs SET runtime_identity_conflict = 'ambiguous' WHERE id = ?", (task.task_run_id,),
    )
    assert state.inspect().session_ids == {"old-session"}


def test_late_milestone_receipt_and_direct_milestone_each_dirty_scope(store):
    conn, state, task, _ = store
    checkpoint(conn, state)
    conn.execute(
        "INSERT INTO task_event_receipts(event_id, task_run_id, applied_at) VALUES ('receipt', ?, ?)",
        (task.task_run_id, OLD),
    )
    assert state.inspect().session_ids == {"old-session"}
    checkpoint(conn, state)
    conn.execute(
        """INSERT INTO improvement_events(id, entity_type, entity_id, event_type, details_json, created_at)
           VALUES ('milestone', 'mcp_task_run', ?, 'workflow_milestone', ?, ?)""",
        (task.task_run_id, json.dumps({"task_run_id": task.task_run_id, "state": "completed"}), OLD),
    )
    assert state.inspect().session_ids == {"old-session"}


def test_checkpoint_participates_in_caller_transaction(store):
    conn, state, task, _ = store
    checkpoint(conn, state)
    MCPTaskRunService(conn).complete(task.task_run_id, outcome="success")
    state.mark_current(state.inspect())
    conn.rollback()
    assert state.inspect().session_ids == {"old-session"}


@pytest.mark.parametrize("change", ["details", "time", "delete"])
def test_milestone_correction_or_retraction_is_detected(store, change):
    conn, state, task, _ = store
    conn.execute(
        """INSERT INTO improvement_events(id, entity_type, entity_id, event_type, details_json, created_at)
           VALUES ('milestone', 'mcp_task_run', ?, 'workflow_milestone', '{}', ?)""",
        (task.task_run_id, OLD),
    )
    checkpoint(conn, state)
    if change == "delete":
        conn.execute("DELETE FROM improvement_events WHERE id = 'milestone'")
    elif change == "time":
        conn.execute("UPDATE improvement_events SET created_at = '2001-01-01' WHERE id = 'milestone'")
    else:
        conn.execute("UPDATE improvement_events SET details_json = '{\"state\":\"failed\"}' WHERE id = 'milestone'")
    assert state.inspect().session_ids == {"old-session"}
