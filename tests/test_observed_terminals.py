"""Redacted shapes captured from hook JSONL/OTLP and native harness events."""
import json
from unittest.mock import patch

import pytest

from reflect.execution_units import ExecutionUnitService
from reflect.parsing import _iter_codex_session_spans
from reflect.store.migrate import load_migrations, migrate
from reflect.store.pending_refresh import PendingRefreshSnapshot, PendingRefreshState
from reflect.store.sqlite import connect_sqlite
from reflect.telemetry_facts import observed_turn_terminal
from reflect.views.measurements import build_measurements


@pytest.fixture
def conn(tmp_path):
    db = connect_sqlite(tmp_path / "events.db")
    with patch("reflect.store.migrate.load_migrations", return_value=[m for m in load_migrations() if m.version <= 42]):
        migrate(db)
    yield db
    db.close()


def session(conn, sid="session"):
    conn.execute("INSERT INTO sessions(id,started_at,status,created_at,updated_at) VALUES (?,'2026-09-07T10:00:00Z','unknown','now','now')", (sid,))


def event(conn, key, minute, attrs, *, sid="session"):
    at = f"2026-09-07T10:{minute:02}:00Z"
    seq = conn.execute("SELECT COUNT(*) FROM steps WHERE session_id=?", (sid,)).fetchone()[0]
    conn.execute("""INSERT INTO steps(id,session_id,seq,type,started_at,ended_at,summary,raw_attrs_json,created_at,updated_at)
        VALUES (?,?,?,'conversation_event',?,?,'redacted',?,'now','now')""", (key, sid, seq, at, at, json.dumps(attrs)))


def assessment(conn, sid="session", source="native_turn"):
    row = conn.execute("SELECT boundary_json,outcome FROM execution_units WHERE session_id=? AND source=? AND eligible=1", (sid, source)).fetchone()
    return json.loads(row[0])["assessment"], row[1]


@pytest.mark.parametrize("terminal", [
    {"gen_ai.client.name": "codex", "gen_ai.client.hook.event": "Stop", "reflect.telemetry.origin": "hook_jsonl"},
    {"gen_ai.client.name": "codex", "gen_ai.client.hook.event": "Stop", "reflect.telemetry.origin": "hook_otlp_trace"},
    {"gen_ai.client.name": "cursor", "gen_ai.client.hook.event": "Stop", "reflect.telemetry.origin": "hook_otlp_trace"},
    {"gen_ai.client.name": "claude", "gen_ai.client.hook.event": "Stop", "reflect.turn.terminal": True},
    {"event.name": "codex.task_complete", "reflect.telemetry.origin": "native_otlp_log"},
])
def test_harness_stops_close_observation_without_claiming_success(conn, terminal):
    session(conn)
    event(conn, "prompt", 0, {"gen_ai.client.turn_id": "turn", "gen_ai.client.hook.event": "UserPromptSubmit"})
    conn.execute("""INSERT INTO llm_calls(id,session_id,step_id,input_tokens,output_tokens,raw_attrs_json,created_at,updated_at)
        VALUES ('usage','session','prompt',50,10,?, 'now','now')""", (json.dumps({
            "gen_ai.usage.input_tokens": 50, "gen_ai.usage.output_tokens": 10,
            "gen_ai.client.turn_id": "turn", "reflect.usage.grain": "request",
        }),))
    event(conn, "stop", 2, {"gen_ai.client.turn_id": "turn", **terminal})
    # Duplicate transport capture cannot multiply the unit or create usage.
    event(conn, "duplicate-stop", 2, {"gen_ai.client.turn_id": "turn", **terminal})
    service = ExecutionUnitService(conn)
    service.refresh()
    before = conn.execute("SELECT id,boundary_json FROM execution_units").fetchall()
    service.refresh()
    assert conn.execute("SELECT id,boundary_json FROM execution_units").fetchall() == before
    fact, outcome = assessment(conn)
    assert fact["boundary"] == "closed"
    assert fact["verification"] == fact["outcome"] == "unknown"
    assert outcome is None
    view = build_measurements(conn, include_turns=True)
    assert view.metrics["completed_turns"] == 1
    assert len(view.turns) == 1
    assert view.totals["input_tokens"] == 50
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1


@pytest.mark.parametrize("attrs", [
    {"gen_ai.client.hook.event": "Stop"},
    {"event.name": "task_complete"},
    {"gen_ai.client.output": "task_complete", "reflect.telemetry.origin": "hook_jsonl"},
    {"gen_ai.client.hook.event": "Stop", "reflect.telemetry.origin": "mcp_report"},
    {"reflect.turn.terminal": True, "reflect.time.provenance": "inferred"},
    {"reflect.turn.terminal": True, "reflect.turn.identity_provenance": "synthetic"},
    {"reflect.turn.terminal": False, "gen_ai.client.hook.event": "Stop", "reflect.telemetry.origin": "hook_jsonl"},
])
def test_unproven_or_synthetic_terminal_is_not_a_boundary(attrs):
    assert not observed_turn_terminal(attrs)


def test_late_work_reopens_boundary_until_observed_rerun_stops(conn):
    session(conn)
    event(conn, "start", 0, {"gen_ai.client.turn_id": "turn"})
    event(conn, "stop", 2, {"gen_ai.client.turn_id": "turn", "reflect.turn.terminal": True})
    service = ExecutionUnitService(conn)
    service.refresh()
    assert assessment(conn)[0]["boundary"] == "closed"
    event(conn, "late-edit", 3, {"gen_ai.client.turn_id": "turn", "gen_ai.client.hook.event": "AfterFileEdit"})
    service.refresh()
    assert assessment(conn)[0]["boundary"] == "open_or_ambiguous"
    event(conn, "rerun-stop", 4, {"gen_ai.client.turn_id": "turn", "reflect.turn.terminal": True})
    service.refresh()
    assert assessment(conn)[0]["boundary"] == "closed"


def test_duplicate_stops_use_latest_observed_end_in_both_views(conn):
    session(conn)
    event(conn, "prompt", 0, {"gen_ai.client.turn_id": "turn", "gen_ai.client.hook.event": "UserPromptSubmit"})
    for key, start, end in (("first", 1, 4), ("second", 2, 3)):
        event(conn, key, start, {"gen_ai.client.turn_id": "turn", "reflect.turn.terminal": True})
        conn.execute("UPDATE steps SET ended_at=? WHERE id=?", (f"2026-09-07T10:{end:02}:00Z", key))
    ExecutionUnitService(conn).refresh()
    view = build_measurements(conn, include_turns=True)
    assert view.turns[0].ended_at == "2026-09-07T10:04:00Z"
    assert view.metrics["mean_turn_elapsed_ms"] == 240000
    assert assessment(conn)[0]["boundary"] == "closed"


def test_overlapping_turns_and_unassigned_stop_cannot_close_other_work(conn):
    session(conn)
    session(conn, "other")
    event(conn, "first", 0, {"gen_ai.client.turn_id": "turn"})
    event(conn, "other-stop", 2, {"gen_ai.client.turn_id": "turn", "reflect.turn.terminal": True}, sid="other")
    event(conn, "unassigned", 2, {"gen_ai.client.hook.event": "Stop", "reflect.telemetry.origin": "hook_jsonl"})
    ExecutionUnitService(conn).refresh()
    assert assessment(conn)[0]["boundary"] == "open_or_ambiguous"
    assert assessment(conn, source="session_fallback")[0]["boundary"] == "open_or_ambiguous"
    assert assessment(conn, sid="other")[0]["boundary"] == "closed"


def test_migration_queues_only_changed_boundaries_and_keeps_late_revision(conn):
    session(conn)
    session(conn, "untouched")
    event(conn, "old-stop", 2, {"gen_ai.client.turn_id": "turn", "gen_ai.client.hook.event": "Stop", "reflect.telemetry.origin": "hook_jsonl"})
    event(conn, "incomplete", 0, {"gen_ai.client.turn_id": "turn"}, sid="untouched")
    ExecutionUnitService(conn).refresh()
    # Reproduce the pre-fix assessment while retaining the real captured Stop.
    conn.execute("""UPDATE execution_units SET status='unknown',
        boundary_json=json_set(boundary_json,'$.assessment.boundary','open_or_ambiguous')
        WHERE session_id='session'""")
    state = PendingRefreshState(conn)
    conn.execute("INSERT INTO pending_refresh(phase,session_id,revision,deferred) VALUES('improvements','session','old',1)")
    old_revision = PendingRefreshSnapshot(entries=(("improvements", "session", "old", True),))
    usage_before = conn.execute("SELECT COUNT(*),SUM(input_tokens) FROM llm_calls").fetchone()
    assert migrate(conn) == [43, 44, 45, 46, 47, 48, 49, 50]
    assert state.capture().pending_for("improvements", include_deferred=False) == {"session"}
    state.acknowledge(old_revision, "improvements")
    assert state.capture().pending_for("improvements") == {"session"}
    assert migrate(conn) == []
    migrated = state.capture()
    ExecutionUnitService(conn).refresh(session_ids={"session"})
    state.mark({"session"}, phases=("improvements",))  # telemetry arriving during repair
    state.acknowledge(migrated, "improvements")
    assert state.capture().pending_for("improvements") == {"session"}
    assert assessment(conn)[0]["boundary"] == "closed"
    assert conn.execute("SELECT COUNT(*),SUM(input_tokens) FROM llm_calls").fetchone() == usage_before


def test_migration_reopens_submillisecond_work_after_premature_stop(conn):
    session(conn)
    event(conn, "stop", 0, {"gen_ai.client.turn_id": "turn", "reflect.turn.terminal": True})
    event(conn, "later", 0, {"gen_ai.client.turn_id": "turn"})
    conn.execute("UPDATE steps SET started_at='2026-09-07T10:00:00.000100Z',ended_at='2026-09-07T10:00:00.000100Z' WHERE id='later'")
    ExecutionUnitService(conn).refresh()
    conn.execute("UPDATE execution_units SET boundary_json=json_set(boundary_json,'$.assessment.boundary','closed')")
    assert migrate(conn) == [43, 44, 45, 46, 47, 48, 49, 50]
    assert PendingRefreshState(conn).capture().pending_for("improvements") == {"session"}
    ExecutionUnitService(conn).refresh()
    assert assessment(conn)[0]["boundary"] == "open_or_ambiguous"


def test_migration_tolerates_nonobject_attrs_and_empty_end(conn):
    session(conn)
    event(conn, "unknown", 0, {})
    conn.execute("UPDATE steps SET raw_attrs_json='[]' WHERE id='unknown'")
    event(conn, "stop", 2, {"gen_ai.client.hook.event": "Stop", "reflect.telemetry.origin": "hook_jsonl"})
    conn.execute("UPDATE steps SET ended_at='' WHERE id='stop'")
    ExecutionUnitService(conn).refresh()
    conn.execute("UPDATE execution_units SET boundary_json=json_set(boundary_json,'$.assessment.boundary','open_or_ambiguous')")
    assert migrate(conn) == [43, 44, 45, 46, 47, 48, 49, 50]
    assert PendingRefreshState(conn).capture().pending_for("improvements") == {"session"}
    ExecutionUnitService(conn).refresh()
    assert assessment(conn, source="session_fallback")[0]["boundary"] == "closed"


def test_codex_explicit_terminal_identity_wins_over_another_active_turn(tmp_path):
    records = [
        {"type": "session_meta", "payload": {"id": "redacted-session", "cwd": "/workspace"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "first"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "second"}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "first"}},
    ]
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps({"timestamp": f"2026-11-01T10:0{i}:00Z", **r}) for i, r in enumerate(records)))
    terminal = [span for span in _iter_codex_session_spans(path) if span["attributes"].get("reflect.turn.terminal")]
    assert len(terminal) == 1
    assert terminal[0]["attributes"]["gen_ai.client.turn_id"] == "first"
