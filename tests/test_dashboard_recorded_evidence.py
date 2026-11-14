"""Cold drill-downs inspect recorded evidence without claiming current validation."""

import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_dashboard_sql_api import _seed_improvement_ledger, _seed_sql_report_db

from reflect.dashboard_server import build_dashboard_app
from reflect.execution_units import ExecutionUnitService
from reflect.preparation import PreparationCoordinator
from reflect.store.sqlite import connect_sqlite, connect_sqlite_read_only, mark_snapshot


@pytest.fixture
def evidence(tmp_path):
    db = tmp_path / "reflect.db"
    _seed_sql_report_db(db)
    _seed_improvement_ledger(db)
    with closing(connect_sqlite(db)) as conn:
        ExecutionUnitService(conn).refresh()
        conn.execute("UPDATE execution_units SET source='session_fallback', outcome='success', verification_passed=1, eligible=0")
        unit = conn.execute("SELECT id FROM execution_units LIMIT 1").fetchone()[0]
        candidate = conn.execute("SELECT id FROM workflow_candidates").fetchone()[0]
        conn.execute(
            """INSERT INTO workflow_versions(id,candidate_id,version,content_json,content_hash,created_at)
               VALUES ('recorded-version',?,1,'{}','hash','2026-05-01')""", (candidate,),
        )
        conn.execute(
            """INSERT INTO interventions(id,workflow_version_id,scope_type,scope_id,target_path,
               applied_hash,applied_content,exposure_started_at,created_at,updated_at)
               VALUES ('recorded-intervention','recorded-version','user','local','/unused',
               'hash','content','2026-05-01','2026-05-01','2026-05-01')"""
        )
        conn.execute(
            """INSERT INTO workflow_exposures(id,intervention_id,session_id,execution_unit_id,state,evidence_json,created_at)
               VALUES ('recorded-exposure','recorded-intervention','sess-sql',?,'followed',
                       '{"verification_observed":true}', '2026-05-01')""", (unit,),
        )
        cohort = {"unit": "execution_units", "before_execution_unit_ids": [unit],
                  "after_execution_unit_ids": [], "measurement_state": "collecting"}
        for kind in ("exact", "legacy"):
            stored = dict(cohort)
            if kind == "exact":
                stored["execution_unit_snapshots"] = {"before": [{
                    "execution_unit_id": unit, "session_id": "sess-sql", "title": "Frozen title",
                    "status": "completed", "metric_value": 12.5, "outcome": "success",
                    "verification_passed": True, "assessment": {"outcome": "success"},
                }], "after": []}
            conn.execute(
                """INSERT INTO measurements(id,intervention_id,metric_name,cohort_json,before_value,
                   before_count,after_count,measured_at,created_at,updated_at)
                   VALUES (?,'recorded-intervention',?,?,12.5,1,0,'2026-05-01','2026-05-01','2026-05-01')""",
                (kind, kind, json.dumps(stored)),
            )
        conn.execute("UPDATE sessions SET title='Current changed title'")
        mark_snapshot(conn, ready=False)
    return db, candidate, unit


def _client(db):
    return TestClient(build_dashboard_app(
        docs_dir=db.parent, db_path=db, preparation_worker=PreparationCoordinator(lambda _: {}),
    ))


def test_recorded_mcp_findings_do_not_revalidate_candidate_measurements(evidence, monkeypatch):
    from reflect import mcp
    from reflect.improvements.measurement_validity import MeasurementValidity

    db, candidate, _ = evidence
    monkeypatch.setenv("REFLECT_DB_PATH", str(db))

    def forbidden(*_args, **_kwargs):
        pytest.fail("Recorded candidate state must not validate partly rebuilt execution units")

    monkeypatch.setattr(MeasurementValidity, "apply", forbidden)
    payload = mcp.reflect_improvements(global_scope=True, period="all", detail="full")
    assert any(item["candidate_id"] == candidate for item in payload["findings"])
    assert payload["evidence_state"] == "recorded"
    assert payload["analysis_pending"] is True
    assert payload["freshness"]["safe_for_before_after"] is False


def test_cold_workflow_references_survive_pending_eligibility_without_current_claims(evidence):
    db, candidate, unit = evidence
    client = _client(db)
    with closing(connect_sqlite_read_only(db, profile=None)) as observer:
        before = observer.execute("PRAGMA data_version").fetchone()
        response = client.get(f"/api/workflows/{candidate}/evidence")
        observer.rollback()
        assert observer.execute("PRAGMA data_version").fetchone() == before
    assert response.status_code == 200
    payload = response.json()
    assert payload["candidate"]["id"] == candidate
    assert payload["candidate"]["contract_signature"] == payload["contract_signature"]
    assert payload["analysis_state"] == "pending"
    assert payload["evidence_state"] == payload["publication"]["state"] == "recorded"
    assert payload["publication"]["snapshot_completed_at"] is None
    assert payload["support_execution_unit_count"] == 1
    assert payload["provenance_session_count"] == 1
    member = payload["support_execution_units"][0]
    assert member["execution_unit_id"] == unit
    assert member["outcome"] is member["verification_passed"] is None
    exposure = payload["exposed_execution_units"][0]
    assert exposure["exposure_state"] is None
    assert all(summary.startswith("Recorded classification (current validation pending):")
               for summary in exposure["evidence_summaries"])
    assert not db.with_suffix(".dashboard-responses.json").exists()
    assert client.get(f"/api/workflows/{candidate}/preview").status_code == 503
    assert client.get("/api/workflows/missing/evidence").status_code == 404


def test_cold_workflow_list_is_recorded_and_does_not_validate_pending_measurements(evidence, monkeypatch):
    from reflect.improvements.measurement_validity import MeasurementValidity

    db, candidate, _ = evidence
    monkeypatch.setattr(MeasurementValidity, "apply", lambda *_a, **_k: pytest.fail("pending validity"))
    response = _client(db).get("/api/workflows")
    assert response.status_code == 200
    payload = response.json()
    assert payload["analysis_state"] == "pending"
    assert payload["evidence_state"] == "recorded"
    assert candidate in {item["id"] for item in payload["workflows"]}


@pytest.mark.parametrize("path,method", [("/api/workflows", "workflows"), ("/api/impact", "impact")])
def test_pending_panel_cache_precedes_adapter_and_is_exact_scope(evidence, monkeypatch, path, method):
    from reflect.dashboard_improvements import DashboardImprovementAdapter

    db, _, _ = evidence
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=True)
    client = _client(db)
    before = client.get(path).json()
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)
    original = getattr(DashboardImprovementAdapter, method)
    monkeypatch.setattr(DashboardImprovementAdapter, method, lambda *_a, **_k: pytest.fail("cached read queried adapter"))
    response = client.get(path)
    assert response.status_code == 200
    payload = response.json()
    assert payload.pop("publication")["state"] == "stale"
    assert payload.pop("analysis_state") == "stale"
    assert payload == before
    monkeypatch.setattr(DashboardImprovementAdapter, method, original)
    if method == "workflows":
        filtered = client.get(path + "?status=rejected").json()
        assert filtered["workflows"] == []
        assert filtered["evidence_state"] == "recorded"


def test_cold_impact_uses_only_frozen_details_or_explicit_missing_member_references(evidence, monkeypatch):
    db, _, unit = evidence

    def forbidden(*args, **kwargs):
        raise AssertionError("Pending evidence must not reconstruct or validate from current units")

    monkeypatch.setattr("reflect.improvements.measurement.MeasurementService._execution_units_by_ids", forbidden)
    monkeypatch.setattr("reflect.improvements.measurement_validity.MeasurementValidity.invalid_members", forbidden)
    client = _client(db)
    listed = client.get("/api/impact")
    assert listed.status_code == 200
    assert listed.json()["evidence_state"] == "recorded"
    assert listed.json()["publication"]["snapshot_completed_at"] is None
    assert {item["id"] for item in listed.json()["impact_checks"]} == {"exact", "legacy"}
    assert all(item["before_value"] == 12.5 for item in listed.json()["impact_checks"])
    for kind in ("exact", "legacy"):
        response = client.get(f"/api/impact/{kind}/evidence")
        assert response.status_code == 200
        payload = response.json()
        assert payload["analysis_state"] == "pending"
        assert payload["publication"]["state"] == payload["evidence_state"] == "recorded"
        assert payload["publication"]["recorded_at"] == payload["measured_at"] == "2026-05-01"
        assert payload["measurement_state"] == "validation_pending"
        assert payload["invalid_members"] is None
        assert payload["before_count"] == 1
        assert payload["after_count"] == 0
        member = payload["before_execution_units"][0]
        assert member["outcome"] is member["verification_passed"] is member["assessment"] is None
        if kind == "exact":
            assert payload["snapshot_exact"] is True
            assert member["title"] == "Frozen title"
            assert member["metric_value"] == 12.5
            assert payload["missing_snapshot_execution_unit_ids"] == []
        else:
            assert payload["snapshot_exact"] is False
            assert member["snapshot_missing"] is True
            assert member["metric_value"] is None
            assert "session_id" not in member
            assert payload["missing_snapshot_execution_unit_ids"] == [unit]
    assert not db.with_suffix(".dashboard-responses.json").exists()


@pytest.mark.parametrize("kind", ["workflow", "impact"])
def test_completed_exact_evidence_cache_precedes_recorded_reads_and_schema_still_required(evidence, kind):
    db, candidate, _ = evidence
    path = f"/api/workflows/{candidate}/evidence" if kind == "workflow" else "/api/impact/exact/evidence"
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=True)
    client = _client(db)
    before = client.get(path).json()
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)
    retained = client.get(path).json()
    assert retained.pop("publication")["state"] == "stale"
    assert retained.pop("analysis_state") == "stale"
    assert retained == before
    with closing(connect_sqlite(db)) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version=(SELECT MAX(version) FROM schema_migrations)")
        conn.commit()
    assert client.get(path).status_code == 503


def test_new_recorded_impact_supersedes_older_published_cache_during_refresh(evidence, monkeypatch):
    from reflect.dashboard_improvements import DashboardImprovementAdapter
    from reflect.improvements.measurement_validity import MeasurementValidity

    db, _, _ = evidence
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=True)
    client = _client(db)
    old = client.get('/api/impact').json()
    with closing(connect_sqlite(db)) as conn:
        conn.execute("""INSERT INTO measurements
            (id,intervention_id,metric_name,cohort_json,before_value,after_value,
             before_count,after_count,verdict,confidence,measured_at,created_at,updated_at)
            SELECT 'new-recorded',intervention_id,metric_name,
                json_set(cohort_json,'$.measurement_state','measured'),1.25,0,
                20,10,'improved',0.75,'2026-12-01','2026-12-01','2026-12-01'
            FROM measurements LIMIT 1""")
        mark_snapshot(conn, ready=False)
    monkeypatch.setattr(MeasurementValidity, 'apply', lambda *_a, **_k: pytest.fail('pending validation'))
    response = client.get('/api/impact')
    payload = response.json()
    assert payload != old
    assert payload['impact_checks'][0]['id'] == 'new-recorded'
    assert payload['impact_checks'][0]['before_value'] == 1.25
    assert payload['analysis_state'] == 'pending'
    assert payload['publication']['state'] == 'recorded'
    assert payload['evidence_state'] == 'recorded'
    original = DashboardImprovementAdapter.impact
    monkeypatch.setattr(DashboardImprovementAdapter, 'impact', lambda *_a, **_k: pytest.fail('unchanged recorded cache'))
    assert client.get('/api/impact').json() == payload
    monkeypatch.setattr(DashboardImprovementAdapter, 'impact', original)
    restarted = _client(db).get('/api/impact').json()
    assert restarted['impact_checks'] == payload['impact_checks']
    assert restarted['publication']['state'] == 'recorded'


def test_token_coverage_migration_withdraws_old_claim_but_preserves_audit(evidence):
    from reflect.improvements.measurement import MeasurementService
    from reflect.store.migrate import migrate

    db, _, _ = evidence
    with closing(connect_sqlite(db)) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version=55")
        conn.execute("UPDATE measurements SET metric_name='context_outlier_sessions', verdict='unchanged', before_value=0, after_value=0 WHERE id='exact'")
        assert migrate(conn) == [55]
        row = conn.execute("SELECT cohort_json, before_value, after_value, verdict FROM measurements WHERE id='exact'").fetchone()
        cohort = json.loads(row[0])
        assert row[1:] == (None, None, "insufficient_data")
        assert cohort["measurement_state"] == "not_measurable"
        assert cohort["superseded_token_comparison"]["verdict"] == "unchanged"
        assert cohort["superseded_token_comparison"]["snapshots"]["before"][0]["title"] == "Frozen title"
        public = MeasurementService(conn).list(measurement_id="exact", recorded_only=True)[0]
        assert public["before_value"] is None
        assert "superseded_token_comparison" not in public["cohort"]
        details = MeasurementService(conn).sessions("exact", recorded_only=True)
        assert details["before_execution_units"][0]["metric_value"] is None
