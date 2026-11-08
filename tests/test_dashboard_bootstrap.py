"""Exercise initial browser loading against the preparation HTTP contract."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_document_navigation_stops_new_reads_and_resumes_one_status_poll():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for browser loading contract checks")
    source = Path("src/reflect/frontend/js_bootstrap.js").read_text()
    lifecycle = source[source.index("let preparationStatusPollTimer ="):
                       source.index("const PREPARATION_PHASES")]
    lifecycle += source[source.index("async function pollPreparationStatus()"):
                        source.index("async function loadDashboardData()")]
    lifecycle += source[source.index("function updateUrlParams("):
                        source.index("function reportSupportsServerFiltering(")]
    sessions = Path("src/reflect/frontend/js_sessions.js").read_text()
    reload = sessions[sessions.index("  function scheduleDashboardReload()"):
                      sessions.index("  function isMobileShell()")]
    script = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const listeners=new Map(),timers=new Map(),calls=[],pending=[];
let timerId=0,replacements=0;
const context={URL,URLSearchParams,D:{sql_backed:true},reloadTimer:null,
  window:{location:{href:'http://localhost/?tab=sessions',search:'?tab=sessions',reload:()=>{}},
    addEventListener:(name,listener)=>listeners.set(name,listener),
    setTimeout:(fn,ms)=>{const id=++timerId;timers.set(id,{fn,ms});return id;},
    clearTimeout:id=>timers.delete(id)},
  history:{replaceState:()=>{replacements++;}},
  currentParams:()=>new URLSearchParams('tab=sessions'),
  reportSupportsServerFiltering:()=>true,showReportLoader:()=>{},
  renderPreparationStatus:()=>false,renderRawStorageStatus:()=>{},updatePreparationStatus:()=>{},
  fetch:url=>{calls.push(url);return new Promise(resolve=>pending.push(resolve));}};
context.window.history=context.history;
vm.createContext(context);vm.runInContext(input.lifecycle+'\n'+input.reload,context);
const active=()=>vm.runInContext('dashboardDocumentActive',context);
const finish=body=>pending.shift()({ok:true,json:async()=>body});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  context.updateUrlParams(params=>params.set('session','one'));
  assert.equal(replacements,1);assert.ok(active(),'history-only selection keeps reads active');
  const refresh=context.requestDashboardRefresh();
  listeners.get('pagehide')({persisted:true});
  finish({refresh_available:true,automatic_refresh:{enabled:false},preparation:{state:'idle'}});
  await refresh;
  await context.requestDashboardRefresh();await context.pollPreparationStatus();context.startPreparationStatusPolling();
  assert.deepEqual(calls,['/api/status'],'navigation suppresses follow-up refresh and status reads');
  listeners.get('pageshow')({persisted:true});
  assert.equal(calls.length,2);
  listeners.get('pagehide')({persisted:true});
  listeners.get('pageshow')({persisted:true});
  assert.equal(calls.length,2,'restoration does not duplicate an in-flight poll');
  finish({preparation:{state:'idle'}});await flush();
  assert.equal(timers.size,1,'restored status read keeps exactly one timer chain');
  const staleTimer=[...timers.values()][0];
  listeners.get('pagehide')({persisted:true});
  assert.equal(timers.size,0);
  await staleTimer.fn();assert.equal(calls.length,2,'already queued timers cannot read after pagehide');
  listeners.get('pageshow')({persisted:true});
  finish({preparation:{state:'idle'}});await flush();
  assert.equal(calls.length,3);assert.equal(timers.size,1);
  assert.equal(context.scheduleDashboardReload(),true);
  assert.equal(active(),false,'filter navigation suspends reads before the reload delay');
  assert.equal(timers.size,1);assert.equal([...timers.values()][0].ms,120);
  await context.pollPreparationStatus();await context.requestDashboardRefresh();
  assert.equal(calls.length,3);
})().then(()=>process.stdout.write('lifecycle-ok\n'))
  .catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", script], input=json.dumps({"lifecycle": lifecycle, "reload": reload}),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "lifecycle-ok" in result.stdout


@pytest.mark.parametrize("state", ["running", "failed", "unavailable"])
def test_initial_report_waits_for_preparation_and_surfaces_failure(state):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for browser loading contract checks")
    source = Path("src/reflect/frontend/js_bootstrap.js").read_text()
    loader = source[source.index("async function loadDashboardData()"):
                    source.index("async function loadImprovementData(")]
    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const calls = [], messages = [], errors = [];
let hidden = false;
const response = (status, body) => ({status, ok:status === 200, json:async () => body});
const queue = [response(503, {state:input.state === 'unavailable' ? 'unavailable' : 'preparing'})];
if (input.state !== 'unavailable') {
  queue.push(response(200, {preparation:{state:input.state, message:'Normalizing 5 events', error:'Invalid evidence'}}));
}
if (input.state === 'running') queue.push(response(200, {sessions:[{id:'ready'}]}));
const context = {
  URLSearchParams, AbortController, dashboardDocumentActive:true,
  window:{location:{search:'?report=/api/data'},
    setTimeout:(fn, ms) => ms === 2000 ? setImmediate(fn) : 0, clearTimeout:() => {}},
  showReportLoader:message => messages.push(message), hideReportLoader:() => {hidden = true;},
  renderDashboardError:(kind, message) => errors.push(message),
  reportUrlWithCurrentFilters:() => '/api/data?agents=cursor',
  formatPreparationElapsed:() => '2s elapsed',
  fetch:async url => {calls.push(url); assert.ok(queue.length, 'unexpected fetch'); return queue.shift();},
};
vm.createContext(context);
vm.runInContext(input.loader, context);
(async () => {
  if (input.state === 'running') {
    const result = await context.loadDashboardData();
    assert.equal(result.sessions[0].id, 'ready');
    assert.deepEqual(calls, ['/api/data?agents=cursor', '/api/status', '/api/data?agents=cursor']);
    assert.ok(messages.includes('Normalizing 5 events · 2s elapsed'));
    assert.equal(errors.length, 0);
  } else {
    await assert.rejects(context.loadDashboardData(),
      input.state === 'failed' ? /Invalid evidence/ : /report-fetch-failed/);
    assert.equal(errors.length, 1);
  }
  assert.ok(hidden);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    subprocess.run(
        [node, "-e", script], input=json.dumps({"state": state, "loader": loader}),
        text=True, capture_output=True, check=True,
    )


@pytest.mark.parametrize("case", ["preparing", "reading", "busy_reading", "skills_reading", "hung_fetch", "hung_body"])
def test_filtered_bootstrap_retries_quick_pending_responses_with_per_request_deadlines(case):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for browser loading contract checks")
    source = Path("src/reflect/frontend/js_bootstrap.js").read_text()
    loader = source[source.index("async function loadDashboardData()"):
                    source.index("async function loadImprovementData(")]
    script = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const reading=['reading','busy_reading','skills_reading'].includes(input.case);
const tab=input.case==='skills_reading' ? 'skills' : 'sessions';
const timers=new Map(),signals=[],calls=[],messages=[],errors=[];
let now=0,nextTimer=0,reads=0,hidden=false;
const query='/api/data?tab='+tab+'&q=long-lived&range=7d';
function advance(ms){now+=ms;for(const [id,timer] of [...timers])if(timer.at<=now){timers.delete(id);timer.fn();}}
const response=(status,body)=>({status,ok:status===200,json:async()=>body});
const hung=signal=>new Promise((_resolve,reject)=>{
  signal.addEventListener('abort',()=>{const error=new Error('Fetch aborted');error.name='AbortError';reject(error);});
  setImmediate(()=>advance(30001));
});
const context={URLSearchParams,AbortController,dashboardDocumentActive:true,
  window:{location:{search:'?report=/api/data&tab='+tab+'&q=long-lived&range=7d'},
    setTimeout:(fn,ms)=>{const id=++nextTimer;timers.set(id,{at:now+ms,fn});
      if(ms===2000)setImmediate(()=>advance(ms));return id;},
    clearTimeout:id=>timers.delete(id)},
  showReportLoader:message=>messages.push(message),hideReportLoader:()=>{hidden=true;},
  renderDashboardError:(_kind,message)=>errors.push(message),
  reportUrlWithCurrentFilters:()=>query,formatPreparationElapsed:()=>'',
  fetch:async(url,{signal})=>{
    calls.push(url);advance(50);
    if(url==='/api/status')return response(200,{preparation:{state:reading?'failed':'running',error:'Unrelated analysis failure'}});
    assert.equal(url,query,'the exact filtered scope survives every retry');
    signals.push(signal);reads++;
    if(input.case==='hung_fetch')return hung(signal);
    if(input.case==='hung_body')return {ok:true,status:200,json:()=>hung(signal)};
    if(reads<=20)return response(503,{state:input.case==='busy_reading'?'busy':'preparing',reason:reading?'reading':undefined,error:'Selected sessions are loading in the background.'});
    return response(200,{sessions:[{id:'long-lived'}]});
  }};
vm.createContext(context);vm.runInContext(input.loader,context);
(async()=>{
  if(input.case.startsWith('hung_')){
    await assert.rejects(context.loadDashboardData(),{name:'AbortError'});
    assert.equal(reads,1);assert.equal(errors.length,1);assert.ok(signals[0].aborted);
  }else{
    const result=await context.loadDashboardData();
    assert.equal(result.sessions[0].id,'long-lived');assert.equal(reads,21);
    assert.ok(now>30000,'pending jobs can take longer than one request deadline');
    assert.equal(new Set(signals).size,21,'every retry owns a new deadline');
    assert.ok(signals.every(signal=>!signal.aborted));assert.equal(errors.length,0);
    if(reading){
      assert.ok(!calls.includes('/api/status'),'background reads are independent of analysis failure');
      assert.ok(messages.includes('Selected sessions are loading in the background.'));
    }
  }
  assert.ok(hidden);assert.equal(timers.size,0,'request deadlines are cleared after completion');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", script], input=json.dumps({"case": case, "loader": loader}),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.parametrize("case", ["cached", "absent", "filtered", "explore", "explore_filtered"])
def test_session_link_can_navigate_completed_cache_without_claiming_scoped_totals(case):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for browser loading contract checks")
    source = Path("src/reflect/frontend/js_bootstrap.js").read_text()
    loader = source[source.index("async function loadDashboardData()"):
                    source.index("async function loadImprovementData(")]
    script = r"""
const assert = require('node:assert/strict'), vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const params = new URLSearchParams('report=/api/data&tab=sessions&session=selected');
if (input.case === 'filtered') params.set('agents', 'codex');
if (input.case.startsWith('explore')) params.set('tab', 'explore');
if (input.case === 'explore_filtered') params.set('agents', 'cursor');
const usesCache = true;
const calls = [];
const response = (status, body) => ({status, ok:status === 200, json:async () => body});
const cached = {focused_session_id:'', sessions:[{full_id:input.case === 'absent' ? 'other' : 'selected'}],
  measurements:{totals:{input_tokens:500}}};
const scoped = {focused_session_id:'selected', sessions:[{full_id:'selected'}],
  measurements:{totals:{input_tokens:7}}};
const queue = [response(503, {state:'preparing'})];
if (usesCache || input.case === 'absent') queue.push(response(200, cached));
if (!usesCache) queue.push(response(200, {preparation:{state:'running'}}), response(200, scoped));
const context = {
  URL, URLSearchParams, AbortController, dashboardDocumentActive:true,
  window:{location:{search:'?'+params, href:'http://localhost/?'+params},
    setTimeout:(fn,ms) => ms === 2000 ? setImmediate(fn) : 0, clearTimeout:()=>{}},
  showReportLoader:()=>{}, hideReportLoader:()=>{}, renderDashboardError:()=>{},
  reportSupportsServerFiltering:()=>true, reportUrlWithCurrentFilters:()=>'/api/data?session=selected',
  formatPreparationElapsed:()=>'', updateUrlParams:fn=>fn(params),
  fetch:async url=>{calls.push(url); assert.ok(queue.length); return queue.shift();},
};
vm.createContext(context); vm.runInContext(input.loader, context);
(async()=>{
  const result = await context.loadDashboardData();
  if (usesCache) {
    assert.equal(result, cached);
    assert.equal(result.focused_session_id, '');
    assert.equal(params.get('tab'), input.case.startsWith('explore') ? 'explore' : 'sessions');
    assert.equal(result._navigationFromCache, true);
    assert.equal(result._unfilteredNavigation, true);
    assert.equal(params.get('session'), 'selected');
    assert.deepEqual(calls, ['/api/data?session=selected', 'http://localhost/api/data']);
  } else {
    assert.equal(result, scoped);
    assert.equal(result.measurements.totals.input_tokens, 7);
    if (input.case !== 'absent') assert.ok(!calls.includes('http://localhost/api/data'));
  }
  assert.equal(queue.length, 0);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    subprocess.run([node, "-e", script], input=json.dumps({"case": case, "loader": loader}),
                   text=True, capture_output=True, check=True)


def test_explore_navigation_hydrates_in_place_with_selected_scope():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for browser navigation checks")
    source = Path("src/reflect/frontend/js_navigation.js").read_text()
    navigation = source[source.index("function activateTab("):
                        source.index("tabButtons.forEach((button, index)")]
    script = r"""
const assert = require('node:assert/strict'), vm = require('node:vm');
const source = JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const params = new URLSearchParams('tab=sessions&session=selected');
const hydrated = [];
const button = {classList:{toggle:()=>{}}, setAttribute:()=>{}};
const panel = {id:'tab-explore-usage', classList:{toggle:()=>{}}, hidden:true};
const context = {activeExploreView:'usage', D:{sql_backed:true, focused_session_id:''},
  tabButtons:[button], updateTabIndicator:()=>{}, animateTabIndicator:()=>{},
  currentParams:()=>params, canonicalDashboardLocation:(tab,view)=>({tab,view}),
  updateUrlParams:fn=>fn(params),
  showReportLoader:()=>{throw new Error('must not block whole dashboard');},
  hydrateExploreView:view=>{hydrated.push([view,params.get('session'),params.get('tab')]);return Promise.resolve();},
  window:{location:{reload:()=>{throw new Error('must not reload');}}},
  document:{querySelector:()=>button, querySelectorAll:selector=>selector === '.tab-panel' ? [panel] : []},
};
vm.createContext(context); vm.runInContext(source, context);
context.activateTab('explore');
assert.deepEqual(hydrated,[['usage','selected','explore']]);
assert.equal(panel.hidden,false); assert.equal(params.get('session'),'selected');
assert.equal(params.get('tab'),'explore'); assert.equal(params.get('view'),'usage');
"""
    subprocess.run([node, "-e", script], input=json.dumps(navigation),
                   text=True, capture_output=True, check=True)


@pytest.mark.parametrize("handled", [True, False])
def test_publication_revalidates_active_reader_without_forcing_reload(handled):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for publication contract checks")
    source = Path("src/reflect/frontend/js_bootstrap.js").read_text()
    renderer = source[source.index("function renderPreparationStatus("):
                      source.index("async function pollPreparationStatus(")]
    script = r"""
const assert = require('node:assert/strict'), vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const storage = new Map(), events = [], scheduled = [], notices = [];
const context = {PREPARATION_PHASES:{complete:5},PREPARATION_PHASE_LABELS:[],
  suspendDashboardReads:()=>{},
  updatePreparationStatus:notice=>notices.push(notice),formatNextRefresh:()=>'',
  CustomEvent:class {constructor(type,options){this.type=type;Object.assign(this,options);}},
  document:{dispatchEvent:event=>{events.push(event);return !input.handled;}},
  window:{sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)},
    setTimeout:(fn,delay)=>scheduled.push(delay),location:{reload:()=>{}}}};
vm.createContext(context);vm.runInContext(input.renderer,context);
context.renderPreparationStatus({state:'complete',finished_at:'2026-02-01'},{});
context.renderPreparationStatus({state:'complete',finished_at:'2026-02-01'},{});
assert.equal(events.length,1);
assert.equal(events[0].type,'reflect-snapshot-published');
assert.equal(events[0].cancelable,true);
assert.equal(events[0].detail.completion,'2026-02-01');
assert.equal(events[0].detail.profile,'snapshot');
assert.deepEqual(scheduled,input.handled ? [] : [450]);
context.D={_unpublishedNavigation:true};
assert.equal(context.renderPreparationStatus({state:'idle'},{}),false);
assert.equal(notices.at(-1).kicker,'Report not loaded');
assert.match(notices.at(-1).message,/Published report is not loaded.*Captured session activity is available/);
assert.ok(!/ready|refreshing|waiting/i.test(notices.at(-1).message));
context.D={};
context.renderPreparationStatus({state:'idle'},{});
assert.equal(notices.at(-1).kicker,'Telemetry ready');
"""
    subprocess.run([node, "-e", script], input=json.dumps({"renderer": renderer, "handled": handled}),
                   text=True, capture_output=True, check=True)


@pytest.mark.parametrize("initial_tab", ["session_detail", "skills", "skills_empty", "workflows", "impact", "inbox"])
def test_complete_bootstrap_opens_captured_session_before_first_published_report(initial_tab):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for complete dashboard initialization")
    frontend = Path("src/reflect/frontend")
    bundle = "\n".join((frontend / name).read_text() for name in (
        "js_bootstrap.js", "js_ledger.js", "js_navigation.js", "js_sessions.js",
        "js_impact.js", "js_explore.js", "js_graphs.js",
    ))
    # Surface initialization errors instead of the packaged top-level catch.
    bundle = bundle[:bundle.rindex("initApp().catch(")] + "globalThis.appReady = initApp();"
    script = r"""
const assert = require('node:assert/strict'), vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const bundle = input.bundle, independent = input.initial_tab !== 'session_detail';
const empty = input.initial_tab === 'skills_empty', initialTab = empty ? 'skills' : input.initial_tab;
const elements = new Map(), listeners = new Map(), requests = [];
class Element {
  constructor(id='') {
    this.id=id;this.children=[];this.dataset={};this.style={setProperty:()=>{}};
    this.hidden=false;this.value='all';this.options=[{textContent:'All'}];this.selectedIndex=0;
    this.clientWidth=800;this.scrollLeft=0;this.scrollTop=0;this.attributes={};
    this.firstChild={textContent:'0'};
    const classes = new Set();
    this.classList={contains:name=>classes.has(name),add:name=>classes.add(name),
      remove:name=>classes.delete(name),toggle:(name,on)=>on ? classes.add(name) : classes.delete(name)};
  }
  set innerHTML(value){this.html=value;this.children=[];}
  get innerHTML(){return this.html || '';}
  set textContent(value){this.text=value;this.children=[];}
  get textContent(){return this.text || '';}
  setAttribute(name,value){this.attributes[name]=value;}
  appendChild(child){child.parentElement=this;this.children.push(child);}
  prepend(child){child.parentElement=this;this.children.unshift(child);}
  addEventListener(name,fn){(this.listeners ||= {})[name]=fn;}
  querySelector(selector){return this.children.find(child=>selector === '.'+child.className) || null;}
  querySelectorAll(){return [];}
  getBoundingClientRect(){return {left:0,width:100,top:0,bottom:100,height:100};}
  closest(){return null;}
}
function element(id){if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);}
const names=['sessions','inbox','workflows','skills','impact','explore'];
const tabs=names.map(name=>{const item=element('button-'+name);item.dataset.tab=name;return item;});
const panels=names.filter(name=>name!=='explore').map(name=>element('tab-'+name));
for(const view of ['usage','tools','graph','context']) {
  const panel=element('tab-explore-'+view);panels.push(panel);
  panel.appendChild(element(view+'-view-status'));panel.appendChild(element(view+'-content'));
}
const location={href:'http://localhost:4000/?report=/api/data&session=first&tab=explore&view=usage',
  search:'?report=/api/data&session=first&tab=explore&view=usage',hostname:'localhost',
  origin:'http://localhost:4000',pathname:'/',reload:()=>{throw new Error('unexpected reload');}};
if(independent){
 const url=new URL(location.href);url.search='?report=/api/data'+(empty ? '' : '&session=first')+'&tab='+initialTab+'&range=all&agents=codex&q=needle&model=model-a&status=completed&sort=cost';
 location.href=url.href;location.search=url.search;
}
const response=(status,body)=>({status,ok:status===200,json:async()=>body});
const captured={session:{id:'first',agent:'codex'},derived_state:'preparing',
  conversation:[{type:'response',preview:'Captured activity before first publication',ts:1}],
  telemetry:{spans:[]},tool_inventory:{tools:[]}};
const context={URL,URLSearchParams,AbortController,console,navigator:{language:'en-US'},
  CustomEvent:class{constructor(type,opts={}){this.type=type;Object.assign(this,opts);}preventDefault(){this.defaultPrevented=true;}},
  Chart:class{static defaults={};constructor(){throw new Error('unpublished aggregates must not create charts');}},
  document:{body:element('body'),getElementById:element,createElement:()=>new Element(),
    querySelector:selector=>selector.startsWith('.tab[data-tab=') ? tabs.find(tab=>selector.includes('"'+tab.dataset.tab+'"')) : null,
    querySelectorAll:selector=>selector==='.tab' ? tabs : selector==='.tab-panel' ? panels : [],
    addEventListener:(name,fn)=>{if(!listeners.has(name))listeners.set(name,[]);listeners.get(name).push(fn);},
    dispatchEvent:event=>{for(const fn of listeners.get(event.type)||[])fn(event);return !event.defaultPrevented;}},
  window:{location,innerWidth:1200,setTimeout:()=>1,setInterval:()=>1,clearTimeout:()=>{},addEventListener:()=>{},
    matchMedia:()=>({matches:false}),sessionStorage:{getItem:()=>null,setItem:()=>{}},
    history:{replaceState:(_,__,url)=>{const parsed=new URL(url,location.href);location.href=parsed.href;location.search=parsed.search;}}},
  requestAnimationFrame:fn=>fn(),
  fetch:async url=>{const path=new URL(url,location.href).pathname;requests.push(path);
    if(independent && ['/api/skills','/api/workflows','/api/impact','/api/findings','/api/loops'].includes(path))return response(200,{skills:[],workflows:[],impact_checks:[],findings:[],loops:[]});
    if(path==='/api/session/first')return response(200,captured);
    if(path==='/api/status')return response(200,{preparation:{state:'running',stage:'refreshing_improvements'}});
    return response(503,{state:'preparing'});}};
vm.createContext(context);vm.runInContext(bundle,context);
(async()=>{
  await context.appReady;await new Promise(resolve=>setImmediate(resolve));
  const data=vm.runInContext('D',context);
  assert.equal(data._unpublishedNavigation,true);
  if(independent){
    const params=new URLSearchParams(location.search);
    assert.equal(params.get('tab'),initialTab);
    for(const [key,value] of Object.entries({session:empty ? null : 'first',range:'all',agents:'codex',q:'needle',model:'model-a',status:'completed',sort:'cost'}))assert.equal(params.get(key),value);
    assert.equal(requests.filter(path=>path==='/api/data').length,1);
    assert.ok(element('button-'+initialTab).classList.contains('active'));
    assert.ok(requests.includes('/api/skills'));
    element('button-impact').listeners.click();
    assert.equal(new URLSearchParams(location.search).get('tab'),'impact');
    const event=new context.CustomEvent('reflect-snapshot-published',{cancelable:true,detail:{completion:'2026-11-09T12:00:00Z'}});
    assert.equal(context.document.dispatchEvent(event),true,'first publication must recover missing session navigation through normal reload');
    assert.equal(new URLSearchParams(location.search).get('session'),empty ? null : 'first');
    if(empty){assert.match(element('sb-list').innerHTML,/Session navigation is updating/);assert.ok(!element('sb-list').innerHTML.includes('No sessions match'));}
    element('button-sessions').listeners.click();
    const sessionEvent=new context.CustomEvent('reflect-snapshot-published',{cancelable:true,detail:{completion:'2026-11-09T12:00:01Z'}});
    assert.equal(context.document.dispatchEvent(sessionEvent),true,'unpublished selected session also permits first publication recovery');
    return;
  }
  assert.ok(!data._navigationFromCache,'no completed report exists to call cached');
  assert.equal(data.sessions.length,1);assert.equal(data.sessions[0].agent,'codex');
  assert.equal(data.sessions[0]._activityLoaded,true);assert.equal(data.sessions[0]._fullLoaded,false);
  assert.ok(!Object.hasOwn(data,'measurements'));assert.ok(!Object.hasOwn(data,'git_activity'));
  assert.ok(!Object.hasOwn(data.sqlite,'overview'));assert.deepEqual(Object.keys(data.sqlite.tabs),[]);
  assert.equal(requests.filter(path=>path==='/api/session/first').length,1,'reuse bootstrap detail in existing session loader');
  assert.equal(new URLSearchParams(location.search).get('tab'),'sessions');
  assert.match(element('sb-detail').innerHTML,/Captured activity available; analysis is refreshing/);
  assert.ok(!/Duration:|Events:|Cost:|quality-score-value/.test(element('sb-detail').innerHTML));
  assert.match(element('global-filter-meta').textContent,/published navigation not loaded/);
  for(const view of ['usage','tools','graph','context']) {
    assert.equal(element(view+'-content').hidden,true);
    assert.match(element(view+'-view-status').textContent,/No published report loaded/);
  }
  for(const name of ['workflows','impact']) {
    assert.equal(element('tab-'+name).classList.contains('evidence-unavailable'),true);
  }
  assert.equal(element('tab-inbox').classList.contains('evidence-unavailable'),false);
  assert.equal(element('tab-skills').classList.contains('evidence-unavailable'),false);
  assert.match(element('finding-ledger').innerHTML,/Findings are updating/);
})().then(()=>process.stdout.write('bootstrap-contract-ok\n')).catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", script], input=json.dumps({"bundle": bundle, "initial_tab": initial_tab}),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "bootstrap-contract-ok" in result.stdout
