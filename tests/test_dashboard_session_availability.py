import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_dashboard_sql_api import _seed_sql_report_db

from reflect.dashboard_server import build_dashboard_app
from reflect.dashboard_sessions import load_session_detail
from reflect.preparation import PreparationCoordinator
from reflect.store.hook_facts import HookFactRepository
from reflect.store.sqlite import connect_sqlite, mark_snapshot

DERIVED_FIELDS = {
    "measurements", "pricing_coverage", "cost_scope", "total_cost_usd",
    "git_activity", "insights", "assessments",
}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "reflect.db"
    _seed_sql_report_db(path)
    with closing(connect_sqlite(path)) as conn:
        conn.execute(
            """INSERT INTO execution_units(id, session_id, source, started_at,
               outcome, verification_passed, created_at, updated_at)
               VALUES ('stale-assessment', 'sess-sql', 'session_fallback',
                       '2026-05-01', 'success', 1, '2026-05-01', '2026-05-01')"""
        )
        mark_snapshot(conn, ready=True)
    return path


def test_ready_session_retains_published_derived_contract(db):
    detail = load_session_detail(db, "sess-sql")
    assert detail["derived_state"] == "ready"
    assert detail["session"] == {"id": "sess-sql", "agent": "codex"}
    assert detail.keys() >= DERIVED_FIELDS
    assert detail["assessments"][0]["outcome"] == "success"
    assert detail["total_cost_usd"] == pytest.approx(0.42)
    assert detail["conversation"] and detail["telemetry"]["spans"]


def test_preparing_serves_capture_without_reading_stale_derived_evidence(db, monkeypatch):
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)
    app = build_dashboard_app(
        docs_dir=db.parent, db_path=db,
        preparation_worker=PreparationCoordinator(lambda progress: {}),
    )

    def unpublished(*args, **kwargs):
        raise AssertionError("unpublished derived evidence was queried")

    monkeypatch.setattr("reflect.views.measurements.build_measurements", unpublished)
    monkeypatch.setattr("reflect.store.git_activity.git_activity", unpublished)
    monkeypatch.setattr("reflect.improvements.session_insights.read_session_insights", unpublished)
    client = TestClient(app)
    response = client.get("/api/session/sess-sql")
    assert response.status_code == 200
    detail = response.json()
    assert detail["derived_state"] == "preparing"
    assert detail["session"] == {"id": "sess-sql", "agent": "codex"}
    assert not DERIVED_FIELDS & detail.keys()
    assert detail["conversation"] and detail["tool_inventory"] and detail["telemetry"]["spans"]
    assert client.get("/api/session/not-captured").status_code == 404
    assert client.get("/api/data").status_code == 503


@pytest.mark.parametrize("status", ["unknown", "ok"])
@pytest.mark.parametrize("event", ["PreToolUse", None])
def test_invocation_only_tool_does_not_fabricate_result(db, status, event):
    attrs = json.dumps({"gen_ai.client.hook.event": event, "reflect.tool.terminal": False})
    with closing(connect_sqlite(db)) as conn:
        conn.execute(
            "UPDATE tool_calls SET status = ?, raw_attrs_json = ? WHERE id = 'tool-sql'", (status, attrs),
        )
        mark_snapshot(conn, ready=False)
    detail = load_session_detail(db, "sess-sql")
    events = [event for event in detail["conversation"] if event.get("tool_name") == "exec_command"]
    assert [event["type"] for event in events] == ["tool_call"]


@pytest.mark.parametrize("status", ["unknown", "ok"])
def test_nonterminal_tool_and_mcp_outcomes_stay_unknown(db, status):
    with closing(connect_sqlite(db)) as conn:
        conn.execute(
            """UPDATE tool_calls SET status = ?, output_preview_redacted = 'still running',
               raw_attrs_json = '{"gen_ai.client.hook.event":"PostToolUse","reflect.tool.terminal":false}'
               WHERE id = 'tool-sql'""", (status,),
        )
        conn.execute(
            """UPDATE tool_calls SET status = ?, raw_attrs_json = '{"reflect.tool.terminal":false}'
               WHERE id = 'mcp-sql'""", (status,),
        )
        mark_snapshot(conn, ready=False)
    detail = load_session_detail(db, "sess-sql")
    result = next(event for event in detail["conversation"] if event["type"] == "tool_result")
    assert result["success"] is None
    assert result["preview"] == "still running"
    assert next(event for event in detail["conversation"] if event["type"] == "mcp_call")["success"] is None


def test_session_endpoint_still_requires_current_schema(db):
    with closing(connect_sqlite(db)) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = (SELECT MAX(version) FROM schema_migrations)")
        mark_snapshot(conn, ready=False)
    client = TestClient(build_dashboard_app(docs_dir=db.parent, db_path=db))
    response = client.get("/api/session/sess-sql")
    assert response.status_code == 503
    assert response.json()["state"] == "preparing"
    assert response.headers["Retry-After"] == "2"


@pytest.mark.parametrize("ready", [False, True])
def test_one_pinned_reader_survives_concurrent_commit_and_readiness_change(db, monkeypatch, ready):
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=ready)
    original = HookFactRepository.load_session

    def concurrent_write(repository, session_id):
        result = original(repository, session_id)
        with closing(connect_sqlite(db)) as writer:
            writer.execute(
                """UPDATE tool_calls SET status = 'error', output_preview_redacted = 'new failure'
                   WHERE id = 'tool-sql'"""
            )
            mark_snapshot(writer, ready=not ready)
        return result

    monkeypatch.setattr(HookFactRepository, "load_session", concurrent_write)
    detail = load_session_detail(db, "sess-sql")
    assert detail["derived_state"] == ("ready" if ready else "preparing")
    assert bool(DERIVED_FIELDS & detail.keys()) is ready
    result = next(event for event in detail["conversation"] if event["type"] == "tool_result")
    assert result["success"] is True
    assert result["preview"] != "new failure"


def test_native_conversation_provenance_is_preserved_while_preparing(db):
    native = db.parent / "native.jsonl"
    native.write_text("\n".join(json.dumps(event) for event in [
        {"type": "session_meta", "payload": {"id": "sess-sql"}},
        {"type": "response_item", "timestamp": "2026-05-01T10:00:30Z", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Native response"}],
        }},
    ]) + "\n")
    with closing(connect_sqlite(db)) as conn:
        conn.execute(
            "UPDATE sessions SET source_kind='native_session', source_ref=? WHERE id='sess-sql'",
            (f"native_session:codex:{native}",),
        )
        mark_snapshot(conn, ready=False)
    detail = load_session_detail(db, "sess-sql")
    assert detail["derived_state"] == "preparing"
    assert detail["conversation_source"] == "native"
    assert any(event.get("content") == "Native response" for event in detail["conversation"])
