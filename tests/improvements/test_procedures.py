import json

import pytest

from reflect.improvements.contracts import ContractEvaluationService
from reflect.improvements.procedures import ProcedureEvidenceRepository
from reflect.improvements.rules import HighPerformingWorkflowRule
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite

NOW = '2026-11-08T12:00:00+00:00'


@pytest.fixture
def conn(tmp_path):
    db = connect_sqlite(tmp_path / 'procedures.db')
    migrate(db)
    db.execute("INSERT INTO agents(id,name,created_at,updated_at) VALUES ('agent','codex',?,?)", (NOW, NOW))
    db.execute("INSERT INTO repos(id,full_name,created_at,updated_at) VALUES ('repo','example/project',?,?)", (NOW, NOW))
    db.execute("INSERT INTO task_archetypes(id,name,description,created_at,updated_at) VALUES ('implementation','implementation','Build a change',?,?)", (NOW, NOW))
    yield db
    db.close()


def seed_execution(conn, index, *, area='payments', verification='passed', session=None, harness='codex', mixed=False):
    sid, uid = session or f'session-{index}', f'execution-{index}'
    conn.execute("INSERT OR IGNORE INTO sessions(id,agent_id,repo_id,started_at,status,created_at,updated_at) VALUES (?,'agent','repo',?,'completed',?,?)", (sid, NOW, NOW, NOW))
    assessment = {'version': 1, 'boundary': 'closed', 'verification': verification, 'outcome': 'unknown',
                  'checks': [{'step_id': f'{uid}-2', 'exit_code': 1 if verification == 'failed' else 0,
                              'scope': ['.'], 'invalidated': verification == 'invalidated',
                              'invalidated_by': [f'{uid}-late'] if verification == 'invalidated' else []}]}
    conn.execute("""INSERT INTO execution_units(id,session_id,source,source_confidence,repo_id,agent_id,
        started_at,status,eligible,boundary_json,created_at,updated_at)
        VALUES (?,?,'native_turn',1,'repo','agent',?,'completed',1,?,?,?)""",
        (uid, sid, NOW, json.dumps({'assessment': assessment}), NOW, NOW))
    conn.execute("""INSERT INTO execution_unit_archetypes VALUES (?,'implementation',0.9,?,'{}',?,?)""", (uid, int(mixed), NOW, NOW))
    names = {'codex': ('read_file', 'apply_patch', 'exec_command'), 'claude': ('Read', 'Edit', 'Bash'),
             'cursor': ('read_file', 'edit_file', 'run_terminal_cmd')}[harness]
    for seq, name in enumerate(names):
        step = f'{uid}-{seq}'
        payload = {'file_path': f'src/{area}/worker.py'} if seq < 2 else {'command': f'pytest tests/{area} -q'}
        attrs = {'gen_ai.client.file_path': payload.get('file_path'),
                 'gen_ai.client.command': payload.get('command'), 'process.exit.code': 0}
        conn.execute("INSERT INTO steps(id,session_id,seq,type,status,started_at,raw_attrs_json,created_at,updated_at) VALUES (?,?,?,'tool_call','ok',?,?,?,?)", (step, sid, index * 100 + seq, NOW, json.dumps(attrs), NOW, NOW))
        conn.execute("INSERT INTO execution_unit_steps VALUES (?,?,?,?)", (uid, step, sid, NOW))
        conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,status,input_preview_redacted,
             raw_attrs_json,created_at,updated_at) VALUES (?,?,?,?,'ok',?,?,?,?)""",
            (f'tool-{step}', step, sid, name, json.dumps(payload), json.dumps(attrs), NOW, NOW))
    prompt_id = f'{uid}-prompt'
    conn.execute("INSERT INTO steps(id,session_id,seq,type,status,started_at,raw_attrs_json,created_at,updated_at) VALUES (?,?,?,'prompt','ok',?,?,?,?)",
                 (prompt_id,sid,index*100-1,NOW,json.dumps({'gen_ai.client.prompt.text':'Please implement the requested change.'}),NOW,NOW))
    conn.execute("INSERT INTO execution_unit_steps VALUES (?,?,?,?)",(uid,prompt_id,sid,NOW))
    return uid


def test_procedure_signature_matches_harnesses_but_separates_artifacts(conn):
    for index, harness in enumerate(('codex', 'claude', 'cursor')):
        seed_execution(conn, index, harness=harness)
    seed_execution(conn, 4, area='auth')
    bundle = ProcedureEvidenceRepository(conn).bundle()
    groups = sorted(bundle['procedures'], key=lambda p: p['execution_count'], reverse=True)
    assert [group['execution_count'] for group in groups] == [3, 1]
    assert groups[0]['session_count'] == 3
    assert groups[0]['signature'] != groups[1]['signature']
    assert groups[0]['candidate_ready']
    assert groups[0]['outcome_counts'] == {'unknown': 3}
    assert groups[0]['verification_counts'] == {'passed': 3}
    assert any('payments' in item['target'] for item in groups[0]['operations'])


def test_failed_unknown_and_invalidated_checks_remain_in_procedure_evidence(conn):
    for index in range(3):
        seed_execution(conn, index)
    for index, state in enumerate(('failed', 'unknown', 'invalidated'), 3):
        seed_execution(conn, index, verification=state)
    group = ProcedureEvidenceRepository(conn).bundle()['procedures'][0]
    assert group['verification_counts'] == {'passed': 3, 'failed': 1, 'unknown': 1, 'invalidated': 1}
    assert group['outcome_counts'] == {'unknown': 6}
    assert len(group['counterexamples']) == 3
    assert any(example['checks'][0]['invalidated_by'] for example in group['counterexamples'])
    assert not group['candidate_ready']  # 3/6 checked; unknown is in the denominator.


def test_independent_sessions_and_ambiguous_boundaries_gate_proposals(conn):
    for index in range(3):
        seed_execution(conn, index, session='one-session')
    assert not ProcedureEvidenceRepository(conn).bundle()['procedures'][0]['candidate_ready']
    seed_execution(conn, 4, mixed=True)
    seed_execution(conn, 5)
    conn.execute("UPDATE execution_units SET eligible=0 WHERE id='execution-5'")
    bundle = ProcedureEvidenceRepository(conn).bundle()
    assert bundle['selection']['excluded_execution_count'] == 2
    assert len(bundle['procedures']) == 1
    assert HighPerformingWorkflowRule().detect(conn) == []


def test_procedure_selection_is_bounded_scoped_deterministic_and_read_only(conn):
    for index in range(6):
        seed_execution(conn, index)
    repo = ProcedureEvidenceRepository(conn)
    before = conn.total_changes
    first = repo.bundle(session_ids={'session-0', 'session-1', 'session-2'}, limit=2)
    assert first == repo.bundle(session_ids={'session-0', 'session-1', 'session-2'}, limit=2)
    assert first['selection']['truncated']
    assert first['selection']['selected_execution_count'] == 2
    assert set(first['procedures'][0]['session_ids']) <= {'session-0', 'session-1', 'session-2'}
    assert repo.bundle(session_ids=set())['procedures'] == []
    assert conn.total_changes == before


def test_workflow_candidate_uses_shared_evidence_without_claiming_task_success(conn):
    for index in range(3):
        seed_execution(conn, index)
    seed_execution(conn, 3, verification='invalidated')
    findings = HighPerformingWorkflowRule().detect(conn)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.baseline_query['procedure']['outcome_counts'] == {'unknown': 4}
    assert len(finding.evidence) == 4
    assert any(item.polarity == 'counterexample' for item in finding.evidence)
    proposal = HighPerformingWorkflowRule().propose(finding)
    assert proposal.content['procedure']['signature'] == finding.baseline_query['procedure']['signature']
    assert any('payments' in step for step in proposal.content['steps'])
    assert all('record' not in step.lower() for step in proposal.content['steps'])
    assert 'proven success' not in finding.summary.lower()


def test_contract_evaluation_rejects_same_roles_on_different_artifacts(conn):
    for index in range(3):
        seed_execution(conn, index)
    seed_execution(conn, 3, area='auth')
    rule = HighPerformingWorkflowRule()
    content = rule.propose(rule.detect(conn)[0]).content
    from reflect.improvements.contracts import WorkflowContract
    contract = WorkflowContract.from_raw(content['workflow_contract'])
    evaluation = ContractEvaluationService(conn).evaluate_units(contract, [
        {'execution_unit_id': f'execution-{i}', 'status': 'completed', 'outcome': 'success',
         'verification_passed': True} for i in (0, 3)])
    assert evaluation['execution-0'].evaluation.followed
    assert not evaluation['execution-3'].evaluation.followed
    assert evaluation['execution-3'].evaluation.evidence_coverage == 0


def test_approved_acceptance_uses_same_procedure_scope(conn):
    from reflect.execution_units import ExecutionUnitService
    from reflect.improvements.repository import ImprovementRepository

    for index in range(3):
        seed_execution(conn, index)
        conn.execute("""INSERT INTO conversation_facts(id,step_id,session_id,kind,role,
            content_hash,content_preview_redacted,content_length,created_at,updated_at)
            VALUES (?,?,?,'prompt','user','approval','Approved, go ahead',18,?,?)""",
            (f'claim-{index}', f'execution-{index}-0', f'session-{index}', NOW, NOW))
    seed_execution(conn, 3, area='auth')
    rule = HighPerformingWorkflowRule()
    finding = rule.detect(conn)[0]
    repository = ImprovementRepository(conn)
    repository.sync_rule_definitions([rule.definition], now=NOW)
    observation = repository.upsert_observation(finding, now=NOW)
    proposal = rule.propose(finding)
    candidate = repository.ensure_candidate(observation, proposal=proposal, now=NOW)
    conn.execute("""INSERT INTO workflow_versions(id,candidate_id,version,content_json,content_hash,status,created_at)
        VALUES ('approved',?,1,?,'test-hash','approved',?)""", (candidate, json.dumps(proposal.content), NOW))
    ExecutionUnitService(conn).assess_contracts()
    outcomes = dict(conn.execute('SELECT id,outcome FROM execution_units'))
    assert outcomes['execution-0'] == 'success'
    assert outcomes['execution-3'] is None


def test_truncated_execution_cannot_produce_a_complete_procedure(conn):
    for index in range(3):
        seed_execution(conn, index)
        for extra in range(80):
            conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,status,
                input_preview_redacted,created_at,updated_at) VALUES (?,?,?,'Read','ok','{}',?,?)""",
                (f'extra-{index}-{extra}', f'execution-{index}-0', f'session-{index}', NOW, NOW))
    group = ProcedureEvidenceRepository(conn).bundle()['procedures'][0]
    assert group['signals_truncated']
    assert not group['candidate_ready']
    assert HighPerformingWorkflowRule().detect(conn) == []


def test_exact_check_file_selectors_remain_distinct(conn):
    for index, target in enumerate(('tests/test_payments.py', 'tests/test_auth.py')):
        seed_execution(conn, index)
        command = f'pytest {target}'
        conn.execute("UPDATE tool_calls SET input_preview_redacted=?,raw_attrs_json=? WHERE id=?",
                     (json.dumps({'command': command}), json.dumps({'gen_ai.client.command': command}),
                      f'tool-execution-{index}-2'))
    assert len(ProcedureEvidenceRepository(conn).bundle()['procedures']) == 2


def test_quoted_selectors_and_dependency_scopes_remain_distinct(conn):
    for index, command in enumerate(("pytest -k 'foo bar'", 'pytest -k foo bar')):
        seed_execution(conn, index)
        conn.execute("UPDATE tool_calls SET input_preview_redacted=?,raw_attrs_json=? WHERE id=?",
                     (json.dumps({'command': command}), json.dumps({'gen_ai.client.command': command}),
                      f'tool-execution-{index}-2'))
    assert len(ProcedureEvidenceRepository(conn).bundle()['procedures']) == 2
    conn.execute("UPDATE tool_calls SET input_preview_redacted='{}',raw_attrs_json=? WHERE tool_name='exec_command'",
                 (json.dumps({'gen_ai.client.command': 'pytest'}),))
    for index in range(2):
        raw = json.loads(conn.execute('SELECT boundary_json FROM execution_units WHERE id=?',
                                     (f'execution-{index}',)).fetchone()[0])
        raw['assessment']['checks'][0]['scope'] = [f'src/area/file{index}.py']
        conn.execute('UPDATE execution_units SET boundary_json=? WHERE id=?', (json.dumps(raw), f'execution-{index}'))
    assert len(ProcedureEvidenceRepository(conn).bundle()['procedures']) == 2


def test_generated_roles_accept_their_own_post_edit_inspection_sequence(conn):
    from reflect.improvements.contracts import WorkflowContract
    for index in range(3):
        uid = seed_execution(conn, index)
        step = f'{uid}-readback'
        conn.execute('UPDATE steps SET seq=seq+1 WHERE id=?', (f'{uid}-2',))
        conn.execute("INSERT INTO steps(id,session_id,seq,type,status,started_at,created_at,updated_at) VALUES (?,?,?,'tool_call','ok',?,?,?)",
                     (step, f'session-{index}', index * 100 + 2, NOW, NOW, NOW))
        conn.execute('INSERT INTO execution_unit_steps VALUES (?,?,?,?)', (uid, step, f'session-{index}', NOW))
        conn.execute("INSERT INTO tool_calls(id,step_id,session_id,tool_name,status,input_preview_redacted,created_at,updated_at) VALUES (?,?,?,'Read','ok',?,?,?)",
                     (f'tool-{step}', step, f'session-{index}', json.dumps({'file_path':'src/payments/worker.py'}), NOW, NOW))
    rule = HighPerformingWorkflowRule()
    contract = WorkflowContract.from_raw(rule.propose(rule.detect(conn)[0]).content['workflow_contract'])
    result = ContractEvaluationService(conn).evaluate_units(contract, [{
        'execution_unit_id':'execution-0','status':'completed','outcome':'success','verification_passed':True}])
    assert result['execution-0'].evaluation.followed


def test_definition_has_task_provenance_and_children_do_not_add_support(conn):
    for index in range(3):
        seed_execution(conn,index)
    group = ProcedureEvidenceRepository(conn).bundle()['procedures'][0]
    assert group['procedure_definition']['task_intent']['source_refs']
    assert all(step['source_refs'] for step in group['procedure_definition']['steps'])
    conn.execute("UPDATE sessions SET parent_session_id='session-0' WHERE id!='session-0'")
    assert not ProcedureEvidenceRepository(conn).bundle()['procedures'][0]['candidate_ready']
    conn.execute("UPDATE steps SET raw_attrs_json='{}' WHERE type='prompt'")
    assert not ProcedureEvidenceRepository(conn).bundle()['procedures'][0]['candidate_ready']


def test_different_explicit_tasks_cannot_supply_shared_recurrence(conn):
    for index,prompt in enumerate(('Implement the requested change.','Review this change.','Deploy this change.')):
        seed_execution(conn,index)
        conn.execute('UPDATE steps SET raw_attrs_json=? WHERE id=?',
                     (json.dumps({'gen_ai.client.prompt.text':prompt}),f'execution-{index}-prompt'))
    assert not any(group['candidate_ready'] for group in ProcedureEvidenceRepository(conn).bundle()['procedures'])
