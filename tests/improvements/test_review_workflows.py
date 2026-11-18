import json

import pytest

from reflect.improvements.review_workflows import RepeatedChangeReviewRule
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite

NOW = '2026-11-01T10:00:00Z'


@pytest.fixture
def conn(tmp_path):
    c = connect_sqlite(tmp_path / 'reviews.db')
    migrate(c)
    c.execute("INSERT INTO repos(id,full_name,created_at,updated_at) VALUES('repo','team/repo',?,?)", (NOW,NOW))
    yield c
    c.close()


def review(c, sid, *, number=1, diff_number=None, status='ok', capture_prompt=True):
    c.execute('INSERT INTO sessions(id,repo_id,started_at,created_at,updated_at) VALUES(?,?,?,?,?)', (sid,'repo',NOW,NOW,NOW))
    c.execute("INSERT INTO execution_units(id,session_id,repo_id,source,eligible,started_at,created_at,updated_at) VALUES(?,?,?,'native_turn',1,?,?,?)", (sid,sid,'repo',NOW,NOW,NOW))
    pstep = sid+'-prompt'
    raw = json.dumps({'gen_ai.client.prompt.text':'Please review this merge request.'}) if capture_prompt else '{}'
    c.execute("INSERT INTO steps(id,session_id,seq,type,raw_attrs_json,started_at,created_at,updated_at) VALUES(?,?, -1,'prompt',?,?,?,?)", (pstep,sid,raw,NOW,NOW,NOW))
    c.execute('INSERT INTO execution_unit_steps(execution_unit_id,step_id,session_id,created_at) VALUES(?,?,?,?)', (sid,pstep,sid,NOW))
    for index, operation in enumerate(['get_merge_request_details','get_merge_request_changes']):
        step = sid + str(index)
        args = json.dumps({'project_id':1,'mr_iid':diff_number if index and diff_number is not None else number})
        c.execute("INSERT INTO steps(id,session_id,seq,type,started_at,created_at,updated_at) VALUES(?,?,?,'tool_call',?,?,?)", (step,sid,index,NOW,NOW,NOW))
        c.execute('INSERT INTO execution_unit_steps(execution_unit_id,step_id,session_id,created_at) VALUES(?,?,?,?)', (sid,step,sid,NOW))
        c.execute('INSERT INTO tool_calls(id,session_id,step_id,tool_name,status,input_preview_redacted,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)', (step,sid,step,'mcp__gitlab__'+operation,status,args,NOW,NOW))


def test_repeated_identified_reviews_create_concrete_draft_without_success_claim(conn):
    review(conn, 's1')
    review(conn, 's2', number=2)
    rule = RepeatedChangeReviewRule()
    assert rule.evaluate(conn) == []
    review(conn, 's3', number=3)
    finding, = rule.evaluate(conn)
    assert finding.affected_session_count == 3
    assert {e.session_id for e in finding.evidence} == {'s1','s2','s3'}
    proposal = rule.propose(finding)
    assert 'head-specific' in proposal.title
    assert proposal.target_value is None
    assert 'unmeasured' in proposal.hypothesis
    assert len(proposal.content['observed_steps']) == 2
    assert 'verify current-head checks' in proposal.content['proposed_steps']


def test_mixed_changes_and_failed_reads_do_not_support_review_procedure(conn):
    review(conn, 's1')
    review(conn, 's2', diff_number=99)
    review(conn, 's3', status='error')
    assert RepeatedChangeReviewRule().evaluate(conn) == []


def test_adherence_without_contract_never_falls_back_to_tool_failures(conn):
    from reflect.improvements.measurement import MeasurementService
    review(conn, 's1', status='error')
    assert MeasurementService(conn)._execution_unit_metric_values('workflow_adherence', ['s1']) == {}


def test_review_definition_has_source_bound_steps_and_proposed_decisions(conn):
    for n in range(3):
        review(conn, f's{n}', number=n+1)
    rule = RepeatedChangeReviewRule()
    proposal = rule.propose(rule.evaluate(conn)[0])
    definition = proposal.content['procedure_definition']
    assert definition['task_intent']['family'] == 'review'
    observed = [s for s in definition['steps'] if s['source_kind']=='observed']
    assert len(observed) == 2
    assert all(s['source_refs'] for s in observed)
    assert all(s['decision'] for s in definition['steps'])
    assert 'revision' in definition['expected_output']
    assert proposal.content['workflow_contract']['milestones'][0]['role'] == 'review_metadata'


def test_missing_intent_and_child_sessions_do_not_create_independent_support(conn):
    for n in range(3):
        review(conn, f's{n}', capture_prompt=False)
    assert RepeatedChangeReviewRule().evaluate(conn) == []
    conn.execute("UPDATE steps SET raw_attrs_json=? WHERE type='prompt'", (json.dumps({'gen_ai.client.prompt.text':'Review this merge request.'}),))
    conn.execute("UPDATE sessions SET parent_session_id='s0' WHERE id!='s0'")
    assert RepeatedChangeReviewRule().evaluate(conn) == []


def test_frozen_catalogue_runs_real_detector_with_conservative_abstention(conn, tmp_path):
    from pathlib import Path

    from scripts.evaluate_workflow_discovery import evaluate_cases

    cases = json.loads(Path('tests/improvements/fixtures/workflow_discovery_cases.json').read_text())
    exports = []
    for case in cases:
        conn.execute('SAVEPOINT evaluation_case')
        variant = case['case_id'].split('-',1)[1]
        for n in range(3):
            sid = f's{n}'
            review(conn, sid, number=1, diff_number=2 if variant == 'mixed_targets' else None,
                   status='failed' if variant in {'failed_diff','retries_only'} else 'ok',
                   capture_prompt=variant != 'missing_prompt')
            if case['case_id'].startswith('other-') or variant in {'quoted_example','unrelated_prompt'}:
                prompt = 'Summarize the text: "review this merge request".' if variant == 'quoted_example' else 'Implement the requested change.'
                conn.execute('UPDATE steps SET raw_attrs_json=? WHERE id=?',
                    (json.dumps({'gen_ai.client.prompt.text':prompt}),sid+'-prompt'))
            if variant == 'changed_revision':
                for seq in (0,1):
                    conn.execute('UPDATE tool_calls SET input_preview_redacted=? WHERE id=?',
                        (json.dumps({'project_id':1,'mr_iid':1,'head_sha':str(seq)}),sid+str(seq)))
            if variant == 'reordered_reads':
                conn.execute('UPDATE steps SET seq=101-seq WHERE session_id=? AND type=\'tool_call\'',(sid,))
            if variant == 'truncated_calls':
                for seq in range(2,82):
                    step = sid+str(seq)
                    conn.execute("INSERT INTO steps(id,session_id,seq,type,started_at,created_at,updated_at) VALUES (?,?,?,'tool_call',?,?,?)",(step,sid,seq,NOW,NOW,NOW))
                    conn.execute('INSERT INTO execution_unit_steps VALUES (?,?,?,?)',(sid,step,sid,NOW))
                    conn.execute("INSERT INTO tool_calls(id,session_id,step_id,tool_name,status,input_preview_redacted,created_at,updated_at) VALUES (?,?,?,'get_merge_request_details','ok',?,?,?)",(step,sid,step,json.dumps({'project_id':1,'mr_iid':1}),NOW,NOW))
        if variant == 'duplicate_child':
            conn.execute("UPDATE sessions SET parent_session_id='s0' WHERE id!='s0'")
        if variant == 'missing_repository':
            conn.execute('UPDATE execution_units SET repo_id=NULL')
        rule = RepeatedChangeReviewRule()
        for finding in rule.evaluate(conn):
            proposal = rule.propose(finding)
            definition = proposal.content['procedure_definition']
            observed = [step for step in definition['steps'] if step['source_kind']=='observed']
            assert all(step['source_refs'] for step in observed)
            assert all(ref['tool_call_id'] for step in observed for ref in step['source_refs'])
            assert all(target == 'gitlab:1:1' for target in definition['task_intent']['target_refs'])
            exports.append({'case_ids':[case['case_id']], 'group_key':case['case_id'], 'target':'change',
                            'observed_steps':[role.removeprefix('review_') for role in
                                [m['role'] for m in proposal.content['workflow_contract']['milestones']]],
                            'outcome':'unknown'})
        conn.execute('ROLLBACK TO evaluation_case')
        conn.execute('RELEASE evaluation_case')
    result = evaluate_cases(cases,exports)
    assert result['safe'], result
    assert result['covered_eligible_cases'] >= 8, result
    assert result['promotion_approved'] is False
    held_out = [case for case in cases if case['split']=='held_out']
    ids = {case['case_id'] for case in held_out}
    held_result = evaluate_cases(held_out,[item for item in exports if item['case_ids'][0] in ids])
    assert held_result['safe'], held_result
    (tmp_path/'evaluation.json').write_text(json.dumps({'all':result,'held_out':held_result},indent=2))


def test_review_contract_rejects_mixed_targets_and_keeps_unknown_outcomes(conn):
    from reflect.improvements.contracts import ContractEvaluationService, WorkflowContract
    for n in range(3):
        review(conn,f's{n}')
    rule = RepeatedChangeReviewRule()
    contract = WorkflowContract.from_raw(rule.propose(rule.evaluate(conn)[0]).content['workflow_contract'])
    service = ContractEvaluationService(conn)
    unit = {'execution_unit_id':'s0','status':'completed','outcome':'unknown','verification_passed':True}
    assert not service.evaluate_units(contract,[unit])['s0'].evaluation.followed
    unit['outcome']='success'
    assert service.evaluate_units(contract,[unit])['s0'].evaluation.followed
    conn.execute('UPDATE tool_calls SET input_preview_redacted=? WHERE id=?',
                 (json.dumps({'project_id':1,'mr_iid':99}),'s01'))
    result = service.evaluate_units(contract,[unit])['s0'].evaluation
    assert not result.followed
    assert result.evidence_coverage == 0


def test_suggestion_assessment_requires_task_procedure_and_independent_support(conn):
    from types import SimpleNamespace

    from reflect.improvements.workflows import WorkflowService
    for n in range(3):
        review(conn,f's{n}')
    rule=RepeatedChangeReviewRule()
    proposal=rule.propose(rule.evaluate(conn)[0])
    candidate=SimpleNamespace(content=proposal.content,contract_signature='review-repo')
    assessment=WorkflowService.suggestion_assessment(candidate)
    assert assessment['eligible']
    assert assessment['label']=='Observed procedure — benefit unmeasured'
    candidate.content={**proposal.content,'independent_session_count':2}
    assert not WorkflowService.suggestion_assessment(candidate)['eligible']
    candidate.content={'steps':['Read files','Check results'],'behavior_type':'recovery'}
    assert not WorkflowService.suggestion_assessment(candidate)['eligible']
