import json

import pytest

from reflect.dashboard_sessions import load_session_detail
from reflect.store.ingest import ingest_local_spans_file
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.sqlite import connect_sqlite
from reflect.telemetry_facts import extract_subagent_name_from_tool
from reflect.views.report_tabs import _usage_subagent_counts


@pytest.mark.parametrize(('tool', 'payload', 'expected'), [
    ('spawn_agent', {'agent_type': 'explorer', 'task_name': 'find-models'}, 'explorer'),
    ('collaboration.spawn_agent', {'agent_type': 'explorer'}, 'explorer'),
    ('collaborationspawn_agent', {'agent_type': 'explorer'}, 'explorer'),
    ('functions.spawn_agent', {'agent_type': 'worker'}, 'worker'),
    ('Task', {'subagent_type': 'Explore'}, 'Explore'),
    ('task', {'agent_type': 'explore', 'name': 'repo-strategy'}, 'explore'),
    ('Agent', {'subagent_type': 'reviewer'}, 'reviewer'),
    ('read_agent', {'agent_id': 'child-uuid'}, ''),
    ('mcp__tracker__task', {'name': 'new ticket'}, ''),
])
def test_structured_launch_identity_keeps_role_and_excludes_nonlaunch_tools(tool, payload, expected):
    assert extract_subagent_name_from_tool(tool, preview=json.dumps(payload)) == expected


def _normalize(tmp_path, events):
    path = tmp_path / 'identity.jsonl'
    path.write_text('\n'.join(json.dumps({
        'name': event, 'traceId': 'trace', 'spanId': f'span-{index}',
        'start_time_ns': 1784728800000000000 + index * 1000000,
        'end_time_ns': 1784728800000000000 + index * 1000000,
        'attributes': {'gen_ai.client.name': 'claude', 'gen_ai.client.session_id': 'session',
                       'gen_ai.client.hook.event': event, **attrs},
    }) for index, (event, attrs) in enumerate(events)))
    db = tmp_path / 'reflect.db'
    conn = connect_sqlite(db)
    migrate(conn)
    ingest_local_spans_file(conn, file_path=path)
    assert normalize_pending_raw_events(conn)['failed'] == 0
    return conn, db


@pytest.mark.parametrize('call_key', ['gen_ai.tool.call.id', 'tool.call_id', 'tool_call_id'])
def test_launch_lifecycle_and_result_share_recorded_identity_without_prompt_launches(tmp_path, call_key):
    conn, db = _normalize(tmp_path, [
        ('UserPromptSubmit', {'gen_ai.client.prompt': 'Use the `reviewer` subagent'}),
        ('PreToolUse', {'gen_ai.client.tool_name': 'collaboration.spawn_agent',
                       'gen_ai.client.tool.input': '{"agent_type":"reviewer","task_name":"audit"}',
                       call_key: 'call-1'}),
        ('PostToolUse', {'gen_ai.client.tool_name': 'collaboration.spawn_agent',
                        'gen_ai.client.tool.output': '{"agent_id":"child-1"}',
                        call_key: 'call-1'}),
        ('SubagentStart', {'gen_ai.client.agent_id': 'child-1', 'gen_ai.client.subagent_type': 'reviewer'}),
        ('SubagentStop', {'gen_ai.client.agent_id': 'child-1'}),
    ])
    try:
        counts = _usage_subagent_counts(conn, ['session'])
        assert counts['subagent_starts'] == {'reviewer': 1}
        assert counts['subagent_stops'] == {'reviewer': 1}
        detail = load_session_detail(db, 'session')
        assert detail['tool_inventory']['total_subagent_starts'] == 1
        assert detail['tool_inventory']['total_subagent_stops'] == 1
        assert [row['name'] for row in detail['tool_inventory']['subagents']] == ['reviewer']
    finally:
        conn.close()


def test_result_only_launch_preserves_input_role(tmp_path):
    conn, _ = _normalize(tmp_path, [('PostToolUse', {
        'gen_ai.client.tool_name': 'Task', 'gen_ai.client.tool.input': '{"subagent_type":"Explore"}',
        'gen_ai.tool.call.id': 'call-only',
    })])
    try:
        assert _usage_subagent_counts(conn, ['session'])['subagent_starts'] == {'Explore': 1}
    finally:
        conn.close()


@pytest.mark.parametrize('key', ['ide.tool_name', 'gen_ai.tool.name', 'tool.name', 'tool_name'])
def test_recorded_tool_name_survives_nameless_result(tmp_path, key):
    conn, _ = _normalize(tmp_path, [
        ('PreToolUse', {key: 'Read', 'gen_ai.tool.call.id': 'read-call'}),
        ('PostToolUse', {'gen_ai.tool.call.id': 'read-call'}),
    ])
    try:
        assert [row[0] for row in conn.execute('SELECT tool_name FROM tool_calls')] == ['Read']
    finally:
        conn.close()


def test_missing_subagent_role_is_not_replaced_by_agent_id(tmp_path):
    conn, _ = _normalize(tmp_path, [('SubagentStart', {'gen_ai.client.agent_id': 'opaque-child-id'})])
    try:
        assert _usage_subagent_counts(conn, ['session'])['subagent_starts'] == {'Role not recorded': 1}
    finally:
        conn.close()


def test_spawn_callers_are_not_mistaken_for_delegated_children(tmp_path):
    conn, _ = _normalize(tmp_path, [
        ('PostToolUse', {'gen_ai.client.tool_name': 'spawn_agent', 'gen_ai.agent.id': 'caller',
                        'gen_ai.tool.call.id': f'call-{index}',
                        'gen_ai.client.tool.input': '{"agent_type":"worker"}',
                        'gen_ai.client.tool.output': json.dumps({'agent_id': f'child-{index}'})})
        for index in range(2)
    ])
    try:
        assert _usage_subagent_counts(conn, ['session'])['subagent_starts'] == {'worker': 2}
        assert {row[0] for row in conn.execute('SELECT agent_id FROM agent_events')} == {'child-0', 'child-1'}
    finally:
        conn.close()


def test_command_only_shell_keeps_command_out_of_tool_identity(tmp_path):
    conn, _ = _normalize(tmp_path, [('BeforeShellExecution', {'gen_ai.client.command': 'git status --short'})])
    try:
        assert [row[0] for row in conn.execute('SELECT tool_name FROM tool_calls')] == ['Shell']
    finally:
        conn.close()


def test_generic_lifecycle_attrs_keep_unlinked_children_distinct(tmp_path):
    conn, _ = _normalize(tmp_path, [
        ('subagent_start', {'gen_ai.agent.id': 'child-1', 'gen_ai.agent.type': 'worker'}),
        ('SubagentStart', {'gen_ai.client.agent_id': 'child-2', 'gen_ai.client.subagent_type': 'worker'}),
        ('PostToolUse', {'tool_name': 'spawn_agent', 'gen_ai.tool.call.id': 'unlinked',
                        'tool.input': '{"agent_type":"worker"}'}),
    ])
    try:
        assert _usage_subagent_counts(conn, ['session'])['subagent_starts'] == {'worker': 3}
    finally:
        conn.close()


@pytest.mark.parametrize(('original', 'expected'), [('', 'Shell'), ('Bash', 'Bash')])
def test_command_result_improves_only_missing_tool_identity(tmp_path, original, expected):
    conn, _ = _normalize(tmp_path, [
        ('PreToolUse', {'gen_ai.client.tool_name': original, 'gen_ai.tool.call.id': 'shell'}),
        ('PostToolUse', {'gen_ai.client.command': 'git status', 'gen_ai.tool.call.id': 'shell'}),
    ])
    try:
        assert [row[0] for row in conn.execute('SELECT tool_name FROM tool_calls')] == [expected]
    finally:
        conn.close()


@pytest.mark.parametrize('lifecycle', [False, True])
@pytest.mark.parametrize('result_event', ['PostToolUseFailure', 'PostToolUse'])
def test_failed_spawn_retracts_only_unconfirmed_tool_start(tmp_path, lifecycle, result_event):
    events = [
        ('PreToolUse', {'gen_ai.client.tool_name': 'spawn_agent', 'gen_ai.tool.call.id': 'failed-call',
                       'gen_ai.client.tool.input': '{"agent_type":"worker"}'}),
        (result_event, {'gen_ai.tool.call.id': 'failed-call', 'error.type': 'tool_result'}),
    ]
    if lifecycle:
        events.append(('SubagentStart', {'gen_ai.client.agent_id': 'real-child',
                                        'gen_ai.client.subagent_type': 'worker'}))
    conn, _ = _normalize(tmp_path, events)
    try:
        assert _usage_subagent_counts(conn, ['session'])['subagent_starts'] == ({'worker': 1} if lifecycle else {})
    finally:
        conn.close()


def test_launch_lookup_uses_recorded_call_identity_index(tmp_path):
    conn, _ = _normalize(tmp_path, [('PostToolUse', {'gen_ai.client.tool_name': 'spawn_agent',
                                                  'gen_ai.tool.call.id': 'indexed-call'})])
    try:
        from reflect.store.hook_facts import HookFactRepository

        queries = []
        conn.set_trace_callback(queries.append)
        HookFactRepository(conn).persist_step(step_id='unused-step', session_id='session',
            fallback_event='PostToolUse', attrs={'gen_ai.tool.call.id': 'indexed-call'}, timestamp='2026-11-09')
        conn.set_trace_callback(None)
        lookup = next(query for query in queries if query.startswith('SELECT * FROM tool_calls'))
        plan = conn.execute('EXPLAIN QUERY PLAN ' + lookup).fetchall()
        assert any('idx_tool_calls_logical_identity' in str(row[3]) for row in plan)
    finally:
        conn.close()
