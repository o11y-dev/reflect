from __future__ import annotations

import subprocess
from pathlib import Path


def _run(script: str) -> None:
    source = Path("src/reflect/frontend/js_skill_usage.js").read_text()
    prelude = """
const assert=require('node:assert/strict'),vm=require('node:vm');
const context={URLSearchParams,AbortController,console,
  window:{location:{search:''},setTimeout,clearTimeout},
  fmt:String,escHtml:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
  sessionInspectionUrl:item=>'?session='+encodeURIComponent(item.session_id)};
vm.createContext(context);
"""
    result = subprocess.run(["node", "-e", prelude + "vm.runInContext(" + repr(source) + ",context);\n" + script],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_skill_usage_graph_labels_unknowns_and_escapes_source_text():
    _run("""
const html=context.skillUsageGraphMarkup({analysis_state:'pending',
  nodes:[{id:'skill',kind:'Skill',label:'Verify'},{id:'run',kind:'Execution',label:'<script>bad</script>'}],
  edges:[{source:'skill',target:'run',kind:'mentioned',evidence_class:'inferred'}],
  counts:{executions:1,sessions:1,occurrences:2},pagination:{next_offset:20},
  records:[{id:'run',session_id:'a&b',title:'<script>bad</script>',occurrence_count:2,
    evidence:[{relationship:'loaded',evidence_class:'observed',source_id:'step',evidence:{path:'<img src=x>'}}]}]});
assert.ok(html.includes('Recorded evidence'));
assert.ok(html.includes('version unknown'));
assert.ok(html.includes('Execution unknown'));
assert.ok(html.includes('inferred'));
assert.ok(html.includes('Evidence table'));
assert.ok(html.includes('Next executions'));
assert.ok(!html.includes('<script>'));
assert.ok(!html.includes('<img src=x>'));
assert.ok(html.includes('?session=a%26b'));
""")


def test_skill_usage_graph_ignores_late_response_and_closed_dialog():
    _run("""
(async()=>{
const pending=[];
context.fetch=()=>new Promise(resolve=>pending.push(resolve));
const container={isConnected:true,innerHTML:'',querySelectorAll:()=>[],querySelector:()=>null};
const payload=count=>({analysis_state:'ready',nodes:[],edges:[],records:[],counts:{executions:0,sessions:0,occurrences:count},pagination:{}});
const response=count=>({ok:true,json:async()=>payload(count)});
const first=context.loadSkillUsageGraph('skill',container);
const second=context.loadSkillUsageGraph('skill',container,{version:'unknown'});
pending[1](response(222));await second;
assert.ok(container.innerHTML.includes('222 references'));
pending[0](response(111));await first;
assert.ok(container.innerHTML.includes('222 references'));
const third=context.loadSkillUsageGraph('skill',container);
container.isConnected=false;pending[2](response(333));await third;
assert.ok(container.innerHTML.includes('222 references'));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
