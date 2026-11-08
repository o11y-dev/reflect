import fcntl
import threading
from contextlib import closing

import pytest
from test_analysis_window import sources as sources

from reflect.dashboard_queries import build_dashboard_payload
from reflect.preparation import (
    PreparationCoordinator,
    PreparationProgress,
    PreparationStage,
    PreparationState,
)
from reflect.preparation_pipeline import prepare_sql_report_db
from reflect.store.sqlite import connect_sqlite_read_only, snapshot_incomplete


@pytest.mark.parametrize("failure", [None, "analysis", "publication"])
def test_coordinator_owns_publication_until_completion_or_failure_and_retry(failure):
    entered, graph_started, release = (threading.Event() for _ in range(3))
    order = []
    attempts = 0

    def publish(details):
        attempt = details["attempt"]
        entered.set()
        assert release.wait(5)
        order.append((attempt, "usage"))
        if attempt == 1 and failure == "publication":
            raise RuntimeError("publication failed")

    def prepare(progress):
        nonlocal attempts
        attempts += 1
        worker.report_usage_ready({"attempt": attempts})
        progress(PreparationProgress(PreparationStage.REFRESHING_GRAPH, "Graph running"))
        graph_started.set()
        if attempts == 1 and failure == "analysis":
            raise RuntimeError("analysis failed")
        return {"timings": {}}

    worker = PreparationCoordinator(prepare)
    worker.add_usage_callback(publish)
    worker.add_completion_callback(lambda result: order.append((attempts, "complete")))
    assert worker.start()
    try:
        assert entered.wait(2) and graph_started.wait(2)
        assert worker.snapshot().state is PreparationState.RUNNING
        assert worker.snapshot().usage_completed_at == ""
        assert worker.snapshot().stage is PreparationStage.REFRESHING_GRAPH
        assert not worker.start(), "A new generation must not overtake pending publication"
        assert order == []
    finally:
        release.set()
        assert worker.wait(5)
    snapshot = worker.snapshot()
    if failure:
        assert snapshot.state is PreparationState.FAILED
        assert snapshot.error == f"{failure} failed"
        assert order == [(1, "usage")]
    else:
        assert snapshot.state is PreparationState.COMPLETE
        assert order == [(1, "usage"), (1, "complete")]
        assert snapshot.result.details["timings"]["usage_publication_seconds"] >= 0
    assert bool(snapshot.usage_completed_at) is (failure != "publication")

    assert worker.start()
    assert worker.wait(5)
    assert worker.snapshot().state is PreparationState.COMPLETE
    assert order[-2:] == [(2, "usage"), (2, "complete")]
    assert worker.snapshot().usage_completed_at


@pytest.mark.parametrize("failure", [None, "graph", "publication"])
def test_pipeline_overlaps_graph_but_keeps_usage_revision_and_lock_until_publication(
    sources, monkeypatch, failure,
):
    import reflect.dashboard_queries as queries
    from reflect.improvements.measurement import MeasurementService
    from reflect.store import graph_normalize

    db, spans, _, _ = sources
    composing, graph_started, analysis_finished, release = (threading.Event() for _ in range(4))
    payloads = []
    original_metrics = queries._sql_dashboard_metrics
    original_graph = graph_normalize.refresh_graph
    original_measure = MeasurementService.measure_active

    def metrics(*args, **kwargs):
        # Pause between the payload's two read-only connections. Graph writes
        # must proceed, but prepared_usage cannot advance across those reads.
        composing.set()
        assert release.wait(5)
        if failure == "publication":
            raise RuntimeError("publication failed")
        return original_metrics(*args, **kwargs)

    def graph(*args, **kwargs):
        assert composing.wait(5)
        graph_started.set()
        if failure == "graph":
            raise RuntimeError("graph failed")
        return original_graph(*args, **kwargs)

    def measure(service):
        result = original_measure(service)
        analysis_finished.set()
        return result

    monkeypatch.setattr(queries, "_sql_dashboard_metrics", metrics)
    monkeypatch.setattr(graph_normalize, "refresh_graph", graph)
    monkeypatch.setattr(MeasurementService, "measure_active", measure)

    def prepare(progress):
        return prepare_sql_report_db(
            db, otlp_traces=None, spans_dir=spans, analysis_days=7,
            progress=progress, usage_ready=worker.report_usage_ready,
        )

    def publish(_details):
        payloads.append(build_dashboard_payload(
            db, range_name="7d", lazy_heavy_tabs=True,
            base_tab_names={"activity", "models", "costs", "usage_tools", "mcp"},
        ))

    worker = PreparationCoordinator(prepare)
    worker.add_usage_callback(publish)
    assert worker.start()
    try:
        assert graph_started.wait(5)
        if failure != "graph":
            assert analysis_finished.wait(5)
        assert worker.snapshot().state is PreparationState.RUNNING
        assert worker.snapshot().usage_completed_at == ""
        assert not worker.start()
        with closing(connect_sqlite_read_only(db, profile="usage")) as conn:
            assert snapshot_incomplete(conn)
            assert not snapshot_incomplete(conn, "usage")
        # Failure cleanup must keep the writer lock until the in-flight read
        # finishes, otherwise another process could invalidate its generation.
        with open(f"{db}.refresh.lock") as lock, pytest.raises(BlockingIOError):
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        release.set()
        assert worker.wait(5)

    snapshot = worker.snapshot()
    assert snapshot.state is (PreparationState.FAILED if failure else PreparationState.COMPLETE)
    if failure:
        assert snapshot.error == f"{failure} failed"
    if failure == "publication":
        assert payloads == []
        assert snapshot.usage_completed_at == ""
    else:
        assert len(payloads) == 1
        assert payloads[0]["measurements"]["totals"]["input_tokens"] == 10
        assert payloads[0]["analysis_state"] == "preparing"
        assert payloads[0]["insights"] == {}
        assert snapshot.usage_completed_at
    with closing(connect_sqlite_read_only(db, profile="usage")) as conn:
        assert snapshot_incomplete(conn) is bool(failure)
        if not failure:
            assert snapshot.result.details["publication_revision"] == [tuple(row) for row in conn.execute(
                "SELECT key,value,updated_at FROM store_metadata "
                "WHERE key IN ('prepared_snapshot','prepared_usage') ORDER BY key"
            )]
