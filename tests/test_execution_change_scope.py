"""Changed native groups retain parity without re-assessing unrelated history."""
import json

import pytest
from test_execution_units import NOW, _base, _step

from reflect.execution_units import ExecutionUnitService
from reflect.store.migrate import migrate
from reflect.store.pending_refresh import PendingRefreshState
from reflect.store.sqlite import connect_sqlite


def native_step(conn, name, seq, turn, at, *, terminal=False):
    _step(conn, name, seq, at, 'Work')
    conn.execute('UPDATE steps SET raw_attrs_json=? WHERE id=?',
                 (json.dumps({'gen_ai.client.turn_id': turn, 'reflect.turn.terminal': terminal}), name))


def snapshot(conn):
    return [tuple(row) for row in conn.execute(
        'SELECT id,source,started_at,ended_at,status,verification_passed,eligible,boundary_json '
        'FROM execution_units ORDER BY id')]


@pytest.mark.parametrize('change', ['append', 'late', 'move'])
def test_native_change_matches_full_repair_and_preserves_other_units(tmp_path, change):
    conn = connect_sqlite(tmp_path / 'state.db')
    migrate(conn)
    _base(conn)
    for i in range(30):
        native_step(conn, f's{i}', i, f'turn{i}', f'2026-08-02T10:{i:02}:00+00:00', terminal=True)
    service = ExecutionUnitService(conn)
    service.refresh(session_ids={'session'})
    original = dict(conn.execute('SELECT id,updated_at FROM execution_units'))
    if change == 'append':
        native_step(conn, 'new', 31, 'turn29', '2026-08-02T10:29:01+00:00')
        changed = {'new'}
    elif change == 'late':
        native_step(conn, 'new', 31, 'turn15', '2026-08-02T10:15:01+00:00')
        changed = {'new'}
    else:
        conn.execute('UPDATE steps SET raw_attrs_json=? WHERE id=?',
                     (json.dumps({'gen_ai.client.turn_id': 'turn29'}), 's15'))
        changed = {'s15'}
    result = service.refresh(session_ids={'session'}, step_scope={'session': changed})
    assert result['execution_units'] <= 2
    assert len(service.changed_unit_ids) <= 2
    for unit, updated in conn.execute('SELECT id,updated_at FROM execution_units'):
        if unit not in service.changed_unit_ids:
            assert updated == original[unit]
    incremental = snapshot(conn)
    service.refresh(session_ids={'session'})
    assert snapshot(conn) == incremental
    conn.close()


def test_change_journal_preserves_new_arrivals_and_full_scope(tmp_path):
    conn = connect_sqlite(tmp_path / 'state.db')
    migrate(conn)
    _base(conn)
    native_step(conn, 'one', 1, 'turn', NOW)
    pending = PendingRefreshState(conn)
    pending.mark_steps('session', {'one'})
    captured = pending.capture()
    assert captured.step_scope('graph') == {'session': {'one'}}
    pending.mark_steps('session', {'two'})
    pending.acknowledge(captured, 'graph')
    assert pending.capture().step_scope('graph') == {'session': {'one', 'two'}}
    pending.mark({'session'}, phases=('graph',))
    pending.mark_steps('session', {'three'})
    assert pending.capture().step_scope('graph') == {'session': None}
    pending.acknowledge(pending.capture(), 'graph')
    assert conn.execute("SELECT COUNT(*) FROM pending_step_changes WHERE phase='graph'").fetchone()[0] == 0
    conn.close()
