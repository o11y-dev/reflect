"""The Impact CLI reads the same persisted payload as the context/MCP service."""

import json
from contextlib import closing

import pytest
from click.testing import CliRunner

from reflect.context import ReflectContextService
from reflect.core import main
from reflect.improvements.service import ImprovementService
from reflect.store.sqlite import connect_sqlite, connect_sqlite_read_only, mark_snapshot


@pytest.fixture
def impact_store(tmp_path, monkeypatch):
    db_path = tmp_path / "reflect.db"
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    cohort = {
        "unit": "execution_units", "measurement_state": "not_measurable",
        "baseline_state": "historical_unavailable",
        "measurement_reasons": ["Selected baseline evidence lacks observed outcomes."],
        "before_execution_unit_ids": [], "after_execution_unit_ids": [],
        "execution_unit_snapshots": {"before": [], "after": []},
        "eligibility": {"before": {"candidate_count": 22, "eligible_count": 0,
                                  "exclusion_reasons": {"outcome_unobserved": 22}}},
        "exposure": {"after": {"selected_count": 0, "followed": 0, "invoked": 0, "unknown": 0}},
    }
    with closing(connect_sqlite(db_path)) as conn:
        service = ImprovementService(conn)
        workflow_ids = service.stage_extracted_skills([
            {"name": f"check-{index}", "description": "Verify captured work.",
             "content": f"Run {'pytest' if index == 0 else 'ruff check'}. Inspect the recorded result.",
             "behavior_type": "verification"}
            for index in range(2)
        ], session_ids=[], source_agent="codex")
        for index, workflow_id in enumerate(workflow_ids):
            service.workflows.apply(workflow_id, project_root=root)
            intervention_id = conn.execute(
                """SELECT i.id FROM interventions i JOIN workflow_versions w ON w.id=i.workflow_version_id
                   WHERE w.candidate_id=?""", (workflow_id,),
            ).fetchone()[0]
            timestamp = f"2026-11-0{index + 1}T10:00:00+00:00"
            conn.execute(
                """INSERT INTO measurements(
                     id,intervention_id,metric_name,cohort_json,before_count,after_count,
                     measured_at,created_at,updated_at)
                   VALUES (?,?,?,?,0,0,?,?,?)""",
                (f"impact-{index}", intervention_id, "tool_failure_rate", json.dumps(cohort),
                 timestamp, timestamp, timestamp),
            )
        mark_snapshot(conn, ready=True)
    from backend_helpers import bind_backend

    from reflect.store.snapshots import PublishedSnapshotStore
    PublishedSnapshotStore(db_path, reserve_bytes=0).publish(evidence_revision="impact-fixture")
    bind_backend(monkeypatch, db_path)
    return db_path, workflow_ids


@pytest.mark.parametrize("selection", ["list", "limit", "workflow", "detail", "missing"])
def test_impact_json_matches_shared_service_without_writes(impact_store, monkeypatch, selection):
    db_path, workflow_ids = impact_store
    kwargs = {"impact_id": None, "workflow_id": None, "limit": 20}
    arguments = []
    if selection == "limit":
        kwargs["limit"] = 1
        arguments = ["--limit", "1"]
    elif selection == "workflow":
        kwargs.update(workflow_id=workflow_ids[0], limit=1)
        arguments = ["--workflow-id", workflow_ids[0], "--limit", "1"]
    elif selection in {"detail", "missing"}:
        kwargs["impact_id"] = "impact-0" if selection == "detail" else "missing"
        arguments = [kwargs["impact_id"]]
    with closing(connect_sqlite_read_only(db_path)) as conn:
        expected = ReflectContextService(conn, initialize_schema=False).impact(**kwargs)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Impact inspection must not refresh, migrate, or open a writable store")

    monkeypatch.setattr("reflect.store.sqlite.connect_sqlite", forbidden)
    monkeypatch.setattr("reflect.improvements.service.migrate", forbidden)
    monkeypatch.setattr("reflect.preparation_pipeline.prepare_sql_report_db", forbidden)
    original = ReflectContextService.impact
    calls = []

    def inspect_read_only(service, **parameters):
        assert service.conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert service.conn.in_transaction
        calls.append(parameters)
        payload = original(service, **parameters)
        assert service.conn.total_changes == 0
        return payload

    monkeypatch.setattr(ReflectContextService, "impact", inspect_read_only)
    result = CliRunner().invoke(main, ["impact", *arguments, "--json", "--db-path", str(db_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == expected
    assert calls == [kwargs]
    if selection == "detail":
        comparison = expected["comparison"]
        assert comparison["eligibility"]["before"]["candidate_count"] == 22
        assert comparison["eligibility"]["before"]["eligible_count"] == 0
        assert comparison["exposure"]["after"]["unknown"] == 0
        assert comparison["snapshot_exact"] is True


def test_impact_human_output_shows_persisted_status_and_reasons(impact_store):
    db_path, _ = impact_store
    result = CliRunner().invoke(main, ["impact", "impact-0", "--db-path", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "impact-0 · not_measurable · baseline: historical_unavailable" in result.output
    assert "Selected baseline evidence lacks observed outcomes." in result.output
    assert "Use --json" in result.output


def test_impact_missing_store_does_not_refresh_or_create(tmp_path, monkeypatch):
    missing_path = tmp_path / "absent" / "reflect.db"
    from backend_helpers import bind_backend
    bind_backend(monkeypatch, missing_path)
    result = CliRunner().invoke(main, ["impact", "--json", "--db-path", str(missing_path)])
    assert result.exit_code != 0
    assert "preparing" in result.output
    assert not missing_path.exists()


def test_impact_pending_store_reads_recorded_evidence_without_writes(impact_store, monkeypatch):
    db_path, _ = impact_store
    with closing(connect_sqlite(db_path)) as conn:
        mark_snapshot(conn, ready=False)
    original = ReflectContextService.impact

    def inspect_read_only(service, **parameters):
        assert service.conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert service.conn.in_transaction
        payload = original(service, **parameters)
        assert service.conn.total_changes == 0
        return payload

    def forbidden(*_args, **_kwargs):
        pytest.fail("Pending Impact inspection must not refresh or mutate the store")

    monkeypatch.setattr(ReflectContextService, "impact", inspect_read_only)
    monkeypatch.setattr("reflect.store.sqlite.connect_sqlite", forbidden)
    monkeypatch.setattr("reflect.improvements.service.migrate", forbidden)
    monkeypatch.setattr("reflect.preparation_pipeline.prepare_sql_report_db", forbidden)
    for arguments in ([], ["impact-0"]):
        result = CliRunner().invoke(main, ["impact", *arguments, "--json", "--db-path", str(db_path)])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["analysis_pending"] is False
        if arguments:
            assert payload["comparison"]["eligibility"]["before"]["candidate_count"] == 22
            assert payload["comparison"]["measurement_state"] == "not_measurable"
        else:
            assert payload["count"] == 2
    result = CliRunner().invoke(main, ["impact", "impact-0", "--db-path", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "not_measurable" in result.output


def test_impact_help_and_limit_validation():
    result = CliRunner().invoke(main, ["impact", "--help"])
    assert result.exit_code == 0
    for option in ("MEASUREMENT_ID", "--workflow-id", "--limit", "--json", "--db-path"):
        assert option in result.output
    assert "--refresh" not in result.output
    result = CliRunner().invoke(main, ["impact", "--limit", "0"])
    assert result.exit_code == 2
