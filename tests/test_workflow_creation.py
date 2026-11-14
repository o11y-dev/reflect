from fastapi.testclient import TestClient
from test_dashboard_sql_api import _seed_sql_report_db

from reflect.dashboard_server import build_dashboard_app


def test_create_preview_enable_user_workflow(tmp_path):
    path = tmp_path / 'reflect.db'
    _seed_sql_report_db(path)
    project = tmp_path / 'project'
    project.mkdir()
    client = TestClient(build_dashboard_app(docs_dir=tmp_path, db_path=path))
    content = {'slug': 'review-changes', 'description': 'Review a bounded change',
               'steps': ['Read the diff', 'Run relevant checks'],
               'verification': ['Record check results'], 'abstain_when': ['Scope is unclear']}
    created = client.post('/api/workflows', json={'content': content})
    assert created.status_code == 201, created.text
    candidate = created.json()
    assert candidate['content']['source']['kind'] == 'user_authored'
    assert candidate['content']['evidence']['status'] == 'unverified_draft'
    assert candidate['support_execution_unit_count'] == 0
    assert not (project / '.agents').exists()
    repeated = client.post('/api/workflows', json={'content': content})
    assert repeated.json()['id'] == candidate['id']
    url = f"/api/workflows/{candidate['id']}"
    invalid_preview = client.get(url + '/preview', params={'project_root': str(tmp_path / 'missing')})
    assert invalid_preview.status_code == 200
    assert not invalid_preview.json()['checks']['apply_allowed']
    # A failed preview leaves the saved candidate available for correction.
    assert client.get(url + '/evidence').json()['candidate']['id'] == candidate['id']
    preview = client.get(url + '/preview', params={'project_root': str(project)})
    assert preview.status_code == 200, preview.text
    assert preview.json()['checks']['apply_allowed']
    applied = client.post(url + '/apply', json={'project_root': str(project)})
    assert applied.status_code == 200, applied.text
    assert applied.json()['status'] == 'active'
    target = project / '.agents/skills/review-changes/SKILL.md'
    assert 'Run relevant checks' in target.read_text()
    rollback = client.post(url + '/rollback')
    assert rollback.status_code == 200, rollback.text
    assert not target.exists()


def test_create_rejects_invalid_instructions_and_evidence_claims(tmp_path):
    path = tmp_path / 'reflect.db'
    _seed_sql_report_db(path)
    client = TestClient(build_dashboard_app(docs_dir=tmp_path, db_path=path))
    for content in ({'slug': '../escape', 'steps': ['Read']},
                    {'slug': 'empty', 'steps': []},
                    {'slug': 'blank', 'steps': ['  ']},
                    {'slug': 'invalid', 'steps': 'Read'},
                    {'slug': 'fabricated', 'steps': ['Read'], 'evidence': {'status': 'verified'}}):
        response = client.post('/api/workflows', json={'content': content})
        assert response.status_code == 422, response.text


def test_create_form_late_response_preserves_new_draft_and_closed_dialog():
    import json
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for form interaction regression')
    source = Path('src/reflect/frontend/js_ledger.js').read_text()
    source = source[source.index('let workflowCreationDraft ='):source.index('async function showWorkflowReview(')]
    script = r'''
const assert=require('node:assert/strict'), vm=require('node:vm');
const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
let finish, form, button, status, previews=[], links=[], renders=0;
const context={isStaticDemo:()=>false, escHtml:String, ledgerDialog:{open:false},
  IMPROVEMENT_DATA:{workflows:[]}, buildImprovementSurfaces:()=>renders++,
  clearLedgerDialogSelection:()=>context.ledgerDialog.inspection=null,
  openLedgerDialog:()=>{context.ledgerDialog.open=true; form={values:{},elements:[{}],handlers:{},
    addEventListener:(name,cb)=>form.handlers[name]=cb,reportValidity:()=>true,querySelector:()=>({focus(){}})};
    button={}; status={append:link=>links.push(link)};},
  document:{getElementById:id=>id==='workflow-create-form'?form:status,createElement:()=>({dataset:{}})},
  ledgerDialogActions:{querySelector:()=>button},
  FormData:class{constructor(f){this.values=f.values;}[Symbol.iterator](){return Object.entries(this.values)[Symbol.iterator]();}},
  ledgerRequest:()=>new Promise(resolve=>finish=resolve),
  showWorkflowReview:async(id,root)=>previews.push({id,root})};
vm.createContext(context); vm.runInContext(source,context);
(async()=>{
  context.showWorkflowCreate();
  form.values={slug:'first',description:'first',steps:'Read',project_root:'/original'};
  const pending=form.handlers.submit({preventDefault(){}});
  assert.equal(form.elements[0].disabled,true);
  context.ledgerDialog.open=false; context.clearLedgerDialogSelection();
  context.showWorkflowCreate();
  form.values={slug:'newer',steps:'Keep this',project_root:'/newer'};
  form.handlers.input();
  finish({id:'saved'}); await pending;
  assert.equal(previews.length,0,'late response must not hijack reopened form');
  assert.equal(vm.runInContext('workflowCreationDraft.slug',context),'newer');
  assert.equal(context.IMPROVEMENT_DATA.workflows[0].id,'saved');
  assert.equal(renders,1);
  // A successful current submission previews exactly its submitted project.
  form.values={slug:'second',description:'second',steps:'Check',project_root:'/submitted'};
  const current=form.handlers.submit({preventDefault(){}});
  form.values.project_root='/mutated';
  finish({id:'second'}); await current;
  assert.equal(previews[0].root,'/submitted');
  // Finding prefill remains editable and failed saves retain its identity/data.
  context.showWorkflowCreate({finding_id:'finding-a',title:'A finding',summary:'Observed gap',
    scope:'repo:one',target_metric:'unverified_change_sessions',project_roots:['/finding-project'],
    content:{slug:'suggestion',description:'Context',steps:['Inspect evidence'],verification:[],abstain_when:[]}});
  assert.equal(vm.runInContext('workflowCreationDraft.steps',context),'Inspect evidence');
  assert.equal(vm.runInContext('workflowCreationDraft.project_root',context),'/finding-project');
  form.values={slug:'edited',description:'Edited',steps:'My corrective procedure',project_root:'/finding-project'};
  let sent;
  context.ledgerRequest=async(url,options)=>{sent=options.body;throw new Error('Refresh running');};
  await form.handlers.submit({preventDefault(){}});
  assert.equal(sent.finding_id,'finding-a');
  assert.equal(sent.content.steps[0],'My corrective procedure');
  assert.equal(sent.content.evidence,undefined);
  assert.match(status.textContent,/Your instructions are kept/);
  assert.equal(form.elements[0].disabled,false);
  assert.equal(vm.runInContext('workflowCreationDraft.slug',context),'edited');
  context.ledgerRequest=async()=>{const error=new Error('Another draft exists');error.candidateId='existing';throw error;};
  await form.handlers.submit({preventDefault(){}});
  assert.equal(links[0].dataset.candidateId,'existing');
  assert.equal(vm.runInContext('workflowCreationDraft.slug',context),'edited');
  context.showWorkflowCreate();
  context.showWorkflowCreate({finding_id:'finding-a',content:{},project_roots:[]});
  assert.equal(vm.runInContext('workflowCreationDraft.slug',context),'edited');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, '-e', script], input=json.dumps(source), text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_finding_workflow_resolves_group_and_keeps_canonical_evidence(tmp_path):
    import json

    from reflect.improvements.models import (
        EvidenceRef,
        ObservationDraft,
        RuleDefinition,
        Severity,
        WorkflowProposal,
    )
    from reflect.improvements.repository import ImprovementRepository
    from reflect.store.sqlite import connect_sqlite

    path = tmp_path / 'reflect.db'
    _seed_sql_report_db(path)
    conn = connect_sqlite(path)
    repository = ImprovementRepository(conn)
    now = '2026-05-01T11:00:00+00:00'
    repository.sync_rule_definitions([RuleDefinition(id='finding-rule', version=1, category='verification',
        title='Finding rule', description='A real stored observation')], now=now)
    ids = []
    for scope in ('first', 'second'):
        ids.append(repository.upsert_observation(ObservationDraft(
            rule_id='finding-rule', rule_version=1, scope_type='user', scope_id=scope,
            fingerprint=scope, title='Missing verification', summary='Observed verification gap',
            category='verification', metric_name='unverified_change_sessions', metric_value=1,
            metric_unit='executions', metric_direction='lower_is_better', baseline_value=None,
            baseline_query={'source_scope': scope}, impact_score=30, severity=Severity.LOW,
            confidence=0.8, evidence=[EvidenceRef(entity_type='session', entity_id='sess-sql',
                                                session_id='sess-sql', summary_redacted=scope)],
        ), now=now))
    conn.commit()
    client = TestClient(build_dashboard_app(docs_dir=tmp_path, db_path=path))
    context = client.get(f'/api/findings/{ids[0]}/workflow-draft')
    assert context.status_code == 200, context.text
    assert set(context.json()['observation_ids']) == set(ids)
    content = context.json()['content']
    content['steps'] = ['Run the project verification command', 'Report actual check results']
    created = client.post('/api/workflows', json={'content': content, 'finding_id': ids[0]})
    assert created.status_code == 201, created.text
    candidate = created.json()
    assert candidate['observation_id'] == ids[0]
    assert candidate['target_metric'] == 'unverified_change_sessions'
    assert candidate['scope'] == 'user:first'
    assert set(candidate['provenance']['finding_observation_ids']) == set(ids)
    assert candidate['content']['steps'] == content['steps']
    repository.ensure_candidate(ids[0], proposal=WorkflowProposal(
        title='Detector replacement', hypothesis='Automatic proposal', risk='low',
        content={'slug':'detector', 'steps':['Replace instructions']}, target_metric='tool_failure_rate',
    ), now=now)
    conn.commit()
    assert repository.get_candidate(candidate['id']).content['steps'] == content['steps']
    assert repository.get_observation(ids[0]).baseline_value is None
    assert repository.get_observation(ids[0]).baseline_query == {'source_scope': 'first'}
    # After grouping changes, either original member still resolves this workflow.
    for finding_id in ids:
        assert client.get(f'/api/findings/{finding_id}/workflow-draft').json()['candidate_id'] == candidate['id']
        repeated = client.post('/api/workflows', json={'content': content, 'finding_id': finding_id})
        assert repeated.json()['id'] == candidate['id']
    conflicting = client.post('/api/workflows', json={'finding_id': ids[0], 'content': {**content, 'steps': ['Different instructions']}})
    assert conflicting.status_code == 409
    assert conflicting.json()['candidate_id'] == candidate['id']
    assert repository.get_candidate(candidate['id']).content['steps'] == content['steps']
    assert conn.execute('SELECT COUNT(*) FROM workflow_candidates').fetchone()[0] == 1
    ledger = client.get(f"/api/workflows/{candidate['id']}/evidence").json()
    assert set(ledger['observation_ids']) == set(ids)
    assert {item for row in ledger['provenance_sessions'] for item in row['evidence_summaries']} == {'first', 'second'}
    assert client.post('/api/workflows', json={'content': content, 'finding_id': 'missing'}).status_code == 404
    assert client.post('/api/workflows', json={'content': content, 'finding_id': []}).status_code == 422
    assert client.get('/api/findings/missing/workflow-draft').status_code == 404
    assert json.loads(conn.execute('SELECT baseline_query_json FROM observations WHERE id=?', (ids[0],)).fetchone()[0]) == {'source_scope': 'first'}
    conn.close()
