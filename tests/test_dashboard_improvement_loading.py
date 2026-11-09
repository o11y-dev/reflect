"""Exercise independent improvement reads and retained data in the browser loader."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _run_loader_contract(scenario: str, **inputs) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for improvement loading contracts")
    source = Path("src/reflect/frontend/js_bootstrap.js").read_text()
    loader = source[source.index("async function loadImprovementData("):
                    source.index("\nfunction updateUrlParams(")]
    loader = Path("src/reflect/frontend/js_demo.js").read_text() + "\n" + loader
    script = r"""
const assert = require('node:assert/strict'), vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const names = ['findings','loops','workflows','skills','impact','rules'];
const oldTime = '2026-11-08T08:00:00.000Z', loadedTime = '2026-11-08T09:00:00.000Z';
const plain = value => JSON.parse(JSON.stringify(value));
const resourceName = url => url.split('/').at(-1).split('?')[0];
const fields = {
  findings:['findings','observations','finding_total_count','observation_record_count',
    'counts_by_status','pending_workflows','active_interventions','verified_improvement_rate',
    'generated_at','analysis_state','analysis_coverage','limitations'],
  loops:['loops'], workflows:['workflows'], impact:['measurements'],
  skills:['skills','skill_total_count','skill_archived_count','skill_counts_by_lifecycle','skill_analysis_state','skill_evidence_state'],
  rules:['rules','rule_extension'],
};
function payloads(empty=false) {
  const items = name => empty ? [] : [{id:`new-${name}`}];
  return {
    findings:{findings:items('findings'),finding_total_count:empty?0:1,
      observation_record_count:empty?0:2,counts_by_status:empty?{}:{new:2},
      pending_workflows:empty?0:1,active_interventions:empty?0:1,
      verified_improvement_rate:empty?null:0.5,generated_at:loadedTime,
      analysis_state:'ready',analysis_coverage:{},limitations:[]},
    loops:{loops:items('loops')}, workflows:{workflows:items('workflows')},
    impact:{impact_checks:items('impact')},
    skills:{skills:items('skills'),total_count:empty?0:4,archived_count:empty?0:3,
      counts_by_lifecycle:empty?{}:{active:1,stale:3}},
    rules:{rules:items('rules'),extension:empty?{}:{kind:'new-extension'}},
  };
}
function previous() {
  const data = {resources:Object.fromEntries(names.map(name => [name,{state:'ready',loaded_at:oldTime}]))};
  for (const [name, keys] of Object.entries(fields)) {
    for (const key of keys) data[key] = [{id:`old-${name}-${key}`}];
  }
  return data;
}
class FixedDate extends Date {
  constructor(...args) { super(...(args.length ? args : [loadedTime])); }
}
const timer = {calls:[]}, cleared = [];
const context = {D:{sql_backed:true}, IMPROVEMENT_DATA:previous(), AbortController, Date:FixedDate,dashboardDocumentActive:true,
  reportSupportsServerFiltering:() => true,
  window:{setTimeout:(fn, ms) => {timer.fn=fn;timer.ms=ms;timer.calls.push({fn,ms});return 19;},
    clearTimeout:id => cleared.push(id)}};
vm.createContext(context); vm.runInContext(input.loader, context);
const response = (status, body) => ({status,ok:status===200,json:async()=>body});
(async () => {
""" + scenario + r"""
})().then(() => process.stdout.write('improvement-loader-ok\n'))
  .catch(error => {console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(
        [node, "-e", script], input=json.dumps({"loader": loader, **inputs}),
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "improvement-loader-ok" in result.stdout


@pytest.mark.parametrize("failure", ["preparing", "http", "other-503", "network", "body"])
def test_endpoint_failure_preserves_only_its_prior_data(failure):
    _run_loader_contract(r"""
for (const failed of names) {
  const prior = previous(), original = JSON.stringify(prior), bodies = payloads();
  context.IMPROVEMENT_DATA = prior;
  const requests = [];
  context.fetch = async url => {
    const name = resourceName(url);requests.push(name);
    if (name !== failed) return response(200,bodies[name]);
    if (input.failure === 'network') throw new Error('offline');
    if (input.failure === 'body') return {ok:true,status:200,json:async()=>{throw new Error('bad JSON');}};
    return response(input.failure === 'http' ? 500 : 503,
      {state:input.failure === 'other-503' ? 'unknown' : 'preparing'});
  };
  const result = await context.loadImprovementData();
  assert.deepEqual(requests.slice().sort(),names.slice().sort());
  assert.equal(JSON.stringify(prior),original,'loading must not mutate retained data');
  assert.ok(!Object.hasOwn(result,'unavailable'));
  assert.deepEqual(Object.keys(result.resources).sort(),names.slice().sort());
  for (const name of names) {
    assert.equal(result.resources[name].state,name === failed
      ? input.failure === 'preparing' ? 'preparing' : 'error' : 'ready');
    assert.equal(result.resources[name].loaded_at,name === failed ? oldTime : loadedTime);
    for (const key of fields[name]) {
      if (name === failed) assert.deepEqual(plain(result[key]),prior[key],`${failed} lost ${key}`);
      else assert.notDeepEqual(plain(result[key]),prior[key],`${name} failed to update ${key}`);
    }
  }
  assert.equal(timer.ms,5000);assert.equal(cleared.at(-1),19);
}
""", failure=failure)


def test_empty_success_replaces_retained_data_after_preparing():
    _run_loader_contract(r"""
context.fetch = async () => response(503,{state:'preparing'});
context.IMPROVEMENT_DATA = await context.loadImprovementData();
for (const name of names) assert.equal(context.IMPROVEMENT_DATA.resources[name].state,'preparing');
const empty = payloads(true);
context.fetch = async url => response(200,empty[resourceName(url)]);
const recovered = await context.loadImprovementData();
for (const key of ['findings','observations','loops','workflows','skills','measurements','rules']) {
  assert.deepEqual(plain(recovered[key]),[],`${key} retained old records after an empty success`);
}
for (const key of ['finding_total_count','observation_record_count','pending_workflows',
                   'active_interventions','skill_total_count','skill_archived_count']) assert.equal(recovered[key],0);
for (const key of ['counts_by_status','skill_counts_by_lifecycle','rule_extension']) {
  assert.deepEqual(plain(recovered[key]),{});
}
for (const name of names) {
  assert.equal(recovered.resources[name].state,'ready');
  assert.equal(recovered.resources[name].loaded_at,loadedTime);
}
""")


def test_successful_findings_reset_missing_optional_counts():
    _run_loader_contract(r"""
for (const findings of [[],[{id:'new-finding'}]]) {
  context.IMPROVEMENT_DATA = {...previous(),finding_total_count:99,
    observation_record_count:123,counts_by_status:{new:123}};
  const bodies = payloads();bodies.findings = {findings};
  context.fetch = async url => response(200,bodies[resourceName(url)]);
  const result = await context.loadImprovementData();
  assert.deepEqual(plain(result.observations),findings);
  assert.equal(result.finding_total_count,findings.length);
  assert.equal(result.observation_record_count,0);
  assert.deepEqual(plain(result.counts_by_status),{});
  assert.equal(result.resources.findings.state,'ready');
}
""")


def test_skills_readiness_is_separate_from_pending_analysis():
    _run_loader_contract(r"""
const bodies=payloads();
bodies.skills.analysis_state='pending';bodies.skills.skills[0].usage_count=null;
context.fetch=async url=>response(200,bodies[resourceName(url)]);
context.IMPROVEMENT_DATA=await context.loadImprovementData();
assert.equal(context.IMPROVEMENT_DATA.resources.skills.state,'ready');
assert.equal(context.IMPROVEMENT_DATA.skill_analysis_state,'pending');
assert.equal(context.IMPROVEMENT_DATA.skills[0].usage_count,null);
bodies.skills.analysis_state='ready';bodies.skills.skills[0].usage_count=4;
context.IMPROVEMENT_DATA=await context.loadImprovementData();
assert.equal(context.IMPROVEMENT_DATA.skill_analysis_state,'ready');
assert.equal(context.IMPROVEMENT_DATA.skills[0].usage_count,4);
bodies.skills.analysis_state='pending';bodies.skills.evidence_state='recorded';
bodies.skills.publication={state:'recorded',loaded_at:loadedTime};
context.IMPROVEMENT_DATA=await context.loadImprovementData();
assert.equal(context.IMPROVEMENT_DATA.resources.skills.state,'recorded');
assert.equal(context.IMPROVEMENT_DATA.skill_evidence_state,'recorded');
assert.equal(context.IMPROVEMENT_DATA.skills[0].usage_count,4);
""")


def test_server_cached_improvements_preserve_publication_freshness():
    _run_loader_contract(r"""
const bodies=payloads();
for (const body of Object.values(bodies)) body.publication={state:'stale',loaded_at:oldTime,reason:'preparing'};
context.fetch=async url=>response(200,bodies[resourceName(url)]);
const result=await context.loadImprovementData();
for(const name of names) {
  assert.equal(result.resources[name].state,'stale');
  assert.equal(result.resources[name].loaded_at,oldTime,'a cached response is not fresh evidence');
}
assert.equal(result.observations[0].id,'new-findings');
""")


def test_improvement_resources_preserve_distinct_publication_metadata_after_retry_failure():
    _run_loader_contract(r"""
const bodies=payloads();
bodies.workflows.publication={state:'stale',snapshot_completed_at:oldTime,loaded_at:loadedTime};
context.fetch=async url=>response(200,bodies[resourceName(url)]);
context.IMPROVEMENT_DATA=await context.loadImprovementData();
assert.equal(context.IMPROVEMENT_DATA.resources.workflows.loaded_at,oldTime);
assert.deepEqual(plain(context.IMPROVEMENT_DATA.resources.workflows.publication),bodies.workflows.publication);
context.fetch=async()=>response(503,{state:'preparing'});
context.IMPROVEMENT_DATA=await context.loadImprovementData();
assert.deepEqual(plain(context.IMPROVEMENT_DATA.resources.workflows.publication),bodies.workflows.publication);
context.fetch=async()=>{throw new Error('offline');};
const retained=await context.loadImprovementData();
assert.deepEqual(plain(retained.resources.workflows.publication),bodies.workflows.publication);
""")


def test_skills_retained_analysis_keeps_live_registry_and_pending_rows():
    _run_loader_contract(r"""
const bodies=payloads();
bodies.skills={analysis_state:'stale',total_count:2,skills:[
  {id:'retained',current_version:3,installation_count:2,analysis_state:'stale',
    usage_count:7,evidence_count:3,measurement_count:0},
  {id:'new',current_version:1,installation_count:1,analysis_state:'pending',
    usage_count:null,evidence_count:null,measurement_count:null}],
  publication:{state:'stale',snapshot_completed_at:oldTime,loaded_at:loadedTime}};
context.fetch=async url=>response(200,bodies[resourceName(url)]);
context.IMPROVEMENT_DATA=await context.loadImprovementData();
assert.equal(context.IMPROVEMENT_DATA.skill_analysis_state,'stale');
assert.equal(context.IMPROVEMENT_DATA.resources.skills.state,'stale');
assert.equal(context.IMPROVEMENT_DATA.resources.skills.loaded_at,oldTime);
assert.deepEqual(plain(context.IMPROVEMENT_DATA.skills),bodies.skills.skills);
context.fetch=async()=>response(503,{state:'preparing'});
const retained=await context.loadImprovementData();
assert.deepEqual(plain(retained.skills),bodies.skills.skills);
assert.equal(retained.resources.skills.loaded_at,oldTime);
assert.equal(retained.skill_analysis_state,'stale');
""")


def test_independent_reads_publish_before_other_requests_finish_and_timeout_separately():
    _run_loader_contract(r"""
const pending = new Map(), bodies = payloads(), signals = new Map(), publications = [];
context.fetch = (url, options) => new Promise((resolve,reject) => {
  const name=resourceName(url);
  pending.set(name,resolve);signals.set(name,options.signal);
  options.signal.addEventListener('abort',()=>reject(new Error('aborted')));
});
const loading = context.loadImprovementData((partial,name)=>publications.push({partial:plain(partial),name}));
assert.equal(pending.size,6,'all endpoints start independently');
assert.equal(new Set(signals.values()).size,6,'each endpoint owns its deadline');
pending.get('impact')(response(200,bodies.impact));
await new Promise(resolve=>setImmediate(resolve));
const published=publications.find(item=>item.name==='impact');
assert.equal(published.partial.measurements[0].id,'new-impact','Impact renders before Workflows settles');
let bodyStarted = false;
pending.get('loops')({ok:true,status:200,json:() => new Promise((_resolve,reject) => {
  bodyStarted=true;signals.get('loops').addEventListener('abort',()=>reject(new Error('body aborted')));
})});
await Promise.resolve();await Promise.resolve();
assert.ok(bodyStarted);
// Request order is findings, workflows, impact, rules, loops, skills.
timer.calls[1].fn();
assert.equal(signals.get('workflows').aborted,true);
assert.equal(signals.get('impact').aborted,false);
timer.calls[4].fn();
for (const name of ['findings','rules','skills']) pending.get(name)(response(200,bodies[name]));
const result = await loading;
for (const name of names) assert.equal(result.resources[name].state,
  ['workflows','loops'].includes(name)?'error':'ready');
assert.equal(result.measurements[0].id,'new-impact');
assert.equal(result.resources.workflows.loaded_at,oldTime);
assert.equal(cleared.length,6);
""")


def test_first_failed_load_has_no_success_timestamp():
    _run_loader_contract(r"""
context.IMPROVEMENT_DATA = {observations:[],loops:[],workflows:[],skills:[],measurements:[],rules:[]};
context.fetch = async () => {throw new Error('offline');};
const result = await context.loadImprovementData();
for (const name of names) {
  assert.equal(result.resources[name].state,'error');
  assert.equal(result.resources[name].loaded_at,null);
}
assert.deepEqual(plain(result.observations),[]);
""")


def test_cached_data_without_timestamp_remains_loaded_without_inventing_freshness():
    _run_loader_contract(r"""
context.IMPROVEMENT_DATA = {};
const bodies=payloads();
context.fetch=async url=>response(200,{...bodies[resourceName(url)],publication:{state:'stale'}});
context.IMPROVEMENT_DATA=await context.loadImprovementData();
for (const name of names) {
  assert.equal(context.IMPROVEMENT_DATA.resources[name].loaded,true);
  assert.equal(context.IMPROVEMENT_DATA.resources[name].loaded_at,null);
}
context.fetch=async()=>{throw new Error('offline');};
const retained=await context.loadImprovementData();
assert.equal(retained.observations[0].id,'new-findings');
assert.equal(retained.resources.findings.loaded,true);
assert.equal(retained.resources.findings.loaded_at,null);
""")
