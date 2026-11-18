"""Assessment reads only workspace edits that can affect selected executions."""

import json
from contextlib import closing

import pytest
from test_execution_units import NOW, _additional_session, _base, _step

from reflect.execution_units import ExecutionUnitService
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


def _check(conn, session, prefix, start, end):
    _step(conn, prefix + "-check", 1, start, "Check", session_id=session)
    _step(conn, prefix + "-stop", 2, end, "Stop", session_id=session)
    conn.execute(
        "UPDATE steps SET raw_attrs_json=? WHERE id=?",
        (json.dumps({"reflect.turn.terminal": True}), prefix + "-stop"),
    )
    conn.execute(
        """INSERT INTO tool_calls(id,step_id,session_id,tool_name,raw_attrs_json,created_at,updated_at)
           VALUES (?,?,?,'exec_command',?,?,?)""",
        (prefix, prefix + "-check", session,
         json.dumps({"gen_ai.client.command": "pytest", "process.exit.code": 0}), NOW, NOW),
    )


def _edit(conn, name, seq, at, *, hook=False, end=None):
    _step(conn, name, seq, at, "Edit", session_id="external")
    attrs = {"gen_ai.client.file_path": "src/app.py", "fixture": name}
    if hook:
        attrs["gen_ai.client.hook.event"] = "afterfileedit"
        conn.execute("UPDATE steps SET raw_attrs_json=?, ended_at=? WHERE id=?", (json.dumps(attrs), end, name))
    else:
        conn.execute("UPDATE steps SET ended_at=? WHERE id=?", (end or at, name))
        conn.execute(
            """INSERT INTO tool_calls(id,step_id,session_id,tool_name,raw_attrs_json,created_at,updated_at)
               VALUES (?,?,'external','Edit',?,?,?)""", (name, name, json.dumps(attrs), NOW, NOW),
        )


@pytest.mark.parametrize("workspace", ["workspace", None])
def test_sql_bounds_exclude_old_payloads_but_keep_earlier_selected_work_and_overlapping_hooks(tmp_path, monkeypatch, workspace):
    with closing(connect_sqlite(tmp_path / "reflect.db")) as conn:
        migrate(conn)
        _base(conn)
        _additional_session(conn, "earlier-work")
        _additional_session(conn, "external")
        conn.execute("UPDATE sessions SET workspace_id=?", (workspace,))
        # Session creation order differs from its observed steps: the shared
        # edit cache must cover both selected executions before the first read.
        conn.execute("UPDATE sessions SET started_at='2026-08-02T11:00:00+00:00' WHERE id='earlier-work'")
        _check(conn, "session", "later", "2026-08-02T10:01:00+00:00", "2026-08-02T10:05:00+00:00")
        _check(conn, "earlier-work", "earlier", "2026-08-02T09:01:00+00:00", "2026-08-02T09:05:00+00:00")
        _edit(conn, "old-tool", 1, "2026-07-01T10:00:00+00:00")
        _edit(conn, "old-hook", 2, "2026-07-01T10:00:00+00:00", hook=True)
        _edit(conn, "earlier-edit", 3, "2026-08-02T09:03:00+00:00")
        _edit(conn, "overlapping-hook", 4, "2026-07-01T10:00:00+00:00", hook=True,
              end="2026-08-02T10:03:00+00:00")
        selected = {"session", "earlier-work"}

        class UnboundedEdits(ExecutionUnitService):
            def _assess(self, boundary, session):
                self._workspace_edit_starts = dict.fromkeys(self._workspace_edit_starts, "")
                super()._assess(boundary, session)

        UnboundedEdits(conn).refresh(session_ids=selected)
        expected = conn.execute("SELECT id, boundary_json FROM execution_units ORDER BY id").fetchall()
        parsed = []
        from reflect.execution_units import attrs_dict

        def track_attrs(value):
            attrs = attrs_dict(value)
            if attrs.get("fixture"):
                parsed.append(attrs["fixture"])
            return attrs

        from reflect.execution_units import edit_paths

        def track_edit(tool):
            parsed.append(json.loads(tool["raw_attrs_json"])["fixture"])
            return edit_paths(tool)

        monkeypatch.setattr("reflect.execution_units.attrs_dict", track_attrs)
        monkeypatch.setattr("reflect.execution_units.edit_paths", track_edit)
        statements = []
        conn.set_trace_callback(statements.append)
        ExecutionUnitService(conn).refresh(session_ids=selected)
        conn.set_trace_callback(None)
        assert conn.execute("SELECT id, boundary_json FROM execution_units ORDER BY id").fetchall() == expected
        assert all(json.loads(row[1])["assessment"]["verification"] == "invalidated" for row in expected)
        assert set(parsed) == {"earlier-edit", "overlapping-hook"}
        edit_queries = [query for query in statements if query.lstrip().startswith((
            "SELECT tc.tool_name", "SELECT st.raw_attrs_json, COALESCE",
        ))]
        assert len(edit_queries) == 2  # One scan per workspace, shared by both executions.
        for query in edit_queries:
            plan = " ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + query))
            assert "idx_steps_measurement_times" in plan
            if "tool_calls" in query:
                assert "idx_tool_calls_step" in plan


@pytest.mark.parametrize("start", ["", "unknown"])
def test_non_date_timestamps_keep_existing_chronology_and_unknown_checks(tmp_path, start):
    with closing(connect_sqlite(tmp_path / "reflect.db")) as conn:
        migrate(conn)
        _base(conn)
        _additional_session(conn, "external")
        _step(conn, "unknown-work", 1, start, "Work")
        _edit(conn, "unknown-edit", 1, start)
        service = ExecutionUnitService(conn)
        service.refresh(session_ids={"session"})
        original = conn.execute("SELECT boundary_json FROM execution_units WHERE eligible=1").fetchone()[0]
        assert json.loads(original)["assessment"]["verification"] == "unknown"
        service.refresh(session_ids={"session"})
        assert conn.execute("SELECT boundary_json FROM execution_units WHERE eligible=1").fetchone()[0] == original


@pytest.mark.parametrize('edit_end', ['10:03', '10:05', None, ''])
@pytest.mark.parametrize('workspace', [True, False])
def test_edit_interval_overlapping_check_cannot_verify(tmp_path, edit_end, workspace):
    with closing(connect_sqlite(tmp_path / 'overlap.db')) as conn:
        migrate(conn)
        _base(conn)
        if not workspace:
            conn.execute('UPDATE sessions SET workspace_id=NULL, repo_id=NULL')
        def timestamp(at):
            return f'2026-08-02T{at}:00+00:00' if at else at

        for seq, (name, start, end) in enumerate([
            ('edit', '10:00', edit_end), ('check', '10:01', '10:02'), ('stop', '10:04', '10:04'),
        ]):
            _step(conn, name, seq, timestamp(start), name)
            conn.execute('UPDATE steps SET ended_at=?,raw_attrs_json=? WHERE id=?',
                (timestamp(end), json.dumps({'gen_ai.client.turn_id': 'turn', 'reflect.turn.terminal': name == 'stop'}), name))
        for name, tool_name, attrs in [
            ('edit', 'Edit', {'gen_ai.client.file_path': 'src/app.py'}),
            ('check', 'exec_command', {'gen_ai.client.command': 'pytest', 'process.exit.code': 0}),
        ]:
            conn.execute('INSERT INTO tool_calls(id,step_id,session_id,tool_name,raw_attrs_json,created_at,updated_at) VALUES (?,?,\'session\',?,?,?,?)',
                (name, name, tool_name, json.dumps(attrs), NOW, NOW))
        ExecutionUnitService(conn).refresh()
        verification, raw = conn.execute('SELECT verification_passed,boundary_json FROM execution_units WHERE eligible=1').fetchone()
        assert verification is None
        assert json.loads(raw)['assessment']['verification'] == 'invalidated'


def test_interval_index_selects_completed_overlap_and_unbounded_edits():
    from reflect.execution_assessment import EditObservation
    from reflect.execution_units import _EditIntervalIndex

    observations = [
        EditObservation('old', '09:30', frozenset(), '09:00'),
        EditObservation('overlap', '10:03', frozenset(), '09:00'),
        EditObservation('unknown', '', frozenset(), '09:15'),
        EditObservation('future', '11:01', frozenset(), '11:00'),
    ]
    index = _EditIntervalIndex(observations)
    assert {edit.step_id for edit in index.overlapping('10:00', '10:05')} == {'overlap', 'unknown'}
    assert {edit.step_id for edit in index.overlapping('12:00', '12:01')} == {'unknown'}


@pytest.mark.parametrize('precise', [True, False])
def test_unknown_edit_propagation_matches_full_refresh_and_recovers_on_retraction(tmp_path, precise):
    from reflect.store.pending_refresh import PendingRefreshState

    with closing(connect_sqlite(tmp_path / 'late-unknown.db')) as conn:
        migrate(conn)
        _base(conn)
        _additional_session(conn, 'external')
        _check(conn, 'session', 'consumer', '2026-08-02T10:01:00+00:00', '2026-08-02T10:05:00+00:00')
        service = ExecutionUnitService(conn)
        service.refresh(session_ids={'session'})
        _edit(conn, 'late-unknown', 1, '2026-08-02T09:00:00+00:00')
        conn.execute("UPDATE steps SET ended_at=NULL WHERE id='late-unknown'")
        scope = {'external': {'late-unknown'}} if precise else None
        affected = service.affected_session_ids({'external'}, step_scope=scope)
        assert affected == {'external', 'session'}
        service.refresh(session_ids=affected, step_scope=scope)
        query = "SELECT id,verification_passed,boundary_json FROM execution_units ORDER BY id"
        incremental = conn.execute(query).fetchall()
        service.refresh()
        assert conn.execute(query).fetchall() == incremental
        assert conn.execute("SELECT verification_passed FROM execution_units WHERE session_id='session'").fetchone()[0] is None
        PendingRefreshState(conn).mark_steps('external', {'late-unknown'}, phases=('improvements',))
        conn.execute("DELETE FROM steps WHERE id='late-unknown'")
        affected = service.affected_session_ids({'external'}, step_scope=scope)
        assert affected == {'external', 'session'}
        service.refresh(session_ids=affected, step_scope=scope)
        incremental = conn.execute(query).fetchall()
        service.refresh()
        assert conn.execute(query).fetchall() == incremental
        assert conn.execute("SELECT verification_passed FROM execution_units WHERE session_id='session'").fetchone()[0] == 1
