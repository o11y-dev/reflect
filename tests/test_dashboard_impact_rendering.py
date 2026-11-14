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


def test_impact_table_keeps_uncertainty_and_dates_visible_without_confidence_claims():
    _run_impact_contract(r"""
item.cohort.baseline_state='frozen';item.measurement_state='measured';item.verdict='improved';
item.before_value=1.25;item.after_value=0;item.before_count=20;item.after_count=10;item.confidence=.75;
item.cohort.periods={before:{start:'2026-08-30',end:'2026-09-10'},after:{start:'2026-09-11',end:'2026-09-28'}};
context.buildImprovementSurfaces();
const html=ledger.innerHTML;
assert.match(html,/<table/);assert.match(html,/<th scope="col">Outcome/);
assert.match(html,/aria-expanded="false"/);assert.match(html,/data-ledger-action="toggle-analysis-row"/);
assert.match(html,/Observed decrease/);assert.match(html,/2026-08-30/);assert.match(html,/2026-09-28/);
assert.match(html,/Adherence unknown/);assert.ok(!html.includes('75% confidence'));
assert.match(html,/data-ledger-action="navigate-analysis-row"/);
item.measurement_state='not_measurable';item.before_value=null;item.after_value=null;
item.cohort.measurement_reasons=['Token telemetry missing'];
context.buildImprovementSurfaces();
assert.match(ledger.innerHTML,/Not measurable/);assert.match(ledger.innerHTML,/Token telemetry missing/);
""")


def test_linked_rows_preserve_filters_and_expansion_across_refresh():
    _run_impact_contract(r"""
const workflowLedger={innerHTML:''},statusFilter={value:'pending'},typeFilter={value:'all'};
const params=new URLSearchParams('workflow_status=pending&range=7d');
const button={setAttribute:(key,value)=>{button[key]=value},focus:()=>{},scrollIntoView:()=>{}};
const detail={hidden:true};
const originalGet=context.document.getElementById;
context.document.getElementById=id=>({'workflow-ledger':workflowLedger,
  'workflow-status-filter':statusFilter,'workflow-type-filter':typeFilter,
  'toggle-workflow-workflow-one':button,'detail-workflow-workflow-one':detail}[id]||originalGet(id));
context.currentParams=()=>new URLSearchParams(params);
context.updateUrlParams=fn=>fn(params);context.activateTab=tab=>params.set('tab',tab);
context.isStaticDemo=()=>false;
context.workflowSourcePresentation=()=>({label:'Observed procedure',reviewLabel:'Review'});
context.renderWorkflowSteps=()=>'<ol><li>Verify change</li></ol>';
context.IMPROVEMENT_DATA.workflows=[{id:'workflow-one',title:'Verify completion',status:'stale',
  content:{steps:[]},checks:{stale_reason:'source_observation_resolved'}}];
context.buildImprovementSurfaces();
assert.ok(!workflowLedger.innerHTML.includes('Verify completion'));
context.navigateAnalysisRow('workflow','workflow-one');
assert.equal(params.get('workflow_status'),'pending');assert.equal(params.get('range'),'7d');
assert.match(workflowLedger.innerHTML,/Verify completion/);assert.match(workflowLedger.innerHTML,/Retired suggestion/);
assert.match(workflowLedger.innerHTML,/aria-expanded="true"/);
context.toggleAnalysisRow('workflow','workflow-one');assert.equal(button['aria-expanded'],'false');assert.equal(detail.hidden,true);
context.buildImprovementSurfaces();assert.match(workflowLedger.innerHTML,/aria-expanded="false"/);
assert.ok(!workflowLedger.innerHTML.includes('Evidence Confidence'));
let inspected=null;context.showWorkflowReview=id=>{inspected=id};
context.navigateAnalysisRow('workflow','non-representative-variant');
assert.equal(inspected,'non-representative-variant');
""")


def test_impact_history_separates_activations_and_keeps_concentration_visible():
    _run_impact_contract(r"""
const checks=[{id:'a',candidate_id:'w',workflow_version_id:'v',intervention_id:'first',measured_at:'2026-10-01'},
 {id:'b',candidate_id:'w',workflow_version_id:'v',intervention_id:'second',measured_at:'2026-11-01'}];
assert.equal(context.groupImpactMeasurements(checks).length,2);
item.cohort.metric_distribution={before:{nonzero_count:1,metric_count:20}};
assert.match(context.impactLimitations(item),/One of 20 baseline tasks/);
item.cohort.exposure.after.followed=3;
assert.match(context.impactLimitations(item),/One of 20 baseline tasks/);
assert.match(context.impactLimitations(item),/3 followed tasks/);
""")


def test_suggestions_expose_dismiss_and_restore_without_requiring_fresh_analysis():
    _run_impact_contract(r"""
context.isStaticDemo=()=>false;
const suggestion={id:'suggestion-one',status:'pending',title:'Verify completion',lifecycle:{deployment:'not_deployed'}};
assert.match(context.workflowSuggestionAction(suggestion),/data-ledger-action="dismiss-suggestion"/);
suggestion.status='rejected';suggestion.checks={dismissal:{batch:'one'}};
assert.match(context.workflowSuggestionAction(suggestion),/>Restore<\/button>/);
suggestion.lifecycle.deployment='active';
assert.equal(context.workflowSuggestionAction(suggestion),'');
suggestion.lifecycle.deployment='not_deployed';context.isStaticDemo=()=>true;
assert.equal(context.workflowSuggestionAction(suggestion),'');
""")


def test_workflow_observed_steps_are_separate_from_proposed_safeguards():
    _run_impact_contract(r"""
const html=context.workflowProcedureEvidence({observed_steps:['read <diff>'],proposed_steps:['check head']});
assert.match(html,/Observed in source executions/);
assert.match(html,/Proposed safeguards · benefit unmeasured/);
assert.match(html,/read &lt;diff>/);
assert.equal(context.workflowProcedureEvidence({}), '');
""")


def test_recommendations_limit_five_keep_enable_and_allow_show_all():
    _run_impact_contract(r'''
const workflowLedger={innerHTML:'',insertAdjacentHTML:(_,html)=>{workflowLedger.innerHTML+=html}},showAll={};
const params=new URLSearchParams();
context.document.getElementById=id=>({'workflow-ledger':workflowLedger,'workflow-show-all':showAll}[id]||null);
context.currentParams=()=>new URLSearchParams(params);context.updateUrlParams=fn=>fn(params);
context.isStaticDemo=()=>false;context.workflowSourcePresentation=()=>({label:'Observed procedure'});
context.renderWorkflowSteps=()=>'';
context.IMPROVEMENT_DATA.resources.workflows={state:'ready',loaded:true};
context.IMPROVEMENT_DATA.workflows=Array.from({length:7},(_,i)=>({id:`w${i}`,title:`Procedure ${i}`,status:'pending',content:{steps:[]},suggestion_assessment:{eligible:i<6}}));
context.buildImprovementSurfaces();
assert.match(workflowLedger.innerHTML,/Show all 6 workflows/);
assert.ok(!workflowLedger.innerHTML.includes('Procedure 5'));
assert.ok(!workflowLedger.innerHTML.includes('Procedure 6'));
assert.equal((workflowLedger.innerHTML.match(/class="analysis-summary"/g)||[]).length,5);
assert.match(workflowLedger.innerHTML,/>Enable workflow<\/button>/);
showAll.onclick();
assert.match(workflowLedger.innerHTML,/Procedure 5/);
assert.equal(params.get('workflow_show'),'all');
context.buildImprovementSurfaces();assert.match(workflowLedger.innerHTML,/Procedure 5/);
''')
