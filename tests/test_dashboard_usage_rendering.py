"""Usage rerenders authoritative scope data without replacing sidebar sessions."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_header_and_session_summary_use_selected_event_window():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for Usage rendering checks")
    frontend = Path("src/reflect/frontend")
    source = (frontend / "js_impact.js").read_text()
    summary = source[source.index("function renderUsageSummary(){"):source.index("function renderSqlTabPayloads(){")]
    source = (frontend / "js_navigation.js").read_text()
    header = source[source.index("const headerScope ="):source.index("document.getElementById('hdr-ts')")]
    script = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
for (const windowed of [true,false]) {
  const elements={'hdr-range':{},'stat-row':{}};
  const context={D:{first_event_ts:'2026-09-24',last_event_ts:'2026-11-08',
    measurements:{scope:{kind:windowed?'event_window':'session_lifetime',
      date_from:'2026-11-01T00:00:00Z',date_to:'2026-11-08T00:00:00Z'}}},
    document:{getElementById:id=>elements[id]},fmt:String,
    fmtWorkflowDate:stamp=>String(stamp).slice(0,10),
    sqlTab:()=>({first_event_ts:'2026-09-24',unique_sessions:4})};
  vm.createContext(context);
  vm.runInContext(source.summary+'\n'+source.header,context);
  context.renderUsageSummary();
  assert.equal(elements['hdr-range'].textContent,
    windowed?'2026-11-01 — 2026-11-08':'2026-09-24 — 2026-11-08');
  assert.ok(elements['stat-row'].innerHTML.includes(
    windowed?'2026-11-01 — 2026-11-08':'2026-09-24 - now'));
  if (windowed) assert.ok(!elements['stat-row'].innerHTML.includes('2026-09-24'));
}
"""
    subprocess.run([node, "-e", script], input=json.dumps({"summary": summary, "header": header}),
                   text=True, capture_output=True, check=True)


def test_usage_rerenders_scope_and_recovers_from_empty_charts():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for Usage rendering checks")
    frontend = Path("src/reflect/frontend")
    impact = (frontend / "js_impact.js").read_text()
    explore = (frontend / "js_explore.js").read_text().split("/* ════════════════════ SESSION COMPARISON")[0]
    # Other product surfaces keep their existing startup lifecycle.
    impact = impact.replace("renderSqlTabPayloads();", "").replace("buildImprovementSurfaces();", "")
    explore = explore.replace("renderToolsView();", "")
    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const elements = new Map(), charts = new Map();
let destroyed = 0;
class Element {
  constructor(id='') { this.id=id; this.style={}; this.dataset={}; this.children=[]; this.hidden=false; }
  set innerHTML(value) { this.html=value; this.children=[]; }
  get innerHTML() { return this.html || ''; }
  appendChild(child) { child.parentElement=this; this.children.push(child); }
  querySelector() { return this.children.find(child => child.dataset.usageChartEmpty); }
  remove() { this.parentElement.children=this.parentElement.children.filter(child => child!==this); }
  closest() { return {clientWidth:800}; }
  getContext() { return {canvas:this}; }
}
function element(id) {
  if (!elements.has(id)) {
    const el=new Element(id); elements.set(id,el);
    if (id.endsWith('Chart')) new Element().appendChild(el);
  }
  return elements.get(id);
}
class Chart {
  static defaults={};
  static getChart(canvas) { return charts.get(canvas.id); }
  constructor(ctx, config) {
    this.id=ctx.canvas.id; this.config=config;
    assert.ok(!charts.has(this.id), `Chart ${this.id} was not destroyed`);
    charts.set(this.id,this);
  }
  destroy() { charts.delete(this.id); destroyed++; }
}
const sidebar=[{id:'unrelated-sidebar-session',agent:'wrong',total_cost_usd:99999,day:'2026-01-01'}];
const today=new Date().toISOString().slice(0,10);
const empty=()=>({sql_backed:true,sessions:sidebar,sqlite:{tabs:{}},measurements:{}});
const context={
  D:empty(), Chart, COLORS:['orange'], P:{blue:'orange'},
  document:{getElementById:element, createElement:()=>new Element(), addEventListener:()=>{}},
  window:{innerWidth:800}, navigator:{language:'en-US'},
  fmt:value=>String(value), fmtCost:value=>String(value), fmtDur:value=>String(value),
  h12:value=>String(value), escHtml:value=>String(value),
};
context.sqlTab=name=>context.D.sqlite.tabs[name] || {};
context.currentAgentComparison=()=>context.sqlTab('agents').agent_comparison || [];
context.makeDoughnut=(id,labels,data)=>new Chart(element(id).getContext(),{data:{labels,datasets:[{data}]}});
context.makeHBar=context.makeDoughnut;
context.makeLine=(id,labels,datasets)=>new Chart(element(id).getContext(),{data:{labels,datasets}});
vm.createContext(context);
vm.runInContext(source.impact+'\n'+source.explore,context);
assert.equal(charts.size,0);
assert.ok(element('agentCostChart').hidden, 'global sidebar cost must not fill an empty SQL scope');

function payload(label,n) {
  return {sql_backed:true,sessions:sidebar,
    measurements:{coverage:{cost_status:'partial',priced_usage:n,observed_usage:n+1},
      metrics:{completed_turns:n},scope:{kind:'event_window'}},
    git_activity:{distinct_commits:n}, sqlite:{tabs:{
      usage:{unique_sessions:n,prompt_submits:n,tool_calls:n,mcp_calls:n,signature_command:label,
        models_by_count:{[label]:n},events_by_type:{[label]:n},model_costs:{[label]:n},
        source_provenance:[{label,transport:'native',event_count:n}],
        total_input_tokens:n,total_output_tokens:n+1,total_cache_read_tokens:n+2,
        total_cost_usd:n,pricing_unit:label==='second'?'eur':'usd',
        agent_cost_over_time:[{day:today,agent:label,total_cost:n}]},
      activity:{activity_by_day:{[today]:n},activity_by_hour:{'2':n},peak_hour:2,
        events_by_type:{[label]:n},weekly_trends:[{week:label,events:n,days_active:1,delta:0}]},
      tools:{subagent_types_by_count:{[label]:n},subagent_stops_by_type:{[label]:n}},
      agents:{agent_comparison:[{name:label,sessions:n}]},
      observations:{achievements:[{icon:'*',name:label,sub:label}]},
    }}};
}
for (const [label,n] of [['first',11],['second',22]]) {
  context.D=payload(label,n);
  context.renderUsageView();
  assert.equal(context.D.sessions,sidebar);
  assert.ok(element('stat-row').innerHTML.includes(`>${n}</div>`));
  assert.ok(element('metric-row').innerHTML.includes(label));
  assert.equal(element('stat-input-tok').textContent,String(n));
  assert.ok(element('measurement-summary').innerHTML.includes(`>${n}</strong>`));
  for (const id of ['cohort-comparison-details','source-provenance','model-cost-share',
                    'subagent-effectiveness','weekly-trends-table','obs-badges']) {
    assert.ok(element(id).innerHTML.includes(label), `${id} did not refresh`);
    if (label==='second') assert.ok(!element(id).innerHTML.includes('first'), `${id} retained old scope`);
  }
  assert.equal(charts.size,5);
  assert.equal(charts.get('modelChart').config.data.labels[0],label);
  assert.equal(charts.get('eventBarChart').config.data.labels[0],label);
  assert.equal(charts.get('tokenChart').config.data.datasets[0].data[0],n);
  assert.equal(charts.get('agentCostChart').config.data.datasets[0].label,label);
  assert.ok(element('hour-bars').children[2].title.includes(`${n} events`));
  assert.ok(element('hm-grid').children.flatMap(column=>column.children).some(cell=>cell.title?.includes(`${n} events`)));
  assert.ok(!element('modelChart').hidden);
  assert.equal(element('modelChart').parentElement.children.length,1, 'empty note was not removed');
}
assert.ok(element('cost-stats').innerHTML.includes('EUR'));
context.D=empty();
context.renderUsageView();
assert.equal(charts.size,0);
assert.equal(destroyed,10);
assert.equal(element('stat-input-tok').textContent,'0');
assert.ok(element('hour-bars').innerHTML.includes('No hourly activity'));
assert.ok(element('modelChart').hidden);
assert.ok(!element('obs-badges').innerHTML.includes('second'));
"""
    subprocess.run(
        [node, "-e", script], input=json.dumps({"impact": impact, "explore": explore}),
        text=True, capture_output=True, check=True,
    )


def test_subagent_activity_preserves_observed_stops_without_claiming_success():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for subagent rendering checks")
    source = Path("src/reflect/frontend/js_explore.js").read_text()
    source = source[source.index("function buildSubagentActivity(){"):source.index("/* ════════════════════ TOOLS")]
    script = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const element={};
const context={document:{getElementById:()=>element},escHtml:String,fmt:String,
  sqlTab:()=>({subagent_types_by_count:{explorer:2,unknown:1},
    subagent_stops_by_type:{explorer:3,worker:1}})};
vm.createContext(context);
vm.runInContext(require('node:fs').readFileSync(0,'utf8'),context);
context.buildSubagentActivity();
assert.ok(element.innerHTML.includes('Role not recorded'));
assert.ok(element.innerHTML.includes('worker'));
assert.ok(element.innerHTML.includes('Recorded stops'));
assert.ok(element.innerHTML.includes('>3</td>'));
assert.ok(element.innerHTML.includes('>4</td>'));
assert.ok(!element.innerHTML.includes('Completed'));
assert.ok(!element.innerHTML.includes('%'));
assert.ok(element.innerHTML.includes('do not verify task success'));
"""
    subprocess.run([node, "-e", script], input=source, text=True, capture_output=True, check=True)
