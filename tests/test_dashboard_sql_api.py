from __future__ import annotations

from collections import Counter

from fastapi.testclient import TestClient

from reflect.dashboard import _build_dashboard_app
from reflect.models import TelemetryStats
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


def _stats() -> TelemetryStats:
    return TelemetryStats(
        session_files=0,
        span_files=0,
        total_events=0,
        events_by_type=Counter(),
        events_by_file={},
        sessions_seen=set(),
        session_events={},
        session_models={},
        session_first_ts={},
        agents={},
        session_tokens={},
    )


def _seed_sql_report_db(db_path):
    now = "2026-05-01T09:00:00+00:00"
    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        conn.execute(
            """
            INSERT INTO agents(id, name, kind, version, created_at, updated_at)
            VALUES ('agent-codex', 'codex', 'cli', 'test', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions(
              id, agent_id, started_at, ended_at, status, title, input_tokens,
              output_tokens, estimated_cost_usd, created_at, updated_at
            )
            VALUES (
              'sess-sql', 'agent-codex', '2026-05-01T10:00:00+00:00',
              '2026-05-01T10:02:00+00:00', 'completed', 'SQL session',
              120, 30, 0.42, ?, ?
            )
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO steps(id, session_id, seq, type, started_at, status, created_at, updated_at)
            VALUES ('step-sql', 'sess-sql', 1, 'llm_call', '2026-05-01T10:00:00+00:00', 'completed', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO llm_calls(
              id, step_id, session_id, provider, request_model, response_model,
              input_tokens, output_tokens, estimated_cost_usd, created_at, updated_at
            )
            VALUES ('llm-sql', 'step-sql', 'sess-sql', 'openai', 'gpt-5.4', 'gpt-5.4', 120, 30, 0.42, ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO session_rollups(
              session_id, agent, started_at, ended_at, duration_ms, prompt_count,
              tool_call_count, error_count, input_tokens, output_tokens,
              cache_read_tokens, cache_write_tokens, total_cost, updated_at
            )
            VALUES (
              'sess-sql', 'codex', '2026-05-01T10:00:00+00:00',
              '2026-05-01T10:02:00+00:00', 120000, 1, 2, 0,
              120, 30, 0, 0, 0.42, ?
            )
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO tool_rollups(
              tool_name, agent, call_count, success_count, error_count, total_duration_ms, updated_at
            )
            VALUES ('exec_command', 'codex', 2, 2, 0, 500, ?)
            """,
            (now,),
        )
        conn.commit()
    finally:
        conn.close()


def test_dashboard_api_embeds_sql_view_models(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    response = TestClient(app).get("/api/data")

    assert response.status_code == 200
    sqlite_payload = response.json()["sqlite"]
    assert sqlite_payload["overview"]["session_count"] == 1
    assert sqlite_payload["overview"]["top_tools"][0]["tool_name"] == "exec_command"
    assert sqlite_payload["sessions"]["rows"][0]["session_id"] == "sess-sql"


def test_sql_only_dashboard_api_does_not_build_legacy_json(tmp_path, monkeypatch):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)

    def _raise_legacy_json(_stats):
        raise AssertionError("legacy dashboard JSON should not be built")

    monkeypatch.setattr("reflect.dashboard._build_dashboard_json", _raise_legacy_json)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path, sql_only=True)

    response = TestClient(app).get("/api/data")

    assert response.status_code == 200
    payload = response.json()
    assert payload["sql_only"] is True
    assert payload["sqlite"]["overview"]["session_count"] == 1
    assert payload["sessions"][0]["id"] == "sess-sql"


def test_dashboard_sql_sessions_endpoint_filters_from_sql(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    overview = TestClient(app).get("/api/sql/overview")
    sessions = TestClient(app).get("/api/sql/sessions", params={"agent": "codex", "model": "gpt-5.4"})

    assert overview.status_code == 200
    assert overview.json()["estimated_cost_usd"] == 0.42
    assert sessions.status_code == 200
    assert sessions.json()["total"] == 1
    assert sessions.json()["rows"][0]["agent"] == "codex"


def test_dashboard_sql_endpoints_are_disabled_without_db(tmp_path):
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=None)

    response = TestClient(app).get("/api/sql/overview")

    assert response.status_code == 404
    assert "not configured" in response.json()["error"]
