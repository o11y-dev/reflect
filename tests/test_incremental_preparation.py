import json
from contextlib import closing

import pytest

from reflect.improvements.service import ImprovementService
from reflect.preparation import SQLiteSnapshotInspector
from reflect.preparation_pipeline import prepare_sql_report_db
from reflect.store.sqlite import connect_sqlite
from reflect.task_runs import MCPTaskRunService, TaskReportRefreshState, TaskStart


@pytest.fixture
def report(tmp_path, monkeypatch):
    monkeypatch.setenv("REFLECT_HOME", str(tmp_path / "home"))

    def no_network(*args, **kwargs):
        raise RuntimeError("pricing network disabled")

    monkeypatch.setattr("reflect.pricing._fetch_json_url", no_network)
    db, spans = tmp_path / "reflect.db", tmp_path / "spans"
    spans.mkdir()
    write_events(spans, "a", 1)
    write_events(spans, "b", 1)
    prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    return db, spans


def write_events(spans, session_id, count):
    (spans / f"{session_id}.jsonl").write_text("".join(json.dumps({
        "name": "chat", "spanId": f"{session_id}-{index}",
        "start_time_ns": 1780000000000000000 + index * 100,
        "end_time_ns": 1780000000000000010 + index * 100,
        "attributes": {
            "gen_ai.client.name": "future-provider",
            "gen_ai.client.session_id": session_id,
            "gen_ai.operation.name": "chat",
            "gen_ai.usage.input_tokens": 10,
        },
    }) + "\n" for index in range(count)))


def guard_membership(conn, session_id=None):
    predicate = "" if session_id is None else f"WHEN OLD.session_id = '{session_id}'"
    conn.execute(f"""CREATE TRIGGER preserve_membership
        BEFORE DELETE ON execution_unit_steps {predicate}
        BEGIN SELECT RAISE(ABORT, 'unchanged evidence was rebuilt'); END""")
    conn.commit()


def observed_usage(conn):
    return tuple(conn.execute("SELECT SUM(input_tokens), COUNT(*) FROM llm_calls").fetchone())


def test_unchanged_refresh_preserves_membership_and_usage(report):
    db, spans = report
    with closing(connect_sqlite(db)) as conn:
        before = observed_usage(conn)
        guard_membership(conn)
    result = prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    assert result["changed_sessions"] == 0
    assert result["improvements"]["execution_units"] == 0
    assert result["graph"]["skipped"] == 1
    assert result["rollups"]["skipped"] == 1
    assert result["timings"]["total_seconds"] > 0
    assert all(value >= 0 for value in result["timings"].values())
    with closing(connect_sqlite(db)) as conn:
        assert observed_usage(conn) == before == (20, 2)


def test_append_refreshes_only_affected_evidence_without_double_counting(report):
    db, spans = report
    with closing(connect_sqlite(db)) as conn:
        guard_membership(conn, "b")
    write_events(spans, "a", 2)
    result = prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    assert result["changed_sessions"] == 1
    with closing(connect_sqlite(db)) as conn:
        assert observed_usage(conn) == (30, 3)
        assert conn.execute("SELECT COUNT(*) FROM execution_unit_steps WHERE session_id = 'a'").fetchone()[0] == 2
        assert conn.execute("SELECT SUM(input_tokens) FROM session_rollups").fetchone()[0] == 30
    prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    with closing(connect_sqlite(db)) as conn:
        assert observed_usage(conn) == (30, 3)


def test_missing_execution_evidence_is_repaired_without_touching_other_sessions(report):
    db, spans = report
    with closing(connect_sqlite(db)) as conn:
        conn.execute("DELETE FROM execution_units WHERE session_id = 'a'")
        guard_membership(conn, "b")
    result = prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    assert result["changed_sessions"] == 0
    assert result["improvements"]["execution_units"] == 1
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_unit_steps WHERE session_id = 'a'").fetchone()[0] == 1
        assert observed_usage(conn) == (20, 2)


def test_failed_report_only_derivation_keeps_checkpoint_for_retry(report, monkeypatch):
    db, spans = report
    with closing(connect_sqlite(db)) as conn:
        task = TaskStart.capture(
            question="Observe the session", workspace_path=spans.parent, task_file_path=None,
            workflow_id=None, selected_skills=[], selected_memories=[],
            runtime_session_id="a", runtime_agent="future-provider",
        )
        MCPTaskRunService(conn).apply_start(task)
        checkpoint_before = conn.execute(
            "SELECT value FROM store_metadata WHERE key = ?", (TaskReportRefreshState.METADATA_KEY,),
        ).fetchone()[0]

    def fail(_self, *, session_ids=None, measure=True, dependencies_expanded=False, step_scope=None, reassess_unit_ids=None):
        assert session_ids == {"a"}
        raise RuntimeError("derivation interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(ImprovementService, "refresh", fail)
        with pytest.raises(RuntimeError, match="derivation interrupted"):
            prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    assert not SQLiteSnapshotInspector(db).inspect().ready
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute(
            "SELECT value FROM store_metadata WHERE key = ?", (TaskReportRefreshState.METADATA_KEY,),
        ).fetchone()[0] == checkpoint_before
        assert TaskReportRefreshState(conn).inspect().session_ids == {"a"}
    prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    assert SQLiteSnapshotInspector(db).inspect().ready
    with closing(connect_sqlite(db)) as conn:
        assert not TaskReportRefreshState(conn).inspect().session_ids
        assert observed_usage(conn) == (20, 2)


def test_report_arriving_after_input_capture_remains_dirty(report, monkeypatch):
    db, spans = report
    original = ImprovementService.refresh

    def complete_during_refresh(service, *, session_ids=None, measure=True, dependencies_expanded=False, step_scope=None, reassess_unit_ids=None):
        result = original(service, session_ids=session_ids, measure=measure, dependencies_expanded=dependencies_expanded, step_scope=step_scope, reassess_unit_ids=reassess_unit_ids)
        task = TaskStart.capture(
            question="A late report", workspace_path=spans.parent, task_file_path=None,
            workflow_id=None, selected_skills=[], selected_memories=[],
            runtime_session_id="a", runtime_agent="future-provider",
        )
        MCPTaskRunService(service.conn).apply_start(task)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(ImprovementService, "refresh", complete_during_refresh)
        prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    with closing(connect_sqlite(db)) as conn:
        assert TaskReportRefreshState(conn).inspect().session_ids == {"a"}
    result = prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    assert result["changed_sessions"] == 1
    with closing(connect_sqlite(db)) as conn:
        assert not TaskReportRefreshState(conn).inspect().session_ids
        assert observed_usage(conn) == (20, 2)


def test_report_before_telemetry_does_not_rebuild_existing_history(report):
    db, spans = report
    with closing(connect_sqlite(db)) as conn:
        task = TaskStart.capture(
            question="Telemetry has not arrived yet", workspace_path=spans.parent, task_file_path=None,
            workflow_id=None, selected_skills=[], selected_memories=[],
            runtime_session_id="pending", runtime_agent="future-provider",
        )
        MCPTaskRunService(conn).apply_start(task)
        guard_membership(conn)
    result = prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    assert result["improvements"]["execution_units"] == 0
    assert result["changed_sessions"] == 0
    with closing(connect_sqlite(db)) as conn:
        conn.execute("DROP TRIGGER preserve_membership")
        conn.commit()
    write_events(spans, "pending", 1)
    prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute(
            "SELECT execution_unit_id FROM mcp_task_runs WHERE id = ?", (task.task_run_id,),
        ).fetchone()[0]
        assert observed_usage(conn) == (30, 3)


@pytest.mark.parametrize("already_pending", [False, True])
def test_consumer_reassessment_is_checkpointed_and_retry_repairs_full_peer(report, monkeypatch, already_pending):
    from reflect.store.pending_refresh import PendingRefreshState

    db, spans = report
    with closing(connect_sqlite(db)) as conn:
        conn.execute("INSERT INTO repos(id,full_name,created_at,updated_at) VALUES ('shared','shared','2026-01-01','2026-01-01')")
        conn.execute("UPDATE sessions SET repo_id='shared'")
        conn.execute("UPDATE execution_units SET repo_id='shared'")
        source = conn.execute("SELECT id FROM steps WHERE session_id='a'").fetchone()[0]
        consumer = conn.execute("SELECT id FROM execution_units WHERE session_id='b' AND eligible=1").fetchone()[0]
        pending = PendingRefreshState(conn)
        pending.mark_steps("a", {source}, phases=("improvements",))
        if already_pending:
            pending.mark({"b"}, phases=("improvements",))
        conn.commit()
    original = ImprovementService.refresh
    calls = []

    def fail_after_checkpoint(service, **kwargs):
        calls.append(kwargs)
        durable = PendingRefreshState(service.conn).capture()
        assert durable.step_scope("improvements")["b"] is None
        assert kwargs["reassess_unit_ids"] == ({} if already_pending else {"b": {consumer}})
        raise RuntimeError("interrupted before consumer assessment")

    with monkeypatch.context() as patch:
        patch.setattr(ImprovementService, "refresh", fail_after_checkpoint)
        with pytest.raises(RuntimeError, match="interrupted before consumer"):
            prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)

    def retry(service, **kwargs):
        calls.append(kwargs)
        assert kwargs["reassess_unit_ids"] == {}
        assert kwargs["session_ids"] == {"a", "b"}
        return original(service, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ImprovementService, "refresh", retry)
        result = prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    assert result["improvements"]["reassessed_execution_units"] == 0
    assert len(calls) == 2
    with closing(connect_sqlite(db)) as conn:
        assert not PendingRefreshState(conn).capture().pending_for("improvements")
