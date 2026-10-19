import json
import os
import selectors
import subprocess
import sys

import pytest

from reflect import mcp, preparation_pipeline
from reflect.context import ReflectContextService
from reflect.memory import MemoryItem, MemoryService, MemorySourceMetadata
from reflect.preparation import PreparationProfile, PreparationStage, SQLiteSnapshotInspector
from reflect.store.migrate import migrate
from reflect.store.sqlite import (
    SnapshotNotReadyError,
    advance_snapshot_revision,
    busy_timeout_ms,
    connect_sqlite,
    connect_sqlite_read_only,
    consistent_snapshot,
    mark_snapshot,
)


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("REFLECT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REFLECT_SQLITE_BUSY_TIMEOUT_MS", "50")

    def no_network(*args, **kwargs):
        raise RuntimeError("pricing network disabled")

    monkeypatch.setattr("reflect.pricing._fetch_json_url", no_network)


def seed_memory(db, root):
    conn = connect_sqlite(db)
    try:
        migrate(conn)
        return MemoryService(conn).remember(MemoryItem(
            content="Run the release gate before publishing.", type="repo_convention", scope="project",
            source_metadata=MemorySourceMetadata(
                source_kind="manual", source_ref="manual", path=str(root / "AGENTS.md"),
                workspace_root=str(root), manual_note=True,
            ),
        ))["id"]
    finally:
        conn.close()


def write_events(spans, count):
    spans.mkdir(exist_ok=True)
    (spans / "events.jsonl").write_text("".join(json.dumps({
        "name": "chat", "spanId": f"event-{i}",
        "start_time_ns": 1780000000000000000 + i * 100,
        "end_time_ns": 1780000000000000010 + i * 100,
        "attributes": {
            "gen_ai.client.name": "future-provider",
            "gen_ai.client.session_id": "session-test",
            "gen_ai.operation.name": "chat",
            "gen_ai.usage.input_tokens": (i + 1) * 10,
        },
    }) + "\n" for i in range(count)))


def test_timeout_configuration_is_validated_and_applied(tmp_path, monkeypatch):
    monkeypatch.setenv("REFLECT_SQLITE_BUSY_TIMEOUT_MS", "1234")
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
        assert conn.isolation_level == "IMMEDIATE"
    finally:
        conn.close()
    for value in ("wrong", "-1", "2147483648"):
        monkeypatch.setenv("REFLECT_SQLITE_BUSY_TIMEOUT_MS", value)
        with pytest.raises(ValueError, match="REFLECT_SQLITE_BUSY_TIMEOUT_MS"):
            busy_timeout_ms()


def test_mcp_keeps_memory_available_when_tracking_writer_is_busy(tmp_path, monkeypatch):
    db = tmp_path / "reflect.db"
    memory_id = seed_memory(db, tmp_path)
    monkeypatch.setenv("REFLECT_DB_PATH", str(db))
    writer = connect_sqlite(db)
    try:
        writer.execute("BEGIN IMMEDIATE")
        answer = mcp.reflect_context("release gate", path=str(tmp_path))
        assert answer["memories"][0]["id"] == memory_id
        assert answer["task_run_id"] is None
        assert answer["next_action"] is None
        assert any("tracking was not recorded" in s for s in answer["limitations"])
        assert writer.execute("SELECT COUNT(*) FROM mcp_task_runs").fetchone()[0] == 0
        writer.rollback()
        retried = mcp.reflect_context("release gate", path=str(tmp_path))
        assert retried["task_run_id"]
    finally:
        writer.close()


def test_interrupted_refresh_rebuilds_existing_session_rollups(tmp_path):
    db, spans = tmp_path / "reflect.db", tmp_path / "spans"
    write_events(spans, 1)
    preparation_pipeline.prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    write_events(spans, 2)

    def interrupt(progress):
        if progress.stage == PreparationStage.UPDATING_CANONICAL_STATE:
            raise RuntimeError("interrupted after canonical commit")

    with pytest.raises(RuntimeError, match="interrupted"):
        preparation_pipeline.prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans, progress=interrupt)
    assert not SQLiteSnapshotInspector(db).inspect().ready
    with pytest.raises(SnapshotNotReadyError):
        connect_sqlite_read_only(db)
    # Recovery must not depend on pending raw rows or a changed session count.
    conn = connect_sqlite_read_only(db, profile=None)
    assert conn.execute("SELECT COUNT(*) FROM raw_events WHERE normalized_status='pending'").fetchone()[0] == 0
    conn.close()
    preparation_pipeline.prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    assert SQLiteSnapshotInspector(db).inspect().ready
    conn = connect_sqlite_read_only(db)
    try:
        assert conn.execute("SELECT input_tokens FROM sessions").fetchone()[0] == 30
        assert conn.execute("SELECT input_tokens FROM session_rollups").fetchone()[0] == 30
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 2
    finally:
        conn.close()


def test_readers_pin_a_generation_and_composed_reads_reject_generation_changes(tmp_path):
    db = tmp_path / "reflect.db"
    writer = connect_sqlite(db)
    migrate(writer)
    mark_snapshot(writer, ready=True)
    reader = connect_sqlite_read_only(db)
    try:
        assert reader.execute("SELECT value FROM store_metadata WHERE key='prepared_snapshot'").fetchone()[0] == "ready"
        mark_snapshot(writer, ready=False)
        assert reader.execute("SELECT value FROM store_metadata WHERE key='prepared_snapshot'").fetchone()[0] == "ready"
        with pytest.raises(SnapshotNotReadyError):
            connect_sqlite_read_only(db)
        mark_snapshot(writer, ready=True)

        @consistent_snapshot
        def mixed_response(db_path):
            mark_snapshot(writer, ready=False)
            mark_snapshot(writer, ready=True)
            return {"must_not_publish": True}

        with pytest.raises(SnapshotNotReadyError, match="changed"):
            mixed_response(db)
    finally:
        reader.close()
        writer.close()


def test_usage_refresh_does_not_publish_stale_dashboard_projections(tmp_path, monkeypatch):
    db, spans = tmp_path / "reflect.db", tmp_path / "spans"
    write_events(spans, 1)
    preparation_pipeline.prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    from reflect.store.ingest import ingest_local_spans_file

    write_events(spans, 2)
    conn = connect_sqlite(db)
    try:
        ingest_local_spans_file(conn, file_path=spans / "events.jsonl")
    finally:
        conn.close()
    preparation_pipeline.prepare_usage_db(db, otlp_traces=None, include_native_sessions=False)
    assert SQLiteSnapshotInspector(db, profile=PreparationProfile.USAGE).inspect().ready
    assert not SQLiteSnapshotInspector(db).inspect().ready
    monkeypatch.setenv("REFLECT_DB_PATH", str(db))
    assert mcp.reflect_usage(global_scope=True, period="all")
    # A later no-change usage refresh must not publish the still-stale report.
    preparation_pipeline.prepare_usage_db(db, otlp_traces=None, include_native_sessions=False)
    assert not SQLiteSnapshotInspector(db).inspect().ready
    preparation_pipeline.prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    assert SQLiteSnapshotInspector(db).inspect().ready


def test_no_change_usage_refresh_preserves_completed_report(tmp_path):
    db, spans = tmp_path / "reflect.db", tmp_path / "spans"
    write_events(spans, 1)
    preparation_pipeline.prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    result = preparation_pipeline.prepare_usage_db(db, otlp_traces=None, include_native_sessions=False)
    assert result["changed_sessions"] == 0
    assert SQLiteSnapshotInspector(db).inspect().ready


@pytest.mark.parametrize("method", ["ask", "begin_task"])
def test_guidance_pins_readiness_and_queries_while_another_connection_refreshes(tmp_path, monkeypatch, method):
    db = tmp_path / "reflect.db"
    seed_memory(db, tmp_path)
    writer, reader = connect_sqlite(db), connect_sqlite(db)
    try:
        mark_snapshot(writer, ready=True)
        service = ReflectContextService(reader, initialize_schema=False)
        original = service.improvements.ask

        def refresh_during_query(*args, **kwargs):
            mark_snapshot(writer, ready=False)
            assert reader.execute("SELECT value FROM store_metadata WHERE key='prepared_snapshot'").fetchone()[0] == "ready"
            return original(*args, **kwargs)

        monkeypatch.setattr(service.improvements, "ask", refresh_during_query)
        answer = getattr(service, method)("release gate", path=tmp_path)
        assert answer.memories
        assert not reader.in_transaction
        assert reader.execute("SELECT value FROM store_metadata WHERE key='prepared_snapshot'").fetchone()[0] == "incomplete"
        if method == "begin_task":
            assert answer.task_run_id
            assert reader.execute("SELECT COUNT(*) FROM mcp_task_runs").fetchone()[0] == 1
    finally:
        reader.close()
        writer.close()


def test_backlog_does_not_repeat_global_hash_repair_per_normalization_batch(tmp_path, monkeypatch):
    from reflect.store import normalize

    db, spans = tmp_path / "reflect.db", tmp_path / "spans"
    write_events(spans, 1001)
    calls = []
    original = normalize.backfill_tool_call_hashes

    def count_global_repairs(conn):
        calls.append(conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0])
        return original(conn)

    monkeypatch.setattr(normalize, "backfill_tool_call_hashes", count_global_repairs)
    preparation_pipeline.prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    assert calls == [1001]


@pytest.mark.parametrize("has_cached_report", [False, True])
def test_cache_publication_race_defers_reload_without_failing_completed_refresh(has_cached_report):
    from reflect.dashboard_server import DashboardDataCache
    from reflect.preparation import PreparationCoordinator, PreparationState

    ready = has_cached_report
    generation = 0

    def load():
        if not ready:
            raise SnapshotNotReadyError("another process is refreshing")
        return {"generation": generation}

    cache = DashboardDataCache(load)

    def prepare(_progress):
        nonlocal ready, generation
        generation += 1
        ready = False  # A queued refresh starts before the cache callback.
        return {"refreshed": True}

    worker = PreparationCoordinator(prepare)
    worker.add_completion_callback(lambda _result: cache.refresh())
    worker.start()
    assert worker.wait(timeout=2)
    assert worker.snapshot().state == PreparationState.COMPLETE
    if has_cached_report:
        assert cache.get() == {"generation": 0}
    else:
        with pytest.raises(SnapshotNotReadyError):
            cache.get()
    ready = True
    assert cache.get() == {"generation": 1}


def test_crashed_process_releases_lock_and_large_backlog_replays_without_duplicates(tmp_path):
    db, spans = tmp_path / "reflect.db", tmp_path / "spans"
    write_events(spans, 1205)
    script = """
import os,sys
from pathlib import Path
from reflect.store import normalize
from reflect.preparation_pipeline import prepare_sql_report_db
original = normalize._normalize_pending_batch
def crash_after_committed_batch(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(17)
normalize._normalize_pending_batch = crash_after_committed_batch
prepare_sql_report_db(Path(sys.argv[1]), otlp_traces=None, spans_dir=Path(sys.argv[2]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(db), str(spans)],
        capture_output=True, text=True, timeout=15, env=os.environ.copy(),
    )
    assert result.returncode == 17, result.stderr
    assert not SQLiteSnapshotInspector(db).inspect().ready
    conn = connect_sqlite_read_only(db, profile=None)
    try:
        assert conn.execute("SELECT COUNT(*) FROM raw_events WHERE normalized_status='ok'").fetchone()[0] == 500
        assert conn.execute("SELECT COUNT(*) FROM raw_events WHERE normalized_status='pending'").fetchone()[0] == 705
    finally:
        conn.close()
    preparation_pipeline.prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    preparation_pipeline.prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    conn = connect_sqlite_read_only(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 1205
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1205
        assert conn.execute("SELECT input_tokens FROM session_rollups").fetchone()[0] == 10 * 1205 * 1206 // 2
    finally:
        conn.close()


def test_atomic_maintenance_advances_revision_without_publishing_incomplete_data(tmp_path):
    db = tmp_path / "reflect.db"
    conn = connect_sqlite(db)
    try:
        migrate(conn)
        mark_snapshot(conn, ready=False)
        before = conn.execute("SELECT updated_at FROM store_metadata WHERE key='prepared_snapshot'").fetchone()
        advance_snapshot_revision(conn)
        conn.commit()
        after = conn.execute("SELECT updated_at FROM store_metadata WHERE key='prepared_snapshot'").fetchone()
        assert before != after
        with pytest.raises(SnapshotNotReadyError):
            connect_sqlite_read_only(db)
    finally:
        conn.close()


def test_rejected_event_is_retained_without_permanently_blocking_snapshot(tmp_path, monkeypatch):
    db, spans = tmp_path / "reflect.db", tmp_path / "spans"
    write_events(spans, 2)
    from reflect.store import normalize

    insert = normalize._insert_agent

    def reject_one(conn, attrs, timestamp):
        if attrs["gen_ai.usage.input_tokens"] == 10:
            raise ValueError("invalid event fixture")
        return insert(conn, attrs, timestamp)

    monkeypatch.setattr(normalize, "_insert_agent", reject_one)
    preparation_pipeline.prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    assert SQLiteSnapshotInspector(db).inspect().ready
    conn = connect_sqlite_read_only(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM raw_events WHERE normalized_status='failed'").fetchone()[0] == 1
        assert conn.execute("SELECT input_tokens FROM session_rollups").fetchone()[0] == 20
    finally:
        conn.close()


def test_dashboard_reports_preparing_until_snapshot_is_published(tmp_path):
    from fastapi.testclient import TestClient

    from reflect.dashboard_server import build_dashboard_app

    db, spans = tmp_path / "reflect.db", tmp_path / "spans"
    write_events(spans, 1)
    preparation_pipeline.prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    conn = connect_sqlite(db)
    try:
        mark_snapshot(conn, ready=False)
        client = TestClient(build_dashboard_app(docs_dir=tmp_path, db_path=db))
        for endpoint in ("/api/data", "/api/data?agents=future-provider", "/api/session/session-test", "/api/explore/usage"):
            response = client.get(endpoint)
            assert response.status_code == 503
            assert response.headers["Retry-After"] == "2"
            assert response.json()["state"] == "preparing"
        mark_snapshot(conn, ready=True)
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/data?agents=future-provider").status_code == 200
        mark_snapshot(conn, ready=False)
        assert client.get("/api/data").status_code == 200  # Last completed cache remains available.
    finally:
        conn.close()


def test_memory_and_task_writes_interleave_with_cross_process_refresh_batches(tmp_path, monkeypatch):
    db, spans = tmp_path / "reflect.db", tmp_path / "spans"
    memory_id = seed_memory(db, tmp_path)
    write_events(spans, 505)
    monkeypatch.setenv("REFLECT_DB_PATH", str(db))
    script = """
import sys
from pathlib import Path
from reflect.store import normalize
from reflect import pricing
from reflect.preparation_pipeline import prepare_sql_report_db
def no_network(*args, **kwargs):
    raise RuntimeError('pricing network disabled')
pricing._fetch_json_url = no_network
insert, batch = normalize._insert_agent, normalize._normalize_pending_batch
def hold_writer(*args, **kwargs):
    normalize._insert_agent = insert
    print('writer-held', flush=True)
    sys.stdin.readline()
    return insert(*args, **kwargs)
def pause_after_commit(*args, **kwargs):
    normalize._normalize_pending_batch = batch
    result = batch(*args, **kwargs)
    print('batch-committed', flush=True)
    sys.stdin.readline()
    return result
normalize._insert_agent = hold_writer
normalize._normalize_pending_batch = pause_after_commit
prepare_sql_report_db(Path(sys.argv[1]), otlp_traces=None, spans_dir=Path(sys.argv[2]))
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(db), str(spans)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
    )

    def expect(message):
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(15), "refresh process did not reach checkpoint"
        assert process.stdout.readline().strip() == message

    try:
        expect(b"writer-held")
        fallback = mcp.reflect_context("release gate", path=str(tmp_path))
        assert fallback["memories"][0]["id"] == memory_id
        assert fallback["task_run_id"] is None
        process.stdin.write(b"continue\n")
        expect(b"batch-committed")
        tracked = mcp.reflect_context("release gate", path=str(tmp_path))
        assert tracked["task_run_id"]
        seed_memory(db, tmp_path)
        _, errors = process.communicate(b"continue\n", timeout=30)
        assert process.returncode == 0, errors.decode()
        assert SQLiteSnapshotInspector(db).inspect().ready
        conn = connect_sqlite_read_only(db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 505
            assert conn.execute("SELECT COUNT(*) FROM mcp_task_runs").fetchone()[0] == 1
            assert conn.execute("SELECT input_tokens FROM session_rollups").fetchone()[0] == 10 * 505 * 506 // 2
        finally:
            conn.close()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
