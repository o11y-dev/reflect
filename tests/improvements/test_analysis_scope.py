import json
from pathlib import Path
from unittest.mock import patch

import pytest

from reflect.improvements.analysis_scope import AnalysisScope
from reflect.improvements.models import EvidenceRef, ObservationSessionRef
from reflect.improvements.repository import ImprovementRepository
from reflect.improvements.rules import RepeatedToolFailureRule
from reflect.improvements.service import ImprovementService
from reflect.improvements.session_insights import SessionInsightRule
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite
from reflect.views.measurements import MeasurementView

NOW = "2026-11-07T12:00:00+00:00"


@pytest.fixture
def conn(tmp_path):
    conn = connect_sqlite(tmp_path / "analysis.db")
    migrate(conn)
    for session_id in ("hot-a", "hot-b", "cold"):
        conn.execute(
            "INSERT INTO sessions(id, started_at, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (session_id, NOW, NOW, NOW),
        )
    for index, session_id in enumerate(("hot-a", "hot-b", *("cold" for _ in range(100)))):
        conn.execute(
            "INSERT INTO steps(id, session_id, seq, type, started_at, created_at, updated_at) VALUES (?, ?, ?, 'tool_call', ?, ?, ?)",
            (f"s{index}", session_id, index, NOW, NOW, NOW),
        )
        conn.execute(
            """INSERT INTO tool_calls(id, step_id, session_id, tool_name, status, created_at, updated_at)
               VALUES (?, ?, ?, 'exec', ?, ?, ?)""",
            (f"t{index}", f"s{index}", session_id, "failed" if session_id != "cold" else "ok", NOW, NOW),
        )
    conn.commit()
    yield conn
    conn.close()


def test_scope_bounds_aggregation_and_evidence_before_detection(conn):
    rule = RepeatedToolFailureRule()
    assert rule.evaluate(conn)[0].metric_value == pytest.approx(2 / 102)
    with AnalysisScope(conn, {"hot-a", "hot-b"}) as scope:
        finding = rule.evaluate(conn, scope=scope)[0]
        assert finding.metric_value == 1
        assert {row.session_id for row in finding.source_sessions} == {"hot-a", "hot-b"}
    assert not conn.execute("SELECT 1 FROM sqlite_temp_master WHERE name='reflect_analysis_scope'").fetchone()


def _observation(conn, sessions, fingerprint):
    repository = ImprovementRepository(conn)
    rule = RepeatedToolFailureRule()
    repository.sync_rule_definitions([rule.definition], now=NOW)
    draft = rule.make_observation(
        identity=(fingerprint,), title=fingerprint, summary="Observed failures",
        metric_name="failures", metric_value=2, metric_unit="calls",
        metric_direction="lower_is_better", impact_score=40, confidence=0.8,
        evidence=[EvidenceRef(entity_type="session", entity_id=s, session_id=s, summary_redacted="Failure") for s in sessions],
    )
    draft.source_sessions = [ObservationSessionRef(session_id=s, occurrence_count=1) for s in sessions]
    observation_id = repository.upsert_observation(draft, now=NOW)
    repository.ensure_candidate(observation_id, proposal=rule.propose(draft), now=NOW)
    return observation_id


def test_retirement_requires_the_complete_support_scope(conn):
    hot = _observation(conn, ["hot-a"], "hot")
    cold = _observation(conn, ["cold"], "cold")
    mixed = _observation(conn, ["hot-a", "cold"], "mixed")
    repository = ImprovementRepository(conn)
    with AnalysisScope(conn, {"hot-a", "hot-b"}) as scope:
        assert repository.resolve_missing(RepeatedToolFailureRule.definition, set(), now="later", scope=scope) == 1
    statuses = dict(conn.execute("SELECT id, status FROM observations"))
    assert statuses[hot] == "resolved"
    assert statuses[cold] == statuses[mixed] == "proposal_ready"
    candidates = dict(conn.execute("SELECT observation_id, status FROM workflow_candidates"))
    assert candidates[hot] == "stale"
    assert candidates[cold] == candidates[mixed] == "pending"


def test_empty_scope_skips_detectors_and_retirement(conn):
    observation_id = _observation(conn, ["hot-a"], "keep")
    service = ImprovementService(conn, rules=[RepeatedToolFailureRule()], initialize_schema=False)
    with patch.object(RepeatedToolFailureRule, "evaluate", side_effect=AssertionError("detector ran")):
        result = service.refresh(session_ids=set(), analysis_session_ids=set())
    assert result["detected"] == result["resolved"] == result["loops"] == 0
    assert conn.execute("SELECT status FROM observations WHERE id=?", (observation_id,)).fetchone()[0] == "proposal_ready"
    with patch.object(service.execution_units, "assess_contracts", wraps=service.execution_units.assess_contracts) as assess:
        result = service.refresh(session_ids=set(), analysis_session_ids=set())
    assess.assert_called_once_with(session_ids=set(), execution_unit_ids=service.execution_units.changed_unit_ids)
    assert result["contracts_changed"] == 0


@pytest.mark.parametrize("changed", [set(), {"hot-a"}])
def test_unchanged_contracts_assess_only_changed_evidence_with_complete_detector_context(conn, changed):
    rule = RepeatedToolFailureRule()
    service = ImprovementService(conn, rules=[rule], initialize_schema=False)
    service.refresh()
    cold = conn.execute("SELECT * FROM execution_units WHERE session_id='cold'").fetchall()
    conn.execute("""CREATE TRIGGER preserve_cold_assessment BEFORE UPDATE ON execution_units
        WHEN OLD.session_id='cold' BEGIN SELECT RAISE(ABORT,'unchanged peer reassessed'); END""")
    selected = {"hot-a", "hot-b", "cold"}
    with (
        patch.object(service.execution_units, "assess_contracts", wraps=service.execution_units.assess_contracts) as assess,
        patch.object(rule, "evaluate", wraps=rule.evaluate) as evaluate,
    ):
        result = service.refresh(session_ids=changed, analysis_session_ids=selected)
    assess.assert_called_once_with(session_ids=changed, execution_unit_ids=service.execution_units.changed_unit_ids)
    assert evaluate.call_args.kwargs["scope"].session_ids == selected
    assert result["contracts_changed"] == 0
    assert conn.execute("SELECT * FROM execution_units WHERE session_id='cold'").fetchall() == cold
    assert conn.execute("SELECT metric_value FROM observations WHERE rule_id=?", (rule.definition.id,)).fetchone()[0] == pytest.approx(2 / 102)


def test_contract_assessment_reuses_expanded_evidence_scope(conn):
    service = ImprovementService(conn, rules=[], initialize_schema=False)
    service.refresh()
    affected = {"hot-a", "hot-b"}
    with (
        patch.object(service.execution_units, "affected_session_ids", side_effect=AssertionError("scope already expanded")),
        patch.object(service.execution_units, "assess_contracts", wraps=service.execution_units.assess_contracts) as assess,
    ):
        service.refresh(session_ids=affected, analysis_session_ids={*affected, "cold"}, dependencies_expanded=True)
    assess.assert_called_once_with(session_ids=affected, execution_unit_ids=service.execution_units.changed_unit_ids)


@pytest.mark.parametrize("changed", [None, {"removed-session-without-retained-scope"}])
def test_contract_assessment_preserves_full_or_unresolvable_dependency_scope(conn, changed):
    service = ImprovementService(conn, rules=[], initialize_schema=False)
    service.refresh()
    with patch.object(service.execution_units, "assess_contracts", wraps=service.execution_units.assess_contracts) as assess:
        result = service.refresh(session_ids=changed, analysis_session_ids=set())
    assess.assert_called_once_with(session_ids=None, execution_unit_ids=service.execution_units.changed_unit_ids)
    assert result["contracts_changed"] == 0


def test_late_edit_and_retraction_assess_overlapping_history_not_stable_peers(conn):
    conn.execute("INSERT INTO repos(id,full_name,created_at,updated_at) VALUES('repo','repo',?,?)", (NOW, NOW))
    conn.execute("""INSERT INTO workspaces(id,root_path,path_hash,label,repo_id,source_key,confidence,created_at,updated_at)
        VALUES('workspace','/workspace','workspace','workspace','repo','test',1,?,?)""", (NOW, NOW))
    conn.execute("UPDATE sessions SET workspace_id='workspace',repo_id='repo'")
    conn.execute("UPDATE steps SET started_at='2026-10-01T12:00:00+00:00' WHERE session_id='cold'")
    conn.execute("""INSERT INTO steps(id,session_id,seq,type,started_at,raw_attrs_json,created_at,updated_at)
        VALUES('peer-stop','hot-b',200,'lifecycle','2026-11-07T12:05:00+00:00',
               '{"reflect.turn.terminal":true}',?,?)""", (NOW, NOW))
    service = ImprovementService(conn, rules=[], initialize_schema=False)
    service.refresh()
    cold = conn.execute("SELECT * FROM execution_units WHERE session_id='cold'").fetchall()
    conn.execute("""INSERT INTO steps(id,session_id,seq,type,started_at,raw_attrs_json,created_at,updated_at)
        VALUES('late-edit','hot-a',201,'file_edit','2026-11-07T12:03:00+00:00',
               '{"gen_ai.client.file_path":"src/app.py"}',?,?)""", (NOW, NOW))
    for retracted in (False, True):
        if retracted:
            conn.execute("DELETE FROM steps WHERE id='late-edit'")
            conn.execute("UPDATE sessions SET workspace_id=NULL,repo_id=NULL WHERE id='hot-a'")
        with patch.object(service.execution_units, "assess_contracts", wraps=service.execution_units.assess_contracts) as assess:
            service.refresh(session_ids={"hot-a"}, analysis_session_ids={"hot-a", "hot-b", "cold"})
        assess.assert_called_once_with(session_ids={"hot-a", "hot-b"}, execution_unit_ids=service.execution_units.changed_unit_ids)
        assert conn.execute("SELECT * FROM execution_units WHERE session_id='cold'").fetchall() == cold


@pytest.mark.parametrize("change", ["add_milestone", "remove_milestone", "revoke"])
def test_contract_changes_reassess_all_history_without_new_evidence(conn, change):
    observation = _observation(conn, ["hot-a"], "contract")
    candidate = conn.execute("SELECT id FROM workflow_candidates WHERE observation_id=?", (observation,)).fetchone()[0]
    content = {"workflow_contract": {"signature_hash": "contract", "milestones": [{"id": "observe"}, {"id": "verify"}]}}
    conn.execute("""INSERT INTO workflow_versions(id,candidate_id,version,content_json,content_hash,status,created_at)
        VALUES('contract-version',?,1,?,'hash','approved',?)""", (candidate, json.dumps(content), NOW))
    service = ImprovementService(conn, rules=[], initialize_schema=False)
    service.refresh()
    if change == "revoke":
        conn.execute("UPDATE workflow_versions SET status='pending' WHERE id='contract-version'")
    else:
        milestones = content["workflow_contract"]["milestones"]
        if change == "add_milestone":
            milestones.insert(1, {"id": "act"})
        else:
            milestones.pop(0)
        conn.execute("UPDATE workflow_versions SET content_json=? WHERE id='contract-version'", (json.dumps(content),))
    with patch.object(service.execution_units, "assess_contracts", wraps=service.execution_units.assess_contracts) as assess:
        result = service.refresh(session_ids=set(), analysis_session_ids=set())
    assess.assert_called_once_with(session_ids=None, execution_unit_ids=None)
    assert result["contracts_changed"] == 1


def test_artifact_drift_still_reassesses_all_history_without_new_evidence(conn, tmp_path):
    observation = _observation(conn, ["hot-a", "hot-b"], "installed")
    candidate = conn.execute("SELECT id FROM workflow_candidates WHERE observation_id=?", (observation,)).fetchone()[0]
    service = ImprovementService(conn, rules=[], initialize_schema=False)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    applied = service.workflows.apply(candidate, project_root=project)
    service.refresh()
    target = conn.execute("SELECT target_path FROM interventions WHERE id=?", (applied["intervention_id"],)).fetchone()[0]
    Path(target).write_text("changed artifact\n")
    with patch.object(service.execution_units, "assess_contracts", wraps=service.execution_units.assess_contracts) as assess:
        result = service.refresh(session_ids=set(), analysis_session_ids=set())
    assess.assert_called_once_with(session_ids=None, execution_unit_ids=None)
    assert result["contracts_changed"] == result["stale_workflows"] == 1


def test_failed_evaluation_never_retires_findings(conn):
    observation_id = _observation(conn, ["hot-a"], "keep")
    conn.execute("INSERT INTO store_metadata(key,value,updated_at) VALUES ('assessed_contract_inputs_v1','before-failure',?)", (NOW,))
    conn.commit()
    service = ImprovementService(conn, rules=[RepeatedToolFailureRule()], initialize_schema=False)
    with (
        patch.object(RepeatedToolFailureRule, "evaluate", side_effect=ValueError("incomplete evidence")),
        pytest.raises(ValueError, match="incomplete evidence"),
    ):
        service.refresh(session_ids=set(), analysis_session_ids={"hot-a"})
    assert conn.execute("SELECT status FROM observations WHERE id=?", (observation_id,)).fetchone()[0] == "proposal_ready"
    assert not conn.execute("SELECT 1 FROM sqlite_temp_master WHERE name='reflect_analysis_scope'").fetchone()
    assert conn.execute("SELECT value FROM store_metadata WHERE key='assessed_contract_inputs_v1'").fetchone()[0] == "before-failure"


def test_r5_evaluated_session_without_turns_resolves_but_history_survives(conn):
    rule = SessionInsightRule("very_long")
    repository = ImprovementRepository(conn)
    repository.sync_rule_definitions([rule.definition], now=NOW)
    ids = {}
    for session_id in ("hot-a", "cold"):
        draft = rule.make_observation(
            identity=(session_id,), title="Long session", summary="Previously observed",
            scope_type="session", scope_id=session_id, metric_name="turns", metric_value=21,
            metric_unit="turns", metric_direction="lower_is_better", impact_score=40, confidence=0.8,
            evidence=[EvidenceRef(entity_type="session", entity_id=session_id, session_id=session_id, summary_redacted="Turns")],
        )
        ids[session_id] = repository.upsert_observation(draft, now=NOW)
    service = ImprovementService(conn, rules=[rule], initialize_schema=False)
    with patch("reflect.improvements.service.build_measurements", return_value=MeasurementView()) as build:
        service.refresh(session_ids=set(), analysis_session_ids={"hot-a"})
    build.assert_called_once_with(conn, session_ids={"hot-a"}, include_turns=True)
    statuses = dict(conn.execute("SELECT id, status FROM observations"))
    assert statuses[ids["hot-a"]] == "resolved"
    assert statuses[ids["cold"]] == "new"


def test_loop_retirement_preserves_outside_and_partial_support(conn):
    service = ImprovementService(conn, rules=[], initialize_schema=False)
    conn.execute("INSERT INTO agents(id,name,created_at,updated_at) VALUES ('claude','claude',?,?)", (NOW, NOW))
    for session_id in ("hot-a", "cold"):
        conn.execute("UPDATE sessions SET agent_id='claude',title='/loop 5m check build' WHERE id=?", (session_id,))
    service.loops.refresh()
    loop_id = conn.execute("SELECT id FROM loop_patterns").fetchone()[0]
    conn.execute("UPDATE sessions SET title='ordinary task'")
    with AnalysisScope(conn, {"hot-a"}) as scope:
        assert service.loops.refresh(scope=scope)["resolved"] == 0
    with AnalysisScope(conn, {"hot-a", "cold"}) as scope:
        assert service.loops.refresh(scope=scope)["resolved"] == 1
    assert conn.execute("SELECT status FROM loop_patterns WHERE id=?", (loop_id,)).fetchone()[0] == "resolved"


def test_loop_detection_uses_only_selected_sessions(conn):
    conn.execute("INSERT INTO agents(id,name,created_at,updated_at) VALUES ('claude','claude',?,?)", (NOW, NOW))
    conn.execute("UPDATE sessions SET agent_id='claude',title='/loop 5m check build'")
    service = ImprovementService(conn, rules=[], initialize_schema=False)
    with AnalysisScope(conn, {"hot-a"}) as scope:
        assert service.loops.refresh(scope=scope)["agent_native"] == 1
    assert {row[0] for row in conn.execute("SELECT session_id FROM loop_occurrences")} == {"hot-a"}


def test_truncated_support_is_not_proof_a_historical_finding_resolved(conn):
    observation_id = _observation(conn, ["hot-a"], "truncated")
    conn.execute("UPDATE observations SET affected_session_count=3 WHERE id=?", (observation_id,))
    with AnalysisScope(conn, {"hot-a", "hot-b", "cold"}) as scope:
        assert ImprovementRepository(conn).resolve_missing(RepeatedToolFailureRule.definition, set(), now="later", scope=scope) == 0
    assert conn.execute("SELECT status FROM observations WHERE id=?", (observation_id,)).fetchone()[0] == "proposal_ready"


def test_measurement_can_wait_for_the_callers_committed_evidence_checkpoint(conn):
    service = ImprovementService(conn, rules=[], initialize_schema=False)
    with patch.object(service.measurements, "measure_active", side_effect=AssertionError("before acknowledgement")):
        result = service.refresh(session_ids=set(), analysis_session_ids=set(), measure=False)
    assert result["measurements_created"] == result["regressions"] == 0
