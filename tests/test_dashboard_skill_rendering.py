"""Keep durable skill packages browsable while derived analysis is pending."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _run_contract(source: str, scenario: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for Skills browser contracts")
    source = Path("src/reflect/frontend/js_demo.js").read_text() + "\n" + source
    script = r"""
const assert = require('node:assert/strict'), vm = require('node:vm');
const source = JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const params = new URLSearchParams('tab=skills');
const context = {ledgerDialog:{dataset:{}},escHtml:String, fmt:String, humanizeLedgerLabel:String,dashboardDocumentActive:true,
  currentParams:()=>params, updateUrlParams:fn=>fn(params),
  skillAvailabilityPresentation:()=>({kind:'installed',label:'Recorded installation',detail:'One recorded package'}),
  document:{addEventListener:(event,fn)=>{if(event==='reflect-status-polled')context.tick=fn;}},
  window:{},
};
vm.createContext(context);vm.runInContext(source,context);
(async()=>{
""" + scenario + r"""
})().then(()=>process.stdout.write('skill-contract-ok\n'))
  .catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(
        [node, "-e", script], input=json.dumps(source), text=True,
        capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "skill-contract-ok" in result.stdout


def test_skill_registry_preserves_packages_search_and_unknown_analysis():
    source = Path("src/reflect/frontend/js_impact.js").read_text()
    renderer = source[source.index("function buildImprovementSurfaces()"):
                      source.index("\nbuildImprovementSurfaces();")]
    _run_contract(renderer, r"""
const classes=new Set(), children=[];
const panel={classList:{toggle:(name,on)=>on?classes.add(name):classes.delete(name)},
  querySelector:()=>children[0],prepend:child=>children.unshift(child)};
const registry={innerHTML:''},search={value:''},summary={},count={firstChild:{textContent:''}};
context.document={getElementById:id=>({'tab-skills':panel,'skill-registry':registry,
  'skill-search':search,'skill-filter-summary':summary,'skill-count':count}[id]||null),
  createElement:()=>({setAttribute:()=>{}}),querySelectorAll:()=>[]};
context.sortObservedLoops=items=>items;context.groupImpactMeasurements=items=>items;
context.skillMatchesSearch=(item,q)=>item.name.toLowerCase().includes(q.toLowerCase());
context.IMPROVEMENT_DATA={skills:[],resources:{skills:{state:'preparing',loaded_at:null}}};
context.buildImprovementSurfaces();
assert.equal(count.firstChild.textContent,'—');
assert.match(registry.innerHTML,/Skills are updating/);
assert.ok(!registry.innerHTML.includes('No skill records'));
assert.ok(!classes.has('evidence-unavailable'));

context.IMPROVEMENT_DATA={skills:[{id:'one',name:'Verify changes',current_version:2,
  installation_count:1,evidence_count:null,usage_count:null,measurement_count:null}],
  resources:{skills:{state:'ready',loaded_at:'2026-11-08T09:00:00Z'}},skill_analysis_state:'pending'};
params.set('skill_q','verify');context.buildImprovementSurfaces();
assert.equal(search.value,'verify');assert.equal(count.firstChild.textContent,'1');
assert.match(registry.innerHTML,/Inspect Skill/);
assert.match(registry.innerHTML,/<dt>References · All Time<\/dt><dd>—<\/dd>/);
assert.match(registry.innerHTML,/<dt>Impact Checks<\/dt><dd>—<\/dd>/);
assert.match(children[0].innerHTML,/Showing recorded packages and installations/);
search.value='missing';search.oninput();assert.match(registry.innerHTML,/No skills match/);
search.value='verify';search.oninput();assert.match(registry.innerHTML,/Inspect Skill/);

const record=context.IMPROVEMENT_DATA.skills[0];
Object.assign(record,{usage_count:7,evidence_count:3,measurement_count:2});
context.IMPROVEMENT_DATA.skill_analysis_state='ready';context.buildImprovementSurfaces();
assert.match(registry.innerHTML,/<dt>References · All Time<\/dt><dd>7<\/dd>/);
assert.equal(children[0].hidden,true);
context.IMPROVEMENT_DATA.resources.skills.state='error';context.buildImprovementSurfaces();
assert.match(children[0].innerHTML,/last loaded/);
assert.match(registry.innerHTML,/Inspect Skill/);
assert.match(registry.innerHTML,/<dt>References · All Time<\/dt><dd>7<\/dd>/);
assert.match(children[0].innerHTML,/historical usage, evidence, and impact/);
assert.equal(search.value,'verify');assert.ok(!classes.has('evidence-unavailable'));

Object.assign(record,{analysis_state:'stale',current_version:3});
context.IMPROVEMENT_DATA.skills.push({id:'two',name:'Verify new work',analysis_state:'pending',
  usage_count:null,evidence_count:null,measurement_count:null});
context.IMPROVEMENT_DATA.skill_analysis_state='stale';
context.IMPROVEMENT_DATA.resources.skills={state:'stale',loaded:true,loaded_at:null};
context.buildImprovementSurfaces();
assert.match(registry.innerHTML,/<dt>Current Version<\/dt><dd>3<\/dd>/);
assert.match(registry.innerHTML,/<dt>References · All Time<\/dt><dd>7<\/dd>/);
assert.match(registry.innerHTML,/<dt>References · All Time<\/dt><dd>—<\/dd>/);
assert.match(children[0].innerHTML,/timestamp unavailable/);
assert.match(children[0].innerHTML,/missing results remain unknown/);
assert.equal(search.value,'verify');
context.IMPROVEMENT_DATA.skill_evidence_state='recorded';context.IMPROVEMENT_DATA.skill_analysis_state='pending';
context.IMPROVEMENT_DATA.resources.skills.state='recorded';record.analysis_state='pending';
context.buildImprovementSurfaces();
assert.match(registry.innerHTML,/<dt>References · All Time<\/dt><dd>7<\/dd>/);
assert.match(children[0].innerHTML,/stored references read/);
assert.ok(!children[0].innerHTML.includes('historical usage'));
assert.match(children[0].innerHTML,/Stored impact results have not been revalidated/);
context.IMPROVEMENT_DATA.skill_evidence_state='published';record.evidence_state='recorded';
context.buildImprovementSurfaces();
assert.match(registry.innerHTML,/<dt>References · All Time<\/dt><dd>7<\/dd>/);
assert.match(registry.innerHTML,/Recorded references · validation pending/);
assert.match(children[0].innerHTML,/Newly recorded references are marked separately/);
context.IMPROVEMENT_DATA.skills=[];params.delete('skill_q');context.buildImprovementSurfaces();
assert.match(registry.innerHTML,/last loaded registry/);
assert.ok(!registry.innerHTML.includes('No skill records are registered'));
context.IMPROVEMENT_DATA.resources.skills.state='ready';context.IMPROVEMENT_DATA.skill_analysis_state='ready';
context.buildImprovementSurfaces();
assert.match(registry.innerHTML,/No skill records are registered/);
""")


def test_skill_detail_shows_packages_without_unpublished_analysis_and_retries():
    source = Path("src/reflect/frontend/js_ledger.js").read_text()
    notice = source[source.index("function staleEvidenceNotice("):
                    source.index("async function showObservationEvidence(")]
    review = source[source.index("async function showSkillReview("):
                    source.index("async function showWorkflowReview(")]
    _run_contract(notice + review, r"""
const dialogs=[];
context.openLedgerDialog=value=>dialogs.push(value);
context.fmtWorkflowDate=String;context.skillTargetLabel=String;
context.compactLedgerId=String;context.sessionInspectionUrl=item=>`?session=${item.session_id}`;
context.renderWorkflowSessionLedger=(items,options)=>items.length
  ? items.map(item=>item.title).join(' ') : options.empty;
context.impactMetricPresentation=()=>({goal:'Measured failures'});
context.impactVerdictPresentation=()=>({label:'Improved'});
context.formatImpactValue=(metric,value)=>String(value);
const detail={analysis_state:'pending',skill:{name:'Verify changes',usage_count:null,evidence_count:null},
  versions:[{version:2,content_markdown:'Run the relevant check',workflow_candidate_id:'workflow-one'}],
  installations:[{path:'/redacted/skill',status:'active'}],evidence:null,usage_occurrences:null,measurements:null};
context.ledgerRequest=async()=>detail;
await context.showSkillReview('one');
let dialog=dialogs.at(-1);
assert.match(dialog.body,/Run the relevant check/);assert.match(dialog.body,/\/redacted\/skill/);
assert.match(dialog.body,/Usage, evidence, and impact are updating/);
assert.match(dialog.body,/<div class="review-kpi-label">Captured References · All Time<\/div><div class="review-kpi-value">—/);
assert.ok(!dialog.body.includes('No before/after impact check'));
assert.ok(!dialog.body.includes('not generated from Reflect'));
assert.ok(!dialog.actions.includes('review-workflow'));
assert.match(dialog.actions,/data-ledger-action="review-skill" data-skill-id="one"/);

detail.analysis_state='ready';detail.skill.usage_count=4;detail.skill.evidence_count=0;
detail.evidence=[];detail.usage_occurrences=[];
detail.measurements=[{metric_name:'failures',verdict:'improved',before_value:5,after_value:2,confidence:1}];
await context.showSkillReview('one');dialog=dialogs.at(-1);
assert.match(dialog.body,/5 before → 2 after/);assert.match(dialog.actions,/review-workflow/);
assert.ok(!dialog.body.includes('Analysis is updating'));

detail.analysis_state='stale';
detail.publication={state:'stale',snapshot_completed_at:'2026-11-07T09:00:00Z',
  loaded_at:'2026-11-08T09:00:00Z',reason:'preparing'};
detail.versions[0].content_markdown='Updated package available now';
detail.usage_occurrences=[{session_id:'prior-session',title:'Retained use session',state:'observed'}];
detail.evidence=[{entity_type:'session',entity_id:'retained-source-session',relationship:'source',confidence:1}];
await context.showSkillReview('one');dialog=dialogs.at(-1);
assert.match(dialog.body,/Historical evidence/);
assert.ok(dialog.body.includes(new Date(detail.publication.snapshot_completed_at).toLocaleString()));
assert.ok(!dialog.body.includes(new Date(detail.publication.loaded_at).toLocaleString()));
assert.match(dialog.body,/Updated package available now/);
assert.match(dialog.body,/Retained use session/);
assert.match(dialog.body,/retained-source-session/);
assert.match(dialog.body,/<div class="review-kpi-label">Captured References · All Time<\/div><div class="review-kpi-value">4/);
assert.match(dialog.body,/5 before → 2 after/);
assert.match(dialog.actions,/Retry Analysis/);
assert.ok(!dialog.actions.includes('review-workflow'));

detail.publication={state:'stale'};detail.measurements=[];detail.usage_occurrences=[];detail.evidence=[];
detail.skill.usage_count=0;
await context.showSkillReview('one');dialog=dialogs.at(-1);
assert.match(dialog.body,/timestamp unavailable/);
assert.match(dialog.body,/<div class="review-kpi-label">Captured References · All Time<\/div><div class="review-kpi-value">0/);
assert.match(dialog.body,/No captured references in the last published analysis/);
assert.match(dialog.body,/No source sessions in the last published analysis/);
assert.match(dialog.body,/No before\/after impact check in the last published analysis/);
assert.ok(!dialog.body.includes('not generated from Reflect'));
detail.analysis_state='pending';detail.evidence_state='recorded';detail.publication={state:'recorded'};
detail.skill.usage_count=2;detail.skill.evidence_count=1;
detail.evidence=[{entity_type:'session',entity_id:'recorded-source',relationship:'source'}];
detail.usage_occurrences=[{session_id:'recorded-use',title:'Recorded use',outcome:'success'}];
detail.measurements=[{metric_name:'failures',verdict:'validation_pending',before_value:null,after_value:null,
  measured_at:'2020-01-01',recorded_result:{before_value:5,after_value:2,verdict:'improved',confidence:0.8}}];
await context.showSkillReview('one');dialog=dialogs.at(-1);
assert.match(dialog.body,/Recorded evidence/);assert.match(dialog.body,/Recorded use/);
assert.match(dialog.body,/recorded-source/);assert.match(dialog.body,/Recorded result: 5 before → 2 after · improved/);
assert.match(dialog.body,/Measured 2020-01-01; current validation is pending/);
assert.match(dialog.body,/<div class="review-kpi-label">Captured References · All Time<\/div><div class="review-kpi-value">2/);
assert.ok(!dialog.body.slice(dialog.body.indexOf('Impact Results')).includes('0% confidence'));
assert.ok(!dialog.actions.includes('review-workflow'));
assert.match(dialog.actions,/Retry Analysis/);
context.ledgerRequest=async()=>{throw new Error('Request failed (503)');};
await context.showSkillReview('one');dialog=dialogs.at(-1);
assert.equal(dialog.title,'Skill history unavailable');
assert.match(dialog.actions,/data-ledger-action="review-skill" data-skill-id="one">Retry/);
context.ledgerRequest=async()=>detail;await context.showSkillReview('one');
assert.equal(dialogs.at(-1).title,'Verify changes');
""")


def test_pending_skill_analysis_retries_only_on_visible_improvement_tabs():
    source = Path("src/reflect/frontend/js_ledger.js").read_text()
    retry = source[source.index("let improvementRequest ="):
                   source.index("const workflowDateFormatter =")]
    _run_contract(retry, r"""
const tick=context.tick;let calls=0;
context.document={visibilityState:'visible'};
context.IMPROVEMENT_DATA={resources:{skills:{state:'ready'}},skill_analysis_state:'pending'};
context.loadImprovementData=async()=>{calls++;return {resources:{skills:{state:'ready'}},skill_analysis_state:'ready'};};
context.buildImprovementSurfaces=()=>{};
context.document.visibilityState='hidden';tick();assert.equal(calls,0);
context.document.visibilityState='visible';params.set('tab','sessions');tick();assert.equal(calls,0);
params.set('tab','skills');tick();await context.refreshImprovementSurfaces();assert.equal(calls,1);
tick();assert.equal(calls,1,'published Skills analysis stops polling');
context.IMPROVEMENT_DATA={resources:{skills:{state:'stale'}},skill_analysis_state:'stale'};
tick();await context.refreshImprovementSurfaces();assert.equal(calls,2,'retained analysis retries');
""")
