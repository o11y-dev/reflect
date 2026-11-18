import json
from dataclasses import replace

import pytest

from reflect.pricing import ModelPricing, PricingTable, pricing_inputs_fingerprint
from reflect.store import cost_refresh
from reflect.store.cost_refresh import CostRefreshState
from reflect.store.migrate import load_migrations, migrate
from reflect.store.sqlite import connect_sqlite


@pytest.fixture
def store(tmp_path, monkeypatch):
    conn = connect_sqlite(tmp_path / "cost.db")
    migrate(conn)
    aliases = tmp_path / "aliases.json"
    aliases.write_text('{}')
    tables = [PricingTable({"used": ModelPricing("used", 0.01, 0.02, 0.03, 0)}, "fixture", 0)]
    monkeypatch.setattr(cost_refresh, "load_pricing_table", lambda: tables[0])
    yield conn, CostRefreshState(conn), aliases, tables
    conn.close()


def model(conn, name, session_id="session"):
    conn.execute(
        "INSERT INTO sessions(id,started_at,created_at,updated_at) "
        "VALUES (?,'2026-11-01T00:00:00Z','fixture','fixture') ON CONFLICT(id) DO NOTHING",
        (session_id,),
    )
    conn.execute(
        "INSERT INTO steps(id,session_id,seq,type,started_at,raw_attrs_json,created_at,updated_at) "
        "VALUES (?, ?, 0, 'llm_call', '2026-11-01T00:00:00Z', ?, 'fixture', 'fixture')",
        (f"{session_id}:{name}", session_id, json.dumps({"gen_ai.request.model": name})),
    )
    conn.commit()


def test_unrelated_catalogue_changes_skip_history_and_repeated_inventory_scan(store):
    conn, state, aliases, tables = store
    model(conn, "used")
    initial = state.inspect(alias_path=aliases)
    assert initial.requires_full_refresh
    state.mark_current(initial)
    tables[0] = replace(tables[0], prices={**tables[0].prices, "unrelated": ModelPricing("unrelated", 99)})
    aliases.write_text(json.dumps({"unrelated-alias": "unrelated"}))
    queries = []
    conn.set_trace_callback(queries.append)
    unchanged = state.inspect(alias_path=aliases, session_ids=set())
    before = conn.total_changes
    state.mark_current(unchanged)
    conn.set_trace_callback(None)
    assert not unchanged.requires_full_refresh
    assert unchanged.fingerprint == initial.fingerprint
    assert conn.total_changes == before
    assert not any("FROM steps" in query or "FROM llm_calls" in query for query in queries)


@pytest.mark.parametrize("change", ["input", "cache", "missing-cache", "alias", "unit", "missing-model"])
def test_changes_affecting_observed_model_invalidate_historical_costs(store, change):
    conn, state, aliases, tables = store
    model(conn, "used")
    state.mark_current(state.inspect(alias_path=aliases))
    price = tables[0].prices["used"]
    if change == "input":
        tables[0] = replace(tables[0], prices={"used": replace(price, input_cost_per_token=0.04)})
    elif change == "cache":
        tables[0] = replace(tables[0], prices={"used": replace(price, cache_read_cost_per_token=0.04)})
    elif change == "missing-cache":
        tables[0] = replace(tables[0], prices={"used": replace(price, cache_read_cost_per_token=None)})
    elif change == "alias":
        aliases.write_text(json.dumps({"used": "different"}))
        tables[0] = replace(tables[0], prices={"different": replace(price, model_key="different")})
    elif change == "unit":
        tables[0] = replace(tables[0], pricing_unit="different")
    else:
        tables[0] = replace(tables[0], prices={})
    assert state.inspect(alias_path=aliases, session_ids=set()).requires_full_refresh


def test_new_observed_model_only_prices_changed_scope_then_joins_inventory(store):
    conn, state, aliases, tables = store
    model(conn, "used")
    state.mark_current(state.inspect(alias_path=aliases))
    model(conn, "new", session_id="new-session")
    fresh = state.inspect(alias_path=aliases, session_ids={"new-session"})
    assert set(fresh.model_fingerprints) == {"used", "new"}
    assert not fresh.requires_full_refresh
    state.mark_current(fresh)
    tables[0] = replace(tables[0], prices={**tables[0].prices, "new": ModelPricing("new", 0.05)})
    # Newly published pricing for the previously unknown model is a real change.
    assert state.inspect(alias_path=aliases, session_ids=set()).requires_full_refresh


def test_corrupt_inventory_recovers_with_complete_model_discovery(store):
    conn, state, aliases, _ = store
    model(conn, "used")
    conn.execute("INSERT INTO store_metadata(key,value,updated_at) VALUES (?,'broken','fixture')", (state.METADATA_KEY,))
    conn.commit()
    captured = state.inspect(alias_path=aliases, session_ids=set())
    assert captured.requires_full_refresh
    assert set(captured.model_fingerprints) == {"used"}


@pytest.mark.parametrize("changed", [False, True])
def test_migration_bootstrap_reuses_only_matching_catalogue_checkpoint(tmp_path, monkeypatch, changed):
    conn = connect_sqlite(tmp_path / "upgrade.db")
    migrations = load_migrations()
    with monkeypatch.context() as context:
        context.setattr("reflect.store.migrate.load_migrations", lambda: [item for item in migrations if item.version <= 39])
        migrate(conn)
    try:
        table = PricingTable({"used": ModelPricing("used", 0.01)}, "fixture", 0)
        model(conn, "used")
        conn.execute("INSERT INTO store_metadata(key,value,updated_at) VALUES ('cost_pricing_inputs_v1',?,'fixture')",
                     (pricing_inputs_fingerprint(table, {}),))
        conn.commit()
        migrate(conn)
        if changed:
            table = replace(table, prices={"used": ModelPricing("used", 0.02)})
        monkeypatch.setattr(cost_refresh, "load_pricing_table", lambda: table)
        path = tmp_path / "aliases.json"
        path.write_text('{}')
        state = CostRefreshState(conn)
        captured = state.inspect(alias_path=path, session_ids=set())
        assert captured.requires_full_refresh is changed
        assert set(captured.model_fingerprints) == {"used"}
        state.mark_current(captured)
        assert not state.inspect(alias_path=path, session_ids=set()).requires_full_refresh
        assert conn.execute("SELECT 1 FROM store_metadata WHERE key='cost_pricing_inputs_v1'").fetchone() is None
    finally:
        conn.close()
