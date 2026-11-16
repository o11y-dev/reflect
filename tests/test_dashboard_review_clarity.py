"""Regressions for scope and evidence units exposed by the product review."""

import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from reflect.dashboard_server import build_dashboard_app
from reflect.improvements.skills import SkillRegistryService
from reflect.store.sqlite import connect_sqlite


def test_skill_graph_all_time_keeps_old_evidence_and_pagination(tmp_path):
    db = tmp_path / "reflect.db"
    conn = connect_sqlite(db)
    registry = SkillRegistryService(conn)
    path = tmp_path / "skills" / "verify" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: verify\ndescription: Verify work\n---\nCheck changes.")
    registry.sync_paths([path])
    skill = registry.list()[0]
    for i, date in enumerate(("2010-01-01", "2026-11-08")):
        conn.execute("INSERT INTO sessions(id,started_at,created_at,updated_at) VALUES (?,?,?,?)",
                     (f"s{i}", date, date, date))
        conn.execute("""INSERT INTO skill_usage
            (id,skill_id,session_id,source_kind,source_id,state,observed_at,created_at,updated_at)
            VALUES (?,?,?,'tool_call',?,'observed',?,?,?)""",
                     (f"u{i}", skill.id, f"s{i}", f"tool{i}", date, date, date))
    conn.commit()
    conn.close()
    from test_dashboard_sql_api import _publish_report
    _publish_report(db)
    client = TestClient(build_dashboard_app(db_path=db, docs_dir=tmp_path))
    url = f"/api/skills/{skill.id}/usage-graph"
    response = client.get(url + "?days=all&limit=1")
    assert response.status_code == 200
    payload = response.json()
    assert payload["counts"]["occurrences"] == 2
    assert payload["scope"]["date_from"] is None
    assert len(payload["records"]) == 1
    assert payload["pagination"]["next_offset"] == 1
    second = client.get(url + "?days=all&limit=1&offset=1").json()
    assert second["counts"] == payload["counts"]
    assert second["records"][0]["session_id"] == "s0"
    assert len(second["nodes"]) <= 75
    assert client.get(url + "?days=bad").status_code == 422


def _run(functions, scenario):
    source = ""
    for filename, start, end in functions:
        text = Path("src/reflect/frontend", filename).read_text()
        source += text[text.index(start):text.index(end, text.index(start))]
    script = """
const assert=require('node:assert/strict'),vm=require('node:vm');
const c={escHtml:String,fmt:String,fmtTokenShort:String,fmtDur:String,
  humanizeLedgerLabel:String,ledgerWorkspaceName:String,fmtWorkflowDate:String,
  compactLedgerId:String,sessionInspectionUrl:x=>'?session='+x.session_id};
vm.createContext(c);vm.runInContext(JSON.parse(require('node:fs').readFileSync(0,'utf8')),c);
(async()=>{
""" + scenario + "\n})().catch(e=>{console.error(e);process.exitCode=1;});"
    result = subprocess.run(["node", "-e", script], input=json.dumps(source), text=True,
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_skill_occurrences_are_never_labelled_distinct_sessions():
    _run([("js_ledger.js", "function renderWorkflowSessionLedger(", "function staleEvidenceNotice(")], """
const rows=Array.from({length:6},()=>({session_id:'same',title:'Same session'}));
const html=c.renderWorkflowSessionLedger(rows,{total:429,occurrences:true});
assert.match(html,/429 captured references/);
assert.match(html,/More References/);
assert.ok(!html.includes('429 linked sessions'));
""")


def test_session_insights_precede_collapsed_assessment_audit():
    _run([("js_sessions.js", "  function renderObservedMeasurements(", "  function renderGitActivity(")], """
const html=c.renderObservedMeasurements({measurements:{},
  insights:[{title:'Repeated failures',summary:'Inspect recovery',metric_value:4,metric_unit:'calls'}],
  assessments:Array.from({length:20},()=>({outcome:'unknown',verification_passed:null}))});
assert.ok(html.indexOf('Repeated failures') < html.indexOf('<details'));
assert.match(html,/<details[^>]*><summary>.*execution assessments/);
assert.ok(!html.includes('<details open'));
assert.match(html,/Outcome: unknown/);
""")


def test_impact_drilldown_explains_excluded_candidates():
    _run([
        ("js_impact.js", "function impactCohortDetails(", "function impactScope("),
        ("js_ledger.js", "async function showImpactSessions(", "document.addEventListener("),
    ], """
let rendered;
c.beginLedgerRead=()=>{const request={};c.ledgerDialog={inspection:request};return request;};
c.presentLedgerRead=(request,value)=>rendered=value;c.failLedgerRead=()=>assert.fail('render failed');
c.impactMetricPresentation=()=>({goal:'Fewer failures'});c.staleEvidenceNotice=()=>'';
c.renderWorkflowSessionLedger=(rows,options)=>options.empty;c.formatImpactSessionValue=String;
const eligibility={candidate_count:259,eligible_count:0,excluded_count:259,selected_count:0,
  metric_count:0,missing_metric_count:0,exclusion_reasons:{missing_verification:259}};
c.ledgerRequest=async()=>({before_execution_units:[],after_execution_units:[],before_count:0,after_count:0,
  eligibility:{before:eligibility,after:eligibility},baseline_state:'historical_unavailable'});
await c.showImpactSessions('one');
assert.match(rendered.body,/259 candidates · 0 eligible/);
assert.match(rendered.body,/259 excluded/);assert.match(rendered.body,/missing_verification: 259/);
assert.ok(!rendered.body.includes('have been collected yet'));
""")
