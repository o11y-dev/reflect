import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from reflect.config import analysis_window_days
from reflect.preparation_pipeline import prepare_sql_report_db
from reflect.store.analysis_window import AnalysisWindow
from reflect.store.pending_refresh import PendingRefreshState, analysis_coverage
from reflect.store.sqlite import connect_sqlite, connect_sqlite_read_only, snapshot_incomplete


def write_events(path, session, when, count=1):
    path.write_text("".join(json.dumps({
        "name": "chat", "spanId": f"{session}-{index}",
        "start_time_ns": int(when.timestamp() * 1e9) + index * 1000,
        "end_time_ns": int(when.timestamp() * 1e9) + index * 1000 + 100,
        "attributes": {
            "gen_ai.client.name": "test-harness", "gen_ai.client.session_id": session,
            "gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": 10,
        },
    }) + "\n" for index in range(count)))


@pytest.fixture
def sources(tmp_path, monkeypatch):
    monkeypatch.setenv("REFLECT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("reflect.pricing._fetch_json_url", lambda *a, **k: {})
    spans = tmp_path / "spans"
    spans.mkdir()
    now = datetime.now(UTC) - timedelta(minutes=1)
    old = now - timedelta(days=30)
    write_events(spans / "recent.jsonl", "recent", now)
    write_events(spans / "old.jsonl", "old", old)
    return tmp_path / "data.db", spans, now, old


def test_window_retains_accounting_defers_old_analysis_and_processes_late_corrections(sources):
    db, spans, _, old = sources
    result = prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans, analysis_days=7)
    assert result["analysis_window"]["session_count"] == 1
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT SUM(input_tokens) FROM session_rollups").fetchone()[0] == 20
        assert {row[0] for row in conn.execute("SELECT DISTINCT session_id FROM execution_units")} == {"recent"}
        assert "old" in PendingRefreshState(conn).capture().pending_for("improvements")
    result = prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans, analysis_days=7)
    assert result["graph"]["skipped"] == 1
    assert result["improvements"]["execution_units"] == 0
    write_events(spans / "old.jsonl", "old", old, count=2)
    prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans, analysis_days=7)
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT SUM(input_tokens) FROM session_rollups").fetchone()[0] == 30
        assert conn.execute("SELECT COUNT(*) FROM execution_units WHERE session_id='old'").fetchone()[0] > 0


def test_usage_is_published_before_graph_failure_and_retry_keeps_phase_progress(sources, monkeypatch):
    db, spans, _, _ = sources
    published = []

    def usage_ready(details):
        with closing(connect_sqlite_read_only(db, profile="usage")) as conn:
            assert not snapshot_incomplete(conn, "usage")
            assert snapshot_incomplete(conn)
            assert conn.execute("SELECT SUM(input_tokens) FROM session_rollups").fetchone()[0] == 20
        published.append(details)

    def fail_graph(*args, **kwargs):
        assert published
        raise RuntimeError("interrupted graph")

    with monkeypatch.context() as patch:
        patch.setattr("reflect.store.graph_normalize.refresh_graph", fail_graph)
        with pytest.raises(RuntimeError, match="interrupted graph"):
            prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans,
                                  analysis_days=7, usage_ready=usage_ready)
    with closing(connect_sqlite(db)) as conn:
        pending = PendingRefreshState(conn).capture()
        assert not pending.pending_for("cost")
        assert not pending.pending_for("rollup")
        assert "recent" in pending.pending_for("graph")
    result = prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans, analysis_days=7)
    assert result["refresh_plan"]["cost_mode"] == "skip"
    assert result["rollups"]["skipped"] == 1
    with closing(connect_sqlite(db)) as conn:
        assert not snapshot_incomplete(conn)
        assert conn.execute("SELECT SUM(input_tokens) FROM session_rollups").fetchone()[0] == 20


def test_explicit_historical_refresh_and_activity_boundaries(sources):
    db, spans, now, old = sources
    prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans, analysis_days=7)
    prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans,
                          analysis_days=7, analysis_session_ids={"old"})
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_units WHERE session_id='old'").fetchone()[0] > 0
        lower = now - timedelta(days=7)
        conn.execute("UPDATE sessions SET started_at=?, ended_at=NULL, updated_at=? WHERE id='old'",
                     (lower.isoformat(), now.isoformat()))
        assert "old" in AnalysisWindow.capture(7, now=now).session_ids(conn)
        conn.execute("UPDATE sessions SET started_at=? WHERE id='old'", (old.isoformat(),))
        assert "old" not in AnalysisWindow.capture(7, now=now).session_ids(conn)
        conn.execute("UPDATE sessions SET started_at='unknown', ended_at=NULL WHERE id='old'")
        assert "old" not in AnalysisWindow.capture(7, now=now).session_ids(conn)
        assert AnalysisWindow.capture(None, now=now).session_ids(conn) == {"old", "recent"}


def test_configured_window_rejects_invalid_values(monkeypatch):
    monkeypatch.delenv("REFLECT_ANALYSIS_WINDOW_DAYS", raising=False)
    assert analysis_window_days() == 7
    monkeypatch.setenv("REFLECT_ANALYSIS_WINDOW_DAYS", "30")
    assert analysis_window_days() == 30
    for value in ("0", "-1", "oops"):
        monkeypatch.setenv("REFLECT_ANALYSIS_WINDOW_DAYS", value)
        with pytest.raises(ValueError, match="positive integer"):
            analysis_window_days()


def test_upgrade_from_interrupted_legacy_refresh_keeps_cold_analysis_explicitly_pending(sources):
    from reflect.store.sqlite import mark_snapshot

    db, spans, _, _ = sources
    prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    with closing(connect_sqlite(db)) as conn:
        conn.execute("DELETE FROM store_metadata WHERE key=?", (PendingRefreshState.METADATA_KEY,))
        mark_snapshot(conn, ready=False)
    prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans, analysis_days=7)
    with closing(connect_sqlite(db)) as conn:
        pending = PendingRefreshState(conn).capture()
        assert pending.pending_for("improvements") == {"old"}
        assert not pending.pending_for("improvements", include_deferred=False)
        assert conn.execute("SELECT SUM(input_tokens) FROM session_rollups").fetchone()[0] == 20


@pytest.mark.parametrize("failure_point", ["graph", "improvements"])
def test_late_historical_correction_survives_restart_after_canonical_ack(sources, monkeypatch, failure_point):
    db, spans, _, old = sources
    prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans, analysis_days=7)
    write_events(spans / "old.jsonl", "old", old, count=2)

    def interrupted(*args, **kwargs):
        with closing(connect_sqlite(db)) as conn:
            pending = PendingRefreshState(conn).capture()
            assert "old" not in pending.pending_for("canonical")
            assert "old" not in pending.pending_for("cost")
            assert "old" not in pending.pending_for("rollup")
            assert "old" in pending.pending_for("improvements", include_deferred=False)
            if failure_point == "improvements":
                assert "old" not in pending.pending_for("graph")
            assert conn.execute("SELECT SUM(input_tokens) FROM session_rollups").fetchone()[0] == 30
        raise RuntimeError("injected interruption after core publication")

    target = (
        "reflect.store.graph_normalize.refresh_graph"
        if failure_point == "graph" else "reflect.improvements.service.ImprovementService.refresh"
    )
    with monkeypatch.context() as patch:
        patch.setattr(target, interrupted)
        with pytest.raises(RuntimeError, match="injected interruption"):
            prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans, analysis_days=7)

    # The native source is unchanged and canonical/cost/rollup were acknowledged.
    # Only persisted required analytics scope can preserve this old correction.
    result = prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans, analysis_days=7)
    assert result["refresh_plan"]["cost_mode"] == "skip"
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_units WHERE session_id='old'").fetchone()[0] > 0
        pending = PendingRefreshState(conn).capture()
        assert "old" not in pending.pending_for("graph")
        assert "old" not in pending.pending_for("improvements")
        assert conn.execute("SELECT SUM(input_tokens) FROM session_rollups").fetchone()[0] == 30


@pytest.mark.parametrize("peer_pending", [False, True])
def test_overlap_discovery_completes_repo_cohort_without_rebuilding_stable_peers(sources, monkeypatch, peer_pending):
    from reflect.execution_units import ExecutionUnitService
    from reflect.improvements.analysis_scope import AnalysisScope
    from reflect.improvements.rules import ContextExplosionRule
    from reflect.improvements.service import ImprovementService

    db, spans, now, old = sources
    # A long hot session overlaps old B. C is older than that entire interval,
    # but belongs to B's repository cohort. D shares B's time, another repo.
    write_events(spans / "peer.jsonl", "peer", old - timedelta(days=5))
    peer_event = json.loads((spans / "peer.jsonl").read_text())
    peer_event["attributes"]["gen_ai.usage.input_tokens"] = 1_000_000
    (spans / "peer.jsonl").write_text(json.dumps(peer_event) + "\n")
    write_events(spans / "other.jsonl", "other", old)
    early = json.loads((spans / "recent.jsonl").read_text())
    early["spanId"] = "recent-earlier"
    early["start_time_ns"] = int((old - timedelta(days=1)).timestamp() * 1e9)
    early["end_time_ns"] = early["start_time_ns"] + 100
    with (spans / "recent.jsonl").open("a") as stream:
        stream.write(json.dumps(early) + "\n")
    prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    with closing(connect_sqlite(db)) as conn:
        for repo in ("repo-a", "repo-b"):
            conn.execute("INSERT INTO repos(id,full_name,created_at,updated_at) VALUES (?,?,?,?)", (repo, repo, now.isoformat(), now.isoformat()))
        conn.execute("UPDATE sessions SET repo_id=CASE WHEN id='other' THEN 'repo-b' ELSE 'repo-a' END")
        conn.execute("UPDATE execution_units SET repo_id=CASE WHEN session_id='other' THEN 'repo-b' ELSE 'repo-a' END")
        pending = PendingRefreshState(conn)
        pending.mark({"recent"}, phases=("improvements",))
        if peer_pending:
            conn.execute("DELETE FROM execution_units WHERE session_id='peer'")
            pending.mark({"peer"}, phases=("improvements",))
            pending.defer({"peer"}, phases=("improvements",))
        conn.commit()
        with AnalysisScope(conn, {"recent", "old", "peer"}) as scope:
            expected = ContextExplosionRule().evaluate(conn, scope=scope)
        assert len(expected) == 1

    dependency_calls, refreshes = [], []
    original_affected = ExecutionUnitService.affected_session_ids
    original_refresh = ImprovementService.refresh

    def affected(self, session_ids, *, step_scope=None, consumer_unit_ids=None):
        dependency_calls.append(set(session_ids))
        return original_affected(self, session_ids, step_scope=step_scope, consumer_unit_ids=consumer_unit_ids)

    def refresh(self, **kwargs):
        refreshes.append(kwargs)
        return original_refresh(self, **kwargs)

    monkeypatch.setattr(ExecutionUnitService, "affected_session_ids", affected)
    monkeypatch.setattr(ImprovementService, "refresh", refresh)
    result = prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans, analysis_days=7)
    assert dependency_calls == [{"recent"}]
    assert refreshes[0]["dependencies_expanded"] is True
    assert refreshes[0]["analysis_session_ids"] == {"recent", "old", "peer"}
    assert refreshes[0]["session_ids"] == {"recent", "old"}
    assert result["analysis_window"]["historical_cohort_sessions"] == 2
    with closing(connect_sqlite(db)) as conn:
        row = conn.execute("SELECT metric_value,affected_session_count FROM observations WHERE rule_id=? AND repo_id='repo-a' AND status<>'resolved'", (ContextExplosionRule.definition.id,)).fetchone()
        assert tuple(row) == (expected[0].metric_value, expected[0].affected_session_count)
        metadata = json.loads(conn.execute("SELECT value FROM store_metadata WHERE key='analysis_window_v1'").fetchone()[0])
        assert metadata["historical_cohort_sessions"] == 2
        pending = PendingRefreshState(conn).capture()
        assert pending.pending_for("improvements") == ({"peer"} if peer_pending else set())
        assert not pending.pending_for("improvements", include_deferred=False)
        if peer_pending:
            assert conn.execute("SELECT COUNT(*) FROM execution_units WHERE session_id='peer'").fetchone()[0] == 0
            assert analysis_coverage(conn, {"peer"})["improvements"] == {
                "session_count": 1, "pending_sessions": 1, "deferred_sessions": 1, "ready_sessions": 0,
            }
        tokens = conn.execute("SELECT SUM(input_tokens) FROM session_rollups").fetchone()[0]
    again = prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans, analysis_days=7)
    assert again["improvements"]["execution_units"] == 0
    assert refreshes[-1]["analysis_session_ids"] == set()
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT SUM(input_tokens) FROM session_rollups").fetchone()[0] == tokens


@pytest.mark.parametrize("peer_already_active", [False, True])
def test_late_correction_keeps_missing_historical_context_deferred_unless_already_due(
    sources, monkeypatch, peer_already_active,
):
    from reflect.improvements.service import ImprovementService

    db, spans, _, old = sources
    write_events(spans / "peer.jsonl", "peer", old - timedelta(days=5))
    prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans, analysis_days=7)
    with closing(connect_sqlite(db)) as conn:
        pending = PendingRefreshState(conn)
        assert pending.capture().pending_for("improvements") == {"old", "peer"}
        assert conn.execute("SELECT COUNT(*) FROM execution_units WHERE session_id='peer'").fetchone()[0] == 0
        if peer_already_active:
            # A durable correction must survive even when its native input is
            # unchanged on this retry; its cause cannot be reconstructed here.
            pending.mark({"peer"}, phases=("improvements",))
        peer_revision = conn.execute(
            "SELECT revision FROM pending_refresh WHERE phase='improvements' AND session_id='peer'",
        ).fetchone()[0]
        conn.commit()

    write_events(spans / "old.jsonl", "old", old, count=2)
    refreshes = []
    original = ImprovementService.refresh

    def refresh(self, **kwargs):
        refreshes.append(kwargs)
        return original(self, **kwargs)

    monkeypatch.setattr(ImprovementService, "refresh", refresh)
    result = prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans, analysis_days=7)
    assert refreshes[0]["session_ids"] == ({"old", "peer"} if peer_already_active else {"old"})
    assert refreshes[0]["analysis_session_ids"] == {"recent", "old", "peer"}
    assert result["analysis_window"]["historical_cohort_sessions"] == 2
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT SUM(input_tokens) FROM session_rollups").fetchone()[0] == 40
        assert conn.execute("SELECT COUNT(*) FROM execution_units WHERE session_id='old'").fetchone()[0] > 0
        peer_units = conn.execute("SELECT COUNT(*) FROM execution_units WHERE session_id='peer'").fetchone()[0]
        assert bool(peer_units) is peer_already_active
        pending = PendingRefreshState(conn).capture()
        assert not pending.pending_for("improvements", include_deferred=False)
        assert pending.pending_for("improvements") == (set() if peer_already_active else {"peer"})
        if not peer_already_active:
            assert conn.execute(
                "SELECT revision,deferred FROM pending_refresh WHERE phase='improvements' AND session_id='peer'",
            ).fetchone() == (peer_revision, 1)
            assert analysis_coverage(conn, {"peer"})["improvements"]["ready_sessions"] == 0


def test_missing_hot_evidence_is_repaired_without_promoting_deferred_history(sources):
    db, spans, _, _ = sources
    prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans, analysis_days=7)
    with closing(connect_sqlite(db)) as conn:
        conn.execute("DELETE FROM execution_units WHERE session_id='recent'")
        assert not PendingRefreshState(conn).capture().pending_for("improvements", include_deferred=False)
        conn.commit()

    result = prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans, analysis_days=7)
    assert result["improvements"]["execution_units"] == 1
    with closing(connect_sqlite(db)) as conn:
        assert {row[0] for row in conn.execute("SELECT DISTINCT session_id FROM execution_units")} == {"recent"}
        pending = PendingRefreshState(conn).capture()
        assert pending.pending_for("improvements") == {"old"}
        assert not pending.pending_for("improvements", include_deferred=False)
