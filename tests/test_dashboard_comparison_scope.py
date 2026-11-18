"""Comparison tables disclose their lifetime scope beside period metrics."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_comparison_scope_is_visible_and_does_not_recalculate_lifetime_values():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for browser rendering contract checks")
    source = Path("src/reflect/frontend/js_impact.js").read_text()
    renderer = source[source.index("function buildCohortComparison(){"):
                      source.index("document.addEventListener('dashboard-filters-changed'")]
    script = r"""
const assert = require('node:assert/strict'), vm = require('node:vm');
const renderer = JSON.parse(require('node:fs').readFileSync(0,'utf8'));
for (const windowed of [true,false]) for (const filtered of [true,false]) {
  const elements = Object.fromEntries(['cohort-comparison-summary','cohort-comparison-details','cohort-baseline-agents'].map(id=>[id,{innerHTML:''}]));
  const row = {name:'codex',sessions:189,prompts:1193,tools:43282,total_cost_usd:1374.83,failures:12,avg_quality:50};
  const summary = {label:'codex',agents:['codex'],sessions:189,prompts:1193,tool_calls:43282,
    avg_quality:50,failure_rate_pct:1,tokens:123,shell_runs:12,mcp_calls:3};
  const cohort = filtered ? {comparison:{primary:summary,baseline:{...summary,label:'Other agents'},
    deltas:{},baseline_agents:[row]}} : {agent_comparison:[row]};
  const context = {D:{measurements:{scope:{kind:windowed?'event_window':'session_lifetime'}}},
    document:{getElementById:id=>elements[id]},sqlTab:name=>name==='cohort_comparison'?cohort:{},
    currentAgentComparison:()=>[],fmt:String,fmtCost:String,escHtml:String,formatSignedDelta:String};
  vm.createContext(context);
  vm.runInContext(renderer,context);
  context.buildCohortComparison();
  const table=elements['cohort-comparison-details'].innerHTML;
  assert.match(table,/<caption[^>]*><strong>Full lifetime of matching sessions<\/strong>/);
  assert.equal(table.includes('Includes activity outside the selected period'),windowed);
  assert.equal(table.includes('period totals count only events within it.'),windowed);
  assert.ok(!table.includes('period totals above'));
  assert.ok(table.includes('1193') && table.includes('43282'),'Lifetime values must remain intact');
  const costs=filtered?elements['cohort-baseline-agents'].innerHTML:table;
  assert.ok(costs.includes('1374.83'));
  if (filtered) assert.match(costs,/<caption/,'Baseline cost table must disclose the same scope');
}
"""
    subprocess.run([node,"-e",script], input=json.dumps(renderer), text=True, capture_output=True, check=True)
