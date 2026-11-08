"""CLI and MCP registry reads retain evidence while snapshot validation runs."""

import json
from contextlib import closing

import pytest
from click.testing import CliRunner

from reflect.core import _open_improvement_service, main
from reflect.improvements.service import ImprovementService
from reflect.mcp import reflect_patterns, reflect_skills
from reflect.store.sqlite import SnapshotNotReadyError, connect_sqlite, mark_snapshot


@pytest.fixture
def registry(tmp_path, monkeypatch):
    db = tmp_path / "reflect.db"
    with closing(connect_sqlite(db)) as conn:
        service = ImprovementService(conn)
        candidate = service.stage_extracted_skills([{
            "name": "verify-work", "description": "Verify the changed work.",
            "content": "# Verify work\n\n1. Run the relevant check.\n",
            "behavior_type": "verification",
        }], session_ids=[], source_agent="codex")[0]
        service.skills.sync_workflow_candidates()
        skill = service.skills.list()[0]
        conn.execute("""INSERT INTO skill_measurements(id,skill_id,skill_version_id,metric_name,
                        before_value,after_value,verdict,confidence,measured_at,details_json,created_at,updated_at)
                        VALUES ('stored-check',?,?,'tokens',10,5,'improved',0.8,'2026-11-08',
                        '{"source_measurement_id":"older-check"}','2026-11-08','2026-11-08')""",
                     (skill.id, skill.current_version_id))
        mark_snapshot(conn, ready=False)
    monkeypatch.setenv("REFLECT_DB_PATH", str(db))
    return db, skill.id, candidate


@pytest.mark.parametrize("ready", [False, True])
def test_cli_registry_list_and_show_label_recorded_analysis(registry, ready, monkeypatch):
    db, skill, _ = registry
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=ready)
    if not ready:
        def forbidden(*args, **kwargs):
            raise AssertionError("Stored skill measurement must not be revalidated while pending")
        monkeypatch.setattr("reflect.improvements.measurement.MeasurementService.list", forbidden)
    for args in (["skills"], ["skills", "show", skill]):
        result = CliRunner().invoke(main, [*args, "--json", "--db-path", str(db)])
        assert result.exit_code == 0, str(result.exception) or result.output
        payload = json.loads(result.output)
        assert payload["analysis_pending"] is not ready
        assert payload["evidence_state"] == ("current" if ready else "recorded")
        assert payload["analysis_state"] == ("ready" if ready else "pending")
    text = CliRunner().invoke(main, ["skills", "--db-path", str(db)])
    assert text.exit_code == 0
    if not ready:
        assert "Recorded evidence" in text.output and "validation is pending" in text.output


def test_mcp_registry_reads_do_not_validate_pending_workflows(registry, monkeypatch):
    _, skill, candidate = registry

    def forbidden(*args, **kwargs):
        raise AssertionError("Pending read must not validate measurements")

    monkeypatch.setattr("reflect.improvements.measurement_validity.MeasurementValidity.apply", forbidden)
    skills = reflect_skills()
    patterns = reflect_patterns()
    assert skill in {item["id"] for item in skills["skills"]}
    assert candidate in {item["id"] for item in patterns["workflows"]}
    for payload in (skills, patterns):
        assert payload["analysis_pending"] is True
        assert payload["analysis_state"] == "pending"
        assert payload["evidence_state"] == "recorded"
        assert payload["limitations"]


def test_schema_gate_and_default_snapshot_gate_remain_required(registry):
    db, _, _ = registry
    with pytest.raises(SnapshotNotReadyError), _open_improvement_service(db, read_only=True):
        pytest.fail("Unchanged full snapshot readers must retain their readiness gate")
    with closing(connect_sqlite(db)) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version=(SELECT MAX(version) FROM schema_migrations)")
        conn.commit()
    result = CliRunner().invoke(main, ["skills", "--json", "--db-path", str(db)])
    assert result.exit_code != 0
    with pytest.raises((SnapshotNotReadyError, RuntimeError)):
        reflect_skills()
