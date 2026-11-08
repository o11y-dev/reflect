"""Each Explore view waits only for the inputs it actually needs."""

import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_dashboard_sql_api import _seed_sql_report_db

from reflect.dashboard_explore import build_explore_payload
from reflect.dashboard_server import build_dashboard_app
from reflect.preparation import PreparationCoordinator
from reflect.store.pending_refresh import PendingRefreshState
from reflect.store.sqlite import connect_sqlite, mark_snapshot


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "reflect.db"
    _seed_sql_report_db(path)
    return path


def _client(db):
    return TestClient(build_dashboard_app(
        docs_dir=db.parent, db_path=db, preparation_worker=PreparationCoordinator(lambda _: {}),
    ))


@pytest.mark.parametrize("phase", ["canonical", "graph", "improvements", "deferred"])
def test_cold_explore_views_use_published_usage_and_recorded_inventories(db, phase):
    with closing(connect_sqlite(db)) as conn:
        pending = PendingRefreshState(conn)
        pending.mark({"sess-sql"}, phases=("graph", "improvements") if phase == "deferred" else (phase,))
        mark_snapshot(conn, ready=False)
        if phase != "canonical":
            mark_snapshot(conn, ready=True, profile="usage")
        if phase == "deferred":
            pending.defer({"sess-sql"}, phases=("graph", "improvements"))
            mark_snapshot(conn, ready=True)
    client = _client(db)
    context = client.get("/api/explore/context").json()
    assert context["analysis_state"] == "pending"
    assert context["publication"]["state"] == "recorded"
    assert context["publication"]["snapshot_completed_at"] is None
    assert context["memory"]["total_memories"] == 1
    assert context["exports"]["row_counts"]["tool_calls"] == 2
    tools = client.get("/api/explore/tools?range=all")
    graph = client.get("/api/explore/graph?range=all")
    if phase == "canonical":
        assert tools.status_code == graph.status_code == 503
        assert not db.with_suffix(".dashboard-responses.json").exists()
    else:
        assert tools.status_code == graph.status_code == 200
        assert tools.json()["analysis_state"] == "ready"
        assert "publication" not in tools.json()
        assert tools.json()["tools_by_count"]["exec_command"] == 1
        assert graph.json()["analysis_state"] == "pending"
        assert graph.json()["publication"]["state"] == "recorded"
        assert graph.json()["graph_dep"]["nodes"]
        if phase == "deferred":
            assert graph.json()["publication"]["reason"] == "historical_pending"
            assert "reflect refresh --all-history" in graph.json()["analysis_pending_reason"]
        cached = json.loads(db.with_suffix(".dashboard-responses.json").read_text())
        assert all(entry["payload"].get("publication", {}).get("state") != "recorded"
                   for entry in cached["entries"].values())


def test_tools_and_graph_preserve_exact_scope_without_building_unrelated_dashboard(db, monkeypatch):
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)
        mark_snapshot(conn, ready=True, profile="usage")

    def unrelated(*args, **kwargs):
        raise AssertionError("Non-Usage Explore must use its bounded typed views directly")

    monkeypatch.setattr("reflect.dashboard_explore.build_dashboard_payload", unrelated)
    client = _client(db)
    for view in ("tools", "graph"):
        response = client.get(f"/api/explore/{view}?range=all&session=sess-sql&agents=codex")
        assert response.status_code == 200
        assert response.json()["session_id"] == "sess-sql"
        empty = client.get(f"/api/explore/{view}?range=all&session=sess-sql&agents=missing").json()
        assert empty["scoped"] is True
        assert empty["analysis_coverage"]["graph"]["session_count"] == 0
        assert empty["tools_by_count"] == {} if view == "tools" else empty["graph_dep"]["nodes"] == []
    context = client.get("/api/explore/context?session=missing&agents=missing").json()
    assert context["scoped"] is False
    assert context["inventory_scope"] == "global_stored_inventory"
    assert context["exports"]["row_counts"]["sessions"] == 1


def test_context_tabs_and_readiness_share_one_pinned_read_during_concurrent_refresh(db, monkeypatch):
    from reflect.views import build_report_tab

    connections = []

    def concurrent_update(conn, tab, **kwargs):
        assert conn.in_transaction
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        connections.append(conn)
        if len(connections) == 1:
            with closing(connect_sqlite(db)) as writer:
                writer.execute("UPDATE memories SET content_preview_redacted='Changed after pinned read'")
                mark_snapshot(writer, ready=False)
        return build_report_tab(conn, tab, **kwargs)

    monkeypatch.setattr("reflect.views.build_report_tab", concurrent_update)
    payload = build_explore_payload(db, "context")
    assert len(connections) == 4
    assert all(conn is connections[0] for conn in connections)
    assert payload["analysis_state"] == "ready"
    assert all(item["preview"] != "Changed after pinned read" for item in payload["memory"]["recent_memories"])
    assert build_explore_payload(db, "context")["analysis_state"] == "pending"


def test_completed_graph_cache_precedes_recorded_read_but_new_scope_stays_exact(db):
    client = _client(db)
    before = client.get("/api/explore/graph?range=all&session=sess-sql").json()
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)
        mark_snapshot(conn, ready=True, profile="usage")
    retained = client.get("/api/explore/graph?range=all&session=sess-sql").json()
    assert retained.pop("publication")["state"] == "stale"
    retained.pop("analysis_state")
    before.pop("analysis_state")
    assert retained == before
    different = client.get("/api/explore/graph?range=all&session=missing").json()
    assert different["publication"]["state"] == "recorded"
    assert different["graph_dep"]["nodes"] == []
    with closing(connect_sqlite(db)) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version=(SELECT MAX(version) FROM schema_migrations)")
        conn.commit()
    for path in ("/api/explore/context", "/api/explore/tools?range=all", "/api/explore/graph?range=all&session=sess-sql"):
        assert client.get(path).status_code == 503
