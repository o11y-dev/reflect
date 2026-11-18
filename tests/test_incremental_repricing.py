import json
import sqlite3

import pytest

from reflect.preparation_pipeline import reprice_sql_store
from reflect.pricing import ModelPricing, PricingTable
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


@pytest.fixture
def conn(tmp_path):
    connection = connect_sqlite(tmp_path / "repricing.db")
    migrate(connection)
    yield connection
    connection.close()


def price(rate=0.01, output_rate=0.02):
    return PricingTable({"fixture": ModelPricing("fixture", rate, output_rate, 0, 0)}, "fixture", 0)


def session(conn, sid, *, tokens=0, cost=0):
    conn.execute(
        """INSERT INTO sessions(id,input_tokens,estimated_cost_usd,started_at,created_at,updated_at)
        VALUES(?,?,?,'2026-01-01T00:00:00Z','before','before')""", (sid, tokens, cost),
    )


def step(conn, sid, key, *, at="2026-01-01T00:00:00Z", attrs=None):
    seq = conn.execute("SELECT COUNT(*) FROM steps WHERE session_id=?", (sid,)).fetchone()[0]
    conn.execute(
        """INSERT INTO steps(id,session_id,seq,type,started_at,raw_attrs_json,created_at,updated_at)
        VALUES(?,?,?,'llm_call',?,?,'before','before')""",
        (key, sid, seq, at, json.dumps(attrs or {})),
    )


def call(conn, sid, key, *, at="2026-01-01T00:00:00Z", model="fixture", tokens=10, request=None):
    step(conn, sid, key, at=at)
    attrs = {"gen_ai.usage.input_tokens": tokens, "gen_ai.usage.output_tokens": 2}
    if request:
        attrs["gen_ai.request.id"] = request
    conn.execute(
        """INSERT INTO llm_calls(id,step_id,session_id,provider,request_model,input_tokens,
        output_tokens,raw_attrs_json,created_at,updated_at)
        VALUES(?,?,?,'fixture',?,?,2,?,'before','before')""",
        (key, key, sid, model, tokens, json.dumps(attrs)),
    )


@pytest.mark.parametrize("model,table", [
    ("fixture", price()), (None, price()), ("fixture", price(output_rate=None)),
])
def test_unchanged_repricing_writes_nothing_and_skips_step_model_scan(conn, model, table):
    session(conn, "observed")
    call(conn, "observed", "usage", model=model)
    reprice_sql_store(conn, pricing_table=table, aliases={})
    before = tuple(conn.execute(
        "SELECT input_tokens,output_tokens,estimated_cost_usd,updated_at FROM sessions"
    ).fetchone())
    for name in ("llm_calls", "sessions"):
        conn.execute(f"""CREATE TRIGGER no_rewrite_{name} BEFORE UPDATE ON {name}
            BEGIN SELECT RAISE(ABORT,'unchanged cost evidence rewritten'); END""")
    conn.commit()

    def guard(action, table_name, column, *_):
        if action == sqlite3.SQLITE_READ and table_name == "steps" and column == "raw_attrs_json":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(guard)
    changes = conn.total_changes
    try:
        reprice_sql_store(conn, pricing_table=table, aliases={})
    finally:
        conn.set_authorizer(None)
    assert conn.total_changes == changes
    assert tuple(conn.execute(
        "SELECT input_tokens,output_tokens,estimated_cost_usd,updated_at FROM sessions"
    ).fetchone()) == before


def test_model_fallback_queries_only_sessions_that_need_it(conn):
    session(conn, "observed")
    call(conn, "observed", "usage")
    session(conn, "fallback", tokens=30)
    step(conn, "fallback", "model", attrs={"gen_ai.request.model": "fixture"})
    session(conn, "rejected", tokens=40, cost=99)
    step(conn, "rejected", "inferred", attrs={
        "gen_ai.request.model": "fixture", "reflect.model.provenance": "config_default",
    })
    session(conn, "llm-hint", tokens=50)
    step(conn, "llm-hint", "hint")
    conn.execute("""INSERT INTO llm_calls(id,step_id,session_id,request_model,created_at,updated_at)
        VALUES('hint','hint','llm-hint','fixture','before','before')""")
    queries = []
    conn.set_trace_callback(queries.append)
    reprice_sql_store(conn, pricing_table=price(), aliases={})
    conn.set_trace_callback(None)
    fallback = [sql for sql in queries if "GROUP BY session_id, model" in sql]
    assert len(fallback) == 1
    assert "'fallback'" in fallback[0] and "'rejected'" in fallback[0]
    assert "'observed'" not in fallback[0] and "'llm-hint'" not in fallback[0]
    costs = dict(conn.execute("SELECT id,estimated_cost_usd FROM sessions"))
    assert costs == pytest.approx({"observed": 0.14, "fallback": 0.3, "rejected": 0, "llm-hint": 0.5})


def test_late_duplicate_reassigns_cost_and_usage_corrections_and_rate_changes_reprice(conn):
    session(conn, "changed")
    call(conn, "changed", "later", at="2026-01-02T00:00:00Z", request="same")
    reprice_sql_store(conn, pricing_table=price(), aliases={})
    call(conn, "changed", "earlier", request="same")
    reprice_sql_store(conn, session_ids={"changed"}, pricing_table=price(), aliases={})
    assert dict(conn.execute("SELECT id,estimated_cost_usd FROM llm_calls")) == pytest.approx({
        "earlier": 0.14, "later": 0,
    })
    conn.execute("""UPDATE llm_calls SET input_tokens=20,
        raw_attrs_json=json_set(raw_attrs_json,'$."gen_ai.usage.input_tokens"',20)
        WHERE id='earlier'""")
    reprice_sql_store(conn, session_ids={"changed"}, pricing_table=price(), aliases={})
    assert tuple(conn.execute("SELECT input_tokens,estimated_cost_usd FROM sessions").fetchone()) == pytest.approx((20, 0.24))
    reprice_sql_store(conn, pricing_table=price(rate=0.02), aliases={})
    assert tuple(conn.execute("SELECT input_tokens,estimated_cost_usd FROM sessions").fetchone()) == pytest.approx((20, 0.44))
    assert conn.execute("SELECT estimated_cost_usd FROM llm_calls WHERE id='later'").fetchone()[0] == 0


def test_explicit_zero_corrects_stale_totals_without_changing_unselected_session(conn):
    session(conn, "changed", tokens=999, cost=99)
    call(conn, "changed", "zero", tokens=0)
    conn.execute("""UPDATE llm_calls SET output_tokens=0,
        raw_attrs_json=json_set(raw_attrs_json,'$."gen_ai.usage.output_tokens"',0)""")
    session(conn, "untouched", tokens=111, cost=12)
    conn.execute("""CREATE TRIGGER preserve_unselected BEFORE UPDATE ON sessions
        WHEN OLD.id='untouched' BEGIN SELECT RAISE(ABORT,'unselected session changed'); END""")
    reprice_sql_store(conn, session_ids={"changed"}, pricing_table=price(), aliases={})
    assert tuple(conn.execute("SELECT input_tokens,estimated_cost_usd FROM sessions WHERE id='changed'").fetchone()) == (0, 0)
    assert tuple(conn.execute("SELECT input_tokens,estimated_cost_usd FROM sessions WHERE id='untouched'").fetchone()) == (111, 12)
