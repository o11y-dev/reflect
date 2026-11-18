"""Metadata-only capture still provides identifiable, honestly scored sessions."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _run(source: str, scenario: str, setup: str = "") -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for browser contracts")
    script = """
const assert=require('node:assert/strict'),vm=require('node:vm');
const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const context={formatAgentLabel:value=>value,escHtml:value=>String(value)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;')};
""" + setup + "\nvm.createContext(context);vm.runInContext(source,context);\n" + scenario
    result = subprocess.run([node, "-e", script], input=json.dumps(source), text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_metadata_identity_and_scores_do_not_invent_missing_evidence():
    source = Path("src/reflect/frontend/js_sessions.js").read_text()
    _run(source[:source.index("class SessionConversationPlayhead")], r"""
const identity=context.sessionListIdentity;
assert.deepEqual(JSON.parse(JSON.stringify(identity({
  native_title:'  Retry dashboard refresh  ', first_prompt:'Check build', agent:'cursor'
}))), {title:'Retry dashboard refresh',missingPrompt:false});
assert.equal(identity({title:'Saved title',first_prompt:'Check build',agent:'windsurf'}).title,'Saved title');
assert.equal(identity({first_prompt:'  Check build  ',primary_model:'model'}).title,'Check build');
assert.equal(identity({first_prompt:'  Check build  '}).missingPrompt,false);
assert.equal(identity({first_prompt:' ',primary_model:'recorded-model',agent:'cursor',full_id:'one-12345678'}).title,
  'recorded-model · 12345678');
assert.equal(identity({agent:'claude',id:'two-87654321'}).title,'claude · 87654321');
assert.equal(identity({}).title,'Session');
assert.equal(identity({primary_model:'recorded-model'}).missingPrompt,true);
assert.notEqual(identity({id:'prefix-one'}).title,identity({id:'prefix-two'}).title);
const badge=context.renderSessionQualityBadge;
assert.match(badge({quality_available:true,quality_score:93,quality_coverage_pct:70}),/Heuristic 93\/100/);
assert.match(badge({quality_available:true,quality_score:93,quality_coverage_pct:70}),/70% evidence coverage/);
assert.match(badge({quality_available:true,quality_score:93}),/coverage unavailable/);
assert.match(badge({quality_available:true,quality_score:null}),/No score/);
assert.match(badge({quality_available:false,quality_score:93}),/No score/);
""")


def test_escape_closes_evidence_and_preserves_session_filters():
    source = Path("src/reflect/frontend/js_ledger.js").read_text()
    _run(source[source.index("const ledgerDialog ="):source.index("async function ledgerRequest(")], """
listeners.cancel({preventDefault:()=>{}});
assert.equal(dialog.inspection,null);
for(const key of ['workflow','workflow_root','loop','skill']) assert.equal(params.has(key),false);
assert.equal(params.get('range'),'7d');assert.equal(params.get('session'),'selected');
assert.equal(params.get('tab'),'workflows');
dialog.open=true;dialog.inspection='new';params.set('workflow','new');
context.closeLedgerDialog({fromHistory:true});
assert.equal(dialog.inspection,null);assert.equal(params.get('workflow'),'new','history synchronization must not rewrite the browser route');
""", setup="""
const listeners={},dialog={inspection:'workflow',open:true,close(){this.open=false;},addEventListener:(name,fn)=>listeners[name]=fn};
const params=new URLSearchParams('workflow=one&workflow_root=root&loop=two&skill=three&range=7d&session=selected&tab=workflows');
context.document={getElementById:()=>dialog};context.window={history:{}};context.updateUrlParams=fn=>fn(params);
""")
