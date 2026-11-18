"""Keep exact completed read responses available through background refresh."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_dashboard_sql_api import _seed_improvement_ledger, _seed_sql_report_db

from reflect.dashboard_server import DashboardResponseCache, build_dashboard_app
from reflect.improvements.skills import SkillRegistryService
from reflect.store.pending_refresh import PendingRefreshState
from reflect.store.sqlite import SnapshotNotReadyError, connect_sqlite, mark_snapshot


@pytest.fixture
def report(tmp_path):
    db = tmp_path / "reflect.db"
    _seed_sql_report_db(db)
    finding = _seed_improvement_ledger(db)
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=True)
    from tests.backend_helpers import publish_actions
    publish_actions(db)
    return db, finding


def client_for(db, **kwargs):
    return TestClient(build_dashboard_app(docs_dir=db.parent, db_path=db, **kwargs))


def test_cold_recorded_workflow_response_is_reused_during_same_refresh(report, monkeypatch):
    db, _ = report
    before = client_for(db).get('/api/workflows?status=pending')
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)
    repeated = client_for(db).get('/api/workflows?status=pending')
    assert before.status_code == repeated.status_code == 200
    assert repeated.json() == before.json()


@pytest.mark.parametrize("path", [
    "/api/data?range=all&agents=codex&tab=explore&view=usage",
    "/api/explore/tools?range=all&session=sess-sql",
    "/api/explore/graph?range=all&session=sess-sql",
    "/api/explore/context",
    "/api/findings", "/api/rules", "/api/workflows", "/api/loops", "/api/impact",
])
def test_completed_response_survives_refresh_and_restart_for_exact_scope(report, path):
    db, _ = report
    original = client_for(db).get(path)
    assert original.status_code == 200, original.text
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)
    restarted = client_for(db)
    response = restarted.get(path)
    assert response.status_code == 200, response.text
    actual, expected = response.json(), original.json()
    actual.pop("generated_at", None)
    expected.pop("generated_at", None)
    assert actual == expected
    uncached = restarted.get(path + ('&' if '?' in path else '?') + 'q=uncached-selection')
    assert uncached.status_code == 200, uncached.text
    assert uncached.json().get('analysis_state') != 'pending'
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=True)
    actual = restarted.get(path).json()
    actual.pop("generated_at", None)
    assert actual == expected


def test_observation_detail_and_evidence_remain_identifiable_while_pending(report):
    db, finding = report
    client = client_for(db)
    paths = [f'/api/findings/{finding}', f'/api/findings/{finding}/evidence?limit=5']
    before = {path: client.get(path).json() for path in paths}
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)
    for path in paths:
        assert client.get(path).json() == before[path]
    assert client.get('/api/findings/not-seen/evidence').status_code == 404


def test_scope_key_preserves_duplicate_parameter_meaning_and_default_window(tmp_path):
    from starlette.datastructures import QueryParams

    cache = DashboardResponseCache(tmp_path / "cache.json", cache_key="test", default_range="7d")
    key = cache.key("/api/explore/usage", QueryParams("agents=codex&range=all"))
    assert key == cache.key("/api/explore/usage", QueryParams("range=all&agents=codex"))
    assert key != cache.key("/api/explore/usage", QueryParams("agents=codex&agents=cursor&range=all"))
    assert cache.key("/api/data", {}) == cache.key("/api/data", {"range": "7d"})
    assert cache.key("/api/data", {"session": "one"}) == cache.key("/api/data", {"session": "one", "range": "all"})
    assert cache.key("/api/data", {"range": "all"}) != cache.key("/api/data", {})
    assert cache.key("/api/explore/not-a-view", {}) is None
    assert cache.key("/api/workflows/one/preview", {}) is None
    assert cache.key("/api/workflows/one/apply", {}, method="POST") is None


def test_bounded_cache_keeps_original_timestamps_and_rejects_other_namespace(tmp_path):
    path = tmp_path / "cache.json"
    cache = DashboardResponseCache(path, cache_key="scope", default_range="7d", max_entries=2, max_bytes=1500)
    first = {"snapshot_completed_at": "2026-11-08T10:00:00Z", "total": 1}
    cache.put("one", first)
    first_stale = cache.stale("one", reason="preparing")
    first["total"] = 99
    cache.put("one", {"snapshot_completed_at": "2026-11-08T10:00:00Z", "total": 1})
    assert cache.stale("one", reason="preparing") == first_stale
    newer = cache.stale("one", reason="preparing", fallback={
        "snapshot_completed_at": "2026-11-08T11:00:00Z", "total": 2,
    })
    assert newer["total"] == 2
    assert newer["publication"]["loaded_at"] is None
    assert newer["publication"]["snapshot_completed_at"] == "2026-11-08T11:00:00Z"
    cache.put("two", {"total": 2})
    cache.put("three", {"total": 3})
    assert cache.stale("one", reason="preparing") is None
    cache.put("oversized", {"text": "x" * 2000})
    assert cache.stale("oversized", reason="preparing") is None
    restarted = DashboardResponseCache(path, cache_key="scope", default_range="7d", max_entries=2, max_bytes=1500)
    assert restarted.stale("two", reason="preparing")["total"] == 2
    assert restarted.stale("three", reason="preparing")["total"] == 3
    assert path.stat().st_mode & 0o777 == 0o600
    different = DashboardResponseCache(path, cache_key="other", default_range="14d")
    assert different.stale("three", reason="preparing") is None
    path.write_text("not JSON")
    assert DashboardResponseCache(path, cache_key="scope", default_range="7d").stale("two", reason="preparing") is None


def test_stale_cache_never_bypasses_schema_readiness(report):
    db, _ = report
    client = client_for(db)
    before = client.get('/api/findings')
    assert before.status_code == 200
    with closing(connect_sqlite(db)) as conn:
        conn.execute('DELETE FROM schema_migrations WHERE version = (SELECT MAX(version) FROM schema_migrations)')
        mark_snapshot(conn, ready=False)
    actual, expected = client.get('/api/findings').json(), before.json()
    actual.pop('generated_at', None)
    expected.pop('generated_at', None)
    assert actual == expected
    # Working schema is deliberately unavailable; only the immutable generation is read.
    from reflect.store.snapshots import PublishedSnapshotStore
    assert PublishedSnapshotStore(db).current() is not None


def test_completion_warms_only_default_panel_reads(report):
    db, _ = report
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)
    client = client_for(db)
    for path in ('/api/findings', '/api/rules', '/api/workflows', '/api/loops', '/api/impact', '/api/skills?limit=500', '/api/explore/graph?range=all'):
        response = client.get(path)
        assert response.status_code == 200, response.text


def test_startup_retains_completed_panels_before_first_worker_run(report):
    db, _ = report
    from tests.test_dashboard_sql_api import _job_client
    client, runner = _job_client(db)
    job = runner.jobs.submit('refresh', {'otlp_traces': None})
    runner.jobs.claim(job.job_id, owner='fixture')
    with closing(connect_sqlite(db)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("DELETE FROM sessions WHERE id='sess-sql'")
        for path in ('/api/workflows', '/api/impact', '/api/skills?limit=500'):
            response = client.get(path)
            assert response.status_code == 200, response.text
        conn.rollback()
    assert client.get('/api/status').json()['preparation']['state'] == 'running'


def test_retained_skill_analysis_preserves_live_packages_and_new_skills_after_restart(report):
    db, _ = report
    with closing(connect_sqlite(db)) as conn:
        skill_id, version_id = conn.execute("SELECT id, current_version_id FROM skills").fetchone()
        conn.execute(
            """INSERT INTO execution_units(id, session_id, source, started_at,
               outcome, verification_passed, created_at, updated_at)
               VALUES ('retained-execution', 'sess-sql', 'session_fallback',
                       '2026-05-01', 'success', 1, '2026-05-01', '2026-05-01')"""
        )
        conn.execute(
            """INSERT INTO skill_usage(id, skill_id, skill_version_id, session_id,
               execution_unit_id, state, outcome, confidence, observed_at, created_at, updated_at)
               VALUES ('retained-usage', ?, ?, 'sess-sql', 'retained-execution', 'observed', 'success',
                       0.9, '2026-05-01', '2026-05-01', '2026-05-01')""",
            (skill_id, version_id),
        )
        conn.execute(
            """INSERT INTO skill_measurements(id, skill_id, skill_version_id,
               metric_name, before_value, after_value, verdict, confidence,
               measured_at, created_at, updated_at)
               VALUES ('retained-measurement', ?, ?, 'tokens', 10, 5, 'improved',
                       0.9, '2026-05-01', '2026-05-01', '2026-05-01')""",
            (skill_id, version_id),
        )
        conn.commit()
    from reflect.store.snapshots import PublishedSnapshotStore
    PublishedSnapshotStore(db, reserve_bytes=0).publish(evidence_revision="measurements")
    client = client_for(db)
    listed = client.get("/api/skills?limit=500").json()
    detail = client.get(f"/api/skills/{skill_id}").json()
    assert listed["skills"][0]["usage_count"] == detail["skill"]["measurement_count"] == 1
    cache_file = db.with_suffix(".dashboard-responses.json")
    recorded_cache = cache_file.read_bytes()

    new_package = db.parent / "new-skill" / "SKILL.md"
    new_package.parent.mkdir()
    new_package.write_text("---\nname: new-skill\ndescription: New package\n---\n# New skill\n")
    from reflect.application import ReflectApplication
    with ReflectApplication(db).state_action() as service:
        conn = service.conn
        conn.execute(
            """INSERT INTO skill_versions(id, skill_id, version, content_markdown,
               content_hash, source_kind, status, created_at, updated_at)
               VALUES ('next-version', ?, 2, 'Current package content', 'next-hash',
                       'imported', 'active', '2026-05-02', '2026-05-02')""", (skill_id,),
        )
        conn.execute(
            "UPDATE skills SET description='Current description', current_version_id='next-version' WHERE id=?",
            (skill_id,),
        )
        conn.execute(
            """INSERT INTO skill_installations(id, skill_id, skill_version_id,
               target_kind, target_ref, path, status, first_seen_at, last_seen_at,
               created_at, updated_at) VALUES ('new-installation', ?, 'next-version',
               'repository', 'test', '/test/SKILL.md', 'active', '2026-05-02',
               '2026-05-02', '2026-05-02', '2026-05-02')""", (skill_id,),
        )
        SkillRegistryService(conn, initialize_schema=False).sync_paths([new_package])
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)

    restarted = client_for(db)
    response = restarted.get("/api/skills?limit=500")
    assert response.status_code == 200
    assert response.headers.get("X-Reflect-Publication") != "stale"
    retained = response.json()
    assert retained["analysis_state"] == "ready"
    assert retained["total_count"] == 2
    existing = next(skill for skill in retained["skills"] if skill["id"] == skill_id)
    new = next(skill for skill in retained["skills"] if skill["id"] != skill_id)
    for field in ("evidence_count", "usage_count", "measurement_count"):
        assert existing[field] == listed["skills"][0][field]
        assert new[field] == 0
    assert existing["description"] == "Current description"
    assert existing["current_version_id"] == "next-version"
    assert existing["installation_count"] == 1
    assert existing.get("analysis_state", "ready") == "ready"
    assert new.get("analysis_state", "ready") == "ready"
    assert existing.get("evidence_state", "published") == "published"
    assert new.get("evidence_state", "published") == "published"

    retained_detail = restarted.get(f"/api/skills/{skill_id}").json()
    assert retained_detail["skill"] == existing
    assert retained_detail["versions"][0]["content_markdown"] == "Current package content"
    assert retained_detail["installations"][0]["id"] == "new-installation"
    for field in ("evidence", "usage_occurrences", "measurements"):
        assert retained_detail[field] == detail[field]
    assert retained_detail["analysis_state"] == "ready"
    assert restarted.get(f"/api/skills/{new['id']}").json()["analysis_state"] == "ready"
    assert restarted.get("/api/skills?limit=500&include_stale=true").json()["analysis_state"] == "ready"
    assert restarted.get("/api/skills/not-registered").status_code == 404
    assert restarted.get("/api/skills?limit=500").json().get("publication") == retained.get("publication")
    assert cache_file.read_bytes() != recorded_cache


def test_pending_skills_without_completed_cache_are_not_persisted(report):
    db, _ = report
    with closing(connect_sqlite(db)) as conn:
        skill_id = conn.execute('SELECT id FROM skills').fetchone()[0]
        mark_snapshot(conn, ready=False)
    client = client_for(db)
    for path in ('/api/skills?limit=500', f'/api/skills/{skill_id}'):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()['analysis_state'] == 'ready'


def test_errors_and_review_tokens_are_never_served_from_cache(report, monkeypatch):
    db, finding = report
    client = client_for(db)
    assert client.get(f"/api/findings/{finding}").status_code == 200
    monkeypatch.setattr("reflect.dashboard_improvements.DashboardImprovementAdapter.finding", lambda *_: None)
    assert client.get(f"/api/findings/{finding}").status_code == 404
    assert client.get("/api/loops?status=invalid").status_code == 422
    assert client.get("/api/explore/not-a-view").status_code == 404
    monkeypatch.setattr("reflect.dashboard_improvements.DashboardImprovementAdapter.workflow_preview", lambda *_, **__: {"approval_token": "must-not-persist"})
    assert client.get("/api/workflows/example/preview").status_code == 200
    document = json.loads(db.with_suffix(".dashboard-responses.json").read_text())
    assert "must-not-persist" not in json.dumps(document)

    def unavailable(*_, **__):
        raise SnapshotNotReadyError("Preparing")

    monkeypatch.setattr("reflect.dashboard_improvements.DashboardImprovementAdapter.workflow_preview", unavailable)
    assert client.get("/api/workflows/example/preview").status_code == 503
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)
    pending_client = client_for(db)
    assert pending_client.get("/api/explore/not-a-view").status_code == 404


def test_default_dashboard_retains_timestamp_and_marks_external_refresh_stale(report):
    db, _ = report
    client = client_for(db)
    before = client.get("/api/data?range=all").json()
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)
    after = client.get("/api/data?range=all").json()
    assert after["analysis_state"] == before["analysis_state"]
    assert after.get("publication") == before.get("publication")
    assert after["snapshot_completed_at"] == before["snapshot_completed_at"]
    assert after["measurements"] == before["measurements"]


@pytest.mark.parametrize("origin", ["request", "restart"])
def test_older_read_cannot_overwrite_newer_cached_response(report, monkeypatch, origin):
    db, _ = report
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    calls = 0

    def findings(*_, **__):
        nonlocal calls
        with lock:
            calls += 1
            first = calls == 1
        payload = {"findings": [{"id": "old-generation" if first else "new-generation"}]}
        if first:
            entered.set()
            assert release.wait(5)
        return payload

    worker = None
    client = client_for(db)
    if origin == "restart":
        client = client_for(db)
    monkeypatch.setattr("reflect.dashboard_improvements.DashboardImprovementAdapter.findings", findings)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first_request = pool.submit(client.get, "/api/findings") if worker is None else None
        if worker is not None:
            assert worker.start()
        try:
            assert entered.wait(3)
            assert client.get("/api/findings").json()["findings"][0]["id"] == "new-generation"
            saved = json.loads(db.with_suffix(".dashboard-responses.json").read_text())
            saved_at = next(entry["loaded_at"] for entry in saved["entries"].values()
                            if entry["payload"].get("findings") == [{"id": "new-generation"}])
        finally:
            release.set()
        if first_request is not None:
            assert first_request.result(5).status_code == 200
        else:
            assert worker.wait(5)

    def unavailable(*_, **__):
        raise SnapshotNotReadyError("Preparing")

    monkeypatch.setattr("reflect.dashboard_improvements.DashboardImprovementAdapter.findings", unavailable)
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)
    retained = client.get("/api/findings").json()
    assert retained["findings"][0]["id"] == "new-generation"
    assert retained["publication"]["loaded_at"] == saved_at


def test_identical_newer_read_advances_order_without_rewriting_timestamp(tmp_path):
    cache = DashboardResponseCache(tmp_path / "cache.json", cache_key="scope", default_range="7d")
    current = {"value": "current"}
    cache.put("key", current)
    original = cache.stale("key", reason="preparing")
    old_order, new_order = cache.start_read(), cache.start_read()
    cache.put("key", current, read_order=new_order)
    cache.put("key", {"value": "older"}, read_order=old_order)
    assert cache.stale("key", reason="preparing") == original


@pytest.mark.parametrize("analysis_state", ["preparing", "historical_pending"])
def test_cached_partial_usage_is_marked_stale_when_next_refresh_starts(report, analysis_state):
    db, _ = report
    with closing(connect_sqlite(db)) as conn:
        if analysis_state == "preparing":
            mark_snapshot(conn, ready=False)
            mark_snapshot(conn, ready=True, profile="usage")
        else:
            pending = PendingRefreshState(conn)
            pending.mark({"sess-sql"}, phases=("graph", "improvements"))
            pending.defer({"sess-sql"}, phases=("graph", "improvements"))
            # Include the fixture session in the default seven-day window.
            recent = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
            for table in ("sessions", "steps", "session_rollups"):
                conn.execute(f"UPDATE {table} SET started_at=?, ended_at=?", (recent, recent))
            mark_snapshot(conn, ready=True)
    client = client_for(db)
    original = client.get("/api/data?range=all").json()
    assert original["analysis_state"] == "ready"
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)
    retained = client.get("/api/data?range=all").json()
    assert retained["analysis_state"] == "ready"
    assert retained["measurements"] == original["measurements"]
    assert retained["snapshot_completed_at"] == original["snapshot_completed_at"]
