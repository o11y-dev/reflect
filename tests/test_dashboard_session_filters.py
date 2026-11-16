"""Sessions keeps canonical filter membership and its default activity window."""
import json
import shutil
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_dashboard_sql_api import _seed_sql_report_db

from reflect.dashboard_queries import build_dashboard_payload
from reflect.dashboard_server import build_dashboard_app
from reflect.store.sqlite import connect_sqlite


def _browser(scenario, **data):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for Sessions filter contracts")
    source = Path("src/reflect/frontend/js_sessions.js").read_text()
    parts = {
        "demo": Path("src/reflect/frontend/js_demo.js").read_text(),
        "initialize": source[source.index("(function initSessionBrowser(){"):
                             source.index("  function sessionTimeMs(")],
        "time": source[source.index("  function sessionTimeMs("):
                       source.index("  function persistFilters(")],
        "persist": source[source.index("  function persistFilters("):
                          source.index("  function scheduleDashboardReload(")],
        "clear": source[source.index("  clearBtn.addEventListener('click'"):
                        source.index("  compactToggleBtn?.addEventListener(")],
        "filter": source[source.index("  function filteredSessions("):
                         source.index("  function renderSessionList(")],
    }
    script = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const params=new URLSearchParams(input.query || 'report=/api/data&tab=sessions');
const elements=new Map();
function element(id){
  if(!elements.has(id)) elements.set(id,{value:'',dataset:{},handlers:{},children:[],
    addEventListener(name,fn){this.handlers[name]=fn;},appendChild(child){this.children.push(child);}});
  return elements.get(id);
}
let reloads=0;
const context={D:input.payload || {sql_backed:true,sessions:[],range:'7d'},
  currentParams:()=>params,updateUrlParams:fn=>fn(params),
  reportSupportsServerFiltering:()=>true,sqlTab:()=>({}),
  document:{addEventListener:()=>{},getElementById:element,createElement:()=>element(Math.random())},
  agentIconSvg:()=>'',formatAgentLabel:String,escHtml:String,
  scheduleDashboardReload:()=>{reloads++;return true;},renderSessionList:()=>{},
  dashboardSelectedSessionId:'selected',Date};
vm.createContext(context);
vm.runInContext(input.demo,context);
""" + scenario + r"""
process.stdout.write('filters-ok\n');
"""
    result = subprocess.run([node, "-e", script], input=json.dumps({**parts, **data}),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "filters-ok" in result.stdout


@pytest.mark.parametrize("query,expected", [
    ("report=/api/data&tab=sessions", "7d"),
    ("report=/api/data&tab=sessions&range=30d", "30d"),
    ("report=/api/data&tab=sessions&session=historical", "all"),
    ("report=/api/data&tab=sessions&session=historical&range=all", "all"),
    ("report=/api/data&tab=explore&view=tools", "7d"),
])
def test_default_range_survives_selection_and_explicit_history_is_preserved(query, expected):
    _browser(r"""
vm.runInContext(input.initialize+'globalThis.state=filterState;})();',context);
assert.equal(context.state.range,input.expected);
if(params.get('tab')==='explore') {
  assert.equal(params.get('range'),null,'initialization must not change an active Explore request scope');
  assert.equal(params.toString(),new URLSearchParams(input.query).toString(),'Explore hydration keeps its captured URL');
} else if(!params.has('session')) {
  params.set('session','selected');
  assert.equal(params.get('range'),input.expected,'selection/reload must retain the activity window');
} else if(!input.query.includes('range=')) assert.equal(params.get('range'),null);
assert.equal(params.get('tab'),new URLSearchParams(input.query).get('tab'));
""", query=query, expected=expected)


def test_empty_results_keep_selected_filter_controls_and_clear_to_seven_days():
    _browser(r"""
vm.runInContext(input.initialize+input.persist+input.clear+'globalThis.state=filterState;})();',context);
assert.ok(context.state,'empty results must still initialize filter controls');
assert.ok(elements.get('filter-model').children.some(option=>option.value==='secondary'));
assert.ok(elements.get('sb-agent-pills').children.some(pill=>pill.dataset.agent==='codex'));
elements.get('filter-clear').handlers.click();
assert.equal(context.state.range,'7d');
assert.equal(elements.get('filter-range').value,'7d');
assert.equal(params.get('range'),'7d');
assert.equal(params.get('tab'),'sessions');
assert.equal(params.get('report'),'/api/data');
for(const key of ['q','agents','model','status','session']) assert.equal(params.get(key),null);
assert.equal(reloads,1);
""", query="report=/api/data&tab=sessions&q=missing&agents=codex&model=secondary&range=all")


def test_sql_filtered_page_is_not_refiltered_using_card_summaries():
    _browser(r"""
Object.assign(context,{sessions:context.D.sessions,initialSessionId:'',
  filterState:{q:'canonical title',agents:new Set(['codex']),model:'secondary',
    status:'completed',range:'7d',sort:'recent'}});
vm.runInContext(input.time+input.filter,context);
assert.equal(context.filteredSessions().length,1,
  'SQL matches recent activity/any model/title, not creation date/primary model/first prompt');
context.D._navigationFromCache=true;
assert.equal(context.filteredSessions().length,1,'retained exact-scope rows keep their SQL membership');
context.D.sql_backed=false;
assert.equal(context.filteredSessions().length,0,'static reports still apply their local filters');
context.D.sql_backed=true;context.D._unfilteredNavigation=true;
assert.equal(context.filteredSessions().length,0,'unscoped fallback navigation still applies local filters');
context.initialSessionId='long-lived';
assert.equal(context.filteredSessions().length,1,'fallback navigation preserves the linked session');
""", payload={"sql_backed": True, "range": "7d", "session_list_total": 1, "sessions": [{
        "id": "long-lived", "created_at": "2020-01-01T00:00:00Z", "agent": "codex",
        "primary_model": "primary", "first_prompt": "different preview", "is_completed": True,
    }]})


@pytest.fixture
def scoped_db(tmp_path, monkeypatch):
    class SnapshotTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 5, 2, tzinfo=UTC)

    monkeypatch.setattr("reflect.dashboard_queries.datetime", SnapshotTime)
    path = tmp_path / "sessions.db"
    _seed_sql_report_db(path)
    with closing(connect_sqlite(path)) as conn:
        conn.execute("UPDATE sessions SET started_at='2026-04-01T00:00:00Z' WHERE id='sess-sql'")
        conn.execute("UPDATE session_rollups SET started_at='2026-04-01T00:00:00Z' WHERE session_id='sess-sql'")
        for name, started, activity in (
            ("other", "2026-04-02", "2026-05-01"),
            ("cold", "2026-03-01", "2026-03-01"),
        ):
            conn.execute("""INSERT INTO sessions(id,agent_id,started_at,status,title,created_at,updated_at)
                VALUES (?,'agent-codex',?,'completed',?,?,?)""", (name, started, name, started, started))
            conn.execute("""INSERT INTO steps(id,session_id,seq,type,started_at,created_at,updated_at)
                VALUES (?,?,1,'llm_call',?,?,?)""", (name, name, activity, activity, activity))
        conn.commit()
    return path


def test_default_seven_day_sql_scope_counts_activity_before_navigation_pagination(scoped_db):
    from test_dashboard_sql_api import _publish_report
    _publish_report(scoped_db)
    client = TestClient(build_dashboard_app(docs_dir=scoped_db.parent, db_path=scoped_db))
    default = client.get("/api/data").json()
    assert default["range"] == "7d"
    assert {row["id"] for row in default["sessions"]} == {"sess-sql", "other"}
    assert default["session_list_total"] == 2
    pages = [build_dashboard_payload(scoped_db, range_name="7d", limit=1, offset=offset,
             lazy_all_tabs=True, include_comparison=False) for offset in (0, 1)]
    assert {page["sessions"][0]["id"] for page in pages} == {"sess-sql", "other"}
    for page in pages:
        assert page["session_list_total"] == page["sqlite"]["sessions"]["total"] == 2
        assert page["sqlite"]["overview"]["session_count"] == 2
    explicit = client.get("/api/data?session=cold&range=all").json()
    assert explicit["focused_session_id"] == "cold"
    assert any(row["id"] == "cold" for row in explicit["sessions"])
