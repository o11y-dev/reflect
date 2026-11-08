import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_dashboard_sql_api import _seed_sql_report_db

from reflect.dashboard_queries import build_dashboard_payload
from reflect.dashboard_server import DashboardDataCache, build_dashboard_app
from reflect.dashboard_sessions import load_session_detail
from reflect.preparation import PreparationCoordinator, PreparationProgress, PreparationStage
from reflect.store.pending_refresh import PendingRefreshState
from reflect.store.sqlite import connect_sqlite, mark_snapshot


def test_usage_publishes_before_delayed_graph_and_survives_restart(tmp_path):
    db = tmp_path / "reflect.db"
    _seed_sql_report_db(db)
    timestamp = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    with closing(connect_sqlite(db)) as conn:
        for table in ("sessions", "steps", "session_rollups"):
            conn.execute(f"UPDATE {table} SET started_at = ?, ended_at = ?", (timestamp, timestamp))
        mark_snapshot(conn, ready=True)
    published, release = threading.Event(), threading.Event()

    def prepare(progress):
        with closing(connect_sqlite(db)) as conn:
            mark_snapshot(conn, ready=False)
            conn.execute("UPDATE llm_calls SET input_tokens=777")
            conn.execute("INSERT INTO store_metadata(key,value,updated_at) VALUES ('last_successful_usage_refresh',?,?)", (timestamp, timestamp))
            mark_snapshot(conn, ready=True, profile="usage")
        worker.report_usage_ready({}).result()
        progress(PreparationProgress(PreparationStage.REFRESHING_GRAPH, "Graph deliberately delayed"))
        published.set()
        assert release.wait(5)
        with closing(connect_sqlite(db)) as conn:
            mark_snapshot(conn, ready=True)
        return {}

    worker = PreparationCoordinator(prepare)
    client = TestClient(build_dashboard_app(docs_dir=tmp_path, db_path=db, preparation_worker=worker))
    assert worker.start()
    assert published.wait(3)
    try:
        payload = client.get("/api/data").json()
        assert payload["measurements"]["totals"]["input_tokens"] == 777
        assert payload["snapshot_completed_at"] == timestamp
        assert payload["analysis_state"] == "preparing"
        assert payload["insights"] == {}
        assert client.get("/api/explore/usage").status_code == 200
        for query in (
            "session=sess-sql",
            "session=sess-sql&range=7d",
            "session=sess-sql&agents=codex",
        ):
            response = client.get(f"/api/data?{query}&tab=explore&view=usage")
            assert response.status_code == 200
            selected = response.json()
            assert selected["focused_session_id"] == "sess-sql"
            assert selected["measurements"]["totals"]["input_tokens"] == 777
            assert selected["sqlite"]["tabs"]["usage"]["unique_sessions"] == 1
            assert selected["snapshot_completed_at"] == timestamp
            assert selected["analysis_state"] == "preparing"
            assert selected["insights"] == {}
            assert selected["sqlite"]["tabs"]["graph"]["graph_tool_transitions"] == []
        recorded_graph = client.get("/api/explore/graph")
        assert recorded_graph.status_code == 200
        assert recorded_graph.json()["publication"]["state"] == "recorded"
        historical_findings = client.get("/api/findings")
        assert historical_findings.status_code == 200
        assert historical_findings.json()["publication"]["state"] == "stale"
        assert historical_findings.json()["analysis_state"] == "stale"
        status = client.get("/api/status").json()["preparation"]
        assert status["state"] == "running"
        assert status["usage_completed_at"]
        assert status["stage"] == "refreshing_graph"

        # A normal published cache remains usable while a new process waits
        # for canonical preparation. It retains the original data timestamp.
        with closing(connect_sqlite(db)) as conn:
            mark_snapshot(conn, ready=False)
        cache = DashboardDataCache(
            lambda: (_ for _ in ()).throw(AssertionError("must use published cache")),
            cache_path=db.with_suffix(".dashboard.json"),
            cache_key=f"{db.resolve()}:7d",
            preparation_running=lambda: True,
        )
        assert cache.get() == payload
    finally:
        release.set()
        assert worker.wait(5)
    assert worker.snapshot().state == "complete", worker.snapshot().error
    assert client.get("/api/data").json()["analysis_state"] == "ready"


def test_default_window_preserves_explicit_history_and_session_access(tmp_path):
    db = tmp_path / "reflect.db"
    _seed_sql_report_db(db)
    client = TestClient(build_dashboard_app(docs_dir=tmp_path, db_path=db))
    recent = client.get("/api/data").json()
    history = client.get("/api/data?range=all").json()
    assert recent["range"] == "7d"
    assert recent["session_list_total"] == 0
    assert history["range"] == "all"
    assert history["session_list_total"] == 1
    assert history["measurements"]["totals"]["input_tokens"] == 120
    assert client.get("/api/session/sess-sql").status_code == 200


def test_deferred_history_keeps_usage_but_never_claims_current_analysis(tmp_path):
    db = tmp_path / "reflect.db"
    _seed_sql_report_db(db)
    with closing(connect_sqlite(db)) as conn:
        pending = PendingRefreshState(conn)
        pending.mark({"sess-sql"}, phases=("graph", "improvements"))
        pending.defer({"sess-sql"}, phases=("graph", "improvements"))
        mark_snapshot(conn, ready=True)
    client = TestClient(build_dashboard_app(docs_dir=tmp_path, db_path=db))
    history = client.get("/api/data?range=all").json()
    assert history["analysis_state"] == "historical_pending"
    assert history["measurements"]["totals"]["input_tokens"] == 120
    assert history["analysis_coverage"]["graph"]["deferred_sessions"] == 1
    assert history["insights"] == {}
    graph = client.get("/api/explore/graph?range=all")
    assert graph.status_code == 200
    assert graph.json()["publication"]["reason"] == "historical_pending"
    assert graph.json()["analysis_state"] == "pending"
    assert "reflect refresh --all-history" in graph.json()["analysis_pending_reason"]
    assert "Retry-After" not in graph.headers
    detail = load_session_detail(db, "sess-sql")
    assert detail["derived_state"] == "historical_pending"
    assert detail["usage_state"] == "ready"
    assert detail["total_cost_usd"] == 0.42
    assert detail["measurements"]["totals"]["input_tokens"] == 120
    assert "insights" not in detail and "assessments" not in detail
    assert "reflect refresh --session sess-sql" in detail["analysis_pending_reason"]


def test_dashboard_reuses_one_measurement_for_models_and_costs(tmp_path, monkeypatch):
    import reflect.views.measurements as measurements

    db = tmp_path / "reflect.db"
    _seed_sql_report_db(db)
    original = measurements.build_measurements
    calls = []

    def counted(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(measurements, "build_measurements", counted)
    payload = build_dashboard_payload(db, lazy_heavy_tabs=True, base_tab_names={"models", "costs"})
    assert len(calls) == 1
    assert payload["sqlite"]["tabs"]["models"]["models_by_count"] == {"gpt-5.4": 1}
    assert payload["sqlite"]["tabs"]["costs"]["model_costs"] == {"gpt-5.4": 0.42}


def test_explore_tools_preserves_payload_without_global_turn_timing(tmp_path, monkeypatch):
    import reflect.views.measurements as measurements
    from reflect.dashboard_explore import build_explore_payload

    db = tmp_path / "reflect.db"
    _seed_sql_report_db(db)
    original_measure = measurements.build_measurements
    original_rows = measurements._rows
    boundary_queries = []
    def measure(*args, **kwargs):
        raise AssertionError("Tools must read its own inventories without computing Usage measurements")

    def rows(conn, sql, params=None):
        if "reflect.turn.terminal" in sql:
            boundary_queries.append(sql)
        return original_rows(conn, sql, params)

    monkeypatch.setattr(measurements, "_rows", rows)
    baseline = build_dashboard_payload(db, session_id="sess-sql", base_tab_names={"tools", "mcp"})
    assert boundary_queries
    boundary_queries.clear()
    monkeypatch.setattr(measurements, "build_measurements", measure)
    tools = build_explore_payload(db, "tools", session_id="sess-sql")
    for tab in ("tools", "mcp"):
        for key, value in baseline["sqlite"]["tabs"][tab].items():
            assert tools[key] == value
    assert boundary_queries == []
    monkeypatch.setattr(measurements, "build_measurements", original_measure)
    usage = build_dashboard_payload(db, session_id="sess-sql", lazy_heavy_tabs=True)
    assert boundary_queries, "Default Usage must retain its turn and idle measurements"
    assert "idle_total_ms" in usage["measurements"]["metrics"]


def test_published_cache_rejects_other_scope_and_corrupt_content(tmp_path):
    path = tmp_path / "dashboard.json"
    cache = DashboardDataCache(lambda: {"generation": 1}, cache_path=path, cache_key="7d")
    cache.refresh()
    assert path.stat().st_mode & 0o777 == 0o600
    other = DashboardDataCache(lambda: {"generation": 2}, cache_path=path, cache_key="all")
    assert other.get() == {"generation": 2}
    path.write_text("not json")
    recovered = DashboardDataCache(lambda: {"generation": 3}, cache_path=path, cache_key="7d")
    assert recovered.get() == {"generation": 3}
