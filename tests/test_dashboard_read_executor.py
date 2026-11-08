from __future__ import annotations

import asyncio
import multiprocessing
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_dashboard_sql_api import _seed_sql_report_db

from reflect.dashboard_read_executor import DashboardReadBusyError, DashboardReadExecutor
from reflect.dashboard_server import build_dashboard_app
from reflect.store.sqlite import (
    SnapshotNotReadyError,
    advance_snapshot_revision,
    connect_sqlite,
    mark_snapshot,
    snapshot_revision,
)


class ControlledPool:
    def __init__(self, **kwargs):
        assert kwargs["mp_context"].get_start_method() == "spawn"
        self.capacity = kwargs["max_workers"]
        self.futures = []
        self.shutdown_calls = []

    def submit(self, *args):
        future = Future()
        self.futures.append(future)
        return future

    def shutdown(self, **kwargs):
        self.shutdown_calls.append(kwargs)


def test_pool_is_lazy_bounded_and_normal_shutdown_cancels_queued_reads(monkeypatch, tmp_path):
    pools = []

    def create(**kwargs):
        pool = ControlledPool(**kwargs)
        pools.append(pool)
        return pool

    monkeypatch.setattr("reflect.dashboard_read_executor.ProcessPoolExecutor", create)
    executor = DashboardReadExecutor()
    assert pools == []
    futures = [executor.submit("explore", tmp_path, "tools") for _ in range(4)]
    assert len(pools) == 1 and pools[0].capacity == 2
    with pytest.raises(DashboardReadBusyError):
        executor.submit("dashboard", tmp_path)
    with pytest.raises(ValueError, match="Unknown dashboard read"):
        executor.submit("apply", tmp_path)
    executor.close()
    assert all(future.cancelled() for future in futures)
    assert pools[0].shutdown_calls == [{"wait": True, "cancel_futures": True}]
    executor.close()
    assert len(pools[0].shutdown_calls) == 1
    with pytest.raises(RuntimeError, match="closed"):
        executor.submit("dashboard", tmp_path)


def test_cancelled_http_wait_retains_capacity_until_running_read_finishes(monkeypatch, tmp_path):
    pool = ControlledPool(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    monkeypatch.setattr("reflect.dashboard_read_executor.ProcessPoolExecutor", lambda **_: pool)
    executor = DashboardReadExecutor(max_workers=1, max_outstanding=1)

    async def exercise():
        task = asyncio.create_task(executor.read("explore", tmp_path, "tools"))
        await asyncio.sleep(0)
        running = pool.futures[0]
        assert running.set_running_or_notify_cancel()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not running.cancelled()
        with pytest.raises(DashboardReadBusyError):
            executor.submit("dashboard", tmp_path)
        running.set_result({"ready": True})
        queued = executor.submit("dashboard", tmp_path)
        queued.cancel()

    asyncio.run(exercise())
    executor.close()


def test_real_spawn_reads_preserve_payload_errors_and_cleanup(tmp_path):
    db = tmp_path / "reflect.db"
    _seed_sql_report_db(db)
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=True)
    before = {process.pid for process in multiprocessing.active_children()}
    executor = DashboardReadExecutor(max_workers=1, max_outstanding=2)
    try:
        payload = executor.submit("explore", db, "tools", range_name="all").result(timeout=15)
        assert payload["tools_by_count"]["exec_command"] == 1
        assert payload["analysis_state"] == "ready"
        published = executor.submit(
            "tools", db, range_name="all", read_revision=snapshot_revision(db, profile="usage"),
        ).result(timeout=15)
        assert published == payload
        children = [process for process in multiprocessing.active_children() if process.pid not in before]
        assert len(children) == 1
        with closing(connect_sqlite(db)) as conn:
            mark_snapshot(conn, ready=False)
        with pytest.raises(SnapshotNotReadyError):
            executor.submit("explore", db, "tools", range_name="all").result(timeout=15)
    finally:
        executor.close()
    assert not any(process.is_alive() for process in children)


def test_routes_keep_scope_cache_and_mutations_in_parent(tmp_path, monkeypatch):
    db = tmp_path / "reflect.db"
    _seed_sql_report_db(db)
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=True)

    class Reads:
        calls = []
        busy = False

        def submit(self, operation, path, *args, **kwargs):
            self.calls.append((operation, path, args, kwargs))
            if self.busy:
                raise DashboardReadBusyError("busy")
            future = Future()
            future.set_result({"analysis_state": "ready", "selected": kwargs.get("q")})
            return future

        async def read(self, operation, path, *args, **kwargs):
            return self.submit(operation, path, *args, **kwargs).result()

    reads = Reads()
    client = TestClient(build_dashboard_app(docs_dir=tmp_path, db_path=db, read_executor=reads))
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/data").status_code == 200
    assert reads.calls == []
    assert client.get("/api/explore/tools?agents=codex&q=one").status_code == 200
    operation, path, args, kwargs = reads.calls[-1]
    assert (operation, path, args) == ("tools", db, ())
    assert kwargs["range_name"] == "7d" and kwargs["agents"] == {"codex"} and kwargs["q"] == "one"
    reads.busy = True
    with closing(connect_sqlite(db)) as conn:
        advance_snapshot_revision(conn)
        conn.commit()
    retained = client.get("/api/explore/tools?agents=codex&q=one")
    assert retained.status_code == 200 and retained.json()["publication"]["state"] == "stale"
    assert retained.json()["selected"] == "one"
    missing = client.get("/api/explore/tools?agents=codex&q=two")
    assert missing.status_code == 503 and missing.json()["state"] == "busy"
    reads.busy = False
    assert client.get("/api/data?tab=sessions&q=one").status_code == 200
    assert reads.calls[-1][0] == "dashboard"
    assert client.get("/api/data?session=sess-sql").status_code == 200
    assert reads.calls[-1][0] == "session"
    assert client.get("/api/session/sess-sql").status_code == 200
    assert reads.calls[-1][0] == "conversation"
    count = len(reads.calls)
    client.get("/api/workflows/missing/preview")
    client.post("/api/workflows/missing/apply", json={})
    assert len(reads.calls) == count


def test_real_server_owns_executor_cleanup_when_serving_fails(monkeypatch, tmp_path):
    import uvicorn

    import reflect.dashboard_server as server

    events = []

    class Reads:
        def close(self):
            events.append("closed")

    reads = Reads()
    monkeypatch.setattr(server, "DashboardReadExecutor", lambda: reads)

    def app(**kwargs):
        assert kwargs["read_executor"] is reads
        events.append("app")
        return object()

    def serve(*args, **kwargs):
        events.append("serve")
        raise RuntimeError("server stopped")

    monkeypatch.setattr(server, "build_dashboard_app", app)
    monkeypatch.setattr(uvicorn, "run", serve)
    with pytest.raises(RuntimeError, match="server stopped"):
        server._serve_dashboard(8765, tmp_path, db_path=Path("unused"), open_browser=False)
    assert events == ["app", "serve", "closed"]


def test_default_cache_reload_does_not_block_status_on_the_same_event_loop(monkeypatch, tmp_path):
    import reflect.dashboard_server as server

    db = tmp_path / "reflect.db"
    _seed_sql_report_db(db)
    entered, release = threading.Event(), threading.Event()
    cache_type = server.DashboardDataCache

    def pending_cache(*args, **kwargs):
        cache = cache_type(*args, **kwargs)
        published = cache.get()

        def reload():
            entered.set()
            assert release.wait(5)
            return published

        cache._refresh_loader = reload
        cache._refresh_pending = True
        return cache

    monkeypatch.setattr(server, "DashboardDataCache", pending_cache)
    app = server.build_dashboard_app(docs_dir=tmp_path, db_path=db)
    # Context-managed TestClient uses one portal/event loop for both requests.
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as requests:
        data = requests.submit(client.get, "/api/data")
        assert entered.wait(2)
        try:
            status = requests.submit(client.get, "/api/status")
            assert status.result(timeout=2).status_code == 200
            assert not data.done()
        finally:
            release.set()
        assert data.result(timeout=3).status_code == 200
