"""View navigation exposes applicable controls without changing report scope."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _run_navigation(scenario: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for browser navigation checks")
    source = Path("src/reflect/frontend/js_navigation.js").read_text()
    source = source[:source.index("tabButtons.forEach((button, index)")]
    ledger = Path("src/reflect/frontend/js_ledger.js").read_text()
    source = ledger[ledger.index("function exploreViewUrl("):ledger.index("// The initial Usage payload")] + source
    sessions = Path("src/reflect/frontend/js_sessions.js").read_text()
    source = sessions[sessions.index("function sessionListIdentity("):sessions.index("function renderSessionQualityBadge(")] + source
    source = Path("src/reflect/frontend/js_demo.js").read_text() + "\n" + source
    script = r"""
const assert=require('node:assert/strict'), vm=require('node:vm');
class Element {
  constructor(id) {
    this.id=id;this.hidden=false;this.dataset={};this.attributes={};this.textContent='';
    const classes=new Set();
    this.classList={contains:name=>classes.has(name),
      remove:(...names)=>names.forEach(name=>classes.delete(name)),
      add:(...names)=>names.forEach(name=>classes.add(name)),
      toggle:(name,on)=>on?classes.add(name):classes.delete(name)};
    this.style={setProperty:()=>{}};this.clientWidth=1200;
  }
  setAttribute(name,value) {this.attributes[name]=value;}
  getAttribute(name) {return this.attributes[name] ?? null;}
  removeAttribute(name) {delete this.attributes[name];}
  getBoundingClientRect() {return {left:0,width:200};}
  focus() {this.focused=true;}
  click() {this.clicks=(this.clicks || 0)+1;}
}
const names=['sessions','inbox','workflows','skills','impact','explore'];
const buttons=names.map(name=>Object.assign(new Element('tab-button-'+name),{dataset:{tab:name}}));
const panels=names.filter(name=>name!=='explore').concat(['explore-usage','explore-tools','explore-graph','explore-context'])
  .map(name=>new Element('tab-'+name));
const elements=new Map([...buttons,...panels].map(el=>[el.id,el]));
for (const id of ['tabs','report-shell','section-sidebar','section-nav-title','section-scope-note','inactive-scope',
  'section-explore-nav','dashboard-scope','session-scope-summary','session-table-heading','section-filters',
  'hdr-range','session-sort-field','workflow-scope-controls','skill-scope-controls','inbox-analysis-window',
  'explore-scope-label','explore-session-change','explore-session-clear']) {
  elements.set(id,new Element(id));
}
const exploreButtons=['usage','tools','graph','context'].map(view=>
  Object.assign(new Element('explore-'+view),{dataset:{exploreView:view}}));
const params=new URLSearchParams('tab=sessions&session=selected&agents=codex&range=30d&q=review&model=gpt&status=failed&sort=cost');
const body=new Element('body');
const hydrated=[];
let mobile=false;
const context={URL,URLSearchParams,D:{analysis_window:{days:7,date_to:'2026-11-09'},sessions:[{id:'selected',agent:'codex',first_prompt:'Fix the retry loop'}]},
  dashboardSelectedSessionId:'selected',formatAgentLabel:String,fmt:String,ledgerSelectionParams:[],CustomEvent:class{},
  window:{matchMedia:()=>({matches:mobile}),location:{href:'http://localhost/?'+params}},
  document:{body,getElementById:id=>elements.get(id),dispatchEvent:()=>{},
    querySelector:selector=>buttons.find(button=>selector.includes('"'+button.dataset.tab+'"')),
    querySelectorAll:selector=>selector==='.tab'?buttons:selector==='.tab-panel'?panels:selector.startsWith('#section-explore-nav')?exploreButtons:[]},
  currentParams:()=>params,updateUrlParams:fn=>fn(params),fmtWorkflowDate:String,
  hydrateExploreView:view=>{hydrated.push(view);return Promise.resolve();}};
vm.createContext(context);
vm.runInContext(JSON.parse(require('node:fs').readFileSync(0,'utf8')),context);
""" + scenario
    result = subprocess.run([node, "-e", script], input=json.dumps(source), text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def test_inventory_navigation_preserves_filters_and_selected_session():
    _run_navigation(r"""
const saved=Object.fromEntries([...params].filter(([key])=>key!=='tab'));
for (const tab of ['inbox','workflows','skills','impact']) {
  context.activateTab(tab);
  for (const id of ['dashboard-scope','section-filters','hdr-range']) assert.equal(elements.get(id).hidden,true);
  assert.equal(elements.get('tab-'+tab).hidden,false);
  assert.equal(elements.get('report-shell').dataset.view,tab);
  for (const [key,value] of Object.entries(saved)) assert.equal(params.get(key),value);
}
context.activateTab('sessions');
assert.equal(elements.get('dashboard-scope').hidden,true,'a selected session owns the whole workspace');
params.delete('session');
context.updateDashboardViewScope('sessions');
assert.equal(elements.get('dashboard-scope').hidden,false);
assert.equal(elements.get('section-filters').hidden,false);
assert.equal(elements.get('session-sort-field').hidden,false);
""")


def test_explore_scope_uses_matching_total_and_context_keeps_filters_saved():
    _run_navigation(r"""
params.delete('session');context.dashboardSelectedSessionId='';params.set('range','7d');
vm.runInContext('exploreMatchingSessionCount=230',context);
context.activateTab('explore',{view:'graph'});
assert.equal(elements.get('dashboard-scope').hidden,false);
assert.equal(elements.get('section-filters').hidden,false);
assert.equal(elements.get('session-sort-field').hidden,true);
assert.match(elements.get('explore-scope-label').textContent,/Last 7 days · codex · 230 matching sessions/);
assert.equal(exploreButtons.filter(button=>button.attributes['aria-current']==='page').length,1);
assert.equal(exploreButtons[2].attributes['aria-current'],'page');
elements.get('section-filters').open=true;
context.activateTab('explore',{view:'context'});
assert.equal(elements.get('section-filters').hidden,true);
assert.equal(elements.get('section-filters').open,false);
assert.match(elements.get('explore-scope-label').textContent,/All captured context · Session filters saved and inactive/);
assert.equal(params.get('range'),'7d');
context.activateTab('explore',{view:'tools'});
assert.equal(elements.get('section-filters').hidden,false);
assert.equal(elements.get('section-filters').open,false);
assert.match(elements.get('explore-scope-label').textContent,/230 matching sessions/);
assert.deepEqual(hydrated,['graph','context','tools']);
""")


def test_analysis_window_is_independent_of_session_range_and_unknown_stays_unknown():
    _run_navigation(r"""
context.activateTab('inbox');
assert.match(elements.get('inbox-analysis-window').textContent,/7 days ending 2026-11-09/);
assert.equal(params.get('range'),'30d');
context.D.analysis_window={date_from:'2026-11-02',date_to:'2026-11-09'};
context.updateDashboardViewScope('inbox');
assert.match(elements.get('inbox-analysis-window').textContent,/2026-11-02 — 2026-11-09/);
context.D.analysis_window=null;
context.updateDashboardViewScope('inbox');
assert.equal(elements.get('inbox-analysis-window').textContent,'Analysis window not supplied by this report.');
""")


def test_main_tab_highlight_uses_the_same_selection_state_as_accessibility():
    css = Path("src/reflect/frontend/css_foundation.css").read_text()
    assert '.tab[aria-selected="true"]{' in css
    assert ".tab.active" not in css
    _run_navigation(r"""
for (const tab of names) {
  context.activateTab(tab);
  const selected=buttons.filter(button=>button.attributes['aria-selected']==='true');
  assert.equal(selected.length,1);
  assert.equal(selected[0].dataset.tab,tab);
  assert.equal(selected[0].tabIndex,0);
  const panelId=tab==='explore'?'tab-explore-usage':'tab-'+tab;
  assert.equal(elements.get(panelId).hidden,false);
  assert.equal(panels.filter(panel=>!panel.hidden).length,1);
}
""")


def test_explore_displays_session_and_clears_only_that_scope():
    _run_navigation(r"""
context.activateTab('explore');
assert.match(elements.get('explore-scope-label').textContent,/Last 30 days · codex · Session: Fix the retry loop/);
assert.equal(elements.get('explore-session-clear').hidden,false);
context.clearExploreSessionScope();
assert.equal(params.has('session'),false);
assert.equal(context.dashboardSelectedSessionId,'');
assert.equal(context.exploreViewUrl('usage').sessionId,'');
assert.equal(params.get('agents'),'codex');
assert.equal(params.get('range'),'30d');
assert.match(elements.get('explore-scope-label').textContent,/Matching sessions/);
assert.equal(elements.get('explore-session-clear').hidden,true);
""")


def test_choose_session_opens_session_table_and_keeps_filters():
    _run_navigation(r"""
context.activateTab('explore',{view:'graph'});
context.chooseExploreSession();
assert.equal(elements.get('report-shell').dataset.view,'sessions');
assert.equal(elements.get('session-scope-summary').hidden,false);
assert.equal(elements.get('session-table-heading').focused,true);
assert.equal(params.has('session'),false);
assert.equal(params.get('agents'),'codex');
assert.equal(params.get('range'),'30d');
""")


def test_synthetic_sample_hides_inapplicable_scope_controls():
    _run_navigation(r"""
context.isStaticDemo=()=>true;
context.activateTab('explore',{view:'usage'});
assert.equal(elements.get('explore-scope-label').textContent,'Full illustrative sample');
assert.equal(elements.get('explore-session-clear').hidden,true);
assert.equal(elements.get('explore-session-change').hidden,true);
assert.equal(elements.get('section-filters').hidden,true);
""")
