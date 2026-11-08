"""Precise invalidation keeps overlapping consumers without expanding session gaps."""

import json

import pytest
from test_execution_units import NOW, _additional_session, _base, _step

from reflect.execution_units import ExecutionUnitService
from reflect.improvements.service import ImprovementService
from reflect.store.migrate import migrate
from reflect.store.pending_refresh import PendingRefreshState
from reflect.store.sqlite import connect_sqlite


def native_step(conn, step, at, turn, *, session="session", terminal=False):
    seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM steps WHERE session_id=?", (session,)).fetchone()[0]
    _step(conn, step, seq, f"2026-08-02T{at}:00+00:00", "Work", session_id=session)
    conn.execute("UPDATE steps SET raw_attrs_json=? WHERE id=?", (
        json.dumps({"gen_ai.client.turn_id": turn, "reflect.turn.terminal": terminal}), step,
    ))


@pytest.mark.parametrize("consumer_workspace", ["workspace", None])
@pytest.mark.parametrize("correction", ["delete", "move"])
def test_precise_dependency_changes_invalidate_and_recover_overlapping_verification(
    tmp_path, consumer_workspace, correction,
):
    with connect_sqlite(tmp_path / "reflect.db") as conn:
        migrate(conn)
        _base(conn)
        conn.execute("UPDATE sessions SET workspace_id=? WHERE id='session'", (consumer_workspace,))
        _additional_session(conn, "changed")
        _additional_session(conn, "unrelated")
        native_step(conn, "check", "10:01", "consumer")
        native_step(conn, "stop", "10:05", "consumer", terminal=True)
        conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,raw_attrs_json,created_at,updated_at)
                        VALUES ('check','check','session','exec_command',?,?,?)""", (
            json.dumps({"gen_ai.client.command": "pytest", "process.exit.code": 0}), NOW, NOW,
        ))
        native_step(conn, "old", "09:00", "old", session="changed", terminal=True)
        native_step(conn, "future", "11:00", "future", session="changed", terminal=True)
        native_step(conn, "gap", "09:30", "gap", session="unrelated", terminal=True)
        service = ImprovementService(conn, rules=[])
        service.refresh()
        query = "SELECT verification_passed FROM execution_units WHERE session_id='session' AND eligible=1"
        assert conn.execute(query).fetchone()[0] == 1
        conn.execute("UPDATE execution_units SET updated_at='untouched' WHERE session_id='unrelated'")
        native_step(conn, "late-edit", "10:03", "edit", session="changed", terminal=True)
        conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,raw_attrs_json,created_at,updated_at)
                        VALUES ('edit','late-edit','changed','Edit',?,?,?)""", (
            json.dumps({"gen_ai.client.file_path": "src/app.py"}), NOW, NOW,
        ))
        scope = {"changed": {"late-edit"}}
        assert service.execution_units.affected_session_ids({"changed"}, step_scope=scope) == {"changed", "session"}
        assert "unrelated" in service.execution_units.affected_session_ids({"changed"})
        service.refresh(session_ids={"changed"}, step_scope=scope)
        assert conn.execute(query).fetchone()[0] is None
        assert conn.execute("SELECT updated_at FROM execution_units WHERE session_id='unrelated'").fetchone()[0] == "untouched"

        PendingRefreshState(conn).mark_steps("changed", {"late-edit"}, phases=("improvements",))
        if correction == "delete":
            conn.execute("DELETE FROM steps WHERE id='late-edit'")
            conn.execute("UPDATE sessions SET workspace_id=NULL,repo_id=NULL WHERE id='changed'")
        else:
            conn.execute("UPDATE steps SET started_at='2026-08-02T11:05:00+00:00' WHERE id='late-edit'")
        assert service.execution_units.affected_session_ids({"changed"}, step_scope=scope) == {"changed", "session"}
        service.refresh(session_ids={"changed"}, step_scope=scope)
        assert conn.execute(query).fetchone()[0] == 1
        assert conn.execute("SELECT updated_at FROM execution_units WHERE session_id='unrelated'").fetchone()[0] == "untouched"


def test_precise_dependencies_preserve_workspace_precedence_and_fallback(tmp_path):
    with connect_sqlite(tmp_path / "reflect.db") as conn:
        migrate(conn)
        _base(conn)
        _additional_session(conn, "changed")
        _additional_session(conn, "repo-only")
        conn.execute("UPDATE sessions SET workspace_id=NULL WHERE id='repo-only'")
        _additional_session(conn, "other-workspace")
        conn.execute("""INSERT INTO workspaces(id,root_path,path_hash,label,repo_id,source_key,confidence,
                        raw_json,created_at,updated_at)
                        VALUES ('other','/other','other','other','repo','test',1,'{}',?,?)""", (NOW, NOW))
        conn.execute("UPDATE sessions SET workspace_id='other' WHERE id='other-workspace'")
        for session in ("session", "repo-only", "other-workspace"):
            native_step(conn, session, "10:03", session, session=session, terminal=True)
        native_step(conn, "old", "09:00", "old", session="changed", terminal=True)
        native_step(conn, "end", "11:00", "end", session="changed", terminal=True)
        service = ExecutionUnitService(conn)
        service.refresh()
        native_step(conn, "new", "10:03", "new", session="changed")
        expected = {"changed", "session", "repo-only"}
        queries = []
        conn.set_trace_callback(queries.append)
        assert service.affected_session_ids({"changed"}, step_scope={"changed": {"new"}}) == expected
        conn.set_trace_callback(None)
        peer_queries = [query for query in queries if "SELECT DISTINCT eu.session_id" in query]
        assert len(peer_queries) == 2  # Workspace consumers and repo-only consumers.
        for query in peer_queries:
            plan = " ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + query))
            assert "SEARCH s USING" in plan and "SEARCH eu USING" in plan
            assert "idx_execution_units_session_started" in plan
        assert not any("MIN(st.started_at)" in query for query in queries)
        # A missing step with no old owner falls back to full selected-session bounds.
        assert service.affected_session_ids({"changed"}, step_scope={"changed": {"missing"}}) == expected
        assert service.affected_session_ids({"deleted"}, step_scope={"deleted": {"missing"}}) is None
        assert service.affected_session_ids(set(), step_scope={}) == set()


def test_precise_native_edit_reassesses_overlapping_group_in_same_session(tmp_path):
    with connect_sqlite(tmp_path / "reflect.db") as conn:
        migrate(conn)
        _base(conn)
        native_step(conn, "check", "10:01", "consumer")
        native_step(conn, "stop", "10:05", "consumer", terminal=True)
        conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,raw_attrs_json,created_at,updated_at)
                        VALUES ('check','check','session','exec_command',?,?,?)""", (
            json.dumps({"gen_ai.client.command": "pytest", "process.exit.code": 0}), NOW, NOW,
        ))
        service = ExecutionUnitService(conn)
        service.refresh()
        query = """SELECT verification_passed FROM execution_units
                   WHERE id=(SELECT execution_unit_id FROM execution_unit_steps WHERE step_id='check')"""
        assert conn.execute(query).fetchone()[0] == 1
        native_step(conn, "late-edit", "10:03", "editor", terminal=True)
        conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,raw_attrs_json,created_at,updated_at)
                        VALUES ('edit','late-edit','session','Edit',?,?,?)""", (
            json.dumps({"gen_ai.client.file_path": "src/app.py"}), NOW, NOW,
        ))
        service.refresh(session_ids={"session"}, step_scope={"session": {"late-edit"}})
        assert conn.execute(query).fetchone()[0] is None


def test_late_prompt_relinks_skill_usage_after_full_boundary_fallback(tmp_path):
    with connect_sqlite(tmp_path / "reflect.db") as conn:
        migrate(conn)
        _base(conn)
        service = ImprovementService(conn, rules=[])
        path = tmp_path / ".agents" / "skills" / "verify" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: verify\ndescription: Verify\n---\nRun checks.\n")
        service.skills.sync_paths([path])
        skill_id = conn.execute("SELECT id FROM skills").fetchone()[0]
        _step(conn, "before", 1, "2026-08-02T09:00:00+00:00", "Work")
        _step(conn, "read", 2, "2026-08-02T10:03:00+00:00", "Read")
        conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,status,raw_attrs_json,created_at,updated_at)
                        VALUES ('read','read','session','Read','ok',?,?,?)""", (
            json.dumps({"reflect.skill.id": skill_id, "gen_ai.client.file_path": str(path)}), NOW, NOW,
        ))
        service.refresh()
        before = conn.execute("SELECT execution_unit_id FROM skill_usage WHERE source_kind='canonical_step'").fetchone()[0]
        _step(conn, "prompt", 3, "2026-08-02T10:01:00+00:00", "New task")
        conn.execute("""INSERT INTO conversation_facts(id,step_id,session_id,kind,role,content_hash,
                        content_length,raw_attrs_json,created_at,updated_at)
                        VALUES ('fact','prompt','session','prompt','user','hash',8,'{}',?,?)""", (NOW, NOW))
        service.refresh(session_ids={"session"}, step_scope={"session": {"prompt"}})
        expected = conn.execute("SELECT execution_unit_id FROM execution_unit_steps WHERE step_id='read'").fetchone()[0]
        actual = conn.execute("SELECT execution_unit_id FROM skill_usage WHERE source_kind='canonical_step'").fetchone()[0]
        assert expected != before
        assert actual == expected


@pytest.mark.parametrize("correction", ["edit", "retract"])
def test_stable_consumer_reassessment_keeps_peer_membership_and_history(tmp_path, correction):
    with connect_sqlite(tmp_path / "reflect.db") as conn:
        migrate(conn)
        _base(conn)
        _additional_session(conn, "changed")
        native_step(conn, "check", "10:01", "consumer")
        native_step(conn, "stop", "10:05", "consumer", terminal=True)
        native_step(conn, "unrelated", "11:00", "unrelated", terminal=True)
        native_step(conn, "source", "09:00", "source", session="changed", terminal=True)
        conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,raw_attrs_json,created_at,updated_at)
                        VALUES ('check','check','session','exec_command',?,?,?)""", (
            json.dumps({"gen_ai.client.command": "pytest", "process.exit.code": 0}), NOW, NOW,
        ))
        service = ImprovementService(conn, rules=[])
        service.refresh()
        native_step(conn, "late-edit", "10:03", "editor", session="changed", terminal=True)
        conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,raw_attrs_json,created_at,updated_at)
                        VALUES ('edit','late-edit','changed','Edit',?,?,?)""", (
            json.dumps({"gen_ai.client.file_path": "src/app.py"}), NOW, NOW,
        ))
        if correction == "retract":
            service.refresh()
            PendingRefreshState(conn).mark_steps("changed", {"late-edit"}, phases=("improvements",))
            conn.execute("DELETE FROM steps WHERE id='late-edit'")
            conn.execute("UPDATE sessions SET workspace_id=NULL,repo_id=NULL WHERE id='changed'")
        collector = {}
        scope = {"changed": {"late-edit"}}
        affected = service.execution_units.affected_session_ids({"changed"}, step_scope=scope, consumer_unit_ids=collector)
        consumer = conn.execute("SELECT execution_unit_id FROM execution_unit_steps WHERE step_id='check'").fetchone()[0]
        assert collector == {"session": {consumer}}
        membership = conn.execute("SELECT * FROM execution_unit_steps WHERE session_id='session' ORDER BY step_id").fetchall()
        untouched = conn.execute("SELECT * FROM execution_units WHERE id=(SELECT execution_unit_id FROM execution_unit_steps WHERE step_id='unrelated')").fetchone()
        conn.execute("""CREATE TRIGGER preserve_peer_membership BEFORE DELETE ON execution_unit_steps
                        WHEN OLD.session_id='session' BEGIN SELECT RAISE(ABORT,'consumer membership rebuilt'); END""")
        PendingRefreshState(conn).mark(collector, phases=("improvements",))
        conn.commit()
        result = service.refresh(session_ids=affected, dependencies_expanded=True, step_scope=scope,
                                 reassess_unit_ids=collector)
        assert result["reassessed_execution_units"] == 1
        assert service.execution_units.rederived_session_ids == {"changed"}
        assert consumer in service.execution_units.changed_unit_ids
        assert conn.execute("SELECT verification_passed FROM execution_units WHERE id=?", (consumer,)).fetchone()[0] == (1 if correction == "retract" else None)
        assert conn.execute("SELECT * FROM execution_unit_steps WHERE session_id='session' ORDER BY step_id").fetchall() == membership
        assert conn.execute("SELECT * FROM execution_units WHERE id=(SELECT execution_unit_id FROM execution_unit_steps WHERE step_id='unrelated')").fetchone() == untouched
        actual = conn.execute("SELECT id,outcome,verification_passed,boundary_json FROM execution_units ORDER BY id").fetchall()
        conn.execute("DROP TRIGGER preserve_peer_membership")
        service.refresh()
        assert conn.execute("SELECT id,outcome,verification_passed,boundary_json FROM execution_units ORDER BY id").fetchall() == actual


def test_precise_usage_skips_unchanged_source_history_after_boundary_fallback(tmp_path, monkeypatch):
    from reflect.improvements.skill_usage import SkillUsageProjector

    with connect_sqlite(tmp_path / "reflect.db") as conn:
        migrate(conn)
        _base(conn)
        for seq in range(10):
            _step(conn, f"old-{seq}", seq, f"2026-08-02T10:{seq:02}:00+00:00", "Work")
            conn.execute("UPDATE steps SET raw_attrs_json=? WHERE id=?", (json.dumps({"fixture": f"old-{seq}"}), f"old-{seq}"))
        service = ImprovementService(conn, rules=[])
        service.refresh()
        _step(conn, "new", 10, "2026-08-02T10:10:00+00:00", "Work")
        conn.execute("UPDATE steps SET raw_attrs_json=? WHERE id='new'", (json.dumps({"fixture": "new"}),))
        parsed = []
        original = SkillUsageProjector.occurrences

        def track(self, attrs, *args):
            parsed.append(attrs["fixture"])
            return original(self, attrs, *args)

        monkeypatch.setattr(SkillUsageProjector, "occurrences", track)
        service.refresh(session_ids={"session"}, step_scope={"session": {"new"}})
        assert service.execution_units.full_repaired_session_ids == {"session"}
        assert parsed == ["new"]
