import json
import sqlite3

import pytest

from reflect.cost_aliases import _observed_sql_models, ensure_cost_aliases
from reflect.pricing import ModelPricing, PricingTable


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE llm_calls(session_id TEXT, response_model TEXT, request_model TEXT)")
    connection.execute("CREATE TABLE steps(session_id TEXT, raw_attrs_json TEXT)")
    yield connection
    connection.close()


def step(conn, sid, response=None, request=None):
    conn.execute("INSERT INTO steps VALUES(?,?)", (sid, json.dumps({
        "gen_ai.response.model": response, "gen_ai.request.model": request,
        "redacted_payload": "x" * 8192,
    })))


@pytest.mark.parametrize("scope,expected", [
    (None, ["beta", "alpha", "gamma"]),
    ({"a"}, ["alpha", "beta"]),
    ({"b"}, ["beta", "gamma"]),
    ({"missing"}, []),
    (set(), []),
])
def test_inventory_preserves_combined_frequency_and_tie_order_with_scope(conn, scope, expected):
    conn.executemany("INSERT INTO llm_calls VALUES(?,?,?)", [
        ("a", "alpha", "ignored-request"), ("a", "", "beta"),
        ("b", "beta", "ignored-request"), ("b", None, "gamma"),
        ("b", None, None),
    ])
    step(conn, "a", "alpha", "ignored-request")
    step(conn, "a", "", "beta")
    step(conn, "b", "beta")
    step(conn, "b", None, "gamma")
    step(conn, "b")
    previous_factory = conn.row_factory
    assert _observed_sql_models(conn, session_ids=scope) == expected
    assert conn.row_factory is previous_factory


def test_step_model_is_extracted_once_before_grouping_large_payloads(conn):
    for _ in range(10):
        step(conn, "a", "fixture", "ignored")
    extracts = []

    def extract(payload, path):
        extracts.append(path)
        return json.loads(payload).get(path.removeprefix('$."').removesuffix('"'))

    conn.create_function("json_extract", 2, extract)
    assert _observed_sql_models(conn) == ["fixture"]
    assert extracts == ['$."gen_ai.response.model"'] * 10


def test_empty_scope_does_not_read_model_tables(conn):
    def reject_reads(action, table, *_):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table in {"steps", "llm_calls"} else sqlite3.SQLITE_OK

    conn.set_authorizer(reject_reads)
    try:
        assert _observed_sql_models(conn, session_ids=set()) == []
    finally:
        conn.set_authorizer(None)


def test_alias_resolution_retains_step_only_models_and_unresolved_inventory(conn, tmp_path):
    conn.execute("INSERT INTO llm_calls VALUES('a','fixture',NULL)")
    step(conn, "a", "fixture20260101")
    step(conn, "b", "unpriced-model")
    alias_path = tmp_path / "aliases.json"
    table = PricingTable({"fixture": ModelPricing("fixture", 1, 1, 1, 1)}, "fixture", 0)
    result = ensure_cost_aliases(conn, alias_path=alias_path, pricing_table=table)
    assert result.observed_models == 3
    assert result.resolved_models == 2
    assert result.added_aliases == {"fixture20260101": "fixture"}
    assert result.unresolved_models == ("unpriced-model",)
    repeated = ensure_cost_aliases(conn, alias_path=alias_path, pricing_table=table)
    assert repeated.added_aliases == {}
    assert repeated.observed_models == result.observed_models
    assert repeated.unresolved_models == result.unresolved_models
