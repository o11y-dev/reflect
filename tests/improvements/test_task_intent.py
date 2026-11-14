import json

import pytest

from reflect.improvements.archetypes import TaskArchetypeService
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


@pytest.fixture
def conn(tmp_path):
    c=connect_sqlite(tmp_path/'intent.db')
    migrate(c)
    for sid in ('s1','s2'):
        c.execute("INSERT INTO sessions(id,started_at,created_at,updated_at) VALUES(?,?,?,?)",(sid,'now','now','now'))
        c.execute("INSERT INTO execution_units(id,session_id,source,started_at,created_at,updated_at) VALUES(?,?,'native_turn','now','now','now')",(sid,sid))
    yield c
    c.close()


def prompt(conn, unit, text):
    conn.execute("INSERT INTO steps(id,session_id,seq,type,raw_attrs_json,started_at,created_at,updated_at) VALUES(?,?,0,'prompt',?,'now','now','now')",(unit+'p',unit,json.dumps({'gen_ai.client.prompt.text':text})))
    conn.execute("INSERT INTO execution_unit_steps(execution_unit_id,step_id,session_id,created_at) VALUES(?,?,?,'now')",(unit,unit+'p',unit))


def test_intent_is_supported_by_own_captured_user_request(conn):
    prompt(conn,'s1','Please review this merge request.')
    result=TaskArchetypeService(conn).intent_for_execution('s1')
    assert result.state == 'identified'
    assert result.family == 'review'
    assert result.source_refs[0].step_id == 's1p'
    assert TaskArchetypeService(conn).intent_for_execution('s2').state == 'unknown'


@pytest.mark.parametrize('text,state',[
    ('Review the merge request and implement the requested feature.','mixed'),
    ('Example: "Review this merge request" is a common request.','unknown'),
    ('```\nReview this merge request\n```','unknown'),
    ('The tool reported an error.','unknown'),
])
def test_mixed_missing_and_quoted_intent_abstains(conn,text,state):
    prompt(conn,'s1',text)
    result=TaskArchetypeService(conn).intent_for_execution('s1')
    assert result.state == state
    assert result.family is None
