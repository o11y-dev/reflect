"""Usage widgets use scoped measurements and expose coverage without inventing zeros."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _run_widgets(scenario: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for dashboard widget contracts")
    source = Path("src/reflect/frontend/js_explore.js").read_text()
    renderer = source[source.index("function renderMeasurementCoverage(){"):
                      source.index("\n/* Heatmap */")]
    script = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const renderer=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const elements=new Map(),actions=[],resized=[];
const element=id=>{
  if(!elements.has(id))elements.set(id,{id,innerHTML:'',textContent:''});
  return elements.get(id);
};
const escape=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const context={D:{},fmtWorkflowDate:value=>new Date(value).toLocaleString(),document:{getElementById:element},escHtml:escape,fmtDur:value=>value+'ms',
  activateTab:(tab,options)=>actions.push({tab,...options}),requestAnimationFrame:fn=>fn(),
  Chart:{getChart:canvas=>({resize:()=>resized.push(canvas.id)})},
  sessionInspectionUrl:row=>'/?tab=sessions&session='+encodeURIComponent(row.session_id)+'&evidence='+encodeURIComponent(row.evidence_focus_id),
};
vm.createContext(context);vm.runInContext(renderer,context);
function complete(){return {
  focused_session_id:'scoped-session',sessions:[{total_cost_usd:999999,input_tokens:999999}],
  measurements:{scope:{kind:'event_window',date_from:'2026-11-01',date_to:'2026-11-08'},
    totals:{estimated_cost_usd:12.34,input_tokens:100,cache_read_tokens:200,cache_creation_tokens:300,output_tokens:400},
    coverage:{cost_status:'complete',observed_usage:10,priced_usage:10,complete_priced_usage:10,
      token_bucket_records:{input_tokens:10,cache_read_tokens:10,cache_creation_tokens:10,output_tokens:10},
      complete_input_usage:10,eligible_usage_turns:4,observed_prompt_turns:5,eligible_tool_turns:3,
      eligible_session_elapsed:2,eligible_turn_elapsed:4,eligible_idle_intervals:3,
      eligible_model_transitions:3,known_model_usage:10},
    metrics:{completed_turns:4,mean_input_tokens:150,cache_read_ratio:1/3,completed_tool_calls:8,
      mean_tool_calls_per_turn:8/3,max_tool_calls_per_turn:5,mean_session_elapsed_ms:3600000,p95_session_elapsed_ms:7200000,
      mean_turn_elapsed_ms:1000,p95_turn_elapsed_ms:3000,idle_total_ms:400000,idle_max_ms:300001,
      idle_over_five_minutes:1,model_switches:2},
    model_mix:[{model:'model-a',calls:6,cost_usd:8,cost_observations:6},{model:'model-b',calls:4,cost_usd:4.34,cost_observations:4}]},
  git_activity:{distinct_commits:3,created_pull_requests:1,merged_pull_requests:0,created_merge_requests:2,
    merged_merge_requests:1,total_operations:7,states:{confirmed:6,unresolved:1},undated_operations:0,operations:[]},
};}
function all(){return [...elements.values()].map(el=>el.innerHTML).join('\n');}
""" + scenario
    result = subprocess.run([node, "-e", script], input=json.dumps(renderer),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def test_widgets_use_canonical_scope_and_keep_metric_definitions_explicit():
    _run_widgets(r"""
context.D=complete();context.renderMeasurementCoverage();
const html=all();
for(const code of ['M1','M2','M3','M4','M5','M6']) assert.ok(html.includes('>'+code+'</span>'));
assert.match(element('measurement-summary').innerHTML,/12\.34/);
assert.ok(!html.includes('999999'),'sidebar cards cannot supply aggregates');
assert.match(element('measurement-scope').textContent,/Selected session/);
assert.ok(element('measurement-scope').textContent.includes(context.fmtWorkflowDate(context.D.measurements.scope.date_from)));
assert.match(element('measurement-scope').textContent,/local time/);
for(const text of ['Uncached input','Cache read','Cache write','Output','10 / 10 records',
  'fully priced','not a provider cache hit rate','not verified task success','3 eligible completed turns']) assert.ok(html.includes(text),text);
assert.match(element('measurement-time').innerHTML,/Observed session span/);
assert.match(element('measurement-time').innerHTML,/does not measure active work/);
assert.match(element('measurement-time').innerHTML,/Pauses do not establish wasted time/);
assert.match(element('measurement-model-summary').innerHTML,/Eligible adjacent pairs/);
assert.match(element('measurement-models').innerHTML,/model-a/);
assert.match(element('measurement-git').innerHTML,/cost is not allocated/);
assert.ok(!html.includes('NaN'));assert.ok(!html.includes('Infinity'));
""")


def test_widgets_distinguish_missing_metrics_from_observed_zero():
    _run_widgets(r"""
context.D={measurements:{totals:{estimated_cost_usd:0,input_tokens:0},coverage:{cost_status:'unavailable'},metrics:{completed_turns:0}}};
context.renderMeasurementCoverage();
assert.match(element('measurement-summary').innerHTML,/Unknown/);
assert.ok(!element('measurement-summary').innerHTML.includes('>0.00</strong>'));
assert.match(element('measurement-time').innerHTML,/Unknown/);
assert.match(element('measurement-git').innerHTML,/Git activity is not available/);
assert.ok(!element('measurement-git').innerHTML.includes('>0</strong>'));
context.D=complete();
const data=context.D.measurements;
for(const key of Object.keys(data.totals))data.totals[key]=0;
for(const key of Object.keys(data.metrics))data.metrics[key]=0;
context.renderMeasurementCoverage();
assert.match(element('measurement-summary').innerHTML,/>0\.00<\/strong>/);
assert.match(element('measurement-summary').innerHTML,/>0%<\/strong>/);
assert.match(element('measurement-time').innerHTML,/>0ms<\/strong>/);
assert.ok(!element('measurement-summary').innerHTML.includes('>Unknown</strong>'));
""")


def test_partial_pricing_and_token_buckets_remain_visible_with_denominators():
    _run_widgets(r"""
context.D=complete();
const data=context.D.measurements;
data.coverage.cost_status='partial';data.coverage.complete_priced_usage=3;data.coverage.priced_usage=3;
data.coverage.token_bucket_records.cache_creation_tokens=0;data.coverage.token_bucket_records.input_tokens=4;
data.totals.cache_creation_tokens=0;
data.model_mix[1].cost_observations=0;data.model_mix[1].cost_usd=0;
context.renderMeasurementCoverage();
const html=element('measurement-summary').innerHTML;
assert.match(html,/Known cost subtotal/);assert.match(html,/3 \/ 10 fully priced/);
assert.match(html,/Unpriced usage is excluded/);assert.match(html,/4 \/ 10 records/);
assert.match(html,/Cache write<\/dt><dd><strong[^>]*>Unknown/);
assert.match(element('measurement-models').innerHTML,/model-b[\s\S]*>Unknown<\/span>/);
""")


def test_widget_actions_and_bounded_git_drilldown_use_captured_evidence():
    _run_widgets(r"""
context.D=complete();context.D.git_activity.total_operations=15;
context.D.git_activity.operations=Array.from({length:15},(_,i)=>({provider:'gitlab',operation:'merge',state:'confirmed',
  repository:'redacted/repo',artifact_id:String(i),session_id:'session '+i,evidence_id:'evidence '+i}));
context.D.measurements.model_mix[0].model='<unsafe-model>';
context.renderMeasurementCoverage();
element('measurement-summary').onclick({target:{closest:()=>({dataset:{usageView:'tools'}})}});
assert.deepEqual(actions,[{tab:'explore',view:'tools'}]);
const git=element('measurement-git').innerHTML;
assert.equal((git.match(/<li>/g)||[]).length,10);
assert.match(git,/10 of 15/);assert.match(git,/session=session%200/);
assert.match(git,/evidence=evidence%200/);assert.ok(!git.includes('session%2010'));
assert.match(element('measurement-models').innerHTML,/&lt;unsafe-model&gt;/);
const diagnostics=element('usage-diagnostics');diagnostics.open=true;diagnostics.ontoggle();
assert.deepEqual(resized,['eventChart','eventBarChart']);
""")


def test_usage_layout_integrates_existing_charts_before_legacy_diagnostics():
    template = Path("src/reflect/frontend/index.template.html").read_text()
    usage = template[template.index('id="tab-explore-usage"'):template.index('id="tab-impact"')]
    assert "Usage &amp; delivery" in usage
    assert '<span class="usage-code">M7</span>' in usage
    diagnostics = usage.index('id="usage-diagnostics"')
    for name in ("agentCostChart", "tokenChart", "modelChart", "measurement-git"):
        assert usage.count(f'id="{name}"') == 1
        assert usage.index(f'id="{name}"') < diagnostics
    for name in ("cohort-comparison-panel", "obs-badges", "stat-row", "metric-row", "source-provenance"):
        assert usage.index(f'id="{name}"') > diagnostics


def test_retained_tokens_are_visible_and_undated_window_exclusion_is_explained():
    _run_widgets(r"""
context.D={measurements:{scope:{kind:'session_lifetime'},totals:{input_tokens:702,output_tokens:269},
 coverage:{observed_usage:1,estimated_usage:1,undated_usage:1,token_bucket_records:{input_tokens:0,output_tokens:0},
 retained_token_bucket_records:{input_tokens:1,output_tokens:1}},metrics:{}}};
context.renderMeasurementCoverage();
assert.match(element('measurement-summary').innerHTML,/971/);
assert.match(element('measurement-summary').innerHTML,/Captured \+ retained tokens/);
assert.match(element('measurement-scope').textContent,/transcript estimates are not exact/);
context.D.measurements.scope.kind='event_window';
context.D.measurements.coverage.retained_token_bucket_records={};
context.D.measurements.totals={};
context.renderMeasurementCoverage();
assert.match(element('measurement-scope').textContent,/1 undated session totals are excluded/);
assert.match(element('measurement-scope').textContent,/Select All time/);
assert.ok(!element('measurement-summary').innerHTML.includes('>971</strong>'));
""")
