"""Run session navigation and asynchronous detail-loading contracts in Node."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


def _run_browser_contract(scenario: str, **data) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for browser session contract checks")
    source = Path("src/reflect/frontend/js_sessions.js").read_text()
    navigation = source[source.index("function setSelectedSession("):
                        source.index("// Pill click")]
    navigation = Path("src/reflect/frontend/js_demo.js").read_text() + "\n" + navigation
    detail_start = re.search(r"(?:async )?function tryLoadFullDetail\(", source)
    assert detail_start is not None
    detail = source[detail_start.start():source.index("\n  renderSessionList();\n  if (selectedIdx")]
    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const rendered = [], tabs = [], timersCleared = [];
const listeners = new Map();
let scroller = {scrollTop:173};
const detailElement = {querySelector:() => scroller};
let reloads = 0, listRenders = 0;
const params = new URLSearchParams('agents=cursor&session=one');
const sessions = [{id:'short-one', full_id:'one'}, {id:'short-two', full_id:'two'}];
const response = (status, body) => ({status, ok:status === 200, json:async () => body});
const context = {
  sessions, selectedIdx:0, dashboardSelectedSessionId:'one', AbortController,dashboardDocumentActive:true,
  filterState:{range:'7d'},
  D:{sessions, _navigationCompletedAt:'2026-01-01T00:00:00Z'},
  document:{addEventListener:(name, listener) => listeners.set(name, listener),
    getElementById:id => id === 'sb-detail' ? detailElement
      : id === 'tab-sessions' ? {classList:{contains:() => true}} : null},
  navigationSnapshotLabel:() => 'report completed 2026-01-01',
  window:{setTimeout:() => 42, clearTimeout:id => timersCleared.push(id),
    addEventListener:(name,listener)=>listeners.set(name,listener)},
  activateTab:tab => tabs.push(tab),
  renderSessionList:() => {listRenders += 1;},
  renderSessionDetail:session => {rendered.push(session); scroller = {scrollTop:0};},
  updateUrlParams:fn => fn(params),
  reportSupportsServerFiltering:() => true,
  scheduleDashboardReload:() => {reloads += 1;},
  normalizeLoadedConversation:events => events,
  getEstimatedSessionTokens:() => null,
};
vm.createContext(context);
vm.runInContext(input.navigation + '\n' + input.detail, context);
(async () => {
""" + scenario + r"""
})().then(() => process.stdout.write('browser-contract-ok\n'))
  .catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run(
        [node, "-e", script],
        input=json.dumps({"navigation": navigation, "detail": detail, **data}),
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "browser-contract-ok" in result.stdout, "asynchronous browser contract did not finish"


def test_session_selection_opens_detail_without_reloading_dashboard():
    _run_browser_contract(r"""
context.setSelectedSession(1);
assert.equal(context.selectedIdx, 1);
assert.equal(context.dashboardSelectedSessionId, 'two');
assert.equal(params.get('session'), 'two');
assert.equal(params.get('agents'), 'cursor');
assert.equal(params.get('range'), '7d', 'selection preserves the current window before a reload');
assert.deepEqual(tabs, ['sessions']);
assert.equal(listRenders, 1);
assert.equal(rendered.at(-1), sessions[1]);
assert.equal(reloads, 0, 'session selection must not replace the whole dashboard');
""")


@pytest.mark.parametrize("failure", ["preparing", "not-found", "network"])
def test_detail_failure_exposes_retry_and_clears_loading(failure):
    _run_browser_contract(r"""
let resolveFetch, rejectFetch;
context.fetch = () => new Promise((resolve, reject) => {resolveFetch=resolve; rejectFetch=reject;});
const button = {disabled:false, textContent:'Retry telemetry load'};
const request = context.tryLoadFullDetail('one', button);
assert.equal(typeof request?.then, 'function', 'detail loader must return its request promise');
assert.equal(sessions[0]._loadingDetail, true);
if (input.failure === 'network') rejectFetch(new TypeError('Failed to fetch'));
else resolveFetch(response(input.failure === 'preparing' ? 503 : 404,
  input.failure === 'preparing' ? {state:'preparing', error:'The report is preparing'} : {error:'Not found'}));
await request;
assert.equal(sessions[0]._loadingDetail, false);
assert.ok(!sessions[0]._fullLoaded);
assert.equal(button.disabled, false);
assert.equal(rendered.at(-1), sessions[0]);
const message = String(sessions[0]._detailLoadError || '');
assert.ok(message, 'failed automatic loading needs a visible session error');
assert.match(message, input.failure === 'preparing' ? /refresh|prepar/i
  : input.failure === 'not-found' ? /not found/i : /retry|try again|network|fetch/i);
assert.ok(timersCleared.length > 0, 'request timeout must be cleared after failure');
""", failure=failure)


@pytest.mark.parametrize("status", [200, 503])
def test_detail_response_does_not_render_over_newer_selection(status):
    _run_browser_contract(r"""
let resolveFetch;
context.fetch = () => new Promise(resolve => {resolveFetch=resolve;});
const request = context.tryLoadFullDetail('one', null);
assert.equal(typeof request?.then, 'function');
context.selectedIdx = 1;
rendered.length = 0;
resolveFetch(response(input.status, input.status === 200
  ? {events:[{type:'response', preview:'Old session response'}], measurements:{tokens:12}}
  : {state:'preparing'}));
await request;
assert.equal(rendered.length, 0, 'an older request must not replace the selected session panel');
assert.equal(sessions[0]._loadingDetail, false);
assert.ok(!sessions[1]._fullLoaded);
if (input.status === 200) {
  assert.equal(sessions[0]._fullLoaded, true);
  assert.equal(sessions[0].conversation[0].preview, 'Old session response');
} else {
  assert.ok(sessions[0]._detailLoadError);
  assert.ok(!sessions[0]._fullLoaded);
}
""", status=status)


def test_explicit_detail_retry_clears_previous_error_and_can_succeed():
    _run_browser_contract(r"""
context.fetch = async () => response(503, {state:'preparing'});
await context.tryLoadFullDetail('one', null);
assert.ok(sessions[0]._detailLoadError);
let resolveFetch;
context.fetch = () => new Promise(resolve => {resolveFetch=resolve;});
const button = {disabled:false, textContent:'Retry telemetry load'};
const retry = context.tryLoadFullDetail('one', button);
assert.equal(typeof retry?.then, 'function');
assert.ok(!sessions[0]._detailLoadError, 'explicit retry must clear the prior failure');
assert.equal(sessions[0]._loadingDetail, true);
resolveFetch(response(200, {events:[{type:'response', preview:'Recovered'}], assessments:[]}));
await retry;
assert.equal(sessions[0]._loadingDetail, false);
assert.equal(sessions[0]._fullLoaded, true);
assert.ok(!sessions[0]._detailLoadError);
assert.equal(sessions[0].conversation[0].preview, 'Recovered');
assert.equal(rendered.at(-1), sessions[0]);
""")




def test_captured_activity_stays_available_while_analysis_refreshes():
    _run_browser_contract(r"""
sessions[0]._detailTab = 'conversation';
sessions[0].assessments = [{outcome:'success'}];
sessions[0].measurements = {tokens:999};
sessions[0].git_activity = {total_operations:9};
sessions[0].insights = [{title:'old analysis'}];
context.fetch = async () => response(200, {derived_state:'preparing', session:{id:'one',agent:'codex'},
  conversation:[{type:'tool_result',success:null,preview:'Captured output'}],
  telemetry:{spans:[{id:'observed'}]},tool_inventory:{tools:[{name:'Read',count:1}]}});
await context.tryLoadFullDetail('one', null);
assert.equal(sessions[0].agent, 'codex');
assert.equal(sessions[0]._activityLoaded, true);
assert.equal(sessions[0]._fullLoaded, false);
assert.equal(sessions[0].derived_state, 'preparing');
assert.equal(sessions[0]._detailTab, 'conversation');
assert.equal(sessions[0].conversation[0].success, null);
assert.equal(sessions[0].tool_inventory.tools[0].count, 1);
assert.equal(scroller.scrollTop, 173, 'asynchronous activity load preserves reader position');
for (const key of ['measurements','git_activity','insights','assessments']) assert.ok(!Object.hasOwn(sessions[0],key));
assert.equal(context.D._navigationFromCache, true);
context.fetch = async () => response(200, {derived_state:'ready',session:{id:'one',agent:'codex'},
  conversation:[{type:'response',preview:'Published'}],measurements:{tokens:12},assessments:[]});
let prevented = false;
listeners.get('reflect-snapshot-published')({preventDefault:()=>{prevented=true;}});
await new Promise(resolve=>setImmediate(resolve));
assert.equal(prevented, true);
assert.equal(sessions[0]._fullLoaded, true);
assert.equal(sessions[0].measurements.tokens, 12);
assert.equal(sessions[0]._detailTab, 'conversation');
assert.equal(scroller.scrollTop, 173);
assert.equal(context.D._navigationCompletedAt, '2026-01-01T00:00:00Z');
assert.equal(reloads, 0);
""")


def test_publication_during_detail_request_revalidates_after_partial_response():
    _run_browser_contract(r"""
let resolveFirst, calls = 0;
context.fetch = () => {
  calls += 1;
  return calls === 1 ? new Promise(resolve=>{resolveFirst=resolve;})
    : Promise.resolve(response(200,{derived_state:'ready',conversation:[],measurements:{tokens:7}}));
};
const first = context.tryLoadFullDetail('one', null);
listeners.get('reflect-snapshot-published')({preventDefault:()=>{}});
assert.equal(calls, 1, 'publication must not duplicate an active detail load');
resolveFirst(response(200,{derived_state:'preparing',conversation:[{type:'response',preview:'Captured'}]}));
await first;
await new Promise(resolve=>setImmediate(resolve));
assert.equal(calls, 2);
assert.equal(sessions[0]._fullLoaded, true);
assert.deepEqual(sessions[0].conversation, []);
assert.equal(sessions[0].measurements.tokens, 7);
""")


@pytest.mark.parametrize("cancelled", [False, True])
def test_navigation_suspends_queued_detail_retry_until_bfcache_restoration(cancelled):
    _run_browser_contract(r"""
let resolveFirst, rejectFirst, calls = 0;
context.fetch = () => {
  calls += 1;
  return calls === 1 ? new Promise((resolve,reject)=>{resolveFirst=resolve;rejectFirst=reject;})
    : Promise.resolve(response(200,{derived_state:'ready',conversation:[],measurements:{tokens:8}}));
};
const first = context.tryLoadFullDetail('one', null);
listeners.get('reflect-snapshot-published')({preventDefault:()=>{}});
assert.equal(sessions[0]._refreshDetailAfterLoad, true);
context.dashboardDocumentActive = false;
if (input.cancelled) rejectFirst(Object.assign(new Error('Navigation cancelled fetch'),{name:'AbortError'}));
else resolveFirst(response(200,{derived_state:'preparing',conversation:[]}));
await first;
await context.tryLoadFullDetail('one', null);
assert.equal(calls, 1, 'old finally and direct requests must not start reads after pagehide');
assert.equal(rendered.length, 0, 'old completion must not rerender the unloading document');
assert.equal(sessions[0]._refreshDetailAfterLoad, true, 'deferred work survives BFCache');
context.dashboardDocumentActive = true;
listeners.get('pageshow')({persisted:false});
assert.equal(calls, 1, 'ordinary pageshow must not introduce a new retry');
listeners.get('pageshow')({persisted:true});
listeners.get('pageshow')({persisted:true});
await new Promise(resolve=>setImmediate(resolve));
assert.equal(calls, 2, 'BFCache resumes the queued read exactly once');
assert.equal(sessions[0]._fullLoaded, true);
assert.equal(sessions[0].measurements.tokens, 8);
assert.ok(!sessions[0]._refreshDetailAfterLoad);
""", cancelled=cancelled)


def test_partial_detail_retains_only_previous_observed_usage():
    _run_browser_contract(r"""
context.fetch=async()=>response(200,{derived_state:'ready',usage_state:'ready',
  measurements:{tokens:41},git_activity:{total_operations:3},total_cost_usd:2,
  assessments:[{outcome:'success'}],insights:[{title:'Old assessment'}]});
await context.tryLoadFullDetail('one',null);
const loadedAt=sessions[0]._usageLoadedAt;
assert.ok(loadedAt);
context.fetch=async()=>response(200,{derived_state:'preparing',usage_state:'preparing',
  conversation:[{type:'response',preview:'New captured activity'}]});
await context.tryLoadFullDetail('one',null);
assert.equal(sessions[0]._retainedUsage,true);
assert.equal(sessions[0]._usageLoadedAt,loadedAt);
assert.equal(sessions[0].measurements.tokens,41);
assert.equal(sessions[0].git_activity.total_operations,3);
assert.equal(sessions[0].total_cost_usd,2);
assert.ok(!Object.hasOwn(sessions[0],'assessments'));
assert.ok(!Object.hasOwn(sessions[0],'insights'));
context.fetch=async()=>response(200,{derived_state:'ready',usage_state:'ready',measurements:{tokens:42}});
await context.tryLoadFullDetail('one',null);
assert.equal(sessions[0]._retainedUsage,false);
assert.equal(sessions[0].measurements.tokens,42);
assert.ok(!Object.hasOwn(sessions[0],'git_activity'),'successful response can remove prior evidence');
""")


def test_uncached_deep_link_uses_identity_only_navigation_entry():
    source = Path("src/reflect/frontend/js_sessions.js").read_text()
    initialization = source[source.index("  const sessions = D.sessions"):
                            source.index("  let activeConversationPlayhead")]
    _run_browser_contract(r"""
const empty = {D:{sessions:[],snapshot_completed_at:'2026-02-01T00:00:00Z'},
  currentParams:()=>new URLSearchParams('session=not-in-cache')};
vm.createContext(empty); vm.runInContext('(function(){'+input.initialization+'})();',empty);
const linked = empty.D.sessions[0];
assert.equal(linked.id, 'not-in-cache');
assert.equal(linked.full_id, 'not-in-cache');
assert.equal(linked._navigationOnly,true);
for (const key of ['input_tokens','output_tokens','event_count','total_cost_usd','quality_score','is_completed']) {
  assert.ok(!Object.hasOwn(linked,key), 'identity must not manufacture summary fields: '+key);
}
assert.equal(empty.D._navigationCompletedAt, '2026-02-01T00:00:00Z');
""", initialization=initialization)


def test_pending_analysis_does_not_render_unknown_outcome_or_zero_git_activity():
    source = Path("src/reflect/frontend/js_sessions.js").read_text()
    rendering = source[source.index("  function renderObservedMeasurements("):
                       source.index("  function renderSessionChangesPanel(")]
    _run_browser_contract(r"""
vm.runInContext(input.rendering,context);
const partial = {derived_state:'preparing',measurements:{metrics:{}},git_activity:{total_operations:0}};
assert.equal(context.renderObservedMeasurements(partial),'');
const git = context.renderGitActivity(partial);
assert.match(git,/refreshing/);
assert.ok(!/0 captured|No Git|unknown/.test(git));
""", rendering=rendering)


def test_identity_only_and_preparing_detail_hide_unpublished_summary_fields():
    source = Path("src/reflect/frontend/js_sessions.js").read_text()
    rendering = source[source.index("  function renderSessionDetail("):
                       source.index("  function getEventNodeColor(")]
    rendering += source[source.index("  function renderQualityPanel("):
                        source.index("  function renderTelemetryCockpit(")]
    _run_browser_contract(r"""
const detail = {innerHTML:'',querySelector:()=>null,querySelectorAll:()=>[]};
Object.assign(context,{document:{getElementById:()=>detail},activeConversationPlayhead:null,
  currentParams:()=>new URLSearchParams(),colorForAgent:()=>'',formatAgentLabel:()=>'',
  sessionCostPresentation:()=>({status:'unavailable',reason:'missing',label:'Unavailable'}),
  sqlTab:()=>({}),escHtml:String,fmt:String,fmtDur:String,fmtTokenShort:String,
  renderObservedMeasurements:()=>'',renderConversationThread:()=>'',renderSessionTimelinePanel:()=>''});
vm.runInContext(input.rendering,context);
for (const extra of [{_navigationOnly:true},{derived_state:'preparing'}]) {
  context.renderSessionDetail({id:'linked',full_id:'linked',_activityLoaded:true,...extra});
  assert.ok(!/Duration:|Events:|Cost:|quality-score-value|Task outcome: unknown/.test(detail.innerHTML));
  assert.match(detail.innerHTML,extra._navigationOnly ? /summary unavailable/i : /Captured activity available; analysis is refreshing/);
}
""", rendering=rendering)


def test_unknown_tool_result_status_is_not_converted_to_pass_or_failure():
    source = Path("src/reflect/frontend/js_sessions.js").read_text()
    functions = []
    for name in ("normalizeLoadedConversation", "getEventNodeColor", "sessionTimelineEventLabel"):
        start = source.index(f"  function {name}(")
        functions.append(source[start:source.index("\n  function ", start + 10)])
    _run_browser_contract(r"""
Object.assign(context,{parseEventTs:()=>1,stringifyEventContent:String,
  EVENT_COLORS:{tool_call:'observed',tool_result_ok:'passed',tool_result_fail:'failed'}});
vm.runInContext(input.functions,context);
for (const value of [undefined,null]) {
  const normalized = context.normalizeLoadedConversation([{type:'tool_result',success:value}],[])[0];
  assert.equal(normalized.success,null);
  assert.equal(context.getEventNodeColor(normalized),'observed');
  assert.match(context.sessionTimelineEventLabel(normalized),/^Result observed/);
}
""", functions="\n".join(functions))


def test_inbox_distinguishes_pending_retained_and_successful_empty_data():
    source = Path("src/reflect/frontend/js_impact.js").read_text()
    renderer = source[source.index("function buildImprovementSurfaces()"):
                      source.index("\nbuildImprovementSurfaces();")]
    _run_browser_contract(r"""
const panels = Object.fromEntries(['inbox','workflows','skills','impact'].map(name => {
  const classes = new Set(), children = [];
  return ['tab-'+name, {classes,children,
    classList:{toggle:(value,active)=>active ? classes.add(value) : classes.delete(value)},
    querySelector:()=>children[0] || null, prepend:child=>children.unshift(child),
  }];
}));
const count = {firstChild:{textContent:'0'}}, ledger = {innerHTML:''}, loops = {innerHTML:''};
context.document = {
  getElementById:id=>panels[id] || ({'finding-count':count,'finding-ledger':ledger,'loop-ledger':loops}[id]) || null,
  createElement:()=>({hidden:false,attributes:{},setAttribute(name,value){this.attributes[name]=value;}}),
  querySelectorAll:()=>[],
};
context.sortObservedLoops=items=>items;
context.groupImpactMeasurements=items=>items.map(item=>[item]);
context.currentParams=()=>new URLSearchParams();
context.escHtml=value=>String(value);
context.fmt=value=>String(value);
const resources = Object.fromEntries(['findings','loops','workflows','skills','impact','rules'].map(name=>[name,{state:'preparing',loaded_at:null}]));
context.IMPROVEMENT_DATA={resources,observations:[]};
vm.runInContext(input.renderer,context);
context.buildImprovementSurfaces();
assert.equal(count.firstChild.textContent,'—');
assert.match(ledger.innerHTML,/Findings are updating/);
assert.ok(!ledger.innerHTML.includes('Inbox is clear'));
assert.ok(!panels['tab-inbox'].classes.has('evidence-unavailable'),'first load keeps Inbox visible');
assert.ok(panels['tab-impact'].classes.has('evidence-unavailable'),'unknown impact stays gated');
assert.match(panels['tab-inbox'].children[0].innerHTML,/retry-improvements/);

context.IMPROVEMENT_DATA.observations=[{id:'observed',title:'Retained finding',status:'new'}];
resources.findings={state:'ready',loaded_at:'2026-11-08T09:00:00Z'};
context.buildImprovementSurfaces();
assert.match(ledger.innerHTML,/Retained finding/);
assert.equal(count.firstChild.textContent,'1');
assert.match(loops.innerHTML,/Loops are updating/);
assert.ok(!panels['tab-inbox'].classes.has('evidence-unavailable'),'other unavailable surfaces do not hide findings');

resources.findings.state='preparing';
context.buildImprovementSurfaces();
assert.match(ledger.innerHTML,/Retained finding/);
assert.match(panels['tab-inbox'].children[0].innerHTML,/last loaded/);
assert.equal(count.firstChild.textContent,'1');
assert.equal(panels['tab-inbox'].children.length,1,'retries must reuse the notice');

context.IMPROVEMENT_DATA.observations=[];
resources.findings.state='ready';resources.loops.state='ready';
context.buildImprovementSurfaces();
assert.match(ledger.innerHTML,/Inbox is clear/);
assert.equal(count.firstChild.textContent,'0');
assert.equal(panels['tab-inbox'].children[0].hidden,true);
for (const name of ['workflows','impact']) {
  resources[name]={state:'preparing',loaded_at:'2026-11-08T09:00:00Z'};
}
context.buildImprovementSurfaces();
for (const name of ['workflows','impact']) {
  assert.ok(!panels['tab-'+name].classes.has('evidence-unavailable'));
  assert.match(panels['tab-'+name].children[0].innerHTML,/Historical results.*current evidence/);
}
""", renderer=renderer)


def test_improvement_retry_recovers_same_publication_without_duplicate_requests():
    source = Path("src/reflect/frontend/js_ledger.js").read_text()
    retry = source[source.index("let improvementRequest ="):source.index("const workflowDateFormatter =")]
    _run_browser_contract(r"""
let tick, resolveLoad, calls=0, rendered=0;
const retryParams=new URLSearchParams('tab=inbox');
context.currentParams=()=>retryParams;
context.document.visibilityState='visible';
context.window.setInterval=(fn,ms)=>{tick=fn;assert.equal(ms,15000);};
context.IMPROVEMENT_DATA={resources:{findings:{state:'error',loaded_at:null}}};
context.loadImprovementData=()=>{calls+=1;return new Promise(resolve=>{resolveLoad=resolve;});};
context.buildImprovementSurfaces=()=>{rendered+=1;};
vm.runInContext(input.retry,context);
tick();tick();
assert.equal(calls,1,'timer ticks share one in-flight request');
resolveLoad({resources:{findings:{state:'ready',loaded_at:'now'}},observations:[{id:'recovered'}]});
await context.refreshImprovementSurfaces();
assert.equal(rendered,1);assert.equal(context.IMPROVEMENT_DATA.observations[0].id,'recovered');
tick();assert.equal(calls,1,'ready data does not keep polling');
context.IMPROVEMENT_DATA.resources.findings.state='error';
retryParams.set('tab','sessions');tick();assert.equal(calls,1);
retryParams.set('tab','inbox');context.document.visibilityState='hidden';tick();assert.equal(calls,1);
context.document.visibilityState='visible';tick();assert.equal(calls,2);
resolveLoad({resources:{findings:{state:'ready',loaded_at:'later'}}});
await context.refreshImprovementSurfaces();
""", retry=retry)


def test_cached_workflow_evidence_never_enables_a_new_review():
    source = Path("src/reflect/frontend/js_ledger.js").read_text()
    review = source[source.index("function staleEvidenceNotice("):
                    source.index("async function showObservationEvidence(")]
    review += source[source.index("async function showWorkflowReview("):
                     source.index("async function submitSessionFeedback(")]
    _run_browser_contract(r"""
const dialogs=[];
context.openLedgerDialog=dialog=>dialogs.push(dialog);
context.IMPROVEMENT_DATA={workflows:[{id:'workflow'}],resources:{workflows:{state:'ready'}}};
context.window.location={origin:'http://localhost'};context.URL=URL;
context.escHtml=String;
context.fmt=String;context.humanizeLedgerLabel=String;context.humanizeLedgerText=String;
context.ledgerWorkspaceName=String;context.ledgerDialog={dataset:{}};
context.workflowSourcePresentation=()=>({label:'Workflow',detail:'Recorded procedure'});
context.renderWorkflowSessionLedger=()=>'<div>Retained evidence</div>';
context.renderWorkflowSteps=()=>'<div>Recorded steps</div>';
context.ledgerRequest=async url=>url.includes('/preview')
  ? {checks:{apply_allowed:true}} : {publication:{state:'stale'},support_execution_unit_count:10};
vm.runInContext(input.review,context);
await context.showWorkflowReview('workflow');
assert.equal(dialogs.at(-1).title,'workflow');
assert.match(dialogs.at(-1).body,/Historical evidence/);
assert.match(dialogs.at(-1).body,/Recorded steps/);
assert.match(dialogs.at(-1).body,/Retained evidence/);
assert.ok(!dialogs.at(-1).actions.includes('data-ledger-action="apply"'));
assert.ok(!dialogs.at(-1).body.includes('workflow-content-editor'));
""", review=review)
