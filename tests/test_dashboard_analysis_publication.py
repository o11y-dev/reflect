import copy
import json
import threading
from contextlib import closing
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_dashboard_sql_api import _seed_improvement_ledger, _seed_sql_report_db

import reflect.dashboard_queries as queries
from reflect.dashboard_server import DashboardDataCache
from reflect.improvements.models import RuleDefinition
from reflect.improvements.repository import ImprovementRepository
from reflect.store.pending_refresh import PendingRefreshState
from reflect.store.sqlite import (
    SnapshotNotReadyError,
    advance_snapshot_revision,
    connect_sqlite,
    mark_snapshot,
)

USAGE_AT = "2026-05-01T11:00:00+00:00"
FINISHED_AT = "2026-05-01T11:01:00+00:00"
WINDOW = {"days": 7, "date_to": USAGE_AT}


def _metadata(conn, key, value):
    conn.execute(
        "INSERT INTO store_metadata(key,value,updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (key, value, value),
    )


def _revision(conn):
    return tuple(tuple(row) for row in conn.execute(
        "SELECT key,value,updated_at FROM store_metadata "
        "WHERE key IN ('prepared_usage','prepared_snapshot') ORDER BY key"
    ))


@pytest.fixture
def publication(tmp_path, monkeypatch):
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 5, 1, 11, tzinfo=UTC)

    monkeypatch.setattr(queries, "datetime", FixedClock)
    db = tmp_path / "reflect.db"
    _seed_sql_report_db(db)
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)
        _metadata(conn, "last_successful_usage_refresh", USAGE_AT)
        _metadata(conn, "analysis_window_v1", json.dumps(WINDOW))
        mark_snapshot(conn, ready=True, profile="usage")
    def build():
        return queries.build_dashboard_payload(
            db, range_name="7d", lazy_heavy_tabs=True,
            base_tab_names={"activity", "models", "costs", "usage_tools", "mcp"},
        )
    return db, build, build()


def _finish(db):
    with closing(connect_sqlite(db)) as conn:
        _metadata(conn, "last_successful_refresh", FINISHED_AT)
        mark_snapshot(conn, ready=True)
        return _revision(conn)


def test_finalizer_matches_full_build_without_rebuilding_usage(publication, monkeypatch):
    db, build, payload = publication
    original = copy.deepcopy(payload)
    assert payload["analysis_state"] == "preparing"
    observation_id = _seed_improvement_ledger(db)
    with closing(connect_sqlite(db)) as conn:
        ImprovementRepository(conn).sync_rule_definitions((RuleDefinition(
            id="session_heavy_turns", version=1, category="efficiency", title="Large input",
            description="Recorded session insight",
        ),), now=FINISHED_AT)
        conn.execute("UPDATE observations SET rule_id='session_heavy_turns',scope_type='session',scope_id='sess-sql' WHERE id=?", (observation_id,))
        conn.commit()
    revision = _finish(db)
    expected = build()
    # Usage capture remains explicit even when analysis completes later.
    expected["snapshot_completed_at"] = USAGE_AT
    monkeypatch.setattr("reflect.views.measurements.build_measurements", lambda *a, **k: pytest.fail("usage rebuilt"))
    result = queries.finalize_dashboard_analysis(db, payload, usage_completed_at=USAGE_AT, publication_revision=revision)
    assert result == expected
    assert result["insights"]["sess-sql"][0]["id"] == observation_id
    assert payload == original
    assert result["measurements"] is payload["measurements"]
    assert result["sqlite"] is payload["sqlite"]


def test_finalizer_keeps_historical_analysis_unknown(publication):
    db, _, payload = publication
    with closing(connect_sqlite(db)) as conn:
        pending = PendingRefreshState(conn)
        pending.mark({"sess-sql"}, phases=("improvements",))
        pending.defer({"sess-sql"}, phases=("improvements",))
        conn.commit()
    result = queries.finalize_dashboard_analysis(db, payload, usage_completed_at=USAGE_AT, publication_revision=_finish(db))
    assert result["analysis_state"] == "historical_pending"
    assert result["analysis_coverage"]["improvements"]["pending_sessions"] == 1
    assert result["insights"] == {}


def test_finalizer_tolerates_missing_window_but_requires_canonical_scope(publication):
    db, _, payload = publication
    with closing(connect_sqlite(db)) as conn:
        _metadata(conn, "analysis_window_v1", "{broken")
        conn.commit()
    revision = _finish(db)
    result = queries.finalize_dashboard_analysis(db, payload, usage_completed_at=USAGE_AT, publication_revision=revision)
    assert result["analysis_window"] is None
    with pytest.raises(SnapshotNotReadyError, match="canonical session scope"):
        queries.finalize_dashboard_analysis(db, {**payload, "measurements": {}}, usage_completed_at=USAGE_AT, publication_revision=revision)


@pytest.mark.parametrize("change", ["maintenance", "next_usage", "incomplete"])
def test_finalizer_rejects_another_publication(publication, change):
    db, _, payload = publication
    revision = _finish(db)
    with closing(connect_sqlite(db)) as conn:
        if change == "maintenance":
            advance_snapshot_revision(conn)
        elif change == "next_usage":
            _metadata(conn, "last_successful_usage_refresh", FINISHED_AT)
        else:
            mark_snapshot(conn, ready=False)
        conn.commit()
    with pytest.raises(SnapshotNotReadyError):
        queries.finalize_dashboard_analysis(db, payload, usage_completed_at=USAGE_AT, publication_revision=revision)


def _result(run=USAGE_AT):
    return SimpleNamespace(details={"analysis_window": {"date_to": run}, "publication_revision": [["prepared_snapshot", "ready", "final"], ["prepared_usage", "ready", "final"]]})


def _details():
    return {"completed_at": USAGE_AT, "analysis_window": WINDOW}


def test_cache_finalizes_only_successful_usage_and_persists(tmp_path):
    payload = {"snapshot_completed_at": USAGE_AT, "analysis_state": "preparing"}
    calls = []
    def load():
        calls.append("usage")
        return payload.copy()
    def analysis(current, **kwargs):
        calls.append("analysis")
        assert kwargs["usage_completed_at"] == USAGE_AT
        return {**current, "analysis_state": "ready"}
    path = tmp_path / "cache.json"
    cache = DashboardDataCache(lambda: {}, refresh_loader=load, analysis_loader=analysis, cache_path=path, cache_key="7d")
    cache.publish_usage(_details())
    before = cache.get()
    assert cache.complete(_result())["analysis_state"] == "ready"
    assert calls == ["usage", "analysis"]
    assert before["analysis_state"] == "preparing"
    assert json.loads(path.read_text())["payload"] == cache.get()
    cache.complete(_result())
    assert calls == ["usage", "analysis", "usage"]  # no cross-run reuse


@pytest.mark.parametrize("condition", ["absent", "different_run", "missing_revision", "failed_usage", "replaced_payload"])
def test_cache_falls_back_when_this_run_cannot_be_proven(condition):
    loads = []
    fail = condition == "failed_usage"
    def load():
        loads.append(1)
        if fail:
            raise SnapshotNotReadyError("preparing")
        return {"snapshot_completed_at": USAGE_AT}
    cache = DashboardDataCache(lambda: {}, refresh_loader=load,
        analysis_loader=lambda *a, **k: pytest.fail("unproven usage finalized"))
    result = _result()
    if condition != "absent":
        cache.publish_usage(_details())
    if condition == "different_run":
        result = _result("another run")
    elif condition == "missing_revision":
        result.details.pop("publication_revision")
    elif condition == "replaced_payload":
        cache.refresh()
    fail = False
    count = len(loads)
    assert cache.complete(result) == {"snapshot_completed_at": USAGE_AT}
    assert len(loads) == count + 1


def test_analysis_failure_retains_publication_and_retries_full_load(tmp_path):
    path = tmp_path / "cache.json"
    payload = {"snapshot_completed_at": USAGE_AT, "analysis_state": "preparing"}
    cache = DashboardDataCache(lambda: {}, refresh_loader=lambda: payload.copy(),
        analysis_loader=lambda *a, **k: (_ for _ in ()).throw(ValueError("failed read")),
        cache_path=path, cache_key="7d")
    cache.publish_usage(_details())
    persisted = path.read_bytes()
    with pytest.raises(ValueError, match="failed read"):
        cache.complete(_result())
    assert path.read_bytes() == persisted
    cache._refresh_loader = lambda: {"recovered": True}
    assert cache.get() == {"recovered": True}


def test_analysis_revision_mismatch_uses_full_loader():
    cache = DashboardDataCache(lambda: {}, refresh_loader=lambda: {"snapshot_completed_at": USAGE_AT},
        analysis_loader=lambda *a, **k: (_ for _ in ()).throw(SnapshotNotReadyError("changed")))
    cache.publish_usage(_details())
    cache._refresh_loader = lambda: {"new_generation": True}
    assert cache.complete(_result()) == {"new_generation": True}


def test_overlapping_refresh_cannot_be_finalized():
    entered, release = threading.Event(), threading.Event()
    def load():
        entered.set()
        assert release.wait(3)
        return {"snapshot_completed_at": USAGE_AT}
    cache = DashboardDataCache(lambda: {}, refresh_loader=load,
        analysis_loader=lambda *a, **k: pytest.fail("overlapping read finalized"))
    worker = threading.Thread(target=cache.publish_usage, args=(_details(),))
    worker.start()
    assert entered.wait(3)
    cache.refresh()
    release.set()
    worker.join(3)
    assert not worker.is_alive()
    cache.complete(_result())
