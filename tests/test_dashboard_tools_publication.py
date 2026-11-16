from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient
from test_dashboard_sql_api import _seed_sql_report_db

from reflect.dashboard_read_executor import _read_payload, read_revision
from reflect.dashboard_server import DashboardResponseCache, build_dashboard_app
from reflect.store.snapshots import PublishedSnapshotStore
from reflect.store.sqlite import (
    SnapshotNotReadyError,
    advance_snapshot_revision,
    connect_sqlite,
    mark_snapshot,
    snapshot_revision,
)
from tests.test_dashboard_sql_api import _publish_report


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "reflect.db"
    _seed_sql_report_db(path)
    with closing(connect_sqlite(path)) as conn:
        mark_snapshot(conn, ready=True)
    _publish_report(path)
    return path


class ControlledReads:
    def __init__(self):
        self.jobs = []
        self.entered = threading.Event()

    def submit(self, operation, db_path, **scope):
        future = Future()
        self.jobs.append((future, operation, scope))
        self.entered.set()
        return future


def client_for(db, reads):
    return TestClient(build_dashboard_app(docs_dir=db.parent, db_path=db, read_executor=reads))


def result(label):
    return {"analysis_state": "ready", "snapshot_completed_at": "original-publication", "label": label}


def test_cold_tools_are_bounded_coalesced_and_published_for_exact_scope(db):
    reads = ControlledReads()
    with client_for(db, reads) as client, ThreadPoolExecutor(max_workers=2) as requests:
        started = time.monotonic()
        first = requests.submit(client.get, "/api/explore/tools?q=one")
        assert reads.entered.wait(2)
        second = requests.submit(client.get, "/api/explore/tools?q=one")
        assert first.result(timeout=3).status_code == second.result(timeout=3).status_code == 503
        assert time.monotonic() - started < 3
        assert len(reads.jobs) == 1
        assert reads.jobs[0][1:] == ("tools", {
            "read_revision": read_revision(db, "tools"), "session_id": "", "q": "one",
            "agents": set(), "model": "all", "status": "all", "range_name": "7d",
        })
        reads.jobs[0][0].set_result(result("one"))
        assert client.get("/api/explore/tools?q=one").json() == result("one")
        assert len(reads.jobs) == 1
        assert client.get("/api/explore/tools?q=two").status_code == 503
        assert len(reads.jobs) == 2
        reads.jobs[1][0].cancel()


def test_ready_revision_survives_restart_and_maintenance_retains_original_timestamp(db):
    reads = ControlledReads()
    client = client_for(db, reads)
    assert client.get("/api/explore/tools").status_code == 503
    reads.jobs[0][0].set_result(result("old"))
    saved = json.loads(db.with_suffix(".dashboard-responses.json").read_text())
    entry = next(value for key, value in saved["entries"].items() if '"/api/explore/tools"' in key)
    restarted_reads = ControlledReads()
    restarted = client_for(db, restarted_reads)
    assert restarted.get("/api/explore/tools").json() == result("old")
    assert restarted_reads.jobs == []
    # Maintenance updates the actual marker without changing the pipeline's last-success time.
    with closing(connect_sqlite(db)) as conn:
        advance_snapshot_revision(conn)
        conn.commit()
    PublishedSnapshotStore(db, reserve_bytes=0).publish(evidence_revision="maintenance")
    stale = restarted.get("/api/explore/tools")
    assert stale.status_code == 200
    assert stale.json()["publication"] == {
        "state": "stale", "reason": "reading", "loaded_at": entry["loaded_at"],
        "snapshot_completed_at": "original-publication",
    }
    assert len(restarted_reads.jobs) == 1
    assert restarted.get("/api/explore/tools").json()["label"] == "old"
    assert len(restarted_reads.jobs) == 1
    restarted_reads.jobs[0][0].set_result(result("new"))
    assert restarted.get("/api/explore/tools").json() == result("new")


def test_revision_change_error_and_cancel_never_publish_current(db):
    reads = ControlledReads()
    client = client_for(db, reads)
    assert client.get("/api/explore/tools").status_code == 503
    with closing(connect_sqlite(db)) as conn:
        advance_snapshot_revision(conn)
        conn.commit()
    PublishedSnapshotStore(db, reserve_bytes=0).publish(evidence_revision="maintenance")
    reads.jobs[0][0].set_result(result("obsolete"))
    assert client.get("/api/explore/tools").status_code == 503
    assert len(reads.jobs) == 2
    reads.jobs[1][0].set_exception(SnapshotNotReadyError("changed during query"))
    assert client.get("/api/explore/tools").status_code == 503
    reads.jobs[2][0].cancel()  # Executor shutdown cancels queued work.
    assert client.get("/api/explore/tools").status_code == 503
    assert len(reads.jobs) == 4
    reads.jobs[3][0].set_result({"analysis_state": "pending"})
    assert not db.with_suffix(".dashboard-responses.json").exists()


def test_small_tools_finish_in_one_request_without_http_owning_the_job(db):
    reads = ControlledReads()
    with client_for(db, reads) as client, ThreadPoolExecutor(max_workers=1) as requests:
        response = requests.submit(client.get, "/api/explore/tools")
        assert reads.entered.wait(2)
        reads.jobs[0][0].set_result(result("quick"))
        assert response.result(timeout=3).json() == result("quick")


def test_cancelled_http_request_does_not_cancel_the_shared_publication(db):
    reads = ControlledReads()
    app = build_dashboard_app(docs_dir=db.parent, db_path=db, read_executor=reads)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            request = asyncio.create_task(client.get("/api/explore/tools"))
            assert await asyncio.to_thread(reads.entered.wait, 2)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert not reads.jobs[0][0].cancelled()
            reads.jobs[0][0].set_result(result("retained"))
            response = await client.get("/api/explore/tools")
            assert response.json() == result("retained")
            assert len(reads.jobs) == 1

    asyncio.run(exercise())


def test_worker_pins_publication_during_maintenance(db, monkeypatch):
    publication = PublishedSnapshotStore(db).current()
    expected = read_revision(db, 'tools')

    def changed(*args, **kwargs):
        with closing(connect_sqlite(db)) as conn:
            advance_snapshot_revision(conn)
            conn.commit()
        PublishedSnapshotStore(db, reserve_bytes=0).publish(evidence_revision='maintenance')
        return result('pinned')

    monkeypatch.setattr('reflect.dashboard_explore.build_explore_payload', changed)
    payload = _read_payload('tools', db, (), {'read_revision': expected, 'publication': publication, 'range_name': '7d'})
    assert payload == result('pinned')
    assert read_revision(db, 'tools') != expected


def test_late_job_cannot_overwrite_newer_cache_and_identical_result_retains_read_time(tmp_path):
    cache = DashboardResponseCache(tmp_path / "responses.json", cache_key="test", default_range="7d")
    revision = ("ready", "generation-1")
    older = Future()
    cache.prepare("scope", revision, read_order=cache.start_read(), submit=lambda: older, current_revision=lambda: revision)
    cache.put("scope", result("newer"), read_order=cache.start_read(), read_revision=revision)
    original = cache.stale("scope", reason="reading")["publication"]["loaded_at"]
    older.set_result(result("older"))
    assert cache.current("scope", revision) == result("newer")
    next_revision = ("ready", "generation-2")
    cache.put("scope", result("newer"), read_revision=next_revision)
    assert cache.current("scope", revision) is None
    assert cache.current("scope", next_revision) == result("newer")
    assert cache.stale("scope", reason="reading")["publication"]["loaded_at"] == original


def snapshot_only(db, state, revision):
    with closing(connect_sqlite(db)) as conn:
        conn.execute(
            "UPDATE store_metadata SET value=?,updated_at=? WHERE key='prepared_snapshot'", (state, revision),
        )
        conn.commit()


def recorded_graph():
    return {
        "analysis_state": "pending", "evidence_state": "recorded",
        "publication": {"state": "recorded", "reason": "preparing", "loaded_at": "original-read"},
        "graph_semantic": {"nodes": [{"id": "recorded-node"}], "edges": []},
    }


def test_filtered_data_invalidates_when_only_derived_snapshot_finishes(db):
    snapshot_only(db, "incomplete", "deriving")
    usage = snapshot_revision(db, profile="usage")
    reads = ControlledReads()
    client = client_for(db, reads)
    path = "/api/data?tab=sessions&q=selected"
    pending = client.get(path)
    assert pending.status_code == 503 and pending.json()["reason"] == "reading"
    partial = {"analysis_state": "preparing", "observed_tokens": 123, "insights": {}}
    reads.jobs[0][0].set_result(partial)
    assert client.get(path).json() == partial
    assert len(reads.jobs) == 1
    snapshot_only(db, "ready", "derived-complete")
    PublishedSnapshotStore(db, reserve_bytes=0).publish(evidence_revision="derived-complete")
    assert snapshot_revision(db, profile="usage") == usage
    stale = client.get(path)
    assert stale.status_code == 200 and stale.json()["publication"]["state"] == "stale"
    assert stale.json()["observed_tokens"] == 123
    assert len(reads.jobs) == 2
    complete = {"analysis_state": "ready", "observed_tokens": 123, "insights": {"validated": True}}
    reads.jobs[1][0].set_result(complete)
    assert client.get(path).json() == complete


def test_cold_graph_retains_recorded_result_only_in_memory_for_its_generation(db):
    snapshot_only(db, "incomplete", "deriving")
    reads = ControlledReads()
    client = client_for(db, reads)
    assert client.get("/api/explore/graph").status_code == 503
    reads.jobs[0][0].set_result(recorded_graph())
    assert client.get("/api/explore/graph").json() == recorded_graph()
    assert len(reads.jobs) == 1
    assert not db.with_suffix(".dashboard-responses.json").exists()
    # Persisting an unrelated complete response cannot leak the recorded graph.
    assert client.get("/api/explore/tools").status_code == 503
    reads.jobs[1][0].set_result(result("tools"))
    saved = json.loads(db.with_suffix(".dashboard-responses.json").read_text())
    assert all("graph" not in key for key in saved["entries"])
    restarted_reads = ControlledReads()
    assert client_for(db, restarted_reads).get("/api/explore/graph").status_code == 503
    restarted_reads.jobs[0][0].cancel()
    snapshot_only(db, "ready", "derived-complete")
    PublishedSnapshotStore(db, reserve_bytes=0).publish(evidence_revision="derived-complete")
    pending = client.get("/api/explore/graph")
    assert pending.status_code == 503 and pending.json()["reason"] == "reading"
    reads.jobs[2][0].set_result(result("complete-graph"))
    assert client.get("/api/explore/graph").json() == result("complete-graph")


def test_completed_graph_is_preferred_without_recomputing_during_pending_snapshot(db):
    reads = ControlledReads()
    client = client_for(db, reads)
    assert client.get("/api/explore/graph").status_code == 503
    reads.jobs[0][0].set_result(result("complete-graph"))
    snapshot_only(db, "incomplete", "deriving")
    retained = client.get("/api/explore/graph").json()
    assert retained["analysis_state"] == "ready"
    assert "publication" not in retained
    assert retained["label"] == "complete-graph"
    assert len(reads.jobs) == 1


def test_recorded_entries_share_existing_cache_bounds_and_never_persist(tmp_path):
    path = tmp_path / "responses.json"
    cache = DashboardResponseCache(path, cache_key="test", default_range="7d", max_entries=2)
    revision = ("ready", "usage", "incomplete", "graph")
    for key in ("one", "two", "three"):
        cache.put(key, recorded_graph(), read_revision=revision, recorded=True)
    assert cache.current("one", revision) is None
    assert cache.current("three", revision) == recorded_graph()
    assert cache.stale("three", reason="preparing") is None
    assert not path.exists()
    cache.put("complete", result("published"), read_revision=revision)
    assert list(json.loads(path.read_text())["entries"]) == ["complete"]


@pytest.mark.parametrize("session_id", ["sess-sql", "absent-session"])
def test_real_session_builder_publishes_populated_and_empty_results(db, session_id):
    from reflect.dashboard_sessions import build_session_payload

    expected = build_session_payload(db, session_id)
    assert "analysis_state" not in expected

    class RealSessionReads:
        calls = 0

        def submit(self, operation, db_path, *args, **kwargs):
            self.calls += 1
            future = Future()
            try:
                future.set_result(_read_payload(operation, db_path, args, {**kwargs, "publication": PublishedSnapshotStore(db_path).current()}))
            except Exception as exc:
                future.set_exception(exc)
            return future

    reads = RealSessionReads()
    client = client_for(db, reads)
    path = f"/api/data?session={session_id}"
    for _ in range(2):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {**expected, "analysis_state": "ready"}
    assert reads.calls == 1
    assert bool(response.json()["sessions"]) is (session_id == "sess-sql")
