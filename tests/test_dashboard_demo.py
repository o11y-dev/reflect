"""Executable contracts for the public report's read-only synthetic source."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_static_demo_reads_fail_closed_and_never_send_mutations():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for dashboard contracts")
    source_path = Path("src/reflect/frontend/js_demo.js")
    assert source_path.exists(), "Static demo source has not been implemented"
    script = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const calls=[];
const context={URL,URLSearchParams,console,Date,
  window:{location:{href:'http://localhost/report.html?report=reports/showcase.json',
    origin:'http://localhost',protocol:'http:',hostname:'localhost'}},
  fetch:async(...args)=>{calls.push(args);return {ok:true,json:async()=>({local:true})};}};
vm.createContext(context);vm.runInContext(source,context);
vm.runInContext(`globalThis.Source = StaticReportSource`,context);
const resources=Object.fromEntries(['findings','workflows','impact','skills','rules','loops'].map(n=>['/api/'+n,{[n === 'impact' ? 'impact_checks' : n]:[]}]));
resources['/api/session/demo-one']={session:{id:'demo-one'},conversation:[]};
resources['/api/explore/usage']={total_sessions:1};
const bundle={sessions:[{id:'demo-one'}],sqlite:{tabs:{usage:{}}},demo:{schema_version:1,
  clock:'2026-11-09T12:00:00Z',default_scenario:'retry-recovery',
  scenarios:[{id:'retry-recovery',session_id:'demo-one',title:'Retries',description:'Illustrative'}],resources}};
(async()=>{
  const reader=new context.Source(bundle);
  const result=await reader.read('/api/session/demo-one');
  assert.equal(result.ok,true);assert.equal((await result.json()).session.id,'demo-one');
  const absent=await reader.read('/api/session/missing');
  assert.equal(absent.status,404);assert.match((await absent.json()).error,/sample/i);
  for(const method of ['POST','PUT','DELETE','PATCH']){
    const denied=await reader.read('/api/workflows/example/apply',{method});
    assert.equal(denied.status,405);assert.match((await denied.json()).error,/read.only/i);
  }
  assert.equal((await reader.read('https://outside.invalid/api/skills')).status,400);
  const copy=await (await reader.read('/api/session/demo-one')).json();copy.session.id='changed';
  assert.equal((await (await reader.read('/api/session/demo-one')).json()).session.id,'demo-one');
  assert.throws(()=>new context.Source({...bundle,demo:{...bundle.demo,schema_version:99}}),/version/i);
  assert.throws(()=>new context.Source({...bundle,demo:{...bundle.demo,clock:'invalid'}}),/clock/i);
  const incomplete={...resources};delete incomplete['/api/impact'];
  assert.throws(()=>new context.Source({...bundle,demo:{...bundle.demo,resources:incomplete}}),/impact/i);
  assert.throws(()=>new context.Source({...bundle,demo:{...bundle.demo,resources:{...resources,'/api/skills':{}}}}),/skills/i);
  context.configureReportSource(bundle);
  assert.equal(context.reportClockNow(),Date.parse(bundle.demo.clock));
  assert.equal((await context.reportReadResponse('/api/skills?limit=500')).ok,true);
  assert.equal(calls.length,0,'static reads and writes never fall back to the network');
  context.configureReportSource({sql_backed:true});
  assert.equal((await (await context.reportReadResponse('/api/skills')).json()).local,true);
  assert.equal(calls.length,1,'local API continues to use fetch');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", script], input=json.dumps(source_path.read_text()),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout
