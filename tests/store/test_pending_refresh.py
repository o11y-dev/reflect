import json

import pytest

from reflect.store.ingest import ingest_local_spans_file, ingest_native_session_file
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.pending_refresh import REFRESH_PHASES, PendingRefreshState
from reflect.store.sqlite import connect_sqlite


def open_store(path):
    conn = connect_sqlite(path)
    migrate(conn)
    return conn


def clear_pending(conn):
    state = PendingRefreshState(conn)
    captured = state.capture()
    for phase in REFRESH_PHASES:
        state.acknowledge(captured, phase)
    conn.commit()


def test_initialization_and_dirty_scope_use_caller_transaction(tmp_path):
    conn = open_store(tmp_path / "state.db")
    try:
        state = PendingRefreshState(conn)
        assert not state.initialized()
        state.initialize({"legacy"})
        assert state.initialized()
        assert state.capture().pending_for("cost") == {"legacy"}
        conn.rollback()
        assert not state.initialized()
        assert not state.capture().entries
        state.mark({"arrived-before-upgrade"}, phases=("graph",))
        state.initialize({"legacy"})
        conn.commit()
        state.initialize({"must-not-reseed"})
        assert state.capture().pending_for("graph") == {"legacy", "arrived-before-upgrade"}
    finally:
        conn.close()


def test_captured_ack_preserves_new_arrivals_and_other_phases(tmp_path):
    path = tmp_path / "state.db"
    conn = open_store(path)
    try:
        state = PendingRefreshState(conn)
        state.initialize({"changed", "unchanged"})
        conn.commit()
        captured = state.capture()
        other = connect_sqlite(path)
        try:
            PendingRefreshState(other).mark({"changed", "new"})
            other.commit()
        finally:
            other.close()
        state.acknowledge(captured, "cost")
        conn.commit()
        assert state.capture().pending_for("cost") == {"changed", "new"}
        assert state.capture().pending_for("graph") == {"changed", "new", "unchanged"}
        state.acknowledge(captured, "cost")
        assert state.capture().pending_for("cost") == {"changed", "new"}
        latest = state.capture()
        state.acknowledge(latest, "graph", session_ids={"unchanged"})
        assert state.capture().pending_for("graph") == {"changed", "new"}
        # Completing all work and then receiving more work cannot reuse a revision.
        clear_pending(conn)
        state.mark({"changed"}, phases=("cost",))
        state.acknowledge(latest, "cost")
        assert state.capture().pending_for("cost") == {"changed"}
    finally:
        conn.close()


def test_deferred_history_becomes_durable_due_work_after_late_correction(tmp_path):
    path = tmp_path / "state.db"
    conn = open_store(path)
    state = PendingRefreshState(conn)
    state.initialize({"cold", "hot"})
    state.defer({"cold"}, phases=("graph", "improvements"))
    conn.commit()
    captured = state.capture()
    assert captured.pending_for("graph") == {"cold", "hot"}
    assert captured.pending_for("graph", include_deferred=False) == {"hot"}
    assert captured.pending_for("cost", include_deferred=False) == {"cold", "hot"}
    state.mark({"cold"})
    current = state.capture()
    state.acknowledge(current, "canonical")
    state.acknowledge(current, "cost")
    state.acknowledge(current, "rollup")
    # A crash after canonical acknowledgement must not hide old late evidence.
    conn.commit()
    conn.close()
    conn = connect_sqlite(path)
    try:
        state = PendingRefreshState(conn)
        assert state.capture().pending_for("graph", include_deferred=False) == {"cold", "hot"}
        assert state.capture().pending_for("improvements", include_deferred=False) == {"cold", "hot"}
        state.acknowledge(captured, "graph")
        assert state.capture().pending_for("graph", include_deferred=False) == {"cold"}
        with pytest.raises(ValueError, match="Only historical"):
            state.defer({"cold"}, phases=("cost",))
    finally:
        conn.close()


def test_normalization_commit_retains_scope_after_connection_loss_and_replay(tmp_path):
    path = tmp_path / "state.db"
    source = tmp_path / "events.jsonl"
    source.write_text(json.dumps({
        "name": "Stop", "traceId": "trace", "spanId": "span",
        "start_time_ns": 1793527200000000000, "end_time_ns": 1793527201000000000,
        "attributes": {"gen_ai.client.session_id": "session", "gen_ai.usage.input_tokens": 17},
    }) + "\n")
    conn = open_store(path)
    ingest_local_spans_file(conn, file_path=source)
    assert normalize_pending_raw_events(conn)["processed"] == 1
    captured = PendingRefreshState(conn).capture()
    conn.close()
    conn = connect_sqlite(path)
    try:
        assert PendingRefreshState(conn).capture() == captured
        for phase in REFRESH_PHASES:
            assert captured.pending_for(phase) == (set() if phase == "canonical" else {"session"})
        assert normalize_pending_raw_events(conn)["processed"] == 0
        assert PendingRefreshState(conn).capture() == captured
        assert conn.execute("SELECT input_tokens FROM sessions").fetchone()[0] == 17
    finally:
        conn.close()


def test_failed_dirty_record_rolls_back_canonical_batch(tmp_path, monkeypatch):
    source = tmp_path / "events.jsonl"
    source.write_text(json.dumps({
        "name": "Stop", "traceId": "trace", "spanId": "span",
        "start_time_ns": 1793527200000000000, "end_time_ns": 1793527201000000000,
        "attributes": {"gen_ai.client.session_id": "session"},
    }) + "\n")
    conn = open_store(tmp_path / "state.db")
    original = PendingRefreshState.mark_steps

    def fail_after_mark(self, session_id, step_ids, **kwargs):
        original(self, session_id, step_ids, **kwargs)
        if step_ids:
            raise OSError("crash before canonical commit")

    try:
        ingest_local_spans_file(conn, file_path=source)
        monkeypatch.setattr(PendingRefreshState, "mark_steps", fail_after_mark)
        with pytest.raises(OSError, match="before canonical commit"):
            normalize_pending_raw_events(conn)
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert conn.execute("SELECT normalized_status FROM raw_events").fetchone()[0] == "pending"
        assert not PendingRefreshState(conn).capture().entries
    finally:
        conn.close()


def test_native_identity_move_retains_old_scope_before_new_normalization(tmp_path):
    path = tmp_path / "claude.jsonl"

    def write_session(session_id):
        path.write_text(json.dumps({
            "type": "assistant", "sessionId": session_id, "timestamp": "2026-11-01T10:00:00Z",
            "message": {"id": "request", "usage": {"input_tokens": 17}, "content": []},
        }) + "\n")

    conn = open_store(tmp_path / "state.db")
    try:
        write_session("old")
        ingest_native_session_file(conn, file_path=path, agent="claude", skip_unchanged=True)
        normalize_pending_raw_events(conn)
        clear_pending(conn)
        write_session("new")
        ingest_native_session_file(conn, file_path=path, agent="claude", skip_unchanged=True)
        # The retraction already committed canonical changes before normalization.
        assert PendingRefreshState(conn).capture().pending_for("cost") == {"old"}
        assert conn.execute("SELECT input_tokens FROM sessions WHERE id='old'").fetchone()[0] == 0
        normalize_pending_raw_events(conn)
        assert PendingRefreshState(conn).capture().pending_for("graph") == {"old", "new"}
        assert conn.execute("SELECT SUM(input_tokens) FROM sessions").fetchone()[0] == 17
        # Deleted canonical identities must remain available to derived cleanup.
        conn.execute("DELETE FROM sessions WHERE id='old'")
        assert "old" in PendingRefreshState(conn).capture().pending_for("rollup")
    finally:
        conn.close()
