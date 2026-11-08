import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_shared_procedure_panel_preserves_unknowns_and_counterexamples():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for browser behavior tests')
    source = Path('src/reflect/frontend/js_ledger.js').read_text()
    start = source.index('function renderProcedureEvidence(')
    end = source.index('\nfunction ', start + 1)
    renderer = source[start:end]
    script = r'''
const assert = require('node:assert/strict'), vm = require('node:vm');
const context = {escHtml:value=>String(value).replaceAll('<','&lt;').replaceAll('>','&gt;'),
  fmt:String, humanizeLedgerLabel:String,
  sessionInspectionUrl:item=>'/session/'+encodeURIComponent(item.session_id)};
vm.createContext(context);vm.runInContext(JSON.parse(require('fs').readFileSync(0,'utf8')),context);
assert.equal(context.renderProcedureEvidence({}), '');
const html = context.renderProcedureEvidence({procedure:{
  match_explanation:'Same operations on <payments>', execution_count:4, session_count:4,
  operations:[{operation:'modify',target:'src/payments/*.py'}],
  verification_counts:{passed:3,invalidated:1},outcome_counts:{unknown:4},
  supporting_examples:[{execution_unit_id:'one',session_id:'s1',verification:'passed',outcome:'unknown'}],
  counterexamples:[{execution_unit_id:'two',session_id:'s2',verification:'invalidated',outcome:'unknown'}]
},existing_workflow_matches:[{id:'existing',title:'Existing procedure',status:'active'}]});
assert.match(html,/Why these executions match/);
assert.match(html,/3 of 4/);
assert.match(html,/unknown: 4/);
assert.match(html,/invalidated/);
assert.match(html,/\/session\/s2/);
assert.match(html,/Existing procedure/);
assert.match(html,/data-candidate-id="existing"/);
assert.ok(!html.includes('<payments>'));
assert.ok(!html.includes('verified success'));
'''
    run = subprocess.run([node, '-e', script], input=json.dumps(renderer), text=True,
                         capture_output=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert source.count('renderProcedureEvidence(') >= 3  # shared by workflow and skill detail
