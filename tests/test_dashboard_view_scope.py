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
  getBoundingClientRect() {return {left:0,width:200};}
  focus() {this.focused=true;}
  click() {this.clicks=(this.clicks || 0)+1;}
}
const names=['sessions','inbox','workflows','skills','impact','explore'];
const buttons=names.map(name=>Object.assign(new Element('tab-button-'+name),{dataset:{tab:name}}));
const panels=names.filter(name=>name!=='explore').concat(['explore-usage','explore-tools','explore-context'])
  .map(name=>new Element('tab-'+name));
const elements=new Map([...buttons,...panels].map(el=>[el.id,el]));
for (const id of ['tabs','report-shell','session-rail','session-rail-open','session-rail-toggle',
  'filter-rail','filter-sheet-open','report-panel-controls','hdr-range','session-sort-field',
  'filter-scope-title','filter-scope-note','filter-sheet-backdrop','inbox-analysis-window',
  'explore-scope','explore-scope-label','explore-scope-detail','explore-session-change','explore-session-clear']) {
  elements.set(id,new Element(id));
}
const params=new URLSearchParams('tab=sessions&session=selected&agents=codex&range=30d&q=review&model=gpt&status=failed&sort=cost');
const body=new Element('body');
const hydrated=[];
let mobile=false;
const context={URL,URLSearchParams,D:{analysis_window:{days:7,date_to:'2026-11-09'},sessions:[{id:'selected',agent:'codex',first_prompt:'Fix the retry loop'}]},
  dashboardSelectedSessionId:'selected',formatAgentLabel:String,
  window:{matchMedia:()=>({matches:mobile}),location:{href:'http://localhost/?'+params}},
  document:{body,getElementById:id=>elements.get(id),
    querySelector:selector=>buttons.find(button=>selector.includes('"'+button.dataset.tab+'"')),
    querySelectorAll:selector=>selector==='.tab'?buttons:selector==='.tab-panel'?panels:[]},
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
body.classList.add('sessions-rail-collapsed');
for (const tab of ['inbox','workflows','skills','impact']) {
  context.activateTab(tab);
  for (const id of ['session-rail','session-rail-open','filter-rail','filter-sheet-open','hdr-range','report-panel-controls'])
    assert.equal(elements.get(id).hidden,true,tab+' should hide '+id);
  assert.equal(elements.get('tab-'+tab).hidden,false);
  assert.equal(elements.get('report-shell').dataset.view,tab);
  assert.equal(params.get('session'),'selected');
  assert.equal(params.get('agents'),'codex');
  assert.equal(params.get('range'),'30d');
  assert.equal(params.get('q'),'review');
  assert.equal(params.get('model'),'gpt');
  assert.equal(params.get('status'),'failed');
  assert.equal(params.get('sort'),'cost');
}
context.activateTab('sessions');
assert.equal(elements.get('session-rail').hidden,false);
assert.equal(elements.get('filter-rail').hidden,false);
assert.equal(elements.get('hdr-range').hidden,false);
assert.equal(elements.get('session-sort-field').hidden,false);
assert.ok(body.classList.contains('sessions-rail-collapsed'),'keep the user rail preference');
assert.equal(elements.get('session-rail-toggle').attributes['aria-expanded'],'false');
""")


def test_usage_keeps_matching_filters_without_session_rail_or_sort():
    _run_navigation(r"""
context.activateTab('explore');
assert.deepEqual(hydrated,['usage']);
assert.equal(params.get('view'),'usage');
assert.equal(params.get('session'),'selected');
assert.equal(elements.get('session-rail').hidden,true);
assert.equal(elements.get('filter-rail').hidden,false);
assert.equal(elements.get('hdr-range').hidden,false);
assert.equal(elements.get('session-sort-field').hidden,true);
assert.equal(elements.get('filter-scope-title').textContent,'Matching-session filters');
context.activateTab('explore',{view:'tools'});
assert.deepEqual(hydrated,['usage','tools']);
assert.equal(elements.get('filter-rail').hidden,false);
assert.equal(elements.get('tab-explore-tools').hidden,false);
""")


def test_mobile_navigation_closes_overlays_without_losing_collapsed_preference():
    _run_navigation(r"""
mobile=true;
body.classList.add('sessions-rail-open','filters-rail-open','rail-overlay-open','sessions-rail-collapsed');
context.activateTab('skills');
for (const name of ['sessions-rail-open','filters-rail-open','rail-overlay-open'])
  assert.equal(body.classList.contains(name),false);
assert.equal(elements.get('filter-sheet-backdrop').hidden,true);
context.activateTab('explore');
assert.equal(elements.get('filter-sheet-open').attributes['aria-expanded'],'false');
assert.equal(elements.get('session-rail-open').hidden,true);
context.activateTab('sessions');
assert.equal(elements.get('session-rail-open').hidden,false);
assert.equal(elements.get('session-rail-open').attributes['aria-expanded'],'false');
assert.ok(body.classList.contains('sessions-rail-collapsed'));
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
    assert '.tab[aria-selected="true"]::before{' in css
    assert ".tab.active" not in css
    _run_navigation(r"""
// A stale presentation class must not determine which main tab is highlighted.
buttons[0].classList.add('active');
for (const button of buttons) button.classList.toggle=()=>{};
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


def test_explore_displays_session_scope_and_clears_only_that_scope():
    _run_navigation(r"""
context.activateTab('explore');
assert.equal(elements.get('explore-scope').hidden,false);
assert.match(elements.get('explore-scope-label').textContent,/Selected session: Fix the retry loop/);
assert.match(elements.get('explore-scope-detail').textContent,/codex.*selected/);
assert.equal(elements.get('explore-session-clear').hidden,false);
context.clearExploreSessionScope();
assert.equal(params.has('session'),false);
assert.equal(context.dashboardSelectedSessionId,'');
assert.equal(context.exploreViewUrl('usage').url.searchParams.has('session'),false);
assert.deepEqual(hydrated,['usage','usage']);
for(const [key,value] of Object.entries({agents:'codex',range:'30d',q:'review',model:'gpt',status:'failed',sort:'cost'}))
  assert.equal(params.get(key),value);
assert.equal(elements.get('explore-scope-label').textContent,'All matching sessions');
assert.equal(elements.get('explore-session-clear').hidden,true);
assert.equal(elements.get('explore-session-change').textContent,'Choose session');
""")


def test_explore_scope_follows_requested_selection_even_with_cached_payload():
    _run_navigation(r"""
context.D.focused_session_id='old-session';
context.D._navigationFromCache=true;
params.set('session','new-session-12345678');
context.activateTab('explore');
assert.match(elements.get('explore-scope-label').textContent,/12345678/);
assert.ok(!elements.get('explore-scope-label').textContent.includes('old-session'));
params.set('session','selected');
context.activateTab('sessions');
assert.equal(elements.get('explore-scope').hidden,true);
context.activateTab('explore',{view:'tools'});
assert.match(elements.get('explore-scope-label').textContent,/Fix the retry loop/);
""")


@pytest.mark.parametrize("mobile", [False, True])
def test_choose_session_opens_existing_browser_and_keeps_filters(mobile):
    _run_navigation(r"""
mobile=MOBILE;
body.classList.add('sessions-rail-collapsed');
context.activateTab('explore');
context.chooseExploreSession();
assert.equal(params.get('tab'),'sessions');
assert.equal(elements.get('session-rail').hidden,false);
assert.equal(elements.get('session-rail-open').clicks,1);
assert.equal(params.get('session'),'selected');
assert.equal(params.get('agents'),'codex');
assert.equal(params.get('range'),'30d');
""".replace("MOBILE", str(mobile).lower()))


def test_context_and_synthetic_sample_explain_scope_without_inapplicable_controls():
    _run_navigation(r"""
context.activateTab('explore',{view:'context'});
assert.equal(elements.get('explore-scope-label').textContent,'All captured context');
assert.match(elements.get('explore-scope-detail').textContent,/Session and date filters do not apply/);
assert.equal(elements.get('explore-session-clear').hidden,true);
assert.equal(elements.get('explore-session-change').hidden,true);
assert.equal(elements.get('filter-rail').hidden,true);
assert.equal(elements.get('report-shell').dataset.exploreView,'context');
assert.equal(params.get('session'),'selected','context navigation preserves the next Usage selection');
context.isStaticDemo=()=>true;
context.activateTab('explore',{view:'usage'});
assert.equal(elements.get('explore-scope-label').textContent,'Full illustrative sample');
assert.equal(elements.get('explore-session-clear').hidden,true);
assert.equal(elements.get('explore-session-change').hidden,true);
assert.equal(elements.get('filter-rail').hidden,true);
""")
