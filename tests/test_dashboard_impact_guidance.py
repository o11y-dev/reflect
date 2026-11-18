"""Blocked comparisons give a next action without inventing evidence or outcomes."""

import pytest
from test_dashboard_impact_rendering import _run_impact_contract


def test_historical_gap_hides_accounting_and_never_requests_more_later_tasks():
    _run_impact_contract(r"""
context.buildImprovementSurfaces();
const html=ledger.innerHTML;
const visible=html.replace(/<details\b[\s\S]*?<\/details>/g,'');
assert.match(visible,/Historical Baseline Unavailable/);
assert.match(visible,/Post-activation tasks cannot fill this baseline/);
assert.match(visible,/Next step:/);
assert.match(visible,/Inspect Baseline Evidence/);
assert.ok(!visible.includes('22 candidates'));
assert.ok(!visible.includes('missing verification'));
assert.ok(!visible.includes('Collecting comparable evidence'));
assert.ok(!visible.includes('<progress'));
assert.equal((visible.match(/<button\b/g)||[]).length,3);
assert.match(visible,/data-ledger-action="review-impact-sessions"/);
assert.match(html,/<details class="impact-history"><summary>Comparison evidence and exclusions<\/summary>/);
assert.match(html,/22 candidates · 0 eligible · 5 required/);
assert.match(html,/missing verification: 18/);
""")


@pytest.mark.parametrize("state", ["baseline", "validation"])
def test_analysis_pending_offers_inspection_without_collection_progress(state):
    setup = (
        "item.cohort.baseline_state='analysis_pending';"
        if state == "baseline"
        else "item.cohort.baseline_state='frozen';item.measurement_state='validation_pending';"
    )
    _run_impact_contract(setup + r"""
context.buildImprovementSurfaces();
const visible=ledger.innerHTML.replace(/<details\b[\s\S]*?<\/details>/g,'');
assert.match(visible,/Analysis Pending/);
assert.match(visible,/Next step:/);
assert.match(visible,/Inspect Recorded Tasks/);
assert.ok(!visible.includes('Collecting comparable evidence'));
assert.ok(!visible.includes('<progress'));
assert.equal((visible.match(/<button\b/g)||[]).length,3);
""")


def test_confirmed_baseline_collects_metric_bearing_later_tasks():
    _run_impact_contract(r"""
item.cohort.baseline_state='frozen';item.before_count=5;item.after_count=2;
item.cohort.eligibility.after.metric_count=2;
context.buildImprovementSurfaces();
const visible=ledger.innerHTML.replace(/<details\b[\s\S]*?<\/details>/g,'');
assert.match(visible,/Collecting Evidence/);
assert.match(visible,/Next step:/);
assert.match(visible,/comparable tasks/);
assert.match(visible,/data-ledger-action="review-workflow"/);
assert.match(visible,/Review Applied Workflow/);
assert.match(visible,/<progress value="2" max="5"/);
assert.equal((visible.match(/<button\b/g)||[]).length,3);
""")


def test_ineligible_comparison_leads_with_plain_blocker_and_discloses_reasons():
    _run_impact_contract(r"""
item.cohort.baseline_state='frozen';item.measurement_state='not_measurable';
item.cohort.measurement_reasons=['Archetype Low Confidence <example>'];
context.buildImprovementSurfaces();
const html=ledger.innerHTML;
const visible=html.replace(/<details\b[\s\S]*?<\/details>/g,'');
assert.match(visible,/Not measurable/);
assert.match(visible,/Inspect Task Evidence/);
assert.ok(visible.includes('Archetype Low Confidence'));
assert.ok(!visible.includes('<progress'));
assert.ok(!visible.includes('Collecting'));
assert.match(html,/Archetype Low Confidence &lt;example/);
assert.ok(!html.includes('<example>'));
""")


def test_unknown_baseline_does_not_assume_later_collection_can_fix_it():
    _run_impact_contract(r"""
item.cohort={measurement_state:'collecting'};
context.buildImprovementSurfaces();
const visible=ledger.innerHTML.replace(/<details\b[\s\S]*?<\/details>/g,'');
assert.match(visible,/Baseline Eligibility Unknown/);
assert.match(visible,/Inspect Baseline Evidence/);
assert.ok(!visible.includes('Collecting Evidence'));
assert.ok(!visible.includes('<progress'));
""")


def test_measured_comparison_keeps_values_visible_and_details_disclosed():
    _run_impact_contract(r"""
Object.assign(item,{measurement_state:'measured',verdict:'improved',
  before_value:0.4,after_value:0.2,before_count:5,after_count:5});
item.cohort.baseline_state='frozen';
context.buildImprovementSurfaces();
const visible=ledger.innerHTML.replace(/<details\b[\s\S]*?<\/details>/g,'');
assert.match(visible,/40%/);assert.match(visible,/20%/);
assert.match(visible,/Comparison available/);
assert.match(visible,/Progress across 1 impact check/);
assert.ok(!visible.includes('22 candidates'));
assert.ok(!visible.includes('Next step:'));
assert.match(visible,/do not establish that the workflow caused the change/);
context.IMPROVEMENT_DATA.resources.impact={state:'error',loaded:true};
context.buildImprovementSurfaces();
assert.match(ledger.innerHTML,/Stored check · Observed decrease/);
assert.match(ledger.innerHTML,/40%/);assert.match(ledger.innerHTML,/20%/);
assert.match(ledger.innerHTML,/current validation is pending/);
""")


@pytest.mark.parametrize("missing_value", ["null", "'not-a-number'"])
def test_measured_state_cannot_override_missing_or_invalid_measurement(missing_value):
    _run_impact_contract(
        "Object.assign(item,{measurement_state:'measured',verdict:'improved',"
        f"before_value:{missing_value},after_value:0.2}});"
        "item.cohort.baseline_state='frozen';"
        + r"""
context.buildImprovementSurfaces();
const visible=ledger.innerHTML.replace(/<details\b[\s\S]*?<\/details>/g,'');
assert.match(visible,/Not measurable/);
assert.match(visible,/Inspect Task Evidence/);
assert.ok(!visible.includes('Improved'));
assert.ok(!visible.includes('Comparison available'));
assert.ok(!visible.includes('<svg'));
assert.ok(!visible.includes('20%'));
"""
    )


def test_findings_separate_priority_and_detection_confidence_from_outcomes():
    _run_impact_contract(r"""
const findings={innerHTML:''},getElementById=context.document.getElementById;
context.document.getElementById=id=>id==='finding-ledger'?findings:getElementById(id);
context.IMPROVEMENT_DATA.observations=[{id:'finding-one',title:'Repeated retries',impact_score:86,confidence:0.95}];
context.buildImprovementSurfaces();
assert.match(findings.innerHTML,/>Priority 86\/100</);
assert.match(findings.innerHTML,/>95% detection confidence(?:<| ·)/);
assert.match(findings.innerHTML,/not a measured outcome/);
assert.match(findings.innerHTML,/data-ledger-action="create-from-finding" data-observation-id="finding-one">Create workflow</);
assert.match(findings.innerHTML,/data-observation-id="finding-one"/);
assert.ok(!findings.innerHTML.includes('impact 86'));
Object.assign(context.IMPROVEMENT_DATA.observations[0],{impact_score:null,confidence:null});
context.buildImprovementSurfaces();
assert.match(findings.innerHTML,/Priority unavailable/);
assert.match(findings.innerHTML,/Detection confidence unavailable/);
assert.ok(!findings.innerHTML.includes('Priority 0/100'));
assert.ok(!findings.innerHTML.includes('0% detection confidence'));
""")
