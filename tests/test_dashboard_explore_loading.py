"""Exercise scoped Explore loading and recovery without a page reload."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _run_explore_contract(scenario: str, **data) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for Explore loading contract checks")
    source = Path("src/reflect/frontend/js_ledger.js").read_text()
    loading = source[source.index("const lazySqlTabRequests ="):]
    navigation = Path("src/reflect/frontend/js_navigation.js").read_text()
    loading += navigation[navigation.index("function clearExploreSessionScope("):navigation.index("function chooseExploreSession(")]
    widgets = Path("src/reflect/frontend/js_graphs.js").read_text().split("/* Tool color map */")[0]
    loading = Path("src/reflect/frontend/js_demo.js").read_text() + "\n" + widgets + loading
    script = r"""
const assert = require('node:assert/strict'), vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const params = new URLSearchParams('tab=explore&view=usage&session=A&agents=codex');
const renders = [], requests = [], timers = [], cleared = [];
const listeners = new Map();
let overlays = 0, reloads = 0, improvementUpdates = 0;
const navSessions = [{id:'navigation-A'}, {id:'navigation-B'}];
const statuses = {}, panels = {};
function element(id) {
  const classes = new Set();
  return {id, hidden:false, _text:'', attributes:{}, children:[], listeners:{}, dataset:{},
    get textContent() {return this._text + this.children.map(child=>child.textContent).join('');},
    set textContent(value) {this._text=value;this.children=[];},
    classList:{contains:name => classes.has(name),
      toggle:(name,on) => on ? classes.add(name) : classes.delete(name),
      add:name => classes.add(name), remove:name => classes.delete(name)},
    setAttribute(name,value) {this.attributes[name]=value;},
    removeAttribute(name) {delete this.attributes[name];},
    toggleAttribute(name,on) {if(on)this.attributes[name]='';else delete this.attributes[name];},
    focus() {},
    querySelectorAll(selector){return this.children.filter(child=>selector === '.'+child.className);},
    querySelector(selector){
      return this.children.find(child=>selector === '.'+child.className || selector===child.id)
        || this.children.map(child=>child.querySelector(selector)).find(Boolean);
    },
    append(...children) {this.children.push(...children);},
    appendChild(child) {this.children.push(child);},
    addEventListener(name,callback) {this.listeners[name]=callback;},
  };
}
for (const name of ['usage','tools','context','graph']) {
  const status = statuses[name] = element(name + '-view-status');
  const content = element(name + '-content');
  panels[name] = element('tab-explore-' + name);
  panels[name].children = [status, content];
  if (name === 'graph') {
    content.className='graph-widget';
    const heading=element('h2');heading.firstChild={textContent:'Graph'};
    const body=element('widget-body');body.className='graph-widget-body';
    const graphContent=element('widget-content');graphContent.className='graph-widget-content';
    body.append(graphContent);content.append(heading,body);
  }
}
const response = (status,body) => ({status,ok:status === 200,json:async()=>body});
const payload = (scope,view='usage') => view === 'usage'
  ? {sql_backed:true,focused_session_id:scope,sessions:[{id:'scoped-navigation'}],
     measurements:{scope},sqlite:{tabs:{usage:{scope,unique_sessions:1}}}}
  : view === 'context' ? {memory:{scope}} : {scope};
const context = {
  URL, URLSearchParams, AbortController, dashboardDocumentActive:true,
  D:{sql_backed:true,focused_session_id:'',sessions:navSessions,
     sqlite:{tabs:{usage:{scope:'global'}}}},
  dashboardSelectedSessionId:'A', activeExploreView:'usage',
  improvementTabs:new Set(['inbox','workflows','skills','impact']),
  IMPROVEMENT_DATA:{resources:{}}, buildImprovementSurfaces:()=>{},
  sqlTab:name=>context.D.sqlite?.tabs?.[name] || {},
  currentParams:()=>params, reportSupportsServerFiltering:()=>true,
  updateUrlParams:fn=>fn(params),updateExploreScope:()=>{},updateDashboardViewScope:()=>{},
  reportUrlWithCurrentFilters:()=>{
    const url = new URL('/api/data','http://localhost:4000');
    for (const [key,value] of params) url.searchParams.set(key,value);
    return url.toString();
  },
  window:{location:{href:'http://localhost:4000/?'+params,search:'?'+params,
      reload:()=>{reloads += 1;}},
    setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},
    clearTimeout:id=>cleared.push(id)},
  document:{addEventListener:(name, listener)=>listeners.set(name,listener),createElement:tag=>element(tag),getElementById:id=>{
    for (const name of Object.keys(panels)) {
      if (id === panels[name].id) return panels[name];
      if (id === statuses[name].id) return statuses[name];
    }
    return null;
  }},
  showReportLoader:()=>{overlays += 1;}, hideReportLoader:()=>{overlays += 1;},
  humanizeLedgerLabel:value=>value,
  renderUsageView:()=>renders.push('usage'),
  renderToolsView:()=>renders.push('tools'),
  renderSqlTabPayloads:()=>renders.push('context'),
  refreshImprovementSurfaces:()=>{improvementUpdates += 1;return Promise.resolve();},
  initGraphs:()=>renders.push('graph'), exploreGraphLifecycle:{},
};
vm.createContext(context); vm.runInContext(input.loading, context);
(async()=>{
""" + scenario + r"""
  assert.equal(overlays, 0, 'Explore must use inline state, not the global loader');
  assert.equal(reloads, 0, 'Explore must not reload the page');
})().then(()=>process.stdout.write('explore-contract-ok\n'))
  .catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(
        [node, "-e", script], input=json.dumps({"loading": loading, **data}),
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "explore-contract-ok" in result.stdout, "asynchronous Explore contract did not finish"


@pytest.mark.parametrize("status", [503, 404, 500])
def test_explore_errors_are_inline_and_recoverable(status):
    _run_explore_contract(r"""
context.fetch = async (url,options)=>{
  requests.push(url);
  assert.ok(options.signal, 'request must have a timeout abort signal');
  return response(input.status, input.status === 503 ? {state:'preparing'} : {error:'unavailable'});
};
await context.hydrateExploreView('tools').catch(()=>{});
assert.equal(renders.length, 0);
assert.equal(statuses.tools.hidden, false);
assert.equal(panels.tools.attributes['aria-busy'], input.status === 503 ? 'true' : 'false');
assert.equal(panels.tools.children[1].hidden, true);
assert.match(statuses.tools.textContent, input.status === 503
  ? /loading|prepar/i : /not found|could not|server|404|500/i);
assert.ok(timers.some(timer=>timer.ms === 30000));
assert.ok(cleared.length > 0);
context.fetch = async url=>{requests.push(url);return response(200,payload('A','tools'));};
const retry = statuses.tools.children.find(child=>child.textContent === 'Retry');
if (input.status === 503) {
  assert.ok(!retry, 'background preparation must not ask the user to retry');
  await context.hydrateExploreView('tools'); // the existing polling loop
} else {
  assert.ok(retry, 'genuine errors retain recovery');
  await retry.listeners.click();
}
assert.equal(requests.length, 2, 'failed request must remain retryable');
assert.equal(statuses.tools.hidden, true);
assert.equal(panels.tools.children[1].hidden, false);
assert.equal(context.D.sqlite.tabs.tools.scope, 'A');
assert.equal(renders.at(-1), 'tools');
""", status=status)


@pytest.mark.parametrize("view", ["usage", "tools", "context", "graph"])
def test_explore_success_uses_authoritative_scope_and_own_renderer(view):
    _run_explore_contract(r"""
context.fetch = async (url,options)=>{
  requests.push(url);
  assert.ok(options.signal);
  return response(200,payload(input.view === 'context' ? 'global' : 'A',input.view));
};
await context.hydrateExploreView(input.view);
assert.equal(requests.length, 1);
const url = new URL(requests[0]);
assert.equal(url.pathname, input.view === 'usage' ? '/api/data' : '/api/explore/'+input.view);
assert.equal(url.searchParams.get('session'), input.view === 'context' ? null : 'A');
if (input.view === 'usage') {
  assert.equal(url.searchParams.get('tab'), 'explore');
  assert.equal(url.searchParams.get('view'), 'usage');
  assert.equal(context.D.focused_session_id, 'A');
  assert.equal(context.D.measurements.scope, 'A');
  assert.equal(context.D.sessions, navSessions, 'scope fetch must preserve session navigation');
}
assert.equal(renders.at(-1), input.view);
assert.equal(statuses[input.view].hidden, true);
assert.equal(panels[input.view].children[1].hidden, false);
""", view=view)


@pytest.mark.parametrize("old_status", [200, 503])
def test_late_explore_response_cannot_replace_new_session_scope(old_status):
    _run_explore_contract(r"""
const pending = new Map();
context.fetch = (url,options)=>new Promise(resolve=>{
  requests.push(url); pending.set(new URL(url).searchParams.get('session'), resolve);
});
const first = context.hydrateExploreView('usage').catch(()=>{});
params.set('session','B'); context.dashboardSelectedSessionId = 'B';
const second = context.hydrateExploreView('usage');
assert.equal(requests.length, 2);
pending.get('B')(response(200,payload('B')));
await second;
assert.equal(context.D.focused_session_id, 'B');
const renderCount = renders.length;
pending.get('A')(response(input.old_status, input.old_status === 200 ? payload('A') : {state:'preparing'}));
await first;
assert.equal(context.D.focused_session_id, 'B');
assert.equal(context.D.measurements.scope, 'B');
assert.equal(renders.length, renderCount, 'obsolete request must not render');
assert.equal(statuses.usage.hidden, true, 'obsolete failure must not hide the ready new scope');
assert.equal(context.D.sessions, navSessions);
""", old_status=old_status)


def test_clearing_session_hides_old_totals_until_matching_cohort_arrives():
    _run_explore_contract(r"""
context.fetch=async url=>{requests.push(url);return response(200,payload('A'));};
await context.hydrateExploreView('usage');
assert.equal(panels.usage.children[1].hidden,false);
let resolveFetch;
context.fetch=url=>new Promise(resolve=>{requests.push(url);resolveFetch=resolve;});
const cleared=context.clearExploreSessionScope();
assert.equal(params.has('session'),false);
assert.equal(context.dashboardSelectedSessionId,'');
const requested=new URL(requests.at(-1));
assert.equal(requested.searchParams.has('session'),false);
assert.equal(requested.searchParams.get('agents'),'codex');
assert.equal(panels.usage.children[1].hidden,true,'old single-session totals must not appear as all-session totals');
resolveFetch(response(200,payload('')));
await cleared;
assert.equal(context.D.focused_session_id,'');
assert.equal(panels.usage.children[1].hidden,false);
""")


@pytest.mark.parametrize("changed_filter", ["session", "agents"])
def test_explore_ready_state_is_keyed_by_current_scope(changed_filter):
    _run_explore_contract(r"""
context.fetch = async url=>{
  requests.push(url); return response(200,{scope:new URL(url).searchParams.get(input.changed_filter)});
};
await context.hydrateExploreView('tools');
await context.hydrateExploreView('tools');
assert.equal(requests.length, 1, 'ready scope should not refetch');
params.set(input.changed_filter, input.changed_filter === 'session' ? 'B' : 'claude');
await context.hydrateExploreView('tools');
assert.equal(requests.length, 2, 'a changed scope must invalidate ready data');
assert.equal(context.D.sqlite.tabs.tools.scope, input.changed_filter === 'session' ? 'B' : 'claude');
await context.hydrateExploreView('tools');
assert.equal(requests.length, 2);
""", changed_filter=changed_filter)


def test_explore_deduplicates_requests_for_same_view_and_scope():
    _run_explore_contract(r"""
let resolveFetch;
context.fetch = url=>new Promise(resolve=>{requests.push(url);resolveFetch=resolve;});
const first = context.hydrateExploreView('tools');
const second = context.hydrateExploreView('tools');
assert.equal(requests.length, 1);
resolveFetch(response(200,payload('A','tools')));
await Promise.all([first,second]);
assert.equal(context.D.sqlite.tabs.tools.scope, 'A');
assert.equal(statuses.tools.hidden, true);
""")


def test_usage_response_without_authoritative_totals_stays_preparing():
    _run_explore_contract(r"""
context.fetch = async ()=>response(200,{
  sql_backed:true,focused_session_id:'A',sessions:[],sqlite:{tabs:{}},
});
await context.hydrateExploreView('usage');
assert.equal(renders.length, 0);
assert.equal(context.D.focused_session_id, '', 'incomplete response must not replace existing data');
assert.equal(context.D.sessions, navSessions);
assert.equal(statuses.usage.hidden, false);
assert.equal(panels.usage.children[1].hidden, true);
assert.match(statuses.usage.textContent, /loading|prepar/i);
""")


def test_usage_hydration_preserves_missing_navigation_until_first_publication():
    _run_explore_contract(r"""
context.D.sessions=[];context.D._unpublishedNavigation=true;
context.fetch=async()=>response(200,payload('A','usage'));
await context.hydrateExploreView('usage');
assert.equal(context.D.sessions.length,0,'Usage does not supply session navigation');
assert.equal(context.D._unpublishedNavigation,true);
assert.equal(renders.at(-1),'usage');
assert.equal(panels.usage.children[1].hidden,false);
const event={detail:{profile:'snapshot'},preventDefault(){this.defaultPrevented=true;}};
listeners.get('reflect-snapshot-published')(event);
assert.ok(event.defaultPrevented,'first publication refreshes visible Explore in place');
""")


def test_publication_invalidates_prior_scope_and_discards_late_old_payload():
    _run_explore_contract(r"""
let resolveOld, calls=0;
context.activeExploreView='tools';
const publish = profile => {
  const event={detail:{profile},preventDefault(){this.defaultPrevented=true;}};
  listeners.get('reflect-snapshot-published')(event);
  return event;
};
context.fetch = () => {
  calls += 1;
  return calls === 1 ? new Promise(resolve=>{resolveOld=resolve;})
    : Promise.resolve(response(200,payload('A','tools')));
};
const old = context.hydrateExploreView('tools');
assert.equal(publish('usage').defaultPrevented,true,'Explore refreshes in place');
resolveOld(response(200,payload('old-publication','tools')));
await old;
assert.equal(renders.length,1,'only the replacement publication renders');
assert.equal(calls,2);
assert.equal(context.D.sqlite.tabs.tools.scope,'A');
publish('snapshot');
await context.hydrateExploreView('tools');
assert.equal(calls,3,'same scope must load the newly published result');
assert.equal(context.D.sessions,navSessions);
assert.equal(improvementUpdates,1,'findings refresh only after completed analysis');
params.set('tab','sessions');publish('snapshot');
await Promise.resolve();
assert.equal(improvementUpdates,1,'a page reload already refreshes findings');
""")


def test_usage_replacement_preserves_other_views_with_their_own_scope():
    _run_explore_contract(r"""
context.fetch = async url=>{
  requests.push(url);
  const view = new URL(url).pathname === '/api/data' ? 'usage' : 'tools';
  const result=payload(new URL(url).searchParams.get('session'),view);
  if(view === 'usage') result.sqlite.tabs.tools={scope:'incorrect-incidental-usage-tools'};
  return response(200,result);
};
await context.hydrateExploreView('tools');
const originalTools=context.D.sqlite.tabs.tools;
params.set('session','B');
await context.hydrateExploreView('usage');
assert.equal(context.D.sqlite.tabs.tools,originalTools,'Usage cannot overwrite a separately loaded view');
assert.equal(context.D.measurements.scope,'B');
params.set('session','A');
await context.hydrateExploreView('tools');
assert.equal(requests.length, 2, 'the matching tools scope remains available');
assert.equal(context.D.sqlite.tabs.tools.scope, 'A');
params.set('session','B');
await context.hydrateExploreView('tools');
assert.equal(requests.length,3,'a different scope still requires its own response');
assert.equal(context.D.sqlite.tabs.tools.scope,'B');
assert.equal(renders.at(-1), 'tools');
""")


def test_tools_event_chart_keeps_its_session_counts_after_usage_changes():
    source = Path("src/reflect/frontend/js_explore.js").read_text()
    chart = source[source.index("function buildEventBar("):source.index("function renderUsageView(")]
    renderers = source[source.index("function renderUsageView("):source.index("\nrenderUsageView();")]
    renderers += source[source.index("function renderToolsView("):source.index("\nrenderToolsView();")]
    _run_explore_contract(r"""
const charts=[];
const liveCharts=new Map();
const originalGetElement=context.document.getElementById;
context.document.getElementById=id=>id==='event-bar-box'?{style:{}}
  :id==='eventBarChart'?{getContext:()=>({})}:originalGetElement(id);
context.COLORS=['orange'];
context.Chart=class{constructor(canvas,config){charts.push(config.data);liveCharts.set('eventBarChart',config.data);}};
context.renderUsageChartEmpty=()=>assert.fail('Tools event totals should be present');
context.resetUsageChart=id=>liveCharts.delete(id);
for(const name of ['renderUsageSummary','renderMeasurementCoverage','buildCohortComparison','renderUsageMetrics',
  'buildHeatmap','buildHourBars','buildSubagentActivity','buildWeeklyTrends','buildAchievements',
  'buildToolsLists','buildMcpPanel','buildPercentiles','buildCommandPatterns'])context[name]=()=>{};
vm.runInContext(input.chart+input.renderers,context);
context.D.sqlite.tabs.activity={events_by_type:{unrelated_initial:99999}};
context.fetch=async url=>{
  requests.push(url);
  const request=new URL(url),scope=request.searchParams.get('session');
  const view=request.pathname==='/api/data'?'usage':'tools';
  const result=payload(scope,view);
  if(view==='tools')result.events_by_type={tool_call:scope==='A'?2:7};
  else{
    result.sqlite.tabs.activity={events_by_type:{unrelated_usage:88888}};
    result.sqlite.tabs.tools={events_by_type:{unrelated_incidental:77777}};
  }
  return response(200,result);
};
await context.hydrateExploreView('tools');
assert.deepEqual(Array.from(charts.at(-1).labels),['tool_call']);
assert.deepEqual(Array.from(charts.at(-1).datasets[0].data),[2]);
params.set('session','B');
await context.hydrateExploreView('tools');
assert.deepEqual(Array.from(charts.at(-1).datasets[0].data),[7]);
params.set('session','C');
await context.hydrateExploreView('usage');
params.set('session','B');
assert.ok(liveCharts.has('eventBarChart'),'Usage must not destroy the retained Tools chart');
assert.deepEqual(Array.from(liveCharts.get('eventBarChart').labels),['tool_call']);
assert.deepEqual(Array.from(liveCharts.get('eventBarChart').datasets[0].data),[7]);
await context.hydrateExploreView('tools');
assert.equal(requests.length,3,'the retained Tools scope should be reused');
""", chart=chart, renderers=renderers)


def test_active_explore_scope_is_updated_before_cached_or_pending_reads():
    _run_explore_contract(r"""
const scopeUpdates=[];
context.updateExploreScope=()=>scopeUpdates.push(params.get('session') || 'all');
context.fetch=async()=>response(200,payload('A'));
await context.hydrateExploreView('usage');
await context.hydrateExploreView('usage');
params.set('session','B');
context.fetch=async()=>response(503,{state:'preparing'});
await context.hydrateExploreView('usage');
assert.deepEqual(scopeUpdates,['A','A','B'],'scope remains current even when data is cached or not ready');
await context.hydrateExploreView('tools');
assert.deepEqual(scopeUpdates,['A','A','B'],'an inactive read cannot relabel the visible scope');
""")


def test_same_scope_keeps_published_content_through_retry_but_new_scope_hides_it():
    _run_explore_contract(r"""
context.fetch=async()=>response(200,{...payload('A','tools'),snapshot_completed_at:'2026-11-08T09:00:00Z'});
await context.hydrateExploreView('tools');
vm.runInContext('explorePublication += 1',context);
let resolveNext;
context.fetch=()=>new Promise(resolve=>{resolveNext=resolve;});
const loading=context.hydrateExploreView('tools');
assert.equal(panels.tools.children[1].hidden,false,'same-scope content remains visible while fetching');
assert.match(statuses.tools.textContent,/Showing data from/);
resolveNext(response(503,{state:'preparing'}));await loading;
assert.equal(panels.tools.children[1].hidden,false);
assert.equal(context.D.sqlite.tabs.tools.scope,'A');
assert.match(statuses.tools.textContent,/Showing data from.*Loading/);
params.set('session','B');const changed=context.hydrateExploreView('tools');
assert.equal(panels.tools.children[1].hidden,true,'old scope must not appear below new filters');
resolveNext(response(503,{state:'preparing'}));await changed;
assert.equal(panels.tools.children[1].hidden,true);
params.set('session','A');
context.fetch=async()=>response(200,payload('updated-A','tools'));
await context.hydrateExploreView('tools');
assert.equal(context.D.sqlite.tabs.tools.scope,'updated-A');
assert.equal(statuses.tools.hidden,true);
assert.equal(panels.tools.children[1].hidden,false);
""")


def test_server_cached_scope_is_visible_as_stale_and_revalidated():
    _run_explore_contract(r"""
context.fetch=async()=>response(200,{...payload('A','tools'),analysis_state:'stale',
  publication:{state:'stale',loaded_at:'2026-11-08T09:00:00Z',reason:'preparing'}});
await context.hydrateExploreView('tools');
assert.equal(panels.tools.children[1].hidden,false);
assert.match(statuses.tools.textContent,/Showing data from.*last published/);
assert.equal(statuses.tools.hidden,false);
context.fetch=async()=>response(200,payload('updated-A','tools'));
await context.hydrateExploreView('tools');
assert.equal(context.D.sqlite.tabs.tools.scope,'updated-A');
assert.equal(statuses.tools.hidden,true);
""")


def test_usage_revalidation_keeps_the_same_retained_response_mounted():
    _run_explore_contract(r"""
let now=10000;
context.Date=class extends Date{static now(){return now;}};
const stale={...payload('A'),analysis_state:'stale',publication:{state:'stale',reason:'reading',
  snapshot_completed_at:'2026-11-08T08:00:00Z',loaded_at:'2026-11-08T09:00:00Z'}};
context.fetch=async url=>{requests.push(url);return response(200,JSON.parse(JSON.stringify(stale)));};
await context.hydrateExploreView('usage');
assert.deepEqual(renders,['usage']);
now+=1000;
await context.hydrateExploreView('usage',{poll:true});
assert.equal(requests.length,1,'a status tick must not immediately reread the scope');
now+=5000;
await context.hydrateExploreView('usage',{poll:true});
assert.equal(requests.length,2);
assert.deepEqual(renders,['usage'],'the same retained read must not rebuild the widgets');
assert.equal(panels.usage.children[1].hidden,false);
context.fetch=async url=>{requests.push(url);return response(200,payload('updated'));};
const event={preventDefault(){},detail:{profile:'snapshot'}};
listeners.get('reflect-snapshot-published')(event);await new Promise(setImmediate);
assert.equal(requests.length,3,'new snapshots bypass the poll delay');
assert.deepEqual(renders,['usage','usage']);
assert.equal(statuses.usage.hidden,true);
""")


@pytest.mark.parametrize("retained_state", ["stale", "recorded"])
@pytest.mark.parametrize("pending_state", ["preparing", "busy"])
def test_background_reads_retry_on_visible_explore_until_ready(retained_state, pending_state):
    source = Path("src/reflect/frontend/js_ledger.js").read_text()
    polling = source[source.index("document.addEventListener('reflect-status-polled', () => {"):
                     source.index("const workflowDateFormatter =")]
    _run_explore_contract(r"""
context.activeExploreView='tools';context.document.visibilityState='visible';
let now=10000;context.Date=class extends Date{static now(){return now;}};
let tick;context.document.addEventListener=(name,fn)=>{assert.equal(name,'reflect-status-polled');tick=fn;};
vm.runInContext(input.polling,context);
const publishedAt='2020-01-01T00:00:00Z';
context.fetch=async url=>{
  requests.push(url);
  if(requests.length===1)return response(503,{state:input.pending_state,reason:'reading',error:'Tool inventory is preparing in the background.'});
  if(requests.length===2)return response(200,{...payload('A','tools'),
    analysis_state:input.retained_state==='recorded'?'pending':'stale',
    publication:{state:input.retained_state,reason:input.retained_state==='stale'?'reading':'preparing',
      snapshot_completed_at:input.retained_state==='stale'?publishedAt:null,loaded_at:'2026-11-08T09:00:00Z'}});
  return response(200,payload('A','tools'));
};
await context.hydrateExploreView('tools');
assert.match(statuses.tools.textContent,/Loading command and tool patterns/);
assert.equal(vm.runInContext("exploreViewStates.get('tools')",context),'preparing');
assert.equal(statuses.tools.classList.contains('error'),false);
assert.equal(panels.tools.children[1].hidden,true);
context.dashboardDocumentActive=false;tick();await context.loadExploreView('tools');
assert.equal(requests.length,1,'unloading documents cannot start timer or direct Explore reads');
context.dashboardDocumentActive=true;
context.document.visibilityState='hidden';tick();assert.equal(requests.length,1);
context.document.visibilityState='visible';params.set('tab','sessions');tick();assert.equal(requests.length,1);
params.set('tab','explore');now+=5000;tick();await new Promise(setImmediate);
assert.equal(requests.length,2);assert.equal(panels.tools.children[1].hidden,false);
assert.equal(statuses.tools.hidden,false);
if(input.retained_state==='stale'){
  assert.match(statuses.tools.textContent,/Reading the current selection in the background/);
  assert.ok(statuses.tools.textContent.includes(new Date(publishedAt).toLocaleString()));
}else assert.match(statuses.tools.textContent,/Showing stored telemetry read/);
now+=5000;tick();await new Promise(setImmediate);
assert.equal(requests.length,3);assert.equal(statuses.tools.hidden,true);
tick();await new Promise(setImmediate);
assert.equal(requests.length,3,'ready same-revision scopes stop polling the API');
params.set('session','B');
let resolveNext;context.fetch=url=>{requests.push(url);return new Promise(resolve=>{resolveNext=resolve;});};
tick();assert.equal(panels.tools.children[1].hidden,true,'a different scope must hide the prior read');
assert.equal(new URL(requests.at(-1)).searchParams.get('session'),'B');
resolveNext(response(200,payload('B','tools')));await new Promise(setImmediate);
assert.equal(context.D.sqlite.tabs.tools.scope,'B');
assert.equal(statuses.tools.hidden,true);assert.equal(panels.tools.children[1].hidden,false);
""", retained_state=retained_state, pending_state=pending_state, polling=polling)


@pytest.mark.parametrize("view", ["tools", "graph", "context"])
def test_recorded_explore_data_stays_visible_and_retryable_without_publication_claim(view):
    _run_explore_contract(r"""
context.D._unpublishedNavigation=true;
const readAt='2026-11-08T09:00:00Z';
context.fetch=async url=>{requests.push(url);return response(200,{
  ...payload(input.view === 'context' ? 'global' : 'A',input.view),
  analysis_state:'pending',evidence_state:'recorded',snapshot_completed_at:'2020-01-01T00:00:00Z',
  publication:{state:'recorded',loaded_at:readAt,snapshot_completed_at:null,reason:'preparing'}});};
await context.hydrateExploreView(input.view);
assert.equal(panels[input.view].children[1].hidden,false);
assert.equal(renders.at(-1),input.view);
assert.match(statuses[input.view].textContent,/Showing stored telemetry read/);
assert.ok(statuses[input.view].textContent.includes(new Date(readAt).toLocaleString()));
assert.ok(!statuses[input.view].textContent.includes('2020'));
assert.ok(!statuses[input.view].textContent.includes('last published'));
assert.match(statuses[input.view].textContent,/Analysis is pending/);
assert.ok(!statuses[input.view].children.some(child=>child.textContent === 'Retry'));
context.fetch=async url=>{requests.push(url);return response(503,{state:'preparing'});};
await context.hydrateExploreView(input.view);
assert.equal(requests.length,2,'recorded data must not stop revalidation');
assert.equal(panels[input.view].children[1].hidden,false);
assert.match(statuses[input.view].textContent,/Showing stored telemetry read/);
assert.ok(!statuses[input.view].textContent.includes('last published'));
context.fetch=async()=>response(200,payload('ready',input.view));
await context.hydrateExploreView(input.view);
assert.equal(statuses[input.view].hidden,true);
assert.equal(panels[input.view].children[1].hidden,false);
""", view=view)


def test_recorded_graph_without_read_time_keeps_scope_and_historical_refresh_hint():
    _run_explore_contract(r"""
context.fetch=async()=>response(200,{...payload('A','graph'),
  publication:{state:'recorded',reason:'historical_pending'}});
await context.hydrateExploreView('graph');
assert.match(statuses.graph.textContent,/stored telemetry; read time unavailable/);
assert.match(statuses.graph.textContent,/explicit historical refresh/);
assert.ok(!statuses.graph.textContent.includes('last published'));
assert.ok(!statuses.graph.textContent.includes('1970'));
assert.ok(!statuses.graph.textContent.includes('Invalid Date'));
params.set('session','B');
let resolveNext;context.fetch=()=>new Promise(resolve=>{resolveNext=resolve;});
const changed=context.hydrateExploreView('graph');
assert.equal(panels.graph.children[1].dataset.presentation,'placeholder','recorded data must not cross scopes');
resolveNext(response(503,{state:'preparing'}));await changed;
assert.equal(panels.graph.children[1].dataset.presentation,'placeholder');
assert.ok(!statuses.graph.textContent.includes('Showing stored telemetry'));
""")


def test_unpublished_navigation_allows_actual_renderers_only_after_exact_scope_load():
    explore = Path("src/reflect/frontend/js_explore.js").read_text()
    impact = Path("src/reflect/frontend/js_impact.js").read_text()
    renderers = explore[explore.index("function renderToolsView(){"):
                        explore.index("\nrenderToolsView();")]
    renderers += impact[impact.index("function renderSqlTabPayloads(){"):
                        impact.index("\nrenderSqlTabPayloads();")]
    _run_explore_contract(r"""
context.D._unpublishedNavigation=true;
let toolsRenders=0;
context.buildToolsLists=()=>{toolsRenders++;};
context.resetUsageChart=()=>{};context.buildEventBar=()=>{};
context.buildMcpPanel=()=>{};context.buildPercentiles=()=>{};context.buildCommandPatterns=()=>{};
context.fmt=String;context.escHtml=String;
const inventoryElements=Object.fromEntries(['data-stat-row','sql-specs-panel','sql-memory-panel',
  'sql-privacy-panel','sql-exports-panel'].map(id=>[id,{innerHTML:''}]));
const previousGet=context.document.getElementById;
context.document.getElementById=id=>inventoryElements[id] || previousGet(id);
vm.runInContext(input.renderers,context);
context.renderToolsView();context.renderSqlTabPayloads();
assert.equal(toolsRenders,0);assert.equal(inventoryElements['data-stat-row'].innerHTML,'');
context.fetch=async url=>response(200,new URL(url).pathname.endsWith('/tools')
  ? {...payload('A','tools'),publication:{state:'stale'}}
  : {memory:{total_memories:2,recent_memories:[{preview:'Recorded inventory'}]},
     publication:{state:'recorded',loaded_at:'2026-11-08T09:00:00Z'},analysis_state:'pending'});
await context.hydrateExploreView('tools');
assert.equal(toolsRenders,1,'the retained Tools renderer must run on a cold dashboard');
params.set('session','B');context.renderToolsView();
assert.equal(toolsRenders,1,'a renderer must not reuse another selected session');
await context.hydrateExploreView('context');
assert.match(inventoryElements['sql-memory-panel'].innerHTML,/Recorded inventory/);
assert.match(inventoryElements['data-stat-row'].innerHTML,/>2<\/div>/);
assert.equal(panels.context.children[1].hidden,false);
""", renderers=renderers)


def test_explore_timeout_restores_inline_retry():
    _run_explore_contract(r"""
context.fetch = (url,options)=>new Promise((resolve,reject)=>{
  options.signal.addEventListener('abort',()=>{
    const error = new Error('aborted'); error.name = 'AbortError'; reject(error);
  });
});
const request = context.hydrateExploreView('tools');
assert.equal(panels.tools.attributes['aria-busy'], 'true');
timers.at(-1).fn();
await request;
assert.equal(panels.tools.attributes['aria-busy'], 'false');
assert.equal(renders.length, 0);
assert.match(statuses.tools.textContent, /too long|timed out|timeout/i);
assert.ok(statuses.tools.children.some(child=>child.textContent === 'Retry'));
assert.ok(cleared.length > 0);
""")


def test_retained_scope_without_timestamp_never_invents_a_publication_date():
    _run_explore_contract(r"""
context.fetch=async()=>response(200,{...payload('A','tools'),publication:{state:'stale'}});
await context.hydrateExploreView('tools');
assert.equal(panels.tools.children[1].hidden,false);
assert.match(statuses.tools.textContent,/last published data; timestamp unavailable/);
assert.ok(!statuses.tools.textContent.includes('1970'));
assert.ok(!statuses.tools.textContent.includes('Invalid Date'));
""")


def test_inbox_keeps_loaded_evidence_until_derived_publication():
    _run_explore_contract(r"""
params.set('tab','inbox');
const finding={id:'retained'};
context.IMPROVEMENT_DATA={observations:[finding],resources:{findings:{state:'ready',loaded_at:'earlier'}}};
let rendered=0;
context.buildImprovementSurfaces=()=>{rendered+=1;};
const event={detail:{profile:'usage'},preventDefault(){this.defaultPrevented=true;}};
listeners.get('reflect-snapshot-published')(event);
assert.equal(event.defaultPrevented,true,'core publication must not clear the loaded Inbox');
assert.equal(context.IMPROVEMENT_DATA.observations[0],finding);
assert.equal(context.IMPROVEMENT_DATA.resources.findings.state,'preparing');
assert.equal(rendered,1);assert.equal(improvementUpdates,0);
const complete={detail:{profile:'snapshot'},preventDefault(){this.defaultPrevented=true;}};
listeners.get('reflect-snapshot-published')(complete);
await Promise.resolve();
assert.equal(complete.defaultPrevented,true);
assert.equal(improvementUpdates,1,'full publication refreshes data in place');
""")


def test_graph_keeps_frames_and_recovers_from_a_widget_retry():
    _run_explore_contract(r"""
const panel=panels.graph;
const widget=panel.children[1];
let finish;
context.fetch=url=>{requests.push(url);return new Promise(resolve=>{finish=resolve;});};
const initial=context.hydrateExploreView('graph');
const notice=widget.querySelector('.graph-widget-notice');
const content=widget.querySelector('.graph-widget-content');
const retry=notice.querySelector('button');
assert.equal(widget.hidden,false);
assert.equal(widget.dataset.presentation,'placeholder');
assert.equal(widget.attributes['aria-busy'],'true');
assert.ok(Object.hasOwn(content.attributes,'inert'));
assert.equal(retry.hidden,true);
finish(response(500,{error:'failed'}));
await initial;
assert.equal(retry.hidden,false);
await context.hydrateExploreView('graph');
assert.equal(requests.length,1,'status polling leaves a failed read available for explicit retry');
retry.listeners.click();
assert.equal(retry.hidden,true);
finish(response(200,payload('A','graph')));
await new Promise(resolve=>setImmediate(resolve));
assert.equal(widget.hidden,false);
assert.equal(widget.dataset.presentation,'data');
assert.equal(renders.at(-1),'graph');
assert.ok(!Object.hasOwn(content.attributes,'inert'));

vm.runInContext('explorePublication += 1',context);
const updating=context.hydrateExploreView('graph');
assert.equal(widget.dataset.presentation,'data','same-scope refresh retains the rendered result');
assert.match(notice.textContent,/Updating/);
finish(response(200,payload('A','graph')));
await updating;
params.set('agents','cursor');
const changed=context.hydrateExploreView('graph');
assert.equal(widget.dataset.presentation,'placeholder','different filters must not show the previous scope');
finish(response(200,payload('B','graph')));
await changed;
assert.equal(widget.dataset.presentation,'data');
assert.equal(context.D.sqlite.tabs.graph.scope,'B');
""")
