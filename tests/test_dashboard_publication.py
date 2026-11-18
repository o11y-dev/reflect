from contextlib import closing

from fastapi.testclient import TestClient
from test_dashboard_sql_api import _seed_sql_report_db

from reflect.dashboard_queries import build_dashboard_payload
from reflect.dashboard_server import build_dashboard_app
from reflect.dashboard_sessions import load_session_detail
from reflect.store.pending_refresh import PendingRefreshState
from reflect.store.snapshots import PublishedSnapshotStore
from reflect.store.sqlite import connect_sqlite, mark_snapshot


def test_default_window_preserves_explicit_history_and_session_access(tmp_path):
    db = tmp_path / "reflect.db"
    _seed_sql_report_db(db)
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=True)
    PublishedSnapshotStore(db, reserve_bytes=0).publish(evidence_revision="fixture")
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
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=True)
    PublishedSnapshotStore(db, reserve_bytes=0).publish(evidence_revision="fixture")
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




def test_complete_publication_survives_partial_refresh_and_server_restart(tmp_path):
    db = tmp_path / 'reflect.db'
    _seed_sql_report_db(db)
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=True)
    store = PublishedSnapshotStore(db, reserve_bytes=0)
    store.publish(evidence_revision='first')
    first = TestClient(build_dashboard_app(docs_dir=tmp_path, db_path=db))
    original = first.get('/api/data?range=all').json()
    with closing(connect_sqlite(db)) as writer:
        mark_snapshot(writer, ready=False)
        writer.execute('UPDATE llm_calls SET input_tokens=777')
        writer.commit()
        restarted = TestClient(build_dashboard_app(docs_dir=tmp_path, db_path=db))
        for client in (first, restarted):
            payload = client.get('/api/data?range=all').json()
            assert payload['measurements']['totals']['input_tokens'] == original['measurements']['totals']['input_tokens']
            assert client.get('/api/explore/tools?range=all').status_code == 200
        mark_snapshot(writer, ready=True)
    store.publish(evidence_revision='second')
    assert first.get('/api/data?range=all').json()['measurements']['totals']['input_tokens'] == 777


def test_response_publication_coalesces_reads_and_rejects_obsolete_results(tmp_path):
    from concurrent.futures import Future

    from reflect.dashboard_server import DashboardResponseCache
    cache = DashboardResponseCache(tmp_path/'cache.json', cache_key='test', default_range='7d')
    key = cache.key('/api/data', {'range':'7d'})
    revision = ('one', '0')
    future = Future()
    def submit():
        return future
    first = cache.prepare(key, revision, read_order=cache.start_read(), submit=submit, current_revision=lambda:revision)
    assert cache.prepare(key, revision, read_order=cache.start_read(), submit=lambda:None, current_revision=lambda:revision) is first
    revision = ('two','0')
    future.set_result({'analysis_state':'ready', 'value':'old'})
    assert cache.current(key, revision) is None
    next_read = Future()
    cache.prepare(key, revision, read_order=cache.start_read(), submit=lambda:next_read, current_revision=lambda:revision)
    next_read.set_result({'analysis_state':'ready', 'value':'new'})
    assert cache.current(key, revision)['value'] == 'new'
    recovered = DashboardResponseCache(tmp_path/'cache.json', cache_key='test', default_range='7d')
    assert recovered.current(key, revision)['value'] == 'new'
    assert recovered.current(recovered.key('/api/data',{'range':'all'}), revision) is None
    (tmp_path/'cache.json').write_text('invalid json')
    assert DashboardResponseCache(tmp_path/'cache.json',cache_key='test',default_range='7d').current(key, revision) is None
