"""Historical read dialogs retain provenance without enabling a fresh mutation."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _run_modal(scenario: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for modal evidence contracts")
    source = Path("src/reflect/frontend/js_ledger.js").read_text()
    fragments = source[source.index("function renderWorkflowSessionLedger("):
                       source.index("async function showSkillReview(")]
    fragments += source[source.index("async function showWorkflowReview("):
                        source.index("async function submitSessionFeedback(")]
    fragments += source[source.index("async function showImpactSessions("):
                        source.index("document.addEventListener('click'", source.index("async function showImpactSessions("))]
    impact = Path("src/reflect/frontend/js_impact.js").read_text()
    fragments += impact[impact.index("function formatImpactSessionValue("):
                        impact.index("function impactMeasurementState(")]
    fragments += impact[impact.index("function impactCohortDetails("):
                        impact.index("function impactScope(")]
    script = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const dialogs=[],requests=[],params=new URLSearchParams();
const context={URL,window:{location:{origin:'http://localhost'}},
  openLedgerDialog:dialog=>dialogs.push(dialog),escHtml:String,fmt:String,
  humanizeLedgerLabel:String,humanizeLedgerText:String,updateUrlParams:fn=>fn(params),
  fmtWorkflowDate:String,compactLedgerId:String,sessionInspectionUrl:item=>`?session=${item.session_id}`,
  impactMetricPresentation:()=>({goal:'Verification'}),formatImpactSessionValue:String,
  workflowSourcePresentation:()=>({label:'Imported',detail:'Local procedure'}),
  renderWorkflowSteps:()=>'<div>Procedure</div>',ledgerWorkspaceName:String,ledgerDialog:{dataset:{}},
};
vm.createContext(context);vm.runInContext(source,context);
const publication={state:'stale',snapshot_completed_at:'2020-01-01T00:00:00Z',loaded_at:'2026-11-08T09:00:00Z',reason:'preparing'};
""" + scenario + r"""
"""
    result = subprocess.run([node, "-e", script], input=json.dumps(fragments),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def test_historical_finding_loop_and_impact_dialogs_show_original_freshness():
    _run_modal(r"""
(async()=>{
context.ledgerRequest=async url=>url.endsWith('/evidence') ? {publication} : {title:'Observed finding',publication};
await context.showObservationEvidence('finding');
assert.match(dialogs.at(-1).body,/Historical evidence.*2020/);
assert.ok(!dialogs.at(-1).body.includes('2026'),'original publication outranks recent cache loading');
context.ledgerRequest=async()=>({publication,loop:{title:'Retained loop',status:'detected'},occurrences:[]});
await context.showLoopReview('loop');
assert.match(dialogs.at(-1).body,/Historical evidence.*2020/);
assert.ok(!dialogs.at(-1).body.includes('reflect loops build'));
assert.ok(!dialogs.at(-1).actions.includes('Next: build'));
context.ledgerRequest=async()=>({publication:{state:'stale',loaded_at:null,reason:'historical_pending'}});
await context.showImpactSessions('impact');
assert.match(dialogs.at(-1).body,/Historical evidence.*timestamp unavailable/);
assert.match(dialogs.at(-1).body,/explicit historical refresh/);
context.ledgerRequest=async()=>({loop:{title:'Current loop',status:'detected'},occurrences:[]});
await context.showLoopReview('loop');
assert.ok(!dialogs.at(-1).body.includes('Historical evidence'));
assert.match(dialogs.at(-1).body,/reflect loops build loop/);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_workflow_review_requires_current_candidate_and_current_detail():
    _run_modal(r"""
(async()=>{
const candidate={id:'workflow',status:'pending',contract_signature:'one',hypothesis:'Stored hypothesis',content:{slug:'current',steps:[]}};
context.IMPROVEMENT_DATA={workflows:[candidate],resources:{workflows:{state:'ready'}}};
context.ledgerRequest=async url=>{requests.push(url);return url.includes('/preview')
  ? {checks:{apply_allowed:true},project_root:'/test'} : {support_execution_unit_count:3};};
for(const state of ['stale','error','preparing',undefined]) {
  context.IMPROVEMENT_DATA.resources.workflows.state=state;
  await context.showWorkflowReview('workflow');
  assert.equal(dialogs.at(-1).title,'current');
  assert.match(dialogs.at(-1).body,/Stored hypothesis/);
  assert.match(dialogs.at(-1).body,/Read-only inspection/);
  assert.ok(!dialogs.at(-1).body.includes('workflow-content-editor'));
  assert.ok(!dialogs.at(-1).body.includes('workflow-project-root'));
  assert.ok(!dialogs.at(-1).actions.includes('data-ledger-action="apply"'));
}
assert.equal(requests.length,4,'historical candidate still loads evidence');
assert.ok(requests.every(url=>url.endsWith('/evidence')),'no preview for an outdated candidate');
context.IMPROVEMENT_DATA.resources.workflows.state='ready';
await context.showWorkflowReview('workflow');
assert.equal(dialogs.at(-1).title,'current');
assert.match(dialogs.at(-1).actions,/data-ledger-action="apply"/);

let resolvePreview,resolveLedger;
context.ledgerRequest=url=>new Promise(resolve=>{
  if(url.includes('/preview'))resolvePreview=resolve;else resolveLedger=resolve;
});
const reviewing=context.showWorkflowReview('workflow');
context.IMPROVEMENT_DATA.workflows=[{...candidate,content:{slug:'changed'}}];
resolvePreview({checks:{apply_allowed:true}});resolveLedger({support_execution_unit_count:3});
await reviewing;
assert.equal(dialogs.at(-1).title,'current');
assert.match(dialogs.at(-1).body,/changed while loading/);
assert.match(dialogs.at(-1).body,/Evidence references are updating/);
assert.ok(!dialogs.at(-1).body.includes('3 linked'));
assert.ok(!dialogs.at(-1).actions.includes('data-ledger-action="apply"'));
context.IMPROVEMENT_DATA.workflows=[candidate];
context.ledgerRequest=async url=>{
  context.IMPROVEMENT_DATA.workflows=[JSON.parse(JSON.stringify(candidate))];
  return url.includes('/preview') ? {checks:{apply_allowed:true}} : {support_execution_unit_count:3};
};
await context.showWorkflowReview('workflow');
assert.equal(dialogs.at(-1).title,'current','an identical polling response does not invalidate the review');
assert.match(dialogs.at(-1).actions,/data-ledger-action="apply"/);

context.ledgerRequest=async url=>url.includes('/preview') ? {checks:{apply_allowed:true}}
  : {contract_signature:'different',support_execution_unit_count:999};
await context.showWorkflowReview('workflow');
assert.match(dialogs.at(-1).body,/different workflow revision/);
assert.ok(!dialogs.at(-1).body.includes('999'));
assert.ok(!dialogs.at(-1).actions.includes('data-ledger-action="apply"'));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_workflow_recorded_evidence_and_failed_preview_remain_inspectable():
    _run_modal(r"""
(async()=>{
context.IMPROVEMENT_DATA={workflows:[{id:'workflow',content:{slug:'recorded',source_markdown:'Stored procedure'},
  support_execution_unit_count:7}],resources:{workflows:{state:'ready'}}};
const recorded={analysis_state:'pending',evidence_state:'recorded',publication:{state:'recorded'},
  support_execution_unit_count:1,support_execution_units:[{execution_unit_id:'task-one',session_id:'session-one',title:'Captured task'}],
  provenance_session_count:0,provenance_sessions:[],exposed_execution_unit_count:0,exposed_execution_units:[]};
context.ledgerRequest=async url=>{requests.push(url);if(url.includes('/preview'))throw new Error('Preparing');return recorded;};
await context.showWorkflowReview('workflow');
let dialog=dialogs.at(-1);
assert.match(dialog.body,/Stored procedure/);assert.match(dialog.body,/Captured task/);
assert.match(dialog.body,/Recorded evidence/);assert.match(dialog.body,/Project preview unavailable/);
assert.match(dialog.body,/\?session=session-one/);
assert.ok(!dialog.body.includes('1 comparable'));
assert.ok(!dialog.body.includes('workflow-content-editor'));
assert.ok(!dialog.actions.includes('data-ledger-action="apply"'));

context.ledgerRequest=async url=>url.includes('/preview')?{checks:{apply_allowed:true}}:{...recorded,analysis_state:'stale',publication};
await context.showWorkflowReview('workflow');dialog=dialogs.at(-1);
assert.match(dialog.body,/Historical evidence.*2020/);
assert.match(dialog.body,/Captured task/);
assert.ok(!dialog.actions.includes('data-ledger-action="apply"'));

context.ledgerRequest=async()=>{throw new Error('Evidence preparing');};
await context.showWorkflowReview('workflow');dialog=dialogs.at(-1);
assert.match(dialog.body,/Stored procedure/);assert.match(dialog.body,/7 linked/);
assert.match(dialog.body,/Provenance Sessions · —/);
assert.match(dialog.body,/Evidence references are updating/);
assert.ok(!dialog.body.includes('No comparable execution units'));
assert.match(dialog.actions,/Retry Review/);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_workflow_inspection_does_not_depend_on_paginated_list_membership():
    _run_modal(r"""
(async()=>{
context.IMPROVEMENT_DATA={workflows:[],resources:{workflows:{state:'preparing'}}};
const candidate={id:'outside-list',status:'pending',contract_signature:'contract',content:{slug:'direct',source_markdown:'Captured procedure'}};
context.ledgerRequest=async url=>{requests.push(url);return {
  candidate,contract_signature:'contract',analysis_state:'pending',evidence_state:'recorded',
  publication:{state:'recorded'},support_execution_units:[],support_execution_unit_count:0,
};};
await context.showWorkflowReview('outside-list');
assert.equal(dialogs.at(-1).title,'direct');
assert.match(dialogs.at(-1).body,/Captured procedure/);
assert.match(dialogs.at(-1).body,/Read-only inspection/);
assert.ok(!dialogs.at(-1).actions.includes('data-ledger-action="apply"'));
assert.deepEqual(requests,['/api/workflows/outside-list/evidence']);
context.ledgerRequest=async url=>url.includes('/preview')
  ? {contract_signature:'contract',checks:{apply_allowed:true},project_root:'/chosen'}
  : {candidate,contract_signature:'contract',support_execution_unit_count:1};
await context.showWorkflowReview('outside-list');
assert.match(dialogs.at(-1).actions,/data-ledger-action="apply"/,'current direct evidence plus preview permits review');
context.ledgerRequest=async url=>url.includes('/preview')
  ? {contract_signature:'changed',checks:{apply_allowed:true}}
  : {candidate,contract_signature:'contract',support_execution_unit_count:1};
await context.showWorkflowReview('outside-list');
assert.match(dialogs.at(-1).body,/different workflow revision/);
assert.ok(!dialogs.at(-1).actions.includes('data-ledger-action="apply"'));
context.ledgerRequest=async()=>{throw new Error('Workflow missing');};
await context.showWorkflowReview('deleted');
assert.match(dialogs.at(-1).body,/Workflow missing/);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_workflow_notice_distinguishes_publication_from_cache_read_time():
    _run_modal(r"""
(async()=>{
const resource={state:'stale',loaded_at:publication.snapshot_completed_at,publication};
context.IMPROVEMENT_DATA={workflows:[{id:'workflow',content:{slug:'stored',steps:[]}}],resources:{workflows:resource}};
context.ledgerRequest=async()=>({support_execution_unit_count:1});
await context.showWorkflowReview('workflow');
assert.match(dialogs.at(-1).body,/published.*2020/);
assert.ok(!dialogs.at(-1).body.includes('2026'),'snapshot publication survives the list projection');
resource.publication={state:'stale',loaded_at:publication.loaded_at};
resource.loaded_at=publication.loaded_at;
await context.showWorkflowReview('workflow');
assert.match(dialogs.at(-1).body,/last loaded.*2026.*publication timestamp unavailable/);
assert.ok(!dialogs.at(-1).body.includes(`published ${new Date(publication.loaded_at).toLocaleString()}`));
resource.publication={state:'stale'};resource.loaded_at=null;
await context.showWorkflowReview('workflow');
assert.match(dialogs.at(-1).body,/publication timestamp unavailable/);
assert.ok(!dialogs.at(-1).body.includes('last loaded'));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_recorded_impact_keeps_frozen_values_and_unknown_legacy_references():
    _run_modal(r"""
(async()=>{
context.ledgerRequest=async()=>({analysis_state:'pending',evidence_state:'recorded',
  publication:{state:'recorded',loaded_at:'2026-11-08',recorded_at:'2020-01-01'},measured_at:'2020-01-01',
  metric_name:'tool_failure_rate',snapshot_exact:false,before_count:2,after_count:null,
  before_execution_units:[{execution_unit_id:'exact-task',session_id:'recorded-session',title:'Frozen task',metric_value:0},
    {execution_unit_id:'legacy-task',snapshot_missing:true,metric_value:null}],after_execution_units:[]});
await context.showImpactSessions('impact');const dialog=dialogs.at(-1);
assert.match(dialog.body,/Recorded evidence/);assert.match(dialog.body,/Measured 2020-01-01/);
assert.ok(!dialog.body.includes('2026-11-08'),'read time must not become measurement time');
assert.match(dialog.body,/Recorded: 0% failed/);
assert.match(dialog.body,/legacy-task/);assert.match(dialog.body,/Snapshot details unavailable/);
assert.match(dialog.body,/Session reference unavailable/);
assert.match(dialog.body,/\?session=recorded-session/);
assert.ok(!dialog.body.includes('?session=undefined'));
assert.match(dialog.body,/— tasks/);assert.match(dialog.body,/Current comparability and impact validation are pending/);
assert.match(dialog.actions,/Retry Evidence/);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_dialog_retries_retain_context_and_late_reads_cannot_replace_selection():
    _run_modal(r"""
(async()=>{
let notices=[];
const body={scrollTop:120,querySelector:()=>notices[0],prepend:node=>notices.unshift(node)};
context.document={createElement:()=>({dataset:{},setAttribute:()=>{}})};
context.ledgerDialog.open=true;
context.ledgerDialog.querySelector=()=>body;
context.openLedgerDialog=dialog=>{dialogs.push(dialog);body.scrollTop=0;};
const first=context.beginLedgerRead('workflow','one','Opening');
first.projectRoot='/selected-project';
context.presentLedgerRead(first,{title:'First workflow',body:'Stored procedure'});
body.scrollTop=120;
const retry=context.beginLedgerRead('workflow','one','Retry');
assert.equal(dialogs.length,2,'retry keeps the current content visible');
assert.equal(retry.projectRoot,'/selected-project');
context.presentLedgerRead(retry,{title:'Updated workflow',body:'Updated evidence'});
assert.equal(body.scrollTop,120,'successful retry preserves scroll');
const failing=context.beginLedgerRead('workflow','one','Retry');
context.failLedgerRead(failing,{title:'Failure',body:'error'});
assert.equal(dialogs.at(-1).title,'Updated workflow');
assert.match(notices[0].textContent,/Showing the previous result/);
const selected=context.beginLedgerRead('impact','two','New selection');
context.presentLedgerRead(selected,{title:'Selected impact'});
context.presentLedgerRead(failing,{title:'Late workflow'});
assert.equal(dialogs.at(-1).title,'Selected impact');

context.IMPROVEMENT_DATA={workflows:[],resources:{workflows:{state:'ready'}}};
let resolveOld;
context.ledgerRequest=url=>new Promise(resolve=>{resolveOld=resolve;});
const old=context.showWorkflowReview('old');
const active=context.beginLedgerRead('impact','new','Impact');
context.presentLedgerRead(active,{title:'Newest selection'});
resolveOld({candidate:{id:'old',content:{slug:'Old workflow'}},analysis_state:'pending'});
await old;
assert.equal(dialogs.at(-1).title,'Newest selection');
assert.equal(params.get('workflow'),null,'late result cannot change navigation');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
