"""Keep cohort eligibility, observed exposure, and outcome comparisons distinct."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _run_impact_contract(scenario: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for Impact browser contracts")
    source = Path("src/reflect/frontend/js_impact.js").read_text()
    renderer = source[source.index("const IMPACT_METRIC_PRESENTATION"):
                      source.index("\nbuildImprovementSurfaces();")]
    script = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const ledger={innerHTML:''},children=[],classes=new Set();
const panel={classList:{toggle:(name,on)=>on?classes.add(name):classes.delete(name)},
  querySelector:()=>children[0],prepend:child=>children.unshift(child)};
const context={escHtml:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;'),fmt:String,
  humanizeLedgerLabel:value=>String(value).replaceAll('_',' '),fmtWorkflowDate:String,
  sortObservedLoops:items=>items,currentParams:()=>new URLSearchParams(),window:{},
  document:{getElementById:id=>({'tab-impact':panel,'measurement-ledger':ledger}[id]||null),
    createElement:()=>({setAttribute:()=>{}}),querySelectorAll:()=>[]}};
const item={id:'check-one',candidate_id:'workflow-one',metric_name:'tool_failure_rate',
  measured_at:'2026-11-01T09:00:00Z',measurement_state:'collecting',verdict:'insufficient_data',
  before_count:0,after_count:5,before_value:null,after_value:null,
  cohort:{baseline_state:'historical_unavailable',minimum_before_execution_units:5,
    minimum_after_execution_units:5,eligibility:{
      before:{candidate_count:22,eligible_count:0,excluded_count:22,selected_count:0,metric_count:0,missing_metric_count:0,
        exclusion_reasons:{missing_verification:18,missing_outcome:4}},
      after:{candidate_count:7,eligible_count:5,excluded_count:2,selected_count:5,metric_count:5,missing_metric_count:0,
        exclusion_reasons:{ambiguous_attribution:2}}},
    exposure:{before:{selected_count:0,followed:0,invoked:0,ignored:0,unknown:0},
      after:{selected_count:5,followed:0,invoked:1,ignored:0,unknown:4}}}};
context.IMPROVEMENT_DATA={measurements:[item],resources:{impact:{state:'ready',loaded:true}}};
vm.createContext(context);vm.runInContext(source,context);
""" + scenario + r"""
process.stdout.write('impact-contract-ok\n');
"""
    result = subprocess.run([node, "-e", script], input=json.dumps(renderer), text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "impact-contract-ok" in result.stdout


def test_candidates_without_eligible_baseline_explain_historical_gap_and_exposure():
    _run_impact_contract(r"""
context.buildImprovementSurfaces();const html=ledger.innerHTML;
assert.match(html,/22 candidates · 0 eligible · 5 required/);
assert.match(html,/22 excluded: missing verification: 18; missing outcome: 4/);
assert.match(html,/Historical Baseline Unavailable/);
assert.match(html,/selected pre-activation evidence currently/);
assert.match(html,/Post-activation tasks cannot fill this baseline/);
assert.match(html,/late or backfilled pre-activation evidence may change eligibility/);
assert.match(html,/0 followed · 1 invoked · 0 ignored · 4 unknown/);
assert.ok(!html.includes('followed tasks collected'));
assert.ok(!html.includes('<progress'),'historical gaps cannot be filled by future collection');
assert.match(html,/data-ledger-action="review-impact-sessions"/);
assert.match(html,/do not establish that the workflow caused the change/);
""")


def test_impact_averages_use_task_grain_not_session_containers():
    _run_impact_contract(r"""
assert.equal(context.formatImpactValue(context.impactMetricPresentation('read_only_exploration_calls'),1.25),'1.25 per task');
assert.equal(context.formatImpactValue(context.impactMetricPresentation('context_outlier_sessions'),1200),'1200 tokens/task');
assert.equal(context.formatImpactValue(context.impactMetricPresentation('successful_workflow_sessions'),0.5),'50%');
assert.equal(context.impactMetricPresentation('successful_workflow_sessions').measure,'Successful workflow tasks');
""")


def test_unknown_diagnostics_never_become_zero_or_implied_following():
    _run_impact_contract(r"""
item.before_count=null;item.after_count=null;
item.cohort={measurement_state:'collecting'};
context.buildImprovementSurfaces();const html=ledger.innerHTML;
assert.match(html,/Unknown candidates · Unknown eligible · Unknown required/);
assert.match(html,/Unknown selected · Unknown with metric values · Unknown missing metric values/);
assert.match(html,/Unknown excluded/);
assert.match(html,/Guidance exposure: unknown/);
assert.ok(!html.includes('0 candidates'));assert.ok(!html.includes('0 followed'));
assert.ok(!html.includes('<progress'));
assert.ok(!html.includes('minimum sample is available'));
""")


def test_pending_analysis_is_distinct_from_unavailable_history_and_retained_results():
    _run_impact_contract(r"""
item.cohort.baseline_state='analysis_pending';
item.cohort.eligibility.before.eligible_count=null;
context.buildImprovementSurfaces();
assert.match(ledger.innerHTML,/Analysis Pending/);
assert.match(ledger.innerHTML,/eligibility has not been established/);
assert.match(ledger.innerHTML,/22 candidates · Unknown eligible · 5 required/);
assert.ok(!ledger.innerHTML.includes('Historical Baseline Unavailable'));
assert.ok(!ledger.innerHTML.includes('<progress'));
context.IMPROVEMENT_DATA.resources.impact={state:'error',loaded:true,loaded_at:'2026-11-01T10:00:00Z'};
context.buildImprovementSurfaces();
assert.match(ledger.innerHTML,/Stored check · Analysis Pending/);
assert.match(ledger.innerHTML,/Stored check 2026-11-01T09:00:00Z; current validation is pending/);
assert.ok(!classes.has('evidence-unavailable'));
assert.match(children[0].innerHTML,/last loaded/);
context.IMPROVEMENT_DATA.measurements=[];
context.IMPROVEMENT_DATA.resources.impact={state:'preparing',loaded:false};
context.buildImprovementSurfaces();
assert.match(ledger.innerHTML,/Impact analysis is pending/);
assert.ok(!ledger.innerHTML.includes('No Impact Checks Yet'));
""")


def test_valid_baseline_keeps_measured_values_and_separate_observed_exposure():
    _run_impact_contract(r"""
Object.assign(item,{measurement_state:'measured',verdict:'improved',before_count:5,after_count:5,
  before_value:0.4,after_value:0.2,confidence:0.45});
item.cohort.baseline_state='frozen';
item.cohort.eligibility.before={candidate_count:8,eligible_count:6,excluded_count:2,selected_count:5,metric_count:5,missing_metric_count:0,
  exclusion_reasons:{missing_verification:2}};
item.cohort.exposure.before={selected_count:5,followed:0,invoked:0,ignored:0,unknown:5};
context.buildImprovementSurfaces();
assert.match(ledger.innerHTML,/Frozen baseline/);
assert.match(ledger.innerHTML,/8 candidates · 6 eligible · 5 required/);
assert.match(ledger.innerHTML,/5 selected · 5 with metric values · 0 missing metric values/);
assert.match(ledger.innerHTML,/40%/);assert.match(ledger.innerHTML,/20%/);
assert.match(ledger.innerHTML,/decreased by 50% across 5 comparable post-activation tasks/);
assert.match(ledger.innerHTML,/0 followed · 0 invoked · 0 ignored · 5 unknown/);
assert.match(ledger.innerHTML,/do not establish that the workflow caused the change/);
item.measurement_state='collecting';item.verdict='insufficient_data';
Object.assign(item.cohort.eligibility.after,{candidate_count:30,eligible_count:25,selected_count:20,metric_count:3,missing_metric_count:17});
context.buildImprovementSurfaces();
assert.match(ledger.innerHTML,/30 candidates · 25 eligible · 5 required/);
assert.match(ledger.innerHTML,/20 selected · 3 with metric values · 17 missing metric values/);
assert.match(ledger.innerHTML,/Post-activation comparison sample: 3 selected tasks with metrics of 5 required/);
assert.match(ledger.innerHTML,/<progress value="3" max="5"/);
assert.ok(!ledger.innerHTML.includes('followed tasks collected'));
item.cohort.eligibility.after.metric_count=null;item.cohort.eligibility.after.missing_metric_count=null;
context.buildImprovementSurfaces();
assert.match(ledger.innerHTML,/20 selected · Unknown with metric values · Unknown missing metric values/);
assert.ok(!ledger.innerHTML.includes('<progress'),'explicitly unknown metric coverage cannot use a legacy sample count');
""")
