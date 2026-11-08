from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from reflect.core import main
from reflect.improvements.procedures import ProcedureEvidenceRepository, procedure_signature
from reflect.improvements.service import ImprovementService
from reflect.improvements.workflow_roles import WorkflowSignal
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite

NOW = '2026-11-09T10:00:00+00:00'


# Same operation and check scope as the canonical fixture below.
PROCEDURE_SIGNATURE = procedure_signature([
    WorkflowSignal(kind="tool", name="Read", path="/repo/src/worker.py", successful_result=True),
    WorkflowSignal(kind="tool", name="Edit", path="/repo/src/worker.py", successful_result=True),
    WorkflowSignal(kind="tool", name="Bash", command="pytest tests", successful_result=True),
], {"checks": [{"scope": ["."]}]}, workspace_root="/repo")


@pytest.fixture
def evidence_service(tmp_path):
    conn = connect_sqlite(tmp_path / 'drafts.db')
    migrate(conn)
    conn.execute("INSERT INTO agents(id,name,created_at,updated_at) VALUES ('a','codex',?,?)", (NOW, NOW))
    conn.execute("INSERT INTO repos(id,full_name,created_at,updated_at) VALUES ('r','example/repo',?,?)", (NOW, NOW))
    conn.execute("INSERT INTO task_archetypes(id,name,description,created_at,updated_at) VALUES ('code-change','code-change','Build a change',?,?)", (NOW, NOW))
    conn.execute("""INSERT INTO workspaces(id,root_path,path_hash,label,repo_id,source_key,confidence,
        raw_json,created_at,updated_at) VALUES ('w','/repo','hash','repo','r','test',1,'{}',?,?)""", (NOW, NOW))
    for index, verification in enumerate(('passed', 'passed', 'passed', 'failed', 'unknown')):
        sid, uid = f's{index}', f'u{index}'
        conn.execute("""INSERT INTO sessions(id,agent_id,repo_id,workspace_id,started_at,status,created_at,updated_at)
            VALUES (?,'a','r','w',?,'completed',?,?)""", (sid, NOW, NOW, NOW))
        assessment = {'version': 1, 'boundary': 'closed', 'verification': verification, 'outcome': 'unknown',
            'checks': [{'step_id': f'{uid}-2', 'exit_code': 1 if verification == 'failed' else 0,
                'scope': ['.'], 'invalidated': False}]}
        conn.execute("""INSERT INTO execution_units(id,session_id,source,source_confidence,workspace_id,repo_id,
            agent_id,started_at,status,outcome,eligible,boundary_json,created_at,updated_at)
            VALUES (?,?,'test',1,'w','r','a',?,'completed','unknown',1,?,?,?)""",
            (uid, sid, NOW, json.dumps({'assessment': assessment}), NOW, NOW))
        conn.execute("INSERT INTO execution_unit_archetypes VALUES (?,'code-change',0.9,0,'{}',?,?)", (uid, NOW, NOW))
        for seq, name in enumerate(('Read', 'Edit', 'Bash')):
            step = f'{uid}-{seq}'
            payload = {'file_path': '/repo/src/worker.py'} if seq < 2 else {'command': 'pytest tests'}
            attrs = {'gen_ai.client.file_path': payload.get('file_path'),
                'gen_ai.client.command': payload.get('command'), 'process.exit.code': 0}
            conn.execute("""INSERT INTO steps(id,session_id,seq,type,status,started_at,raw_attrs_json,created_at,updated_at)
                VALUES (?,?,?,'tool_call','ok',?,?,?,?)""", (step, sid, seq, NOW, json.dumps(attrs), NOW, NOW))
            conn.execute('INSERT INTO execution_unit_steps VALUES (?,?,?,?)', (uid, step, sid, NOW))
            conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,status,input_preview_redacted,
                raw_attrs_json,created_at,updated_at) VALUES (?,?,?,?,'ok',?,?,?,?)""",
                (f'tool-{step}', step, sid, name, json.dumps(payload), json.dumps(attrs), NOW, NOW))
    conn.commit()
    bundle = ProcedureEvidenceRepository(conn).bundle()
    assert len(bundle['procedures']) == 1
    assert bundle['procedures'][0]['signature'] == PROCEDURE_SIGNATURE
    assert bundle['procedures'][0]['candidate_ready']
    yield ImprovementService(conn), conn, bundle
    conn.close()


def _draft(**evidence):
    return {'name': 'bounded-edit-check', 'description': 'Use for scoped edit and check work.',
        'content': '# Bounded edit\n\nInspect, edit, check, and stop on unexplained failures.',
        'preconditions': ['Required input and check scope are known.'],
        'verification': ['Inspect the post-edit check result.'],
        'abstain_when': ['The relevant check cannot run.'],
        'evidence': {'procedure_signature': PROCEDURE_SIGNATURE, 'execution_ids': ['u0', 'u1', 'u2', 'u3'],
            'session_ids': ['s0', 's1', 's2', 's3'], 'why_it_improves': 'Bound changes and inspect checks.', **evidence}}


def test_empty_extraction_does_not_mutate(evidence_service):
    service, conn, bundle = evidence_service
    before = conn.total_changes
    assert service.stage_extracted_skills([], session_ids=[], evidence_bundle=bundle) == []
    assert conn.total_changes == before


def test_citations_are_candidate_specific_and_keep_counterexamples(evidence_service):
    service, conn, bundle = evidence_service
    ids = service.stage_extracted_skills([_draft()], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)
    candidate = service.workflows.show(ids[0])
    assert candidate.support_execution_unit_count == 3
    assert candidate.content['procedure']['counterexamples'] == bundle['procedures'][0]['counterexamples']
    assert candidate.content['evidence']['session_ids'] == ['s0', 's1', 's2', 's3']
    assert candidate.content['evidence']['checked_execution_count'] == 3
    assert candidate.content['evidence']['denominator_execution_count'] == 4
    assert candidate.content['evidence']['why_it_improves'] == 'Bound changes and inspect checks.'
    rows = conn.execute('SELECT entity_id,polarity FROM observation_evidence ORDER BY entity_id').fetchall()
    assert [tuple(row) for row in rows] == [('u0', 'supporting'), ('u1', 'supporting'), ('u2', 'supporting'), ('u3', 'counterexample')]
    repeated = service.stage_extracted_skills([_draft()], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)
    assert repeated == ids
    assert conn.execute('SELECT COUNT(*) FROM workflow_candidates').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM observation_evidence').fetchone()[0] == 4


@pytest.mark.parametrize('overrides', [
    {'execution_ids': ['u0', 'u1', 'invented']}, {'session_ids': ['s0', 's1', 's2', 's4']},
    {'procedure_signature': 'invented'}, {'execution_ids': ['u0'], 'session_ids': ['s0']},
])
def test_invalid_refs_fail_before_any_write(evidence_service, overrides):
    service, conn, bundle = evidence_service
    before = conn.total_changes
    with pytest.raises(ValueError):
        service.stage_extracted_skills([_draft(**overrides)], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)
    assert conn.total_changes == before


def test_out_of_scope_or_ambiguous_procedure_is_rejected(evidence_service):
    service, conn, bundle = evidence_service
    with pytest.raises(ValueError):
        service.stage_extracted_skills([_draft()], session_ids=['s0', 's1', 's2'], evidence_bundle=bundle)
    duplicate = copy.deepcopy(bundle['procedures'][0])
    duplicate['workspace_id'] = 'other'
    bundle['procedures'].append(duplicate)
    with pytest.raises(ValueError, match='ambiguous'):
        service.stage_extracted_skills([_draft()], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)
    assert conn.execute('SELECT COUNT(*) FROM workflow_candidates').fetchone()[0] == 0


def test_manual_and_session_only_drafts_are_explicitly_unverified(evidence_service):
    service, conn, _ = evidence_service
    manual = _draft()
    manual.pop('evidence')
    candidate_id = service.stage_extracted_skills([manual], session_ids=['s0', 's1'])[0]
    candidate = service.workflows.show(candidate_id)
    assert candidate.support_execution_unit_count == 0
    assert candidate.content['evidence']['status'] == 'unverified_draft'
    assert service.repository.get_observation(candidate.observation_id).metric_value == 0
    assert service.repository.get_observation(candidate.observation_id).affected_session_count == 0
    with pytest.raises(ValueError):
        service.stage_extracted_skills([manual], session_ids=['s0'], evidence_bundle={'sessions': [{'id': 's0'}]})
    session_draft = _draft(session_ids=['s0'], execution_ids=[], procedure_signature='')
    session_draft['content'] += '\nAdditional fallback context.'
    candidate_id = service.stage_extracted_skills([session_draft], session_ids=['s0'],
        evidence_bundle={'sessions': [{'id': 's0'}]})[0]
    assert service.workflows.show(candidate_id).support_execution_unit_count == 0


def test_matching_workflows_are_suggestions_and_preserve_approved(evidence_service):
    service, conn, bundle = evidence_service
    first = service.stage_extracted_skills([_draft()], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)[0]
    conn.execute("UPDATE workflow_candidates SET status='approved' WHERE id=?", (first,))
    before = tuple(conn.execute('SELECT content_json,revision_hash,status FROM workflow_candidates WHERE id=?', (first,)).fetchone())
    second = _draft()
    second['content'] += '\nBounded handoff after unsuccessful checks.'
    second_id = service.stage_extracted_skills([second], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)[0]
    assert second_id != first
    candidate = service.repository.get_candidate(second_id)
    assert candidate.content['existing_workflow_matches'][0]['id'] == first
    assert candidate.content['existing_workflow_matches'][0]['status'] == 'approved'
    assert tuple(conn.execute('SELECT content_json,revision_hash,status FROM workflow_candidates WHERE id=?', (first,)).fetchone()) == before


def test_discover_empty_array_succeeds_without_opening_write_service(tmp_path):
    from reflect.models import TelemetryStats
    with patch('reflect.core._resolve_skills_agent', return_value=('claude', [])), \
        patch('reflect.core._resolve_and_analyze', return_value=TelemetryStats(session_files=[], span_files=[], total_events=0, events_by_type={}, events_by_file={})), \
        patch('reflect.core._improvement_snapshot_status', return_value=SimpleNamespace(ready=False)), \
        patch('subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='[]', stderr='')), \
        patch('reflect.core._open_improvement_service') as opened:
        result = CliRunner().invoke(main, ['skills', 'discover', '--yes', '--db-path', str(tmp_path / 'missing.db')])
    assert result.exit_code == 0, result.output
    assert 'No reusable' in result.output
    opened.assert_not_called()
    assert not (tmp_path / 'missing.db').exists()


def test_sql_packet_uses_shared_procedure_repository_and_keeps_empty_scope(evidence_service):
    from reflect.skill_extraction import _build_skill_evidence_bundle_from_sql

    _, conn, bundle = evidence_service
    with patch('reflect.improvements.procedures.ProcedureEvidenceRepository.bundle', return_value=bundle) as read:
        packet = _build_skill_evidence_bundle_from_sql(conn, session_ids={'s0', 's1'})
    assert packet['procedure_evidence'] == bundle
    read.assert_called_once_with(session_ids={'s0', 's1'})
    assert _build_skill_evidence_bundle_from_sql(conn, session_ids=set()) is None


def test_session_fallback_cannot_borrow_other_packet_sessions(evidence_service):
    service, conn, _ = evidence_service
    draft = _draft(session_ids=['s1'], execution_ids=[], procedure_signature='')
    before = conn.total_changes
    with pytest.raises(ValueError):
        service.stage_extracted_skills([draft], session_ids=['s0', 's1'], evidence_bundle={'sessions': [{'id': 's0'}]})
    assert conn.total_changes == before


def test_empty_or_unready_procedure_packet_cannot_claim_session_support(evidence_service):
    service, conn, bundle = evidence_service
    for packet in ({'procedures': []}, {'procedures': [{**bundle['procedures'][0], 'candidate_ready': False}]}):
        with pytest.raises(ValueError):
            service.stage_extracted_skills([_draft()], session_ids=[f's{i}' for i in range(5)], evidence_bundle=packet)
    assert conn.execute('SELECT COUNT(*) FROM workflow_candidates').fetchone()[0] == 0


def test_citation_validation_is_atomic_for_multiple_drafts(evidence_service):
    service, conn, bundle = evidence_service
    before = conn.total_changes
    with pytest.raises(ValueError):
        service.stage_extracted_skills([_draft(), _draft(execution_ids=['invented'])],
            session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)
    assert conn.total_changes == before


def test_unknown_execution_is_preserved_without_becoming_checked_support(evidence_service):
    service, _, bundle = evidence_service
    draft = _draft(execution_ids=['u0', 'u1', 'u2', 'u4'], session_ids=['s0', 's1', 's2', 's4'])
    cid = service.stage_extracted_skills([draft], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)[0]
    candidate = service.workflows.show(cid)
    assert candidate.support_execution_unit_count == 3
    assert candidate.content['evidence']['denominator_execution_count'] == 4
    assert candidate.content['procedure']['outcome_counts'] == {'unknown': 5}
    assert any(item['verification'] == 'unknown' for item in candidate.content['procedure']['counterexamples'])



def test_rendered_draft_keeps_all_structured_guards(evidence_service):
    service, _, bundle = evidence_service
    draft = _draft()
    draft['checkpoints'] = ['Checkpoint sentinel: change state or stop.']
    cid = service.stage_extracted_skills([draft], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)[0]
    candidate = service.workflows.show(cid)
    rendered = service.workflows._render_skill(candidate)
    assert '# Bounded edit' in rendered
    for key in ('preconditions', 'checkpoints', 'verification', 'abstain_when'):
        for instruction in candidate.content[key]:
            assert instruction in rendered
    assert 'repo_id=r, workspace_id=w, task_archetype_id=code-change' in rendered
    registered = service.skills.show(service.skills.skill_for_candidate(cid).id)
    assert all(instruction in registered.versions[0].content_markdown for instruction in candidate.content['abstain_when'])


@pytest.mark.parametrize('mutation', ['failed', 'invalidated', 'check_scope', 'membership', 'eligibility', 'mixed', 'task_scope'])
def test_stale_canonical_packet_is_rejected_before_writes(evidence_service, mutation):
    service, conn, bundle = evidence_service
    raw = json.loads(conn.execute("SELECT boundary_json FROM execution_units WHERE id='u0'").fetchone()[0])
    if mutation in {'failed', 'invalidated', 'check_scope'}:
        if mutation == 'failed':
            raw['assessment']['verification'] = 'failed'
            raw['assessment']['checks'][0]['exit_code'] = 1
        elif mutation == 'invalidated':
            raw['assessment']['checks'][0]['invalidated'] = True
        else:
            raw['assessment']['checks'][0]['scope'] = ['src/other.py']
        conn.execute("UPDATE execution_units SET boundary_json=? WHERE id='u0'", (json.dumps(raw),))
    elif mutation == 'membership':
        conn.execute("DELETE FROM execution_unit_steps WHERE execution_unit_id='u0' AND step_id='u0-1'")
    elif mutation == 'eligibility':
        conn.execute("UPDATE execution_units SET eligible=0 WHERE id='u0'")
    elif mutation == 'mixed':
        conn.execute("UPDATE execution_unit_archetypes SET mixed=1 WHERE execution_unit_id='u0'")
    else:
        conn.execute("DELETE FROM execution_unit_archetypes WHERE execution_unit_id='u0'")
    conn.commit()
    before = conn.total_changes
    with pytest.raises(ValueError, match='changed|scope'):
        service.stage_extracted_skills([_draft()], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)
    assert conn.total_changes == before
    assert conn.execute('SELECT COUNT(*) FROM workflow_candidates').fetchone()[0] == 0


def test_shared_contract_keeps_revised_approved_draft_in_one_skill_lineage(evidence_service):
    service, conn, bundle = evidence_service
    from reflect.improvements.contracts import WorkflowContract
    from reflect.improvements.procedures import procedure_contract

    first = service.stage_extracted_skills([_draft()], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)[0]
    service.workflows.approve(first)
    original = service.workflows.show(first)
    first_skill = service.skills.skill_for_candidate(first).id
    before = tuple(conn.execute('SELECT content_json,revision_hash,status FROM workflow_candidates WHERE id=?', (first,)).fetchone())
    changed = _draft()
    changed['content'] += '\nNew bounded decision point.'
    second = service.stage_extracted_skills([changed], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)[0]
    revised = service.workflows.show(second)
    assert second != first
    assert revised.status.value == 'pending'
    assert revised.content['source_markdown'] == changed['content']
    assert revised.contract_signature == original.contract_signature
    assert service.skills.skill_for_candidate(second).id == first_skill
    assert WorkflowContract.from_raw(revised.content['workflow_contract']) == procedure_contract(bundle['procedures'][0])
    assert revised.content['existing_workflow_matches'][0]['id'] == first
    assert tuple(conn.execute('SELECT content_json,revision_hash,status FROM workflow_candidates WHERE id=?', (first,)).fetchone()) == before
    assert service.stage_extracted_skills([changed], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle) == [second]
    assert conn.execute('SELECT COUNT(*) FROM workflow_candidates').fetchone()[0] == 2


def test_staged_contract_evaluates_only_matching_execution(evidence_service):
    from reflect.execution_units import ExecutionUnitService
    from reflect.improvements.contracts import ContractEvaluationService, WorkflowContract

    service, conn, bundle = evidence_service
    cid = service.stage_extracted_skills([_draft()], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)[0]
    candidate = service.workflows.show(cid)
    contract = WorkflowContract.from_raw(candidate.content.get('workflow_contract'))
    assert contract is not None
    service.workflows.approve(cid)
    conn.execute("""INSERT INTO workflow_versions(id,candidate_id,version,content_json,content_hash,status,created_at)
        VALUES ('approved-draft',?,1,?,'hash','approved',?)""", (cid, json.dumps(candidate.content), NOW))
    # Preserve roles but change one artifact so the procedure must not match.
    conn.execute("UPDATE tool_calls SET raw_attrs_json=? WHERE step_id='u4-1'",
        (json.dumps({'gen_ai.client.file_path': '/repo/docs/guide.md'}),))
    evaluation = ContractEvaluationService(conn).evaluate_units(contract, [
        {'execution_unit_id': uid, 'status': 'completed', 'outcome': 'unknown', 'verification_passed': True}
        for uid in ('u0', 'u4')])
    assert evaluation['u0'].evaluation.evidence_coverage == 1
    assert not evaluation['u0'].evaluation.followed  # Unknown acceptance remains unknown before assessment.
    assert evaluation['u4'].evaluation.evidence_coverage == 0
    assert not evaluation['u4'].evaluation.followed
    ExecutionUnitService(conn).assess_contracts(execution_unit_ids={'u0', 'u4'})
    outcomes = dict(conn.execute("SELECT id,outcome FROM execution_units WHERE id IN ('u0','u4')"))
    assert outcomes == {'u0': 'success', 'u4': None}
    evaluated = ContractEvaluationService(conn).evaluate_units(contract, [{
        'execution_unit_id': uid, 'status': 'completed', 'outcome': outcomes[uid], 'verification_passed': True
    } for uid in ('u0', 'u4')])
    assert evaluated['u0'].evaluation.followed
    assert not evaluated['u4'].evaluation.followed



def test_exact_approved_draft_is_returned_without_mutation(evidence_service):
    service, conn, bundle = evidence_service
    cid = service.stage_extracted_skills([_draft()], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)[0]
    service.workflows.approve(cid)
    before = conn.total_changes
    assert service.stage_extracted_skills([_draft()], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle) == [cid]
    assert conn.total_changes == before


def test_changed_installed_draft_preserves_active_artifact_and_version(evidence_service, tmp_path):
    from pathlib import Path

    service, conn, bundle = evidence_service
    first = service.stage_extracted_skills([_draft()], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)[0]
    project = tmp_path / 'project'
    project.mkdir()
    applied = service.workflows.apply(first, project_root=project)
    service.skills.sync_workflow_candidates([first])
    original_bytes = Path(applied['target_path']).read_bytes()
    skill_id = service.skills.skill_for_candidate(first).id
    active_before = tuple(conn.execute("SELECT id,content_hash,content_markdown,workflow_json,status FROM skill_versions WHERE skill_id=? AND status='active'", (skill_id,)).fetchone())
    changed = _draft()
    changed['checkpoints'] = ['A newly proposed bounded stop checkpoint.']
    second = service.stage_extracted_skills([changed], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)[0]
    assert second != first
    assert service.workflows.show(second).status.value == 'pending'
    assert service.skills.skill_for_candidate(second).id == skill_id
    assert Path(applied['target_path']).read_bytes() == original_bytes
    assert tuple(conn.execute("SELECT id,content_hash,content_markdown,workflow_json,status FROM skill_versions WHERE skill_id=? AND status='active'", (skill_id,)).fetchone()) == active_before
    assert conn.execute("SELECT COUNT(*) FROM skill_versions WHERE skill_id=? AND status='pending'", (skill_id,)).fetchone()[0] == 1


@pytest.mark.parametrize('phase', ['canonical', 'cost', 'improvements'])
def test_pending_reassessment_disqualifies_discovery_and_staging(evidence_service, phase):
    from reflect.store.pending_refresh import PendingRefreshState

    service, conn, bundle = evidence_service
    attrs = json.loads(conn.execute("SELECT raw_attrs_json FROM tool_calls WHERE step_id='u0-2'").fetchone()[0])
    attrs['process.exit.code'] = 1
    conn.execute("UPDATE tool_calls SET raw_attrs_json=? WHERE step_id='u0-2'", (json.dumps(attrs),))
    PendingRefreshState(conn).mark_steps('s0', {'u0-2'}, phases=(phase,))
    current = ProcedureEvidenceRepository(conn).bundle()['procedures'][0]
    assert not current['candidate_ready']
    assert current['verification_counts'] == {'passed': 2, 'failed': 1, 'unknown': 2}
    before = conn.total_changes
    with pytest.raises(ValueError, match='changed while drafting'):
        service.stage_extracted_skills([_draft()], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)
    assert conn.total_changes == before


def test_staging_rejects_saved_packet_while_reassessment_is_pending(evidence_service):
    from reflect.store.pending_refresh import PendingRefreshState

    service, conn, bundle = evidence_service
    PendingRefreshState(conn).mark_steps('s0', {'u0-2'}, phases=('improvements',))
    before = conn.total_changes
    with pytest.raises(ValueError, match='changed while drafting'):
        service.stage_extracted_skills([_draft()], session_ids=[f's{i}' for i in range(5)], evidence_bundle=bundle)
    assert conn.total_changes == before


@pytest.mark.parametrize('phase,expected_checked', [('canonical', 2), ('cost', 2), ('improvements', 3)])
def test_internal_discovery_only_accepts_refreshed_ids_with_current_inputs(evidence_service, phase, expected_checked):
    from reflect.store.pending_refresh import PendingRefreshState

    _, conn, _ = evidence_service
    PendingRefreshState(conn).mark_steps('s0', {'u0-2'}, phases=(phase,))
    group = ProcedureEvidenceRepository(conn).bundle(current_execution_ids=frozenset({'u0'}))['procedures'][0]
    assert group['verification_counts']['passed'] == expected_checked
    unrelated = ProcedureEvidenceRepository(conn).bundle(current_execution_ids=frozenset({'u1'}))['procedures'][0]
    assert unrelated['verification_counts']['passed'] == 2
