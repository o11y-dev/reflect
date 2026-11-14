from scripts.evaluate_workflow_discovery import evaluate_cases


def case(disposition='propose'):
    return {'case_id':'review-1','split':'held_out','session_group':'s1','task_family':'review',
            'expected_target':'change','expected_steps':['metadata','diff'],'expected_outcome':'unknown',
            'expected_disposition':disposition}


def candidate(**kwargs):
    return {'case_ids':['review-1'],'group_key':'review:repo','target':'change',
            'observed_steps':['metadata','diff'],'outcome':'unknown',**kwargs}


def test_evaluator_rejects_fabricated_steps_targets_and_outcomes():
    report = evaluate_cases([case()], [candidate(observed_steps=['metadata','diff','deployed'], target='other', outcome='success')])
    assert report['unsupported_steps'] == 1
    assert report['contaminated_targets'] == 1
    assert report['false_outcomes'] == 1
    assert report['safe'] is False


def test_evaluator_counts_abstention_and_duplicate_groups_without_claiming_human_review():
    report = evaluate_cases([case('abstain')], [candidate(),candidate()])
    assert report['unexpected_proposals'] == 1
    assert report['duplicate_suggestions'] == 1
    assert report['human_review'] == 'pending'
    assert report['promotion_approved'] is False


def test_evaluator_requires_independent_splits_and_known_case_references():
    duplicate = {**case(), 'case_id':'other','split':'development'}
    report = evaluate_cases([case(),duplicate], [candidate(case_ids=['missing'])])
    assert report['split_leaks'] == 1
    assert report['unknown_references'] == 1
    assert not report['safe']
