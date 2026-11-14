"""Suggestion disposition is durable review metadata, independent of analysis freshness."""
from contextlib import closing

from fastapi.testclient import TestClient
from test_dashboard_sql_api import _seed_improvement_ledger, _seed_sql_report_db

from reflect.dashboard_server import DashboardResponseCache, build_dashboard_app
from reflect.preparation import PreparationLock
from reflect.store.sqlite import connect_sqlite, mark_snapshot


def test_dismiss_restore_works_during_refresh_and_invalidates_published_inventory(tmp_path):
    db = tmp_path / 'reflect.db'
    _seed_sql_report_db(db)
    _seed_improvement_ledger(db)
    app = build_dashboard_app(docs_dir=tmp_path, db_path=db)
    with TestClient(app) as client:
        assert client.get('/api/data').status_code == 200
        inventory = client.get('/api/workflows').json()['workflows']
        candidate = next(item for item in inventory if item['status'] == 'pending')
        with closing(connect_sqlite(db)) as conn:
            mark_snapshot(conn, ready=False)
        with PreparationLock(db):
            response = client.post(f"/api/workflows/{candidate['id']}/dismiss")
            assert response.status_code == 200, response.text
            assert client.get('/api/data').status_code == 200
            updated = client.get('/api/workflows').json()['workflows']
            assert next(item for item in updated if item['id'] == candidate['id'])['status'] == 'rejected'
            restored = client.post(f"/api/workflows/{candidate['id']}/restore")
            assert restored.status_code == 200, restored.text
            updated = client.get('/api/workflows').json()['workflows']
            assert next(item for item in updated if item['id'] == candidate['id'])['status'] == 'pending'


def test_disposition_invalidation_prevents_late_reads_and_disk_cache_resurrection(tmp_path):
    path = tmp_path / 'cache.json'
    cache = DashboardResponseCache(path, cache_key='test', default_range='7d')
    key = cache.key('/api/workflows', {})
    order = cache.start_read()
    cache.put(key, {'analysis_state': 'ready', 'workflows': ['old']}, read_order=order)
    usage_key = cache.key('/api/data', {})
    cache.put(usage_key, {'analysis_state': 'ready', 'usage': 'retained'}, read_order=order)
    cache.invalidate()
    assert cache.stale(usage_key, reason='preparing')['usage'] == 'retained'
    cache.put(key, {'analysis_state': 'ready', 'workflows': ['old']}, read_order=order)
    assert cache.stale(key, reason='preparing') is None
    assert DashboardResponseCache(path, cache_key='test', default_range='7d').stale(key, reason='preparing') is None


def test_browser_disposition_waits_for_old_reads_and_reports_failure():
    import json
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for browser contracts')
    source = Path('src/reflect/frontend/js_ledger.js').read_text()
    source = source[source.index('async function changeSuggestionDisposition('):source.index("document.addEventListener('click', async event => {\n  const trigger = event.target.closest('[data-ledger-action]');")]
    script = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const status={},trigger={disabled:false,textContent:'Dismiss'},params=new URLSearchParams('workflow_row=one');
let finishOld,refreshes=0;
const context={document:{getElementById:()=>status},ledgerDialog:{open:false},closeLedgerDialog:()=>{},
  ledgerRequest:async()=>({affected_ids:['one']}),updateUrlParams:fn=>fn(params),
  improvementRequest:new Promise(resolve=>finishOld=resolve),
  refreshImprovementSurfaces:async()=>{refreshes++},window:{alert:()=>assert.fail('unexpected alert')}};
vm.createContext(context);vm.runInContext(JSON.parse(require('node:fs').readFileSync(0,'utf8')),context);
(async()=>{
 const request=context.changeSuggestionDisposition('one',false,trigger);
 assert.equal(trigger.disabled,true);
 await Promise.resolve();assert.equal(refreshes,0);
 finishOld();await request;
 assert.equal(refreshes,1);assert.equal(trigger.disabled,false);assert.equal(params.has('workflow_row'),false);
 assert.match(status.innerHTML,/View History/);
 context.ledgerRequest=async()=>{throw new Error('Database busy. Retry shortly.')};
 await context.changeSuggestionDisposition('one',false,trigger);
 assert.match(status.textContent,/Could not dismiss.*Database busy/);assert.equal(trigger.disabled,false);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', script], input=json.dumps(source), text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_busy_disposition_returns_retryable_error_without_mutating(tmp_path):
    db = tmp_path / 'reflect.db'
    _seed_sql_report_db(db)
    _seed_improvement_ledger(db)
    with TestClient(build_dashboard_app(docs_dir=tmp_path, db_path=db)) as client:
        candidate = client.get('/api/workflows').json()['workflows'][0]['id']
        with closing(connect_sqlite(db)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            response = client.post(f'/api/workflows/{candidate}/dismiss')
            assert response.status_code == 503
            assert response.headers['Retry-After'] == '2'
            assert 'retry' in response.json()['error'].lower()
            writer.rollback()
        assert client.post(f'/api/workflows/{candidate}/dismiss').status_code == 200


def test_enable_action_requires_previewed_project_and_content_and_confirms_success():
    import json
    import shutil
    import subprocess
    from pathlib import Path

    import pytest
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for browser contracts')
    source=Path('src/reflect/frontend/js_ledger.js').read_text()
    source=source[source.index("document.addEventListener('click', async event => {\n  const trigger = event.target.closest('[data-ledger-action]');"):source.index('function sqlTab(')]
    script=r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
let handler,requests=0,refreshes=0;
const content={slug:'review',steps:['inspect']};
const fields={'workflow-project-root':{value:'/other'},'workflow-name':{value:'review'},
'workflow-content-editor':{value:JSON.stringify(content)},'workflow-action-status':{textContent:''}};
const check={classList:{add:()=>{}},textContent:''};
const trigger={dataset:{ledgerAction:'apply',candidateId:'one',idleLabel:'Enable workflow'},textContent:'Enable workflow'};
const context={document:{addEventListener:(_,fn)=>handler=fn,getElementById:id=>fields[id],querySelector:()=>check},
ledgerDialog:{dataset:{projectRoot:'/project',workflowSlug:'review',previewContent:JSON.stringify(content),targetPath:'/project/SKILL.md'}},
isStaticDemo:()=>false,window:{alert:()=>assert.fail('unexpected alert')},
ledgerRequest:async()=>{requests++;return {status:'active',target_path:'/project/SKILL.md'}},
closeLedgerDialog:()=>{},refreshImprovementSurfaces:async()=>{refreshes++}};
vm.createContext(context);vm.runInContext(JSON.parse(require('node:fs').readFileSync(0,'utf8')),context);
const event={target:{closest:()=>trigger},preventDefault:()=>{}};
(async()=>{
await handler(event);assert.equal(requests,0);assert.match(check.textContent,/Preview project/);
fields['workflow-project-root'].value='/project';fields['workflow-content-editor'].value='{}';
await handler(event);assert.equal(requests,0);assert.match(check.textContent,/Save draft and preview/);
fields['workflow-content-editor'].value=JSON.stringify(content);
await handler(event);assert.equal(requests,1);assert.equal(refreshes,1);
assert.match(fields['workflow-action-status'].textContent,/Enabled · \/project\/SKILL.md/);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result=subprocess.run([node,'-e',script],input=json.dumps(source),text=True,capture_output=True,timeout=10)
    assert result.returncode == 0, result.stderr
