import json

import pytest

from reflect.store import normalize
from reflect.store.ingest import ingest_local_spans_file
from reflect.store.migrate import migrate
from reflect.store.pending_refresh import PendingRefreshState
from reflect.store.sqlite import connect_sqlite


def ingest(conn, path, count=3):
    path.write_text("\n".join(json.dumps({
        "name": f"event-{index}", "traceId": "trace", "spanId": f"span-{index}",
        "parentSpanId": "span-2" if index == 0 else "",
        "start_time_ns": 1793527200000000000 + index * 1000000,
        "end_time_ns": 1793527200000000001 + index * 1000000,
        "attributes": {
            "gen_ai.client.name": "claude", "gen_ai.client.session_id": "session",
            "gen_ai.usage.input_tokens": 17,
        },
    }) for index in range(count)) + "\n")
    ingest_local_spans_file(conn, file_path=path)


def test_multiple_raw_batches_run_session_maintenance_once_and_link_late_parent(tmp_path, monkeypatch):
    conn = connect_sqlite(tmp_path / "state.db")
    try:
        migrate(conn)
        ingest(conn, tmp_path / "events.jsonl")
        calls = {}

        def counted(name, operation):
            def run(*args, **kwargs):
                calls[name] = calls.get(name, 0) + 1
                return operation(*args, **kwargs)
            return run

        names = ("_backfill_parent_step_ids", "backfill_session_context", "backfill_hook_facts",
                 "_refresh_session_statuses", "refresh_session_usage")
        for name in names:
            monkeypatch.setattr(normalize, name, counted(name, getattr(normalize, name)))
        result = normalize.normalize_pending_raw_events(conn, batch_size=2)
        assert result == {"processed": 3, "failed": 0, "skipped": 0}
        assert calls == dict.fromkeys((name for name in names if name != "backfill_hook_facts"), 1)
        assert conn.execute(
            "SELECT parent.summary FROM steps child JOIN steps parent ON parent.id=child.parent_step_id "
            "WHERE child.summary='event-0'"
        ).fetchone()[0] == "event-2"
        assert conn.execute("SELECT input_tokens FROM sessions").fetchone()[0] == 51
        assert PendingRefreshState(conn).capture().pending_for("canonical") == set()
        assert PendingRefreshState(conn).capture().pending_for("cost") == {"session"}
        # Once a late parent is linked, another repair must not rewrite it.
        import sqlite3
        previous_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        before = conn.total_changes
        normalize._backfill_parent_step_ids(conn, {"session"})
        assert conn.total_changes == before
        pending = PendingRefreshState(conn)
        for phase in ("graph", "improvements"):
            pending.acknowledge(pending.capture(), phase)
        child = conn.execute("SELECT id FROM steps WHERE summary='event-0'").fetchone()[0]
        conn.execute("UPDATE steps SET parent_step_id=NULL WHERE id=?", (child,))
        normalize._backfill_parent_step_ids(conn, {"session"})
        assert child in pending.capture().step_scope("graph")["session"]
        assert child in pending.capture().step_scope("improvements")["session"]
        conn.row_factory = previous_factory
    finally:
        conn.close()


def test_crash_after_raw_commit_recovers_usage_without_pending_raw_rows(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    conn = connect_sqlite(path)
    migrate(conn)
    ingest(conn, tmp_path / "events.jsonl", count=1)
    normalize.normalize_pending_raw_events(conn)
    # Native retractions can replay a retained stable raw ID. Session additive
    # caches temporarily include it twice until canonical calls are reconciled.
    conn.execute("UPDATE raw_events SET normalized_status='pending'")
    conn.commit()

    def crash(*args, **kwargs):
        raise RuntimeError("after raw commit")

    with monkeypatch.context() as context:
        context.setattr(normalize, "_refresh_normalized_sessions", crash)
        with pytest.raises(RuntimeError, match="after raw commit"):
            normalize.normalize_pending_raw_events(conn)
    assert conn.execute("SELECT input_tokens FROM sessions").fetchone()[0] == 34
    assert PendingRefreshState(conn).capture().pending_for("canonical") == {"session"}
    conn.close()
    conn = connect_sqlite(path)
    try:
        changed = set()
        assert normalize.normalize_pending_raw_events(conn, changed_session_ids=changed)["processed"] == 0
        assert changed == {"session"}
        assert conn.execute("SELECT input_tokens FROM sessions").fetchone()[0] == 17
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1
        assert not PendingRefreshState(conn).capture().pending_for("canonical")
        assert PendingRefreshState(conn).capture().pending_for("rollup") == {"session"}
    finally:
        conn.close()


def test_failed_session_maintenance_rolls_back_and_preserves_recovery_scope(tmp_path, monkeypatch):
    conn = connect_sqlite(tmp_path / "state.db")
    try:
        migrate(conn)
        ingest(conn, tmp_path / "events.jsonl", count=1)
        operation = normalize.refresh_session_usage

        def fail(*args, **kwargs):
            operation(*args, **kwargs)
            raise RuntimeError("during maintenance")

        with monkeypatch.context() as context:
            context.setattr(normalize, "refresh_session_usage", fail)
            with pytest.raises(RuntimeError, match="during maintenance"):
                normalize.normalize_pending_raw_events(conn)
        assert conn.execute("SELECT normalized_status FROM raw_events").fetchone()[0] == "ok"
        assert PendingRefreshState(conn).capture().pending_for("canonical") == {"session"}
        normalize.normalize_pending_raw_events(conn)
        assert not PendingRefreshState(conn).capture().pending_for("canonical")
    finally:
        conn.close()
