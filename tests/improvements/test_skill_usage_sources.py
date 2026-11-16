from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from reflect.dashboard_skill_graph import build_skill_usage_graph
from reflect.improvements.skills import SkillRegistryService
from reflect.store.sqlite import connect_sqlite, mark_snapshot
from reflect.task_runs import MCPSelectedSkillRef, MCPTaskOutcome, TaskRunReconciler

NOW = "2026-11-08T10:00:00+00:00"


@pytest.fixture
def usage(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    registry = SkillRegistryService(conn)
    path = tmp_path / "repo" / ".agents" / "skills" / "verify-first" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: verify-first\ndescription: Verify changes\n---\nRun tests.\n")
    registry.sync_paths([path])
    skill = registry.list()[0]
    for agent in ("codex", "cursor", "claude"):
        conn.execute("INSERT INTO agents(id,name,created_at,updated_at) VALUES (?,?,?,?)", (agent,agent,NOW,NOW))
        conn.execute("INSERT INTO sessions(id,agent_id,started_at,title,status,created_at,updated_at) VALUES (?,?,?,'Work','completed',?,?)", (f"s-{agent}",agent,NOW,NOW,NOW))
    yield conn, registry, skill, path
    conn.close()


def add_read(conn, path, number, *, agent="codex", owner=True, attrs=None, status="ok", tool_name="Read"):
    sid, step, tool, execution = f"s-{agent}",f"step-{agent}-{number}",f"tool-{agent}-{number}",f"execution-{agent}-{number}"
    raw = attrs or {"gen_ai.client.tool.input": json.dumps({"file_path":str(path)})}
    conn.execute("""INSERT INTO steps(id,session_id,seq,type,started_at,status,raw_attrs_json,created_at,updated_at)
        VALUES (?,?,?,'tool_call',?,?,?,?,?)""", (step,sid,number,NOW,status,json.dumps(raw),NOW,NOW))
    conn.execute("""INSERT INTO tool_calls(id,session_id,step_id,tool_name,status,raw_attrs_json,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?)""", (tool,sid,step,tool_name,status,json.dumps(raw),NOW,NOW))
    if owner:
        conn.execute("""INSERT INTO execution_units(id,session_id,source,started_at,created_at,updated_at)
            VALUES (?,?,'native_turn',?,?,?)""", (execution,sid,NOW,NOW,NOW))
        conn.execute("INSERT INTO execution_unit_steps(execution_unit_id,step_id,session_id,created_at) VALUES (?,?,?,?)", (execution,step,sid,NOW))
    return step, execution


def test_occurrences_keep_each_execution_unknown_versions_and_noop(usage):
    conn, registry, skill, path = usage
    add_read(conn,path,1)
    add_read(conn,path,2)
    assert registry.sync_usage() == 2
    detail = registry.show(skill.id)
    assert {item.execution_unit_id for item in detail.usage_occurrences} == {"execution-codex-1","execution-codex-2"}
    assert all(item.skill_version_id is None for item in detail.usage_occurrences)
    assert all(item.evidence["relationship"] == "loaded" for item in detail.usage_occurrences)
    before = conn.execute("SELECT id,updated_at FROM skill_usage ORDER BY id").fetchall()
    registry.sync_usage()
    assert conn.execute("SELECT id,updated_at FROM skill_usage ORDER BY id").fetchall() == before


def test_captured_hash_and_missing_membership(usage):
    conn, registry, skill, path = usage
    digest = conn.execute("SELECT content_hash FROM skill_versions WHERE id=?", (skill.current_version_id,)).fetchone()[0]
    add_read(conn,path,1,owner=False,attrs={"gen_ai.client.file_path":str(path),"skill.content_hash":digest})
    registry.sync_usage()
    item=registry.show(skill.id).usage_occurrences[0]
    assert item.execution_unit_id is None
    assert item.skill_version_id == skill.current_version_id
    assert conn.execute("SELECT COUNT(*) FROM execution_units").fetchone()[0] == 0


def test_reports_do_not_overwrite_observations(usage):
    conn, registry, skill, path = usage
    _, execution = add_read(conn,path,1)
    registry.sync_usage()
    TaskRunReconciler(conn)._record_skill_outcomes("s-codex", execution,
        [MCPSelectedSkillRef(skill_id=skill.id,slug=skill.slug,version_id=skill.current_version_id)],
        task_run_id="report", outcome=MCPTaskOutcome.SUCCESS,verification_passed=True,now=NOW)
    registry.sync_usage()
    rows=registry.show(skill.id).usage_occurrences
    assert len(rows) == 2
    assert {row.state for row in rows} == {"observed","reported"}
    assert next(row for row in rows if row.state=="observed").skill_version_id is None
    TaskRunReconciler(conn)._record_skill_outcomes("s-codex", execution,
        [MCPSelectedSkillRef(skill_id=skill.id,slug=skill.slug,version_id="")], task_run_id="report",
        outcome=MCPTaskOutcome.SUCCESS,verification_passed=None,now=NOW)
    rows=registry.show(skill.id).usage_occurrences
    assert len(rows) == 2  # Correcting a report does not create a new source.
    assert next(row for row in rows if row.state=="reported").skill_version_id is None


@pytest.mark.parametrize("agent,attrs,tool_name,status,relationship", [
    ("codex",{"gen_ai.client.tool.input":json.dumps({"cmd":"cat PATH"}),"process.exit.code":0},"exec_command","ok","loaded"),
    ("claude",{"gen_ai.client.tool.input":json.dumps({"file_path":"PATH"})},"Read","error","load_attempted"),
    ("cursor",{"gen_ai.client.file_path":"PATH"},"read_file","ok","loaded"),
])
def test_harness_shapes(usage,agent,attrs,tool_name,status,relationship):
    conn, registry, skill, path = usage
    attrs=json.loads(json.dumps(attrs).replace("PATH",str(path)))
    add_read(conn,path,1,agent=agent,attrs=attrs,tool_name=tool_name,status=status)
    registry.sync_usage()
    assert registry.show(skill.id).usage_occurrences[0].evidence["relationship"] == relationship


def test_correction_retraction_and_source_scope(usage):
    conn, registry, skill, path = usage
    step, _=add_read(conn,path,1)
    other, _=add_read(conn,path,2)
    registry.sync_usage()
    conn.execute("UPDATE tool_calls SET raw_attrs_json='{}' WHERE step_id=?", (step,))
    conn.execute("UPDATE steps SET raw_attrs_json='{}' WHERE id=?", (step,))
    registry.sync_usage(step_ids={step})
    assert [item.evidence["step_id"] for item in registry.show(skill.id).usage_occurrences] == [other]
    conn.execute("DELETE FROM steps WHERE id=?", (other,))
    registry.sync_usage(step_ids={other})
    assert registry.show(skill.id).usage_occurrences == []


def test_graph_counts_pagination_unknown_version_and_pending(usage):
    conn, registry, skill, path = usage
    for i in range(25):
        add_read(conn,path,i)
    registry.sync_usage()
    mark_snapshot(conn,ready=False)
    now=datetime(2026,11,8,11,tzinfo=UTC)
    first=build_skill_usage_graph(conn,skill.id,now=now)
    second=build_skill_usage_graph(conn,skill.id,now=now,offset=20)
    assert first["counts"] == second["counts"]
    assert first["counts"]["executions"] == 25
    assert first["pagination"]["next_offset"] == 20
    assert len(first["records"]) == 20 and len(second["records"]) == 5
    assert len(first["nodes"]) <= 75 and len(first["edges"]) <= 150
    assert first["analysis_state"] == "pending"
    assert build_skill_usage_graph(conn,skill.id,now=now,version=skill.current_version_id)["counts"]["occurrences"] == 0
    assert build_skill_usage_graph(conn,skill.id,now=now,version="unknown")["counts"]["occurrences"] == 25
    assert build_skill_usage_graph(conn,skill.id,now=now,agents={"claude"})["counts"]["occurrences"] == 0


def test_graph_cold_api_read_during_refresh(usage):
    from pathlib import Path

    from fastapi.testclient import TestClient

    from reflect.dashboard_server import build_dashboard_app

    conn, registry, skill, path = usage
    add_read(conn,path,1)
    registry.sync_usage()
    conn.commit()
    db_path=Path(conn.execute("PRAGMA database_list").fetchone()[2])
    from test_dashboard_sql_api import _publish_report
    _publish_report(db_path)
    mark_snapshot(conn, ready=False)
    conn.commit()
    client=TestClient(build_dashboard_app(db_path=db_path,docs_dir=path.parent))
    response=client.get(f"/api/skills/{skill.id}/usage-graph?days=3650")
    assert response.status_code == 200
    assert response.json()["analysis_state"] == "ready"
    assert client.get(f"/api/skills/{skill.id}/usage-graph?limit=500").status_code == 422


def test_name_mentions_and_hook_attempts_do_not_gain_the_current_version(usage):
    conn, registry, skill, path = usage
    add_read(conn,path,1,tool_name="Skill",attrs={"skill.name":skill.slug})
    step, _ = add_read(conn,path,2,attrs={"gen_ai.client.hook.event":"BeforeReadFile","gen_ai.client.file_path":str(path)})
    conn.execute("DELETE FROM tool_calls WHERE step_id=?", (step,))
    registry.sync_usage()
    path.write_text(path.read_text()+"\nAn updated procedure.\n")
    registry.sync_paths([path])
    registry.sync_usage()
    rows=registry.show(skill.id).usage_occurrences
    assert {row.evidence["relationship"] for row in rows} == {"mentioned","load_attempted"}
    assert all(row.skill_version_id is None for row in rows)


def test_unresolved_expansion_has_bounded_evidence_and_exact_pagination(usage):
    conn, registry, skill, path = usage
    for i in range(25):
        add_read(conn,path,i,owner=False)
    add_read(conn,path,100)
    registry.sync_usage()
    now=datetime(2026,11,8,11,tzinfo=UTC)
    first=build_skill_usage_graph(conn,skill.id,now=now,session_id="s-codex")
    second=build_skill_usage_graph(conn,skill.id,now=now,session_id="s-codex",evidence_offset=20)
    assert first["counts"] == second["counts"]
    assert first["counts"]["occurrences"] == 25
    assert first["counts"]["executions"] == 0
    assert len(first["records"][0]["evidence"]) == 20
    assert len(second["records"][0]["evidence"]) == 5
    assert first["pagination"]["next_evidence_offset"] == 20
    assert second["pagination"]["next_evidence_offset"] is None
    assert len(first["nodes"]) <= 75 and len(first["edges"]) <= 150


def test_measurement_version_requires_the_captured_applied_hash(usage):
    from reflect.improvements.service import ImprovementService

    conn, registry, skill, path = usage
    old_version=skill.current_version_id
    old_hash=conn.execute("SELECT content_hash FROM skill_versions WHERE id=?", (old_version,)).fetchone()[0]
    candidate=ImprovementService(conn).stage_extracted_skills([{
        "name":skill.slug,"description":"Check work","content":"A newer procedure.",
        "behavior_type":"verification"}],session_ids=[],source_agent="codex")[0]
    conn.execute("UPDATE skill_versions SET workflow_candidate_id=? WHERE id=?", (candidate,old_version))
    conn.execute("""INSERT INTO workflow_versions(id,candidate_id,version,content_json,content_hash,created_at)
        VALUES('wv',?,1,'{}','workflow-hash',?)""", (candidate,NOW))
    conn.execute("""INSERT INTO interventions(id,workflow_version_id,scope_type,scope_id,target_path,
        applied_hash,applied_content,exposure_started_at,created_at,updated_at)
        VALUES('applied','wv','repo','repo',?,?,'old procedure',?,?,?)""", (str(path),old_hash,NOW,NOW,NOW))
    conn.execute("""INSERT INTO measurements(id,intervention_id,metric_name,cohort_json,measured_at,created_at,updated_at)
        VALUES('measurement','applied','tests','{}',?,?,?)""", (NOW,NOW,NOW))
    registry.sync_measurements()
    assert registry.show(skill.id).measurements[0].skill_version_id == old_version
    conn.execute("UPDATE interventions SET applied_hash='unretained-content' WHERE id='applied'")
    registry.sync_measurements()
    assert registry.show(skill.id).measurements[0].skill_version_id is None


def test_detail_contract_names_occurrences_and_keeps_repeated_session_evidence(usage):
    conn, registry, skill, path = usage
    add_read(conn, path, 1)
    add_read(conn, path, 2)
    registry.sync_usage()
    payload = registry.show(skill.id).model_dump(mode='json')
    assert 'usage_sessions' not in payload
    assert payload['skill']['usage_count'] == 2
    assert len(payload['usage_occurrences']) == 2
    assert {item['session_id'] for item in payload['usage_occurrences']} == {'s-codex'}
    assert len({item['usage_id'] for item in payload['usage_occurrences']}) == 2


def test_refresh_discovers_captured_plugin_skills_and_keeps_read_provenance(usage, tmp_path):
    conn, registry, _, _ = usage
    path = tmp_path / 'plugin' / 'skills' / 'review-change' / 'SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text('---\nname: review-change\ndescription: Inspect a change\n---\nRead the diff.\n')
    wrapped = 'text(await tools.exec_command({cmd:' + json.dumps('cat ' + str(path)) + '}));'
    add_read(conn, path, 1, tool_name='exec', attrs={
        'gen_ai.client.tool.input':wrapped,
        'gen_ai.client.tool.output':json.dumps({'exit_code':0,'output':'skill instructions'})})
    registry.refresh()
    record = next(item for item in registry.list() if item.slug == 'review-change')
    occurrences = registry.show(record.id).usage_occurrences
    assert len(occurrences) == 1
    assert occurrences[0].evidence['relationship'] == 'loaded'
    assert occurrences[0].skill_version_id is None
    registry.refresh()
    assert len(registry.show(record.id).usage_occurrences) == 1
    from reflect.dashboard_sessions import load_session_detail
    detail = load_session_detail(tmp_path / 'reflect.db', 's-codex')
    assert any(row['name'] == 'review-change' and row['count'] == 1 for row in detail['tool_inventory']['skills'])


def test_compound_skill_read_is_attempted_not_confirmed_by_last_command(usage):
    conn, registry, skill, path = usage
    add_read(conn, path, 1, tool_name='exec_command', attrs={
        'gen_ai.client.tool.input':json.dumps({'cmd':f'cat {path}\ngit status'}),
        'process.exit.code':0})
    registry.sync_usage()
    assert registry.show(skill.id).usage_occurrences[0].evidence['relationship'] == 'load_attempted'


def test_skill_projection_does_not_scan_all_session_tools_for_every_step(usage):
    from reflect.improvements.skill_usage import SkillUsageProjector
    conn, _, _, path = usage
    for index in range(1000):
        add_read(conn, path, index)
    ticks = 0
    def bounded_work():
        nonlocal ticks
        ticks += 1
        return ticks > 1000
    conn.set_progress_handler(bounded_work, 1000)
    try:
        assert SkillUsageProjector(conn).refresh(session_ids={'s-codex'}, step_ids=None, now=NOW) == 1000
    finally:
        conn.set_progress_handler(None, 0)


def test_non_skill_commands_do_not_parse_large_results_for_skill_usage(usage, monkeypatch):
    from reflect.improvements.skill_usage import SkillUsageProjector
    def unexpected(*args, **kwargs):
        raise AssertionError('Unrelated command output must not be decoded for skills')
    monkeypatch.setattr('reflect.execution_assessment.explicit_exit', unexpected)
    monkeypatch.setattr('reflect.tool_inputs._results', unexpected)
    projector = SkillUsageProjector(usage[0])
    for name, args in [('exec_command', {'cmd':'git status'}), ('exec', 'text(await tools.exec_command({cmd:"git status"}));')]:
        assert projector.occurrences({'gen_ai.client.tool.input':args}, name, 'ok', '') == []
