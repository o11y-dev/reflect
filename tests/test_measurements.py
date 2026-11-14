import json
from dataclasses import asdict

import pytest

from reflect.pricing import ModelPricing, PricingTable, calculate_cost
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite
from reflect.views.measurements import build_measurements


@pytest.fixture
def conn(tmp_path):
    db = connect_sqlite(tmp_path / "test.db")
    migrate(db)
    yield db
    db.close()


def session(conn, sid="s1", **fields):
    values = {"id": sid, "started_at": "2020-01-01T00:00:00Z", "ended_at": "2026-11-01T23:00:00Z", "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-01T00:00:00Z", **fields}
    conn.execute(f"INSERT INTO sessions({','.join(values)}) VALUES({','.join('?' for _ in values)})", list(values.values()))


def step(conn, sid, key, at, event, turn="t1", **attrs):
    values = {"gen_ai.client.hook.event": event, "gen_ai.client.turn_id": turn, "reflect.telemetry.origin": "hook_otlp_trace", **attrs}
    seq = conn.execute("SELECT COUNT(*) FROM steps WHERE session_id=?", (sid,)).fetchone()[0]
    conn.execute("INSERT INTO steps(id,session_id,seq,type,started_at,ended_at,summary,raw_attrs_json,created_at,updated_at) VALUES(?,?,?,'conversation_event',?,?,?,?,?,?)", (key, sid, seq, at, at, event, json.dumps(values), at, at))


def call(conn, sid="s1", key="c1", at="2026-11-01T10:01:00Z", *, input_tokens=300, cache_read=600, cache_write=100, output_tokens=50, model="fixture", request=None, turn="t1", semantics="uncached", priced=True):
    raw = {"gen_ai.client.turn_id": turn, "reflect.usage.grain": "request", "reflect.usage.input_semantics": semantics, "gen_ai.usage.input_tokens": input_tokens, "gen_ai.usage.cache_read.input_tokens": cache_read, "gen_ai.usage.cache_creation.input_tokens": cache_write, "gen_ai.usage.output_tokens": output_tokens}
    if request:
        raw["gen_ai.request.id"] = request
    table = PricingTable({"fixture": ModelPricing("fixture", 3e-6, 15e-6, 3.75e-6, 0.3e-6)}, "fixture", 0)
    uncached = input_tokens - cache_read - cache_write if semantics == "inclusive" and cache_read is not None and cache_write is not None else input_tokens
    cost = calculate_cost({"input": uncached, "output": output_tokens, "cache_read": cache_read or 0, "cache_creation": cache_write or 0}, "fixture", table)
    if priced:
        raw.update({"reflect.cost.source": "fixture", "reflect.cost.breakdown": asdict(cost)})
    step(conn, sid, key, at, "usage", turn)
    conn.execute("INSERT INTO llm_calls(id,session_id,step_id,provider,request_model,input_tokens,output_tokens,cache_read_input_tokens,cache_creation_input_tokens,estimated_cost_usd,raw_attrs_json,created_at,updated_at) VALUES(?,?,?,'fixture',?,?,?,?,?,?,?,?,?)", (key, sid, key, model, input_tokens, output_tokens, cache_read or 0, cache_write or 0, cost.total_cost_usd if priced else 0, json.dumps(raw), at, at))


@pytest.mark.parametrize(("input_tokens", "semantics"), [(300, "uncached"), (1000, "inclusive")])
def test_cursor_and_claude_usage_have_identical_rate_based_costs(conn, input_tokens, semantics):
    session(conn)
    step(conn, "s1", "prompt", "2026-11-01T10:00:00Z", "UserPromptSubmit")
    call(conn, input_tokens=input_tokens, semantics=semantics)
    step(conn, "s1", "stop", "2026-11-01T10:02:00Z", "Stop")
    result = build_measurements(conn, include_turns=True)
    assert result.totals["input_tokens"] == 300
    assert result.totals["estimated_cost_usd"] == pytest.approx(0.002205)
    assert result.cost_breakdown["cache_read_cost_usd"] == pytest.approx(0.00018)
    assert result.cost_breakdown["output_cost_usd"] == pytest.approx(0.00075)
    assert result.metrics["cache_read_ratio"] == 0.6
    assert result.metrics["completed_turns"] == 1
    assert result.metrics["mean_input_tokens"] == 1000
    assert result.turns[0].input_tokens == 300
    assert result.model_mix[0]["input_tokens"] == 300
    assert result.model_mix[0]["tokens"] == 1050


def test_partial_buckets_missing_prices_and_model_stay_unavailable(conn):
    session(conn)
    call(conn, cache_write=None, priced=False, model=None)
    result = build_measurements(conn)
    assert result.metrics["cache_read_ratio"] is None
    assert result.coverage["complete_input_usage"] == 0
    assert result.coverage["cost_status"] == "unavailable"
    assert result.model_mix[0]["model"] is None


@pytest.mark.parametrize("ttl", [None, "1h", "5m"])
def test_repricing_preserves_unknown_cache_write_variant_and_known_components(conn, ttl):
    from reflect.preparation_pipeline import reprice_sql_store

    session(conn)
    call(conn)
    if ttl:
        conn.execute(
            "UPDATE llm_calls SET raw_attrs_json=json_set(raw_attrs_json, ?, 100)",
            (f'$."gen_ai.usage.cache_creation.{ttl}_input_tokens"',),
        )
    table = PricingTable({"fixture": ModelPricing("fixture", 3e-6, 15e-6, 3.75e-6, 0.3e-6)}, "fixture", 0)
    reprice_sql_store(conn, pricing_table=table, aliases={})
    view = build_measurements(conn)
    assert view.totals["cache_creation_tokens"] == 100
    assert view.totals["estimated_cost_usd"] == pytest.approx(0.002205 if ttl == "5m" else 0.00183)
    assert view.coverage["cost_status"] == ("complete" if ttl == "5m" else "partial")
    raw = json.loads(conn.execute("SELECT raw_attrs_json FROM llm_calls").fetchone()[0])
    assert raw["reflect.cost.price_source"] == "fixture"
    assert raw["reflect.cost.breakdown"]["missing_price_buckets"] == ([] if ttl == "5m" else ["cache_creation"])
    assert view.sessions["s1"]["cost_status"] == view.coverage["cost_status"]


@pytest.mark.parametrize("rate,status", [(0.0, "complete"), (None, "unavailable")])
def test_card_uses_canonical_price_coverage_for_zero_cost(conn, rate, status):
    from reflect.dashboard_query_common import session_card_from_row
    from reflect.preparation_pipeline import reprice_sql_store

    session(conn)
    call(conn, cache_write=0)
    reprice_sql_store(conn, pricing_table=PricingTable({"fixture": ModelPricing("fixture", rate, rate, rate, rate)}, "fixture", 0), aliases={})
    view = build_measurements(conn)
    card = session_card_from_row(
        {"session_id": "s1", "estimated_cost_usd": 123},
        measurement=view.sessions["s1"], cost_scope="event_window",
    )
    assert card["total_cost_usd"] == 0
    assert card["cost_status"] == status
    assert card["pricing_coverage"]["complete_priced_usage"] == (1 if rate == 0 else 0)
    assert card["cost_scope"] == "event_window"


def test_event_time_window_includes_recent_usage_in_old_session_and_deduplicates_requests(conn):
    session(conn)
    call(conn, request="same")
    call(conn, key="c2", request="same")
    call(conn, key="c3", at="2026-11-02T00:00:00Z")
    result = build_measurements(conn, date_from="2026-11-01T00:00:00Z", date_to="2026-11-02T00:00:00Z")
    assert result.totals["input_tokens"] == 300
    assert result.coverage["observed_usage"] == 1
    assert result.coverage["usage_grains"] == {"request": 1}


def test_repricing_and_reader_choose_same_duplicate_before_window_filter(conn):
    from reflect.preparation_pipeline import reprice_sql_store

    session(conn)
    call(conn, key="later_inserted_first", at="2026-11-02T10:02:00Z", request="duplicate", cache_write=0)
    call(conn, key="earlier_inserted_second", at="2026-11-01T10:01:00Z", request="duplicate", cache_write=0)
    table = PricingTable({"fixture": ModelPricing("fixture", 3e-6, 15e-6, 3.75e-6, 0.3e-6)}, "fixture", 0)
    for _ in range(2):
        reprice_sql_store(conn, pricing_table=table, aliases={})
        lifetime = build_measurements(conn)
        assert lifetime.totals["estimated_cost_usd"] == pytest.approx(0.00183)
        assert lifetime.totals["estimated_cost_usd"] == conn.execute("SELECT estimated_cost_usd FROM sessions").fetchone()[0]
        assert lifetime.totals["input_tokens"] == 300
        assert lifetime.coverage["observed_usage"] == 1
        later = build_measurements(conn, date_from="2026-11-02T00:00:00Z")
        assert later.totals["estimated_cost_usd"] == 0
        assert later.coverage["observed_usage"] == 0


@pytest.mark.parametrize("semantics,input_tokens", [("inclusive", 1000), ("uncached", 400)])
def test_repricing_uses_same_uncached_input_as_measurements(conn, semantics, input_tokens):
    from reflect.preparation_pipeline import reprice_sql_store

    session(conn)
    call(conn, input_tokens=input_tokens, cache_write=0, semantics=semantics)
    table = PricingTable({"fixture": ModelPricing("fixture", 3e-6, 15e-6, 3.75e-6, 0.3e-6)}, "fixture", 0)
    for _ in range(2):
        reprice_sql_store(conn, pricing_table=table, aliases={})
        result = build_measurements(conn)
        assert result.totals["input_tokens"] == 400
        assert result.model_mix[0]["input_tokens"] == 400
        assert result.totals["estimated_cost_usd"] == pytest.approx(0.00213)
        assert conn.execute("SELECT input_tokens FROM sessions").fetchone()[0] == 400


def test_explicit_zero_usage_clears_stale_session_counters_and_cost(conn):
    from reflect.preparation_pipeline import reprice_sql_store

    session(conn, input_tokens=999, estimated_cost_usd=123)
    call(conn, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0)
    table = PricingTable({"fixture": ModelPricing("fixture", 3e-6, 15e-6, 3.75e-6, 0.3e-6)}, "fixture", 0)
    reprice_sql_store(conn, pricing_table=table, aliases={})
    assert tuple(conn.execute("SELECT input_tokens,estimated_cost_usd FROM sessions").fetchone()) == (0, 0)
    result = build_measurements(conn)
    assert result.coverage["observed_usage"] == 1
    assert result.coverage["cost_status"] == "complete"


def main_turns(conn):
    session(conn)
    step(conn, "s1", "prompt1", "2026-11-01T10:00:00Z", "UserPromptSubmit", "t1")
    step(conn, "s1", "stop1", "2026-11-01T10:02:00Z", "Stop", "t1")
    step(conn, "s1", "prompt2", "2026-11-01T10:12:00Z", "UserPromptSubmit", "t2")
    step(conn, "s1", "stop2", "2026-11-01T10:14:00Z", "Stop", "t2")


def test_idle_uses_stop_to_prompt_not_stop_to_stop_or_session_lifetime(conn):
    main_turns(conn)
    session(conn, "long_lived", started_at="2019-01-01", ended_at="2030-01-01")
    view = build_measurements(conn, session_ids={"s1"}, include_turns=True)
    assert view.metrics["idle_total_ms"] == 600_000
    assert view.metrics["idle_over_five_minutes"] == 1
    assert view.turns[1].idle_before_ms == 600_000
    all_view = build_measurements(conn, include_turns=True)
    assert all_view.metrics["idle_total_ms"] == view.metrics["idle_total_ms"]


def test_overlapping_child_work_is_visible_even_when_report_is_filtered(conn):
    main_turns(conn)
    session(conn, "child", parent_session_id="s1")
    step(conn, "child", "childprompt", "2026-11-01T10:05:00Z", "UserPromptSubmit")
    step(conn, "child", "childstop", "2026-11-01T10:06:00Z", "Stop")
    scoped = build_measurements(conn, session_ids={"s1"}, include_turns=True)
    full = build_measurements(conn, include_turns=True)
    assert scoped.turns[1].idle_before_ms is None
    assert next(t for t in full.turns if t.session_id == "s1" and t.turn_id == "t2").idle_before_ms is None


@pytest.mark.parametrize("relationship", ["child", "parent", "sibling", "unrelated"])
@pytest.mark.parametrize("windowed", [False, True])
def test_scoped_idle_keeps_context_turns_spanning_entire_gap(conn, relationship, windowed):
    main_turns(conn)
    session(conn, "context", parent_session_id="s1" if relationship == "child" else None)
    if relationship == "parent":
        conn.execute("UPDATE sessions SET parent_session_id='context' WHERE id='s1'")
    elif relationship == "sibling":
        session(conn, "shared-parent")
        conn.execute("UPDATE sessions SET parent_session_id='shared-parent' WHERE id IN ('s1','context')")
    # Neither boundary is in the potential idle gap or reporting window.
    # The paired interval, not session metadata, prevents an idle claim.
    step(conn, "context", "context-prompt", "2026-11-01T09:00:00Z", "UserPromptSubmit")
    step(conn, "context", "context-stop", "2026-11-01T11:00:00Z", "Stop")
    dates = {"date_from": "2026-11-01T10:00:00Z", "date_to": "2026-11-01T10:15:00Z"} if windowed else {}
    scoped = build_measurements(conn, session_ids={"s1"}, include_turns=True, **dates)
    full = build_measurements(conn, include_turns=True, **dates)
    assert scoped.turns == [turn for turn in full.turns if turn.session_id == "s1"]
    assert scoped.turns[1].idle_before_ms is None
    assert scoped.coverage["eligible_idle_intervals"] == 0


def test_scoped_boundary_reads_skip_irrelevant_history_and_preserve_empty_full_scopes(conn, monkeypatch):
    from reflect.views import measurements

    main_turns(conn)
    expected = build_measurements(conn, session_ids={"s1"}, include_turns=True).model_dump()
    for period, date in (("old", "2020-01-01"), ("future", "2030-01-01")):
        session(conn, period)
        for number in range(40):
            step(conn, period, f"{period}-prompt-{number}", f"{date}T09:00:00Z", "UserPromptSubmit", str(number))
            step(conn, period, f"{period}-stop-{number}", f"{date}T09:01:00Z", "Stop", str(number))
    original = measurements._rows
    boundary_reads = []

    def capture(db, sql, params=None):
        rows = original(db, sql, params)
        if "reflect.turn.terminal" in sql:
            plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, params or [])]
            assert any("SEARCH steps" in row and "session_id=?" in row for row in plan)
            boundary_reads.extend(row["id"] for row in rows)
        return rows

    monkeypatch.setattr(measurements, "_rows", capture)
    assert build_measurements(conn, session_ids={"s1"}, include_turns=True).model_dump() == expected
    assert set(boundary_reads) == {"prompt1", "stop1", "prompt2", "stop2"}
    boundary_reads.clear()
    empty = build_measurements(conn, session_ids=set(), include_turns=True)
    assert empty.sessions == {}
    assert empty.metrics["completed_turns"] == 0
    assert boundary_reads == []
    full = build_measurements(conn, include_turns=True)
    assert full.metrics["completed_turns"] == 82
    assert len(boundary_reads) == 164


def test_context_extents_keep_submillisecond_and_python_iso_timestamps(conn):
    main_turns(conn)
    conn.execute("UPDATE steps SET started_at=?,ended_at=? WHERE id='stop1'",
                 ("2026-11-01T10:02:00.000200Z",) * 2)
    session(conn, "context")
    step(conn, "context", "context-prompt", "2026-W44-7T09:00:00Z", "UserPromptSubmit")
    step(conn, "context", "context-stop", "2026-11-01T12:02:00.000400+02:00", "Stop")
    scoped = build_measurements(conn, session_ids={"s1"}, include_turns=True)
    full = build_measurements(conn, include_turns=True)
    assert scoped.turns == [turn for turn in full.turns if turn.session_id == "s1"]
    assert scoped.turns[1].idle_before_ms is None


def test_synthetic_boundaries_do_not_become_completed_turns(conn):
    session(conn)
    step(conn, "s1", "prompt", "2026-11-01T10:00:00Z", "UserPromptSubmit", **{"reflect.time.provenance": "synthetic"})
    step(conn, "s1", "stop", "2026-11-01T10:01:00Z", "Stop", **{"reflect.time.provenance": "synthetic"})
    assert build_measurements(conn).metrics["completed_turns"] == 0


@pytest.mark.parametrize("provenance", ["local_telemetry", "estimated_cursor_transcript"])
def test_session_only_usage_is_preserved_lifetime_but_never_dated(conn, provenance):
    session(conn, input_tokens=500, estimated_cost_usd=2.0, token_provenance=provenance)
    lifetime = build_measurements(conn)
    assert lifetime.totals["input_tokens"] == 500
    assert lifetime.coverage["retained_token_bucket_records"]["input_tokens"] == 1
    assert lifetime.totals["estimated_cost_usd"] == 2.0
    assert lifetime.coverage["unattributed_usage"] == 1
    assert lifetime.coverage["cost_status"] == "partial"
    period = build_measurements(conn, date_from="2026-11-01T00:00:00Z")
    assert period.totals["input_tokens"] == 0
    assert period.coverage["undated_usage"] == 1
    assert period.coverage["retained_token_bucket_records"]["input_tokens"] == 0


def test_stale_session_aggregates_never_override_corrected_canonical_usage(conn):
    session(conn, input_tokens=999999, estimated_cost_usd=999)
    call(conn)
    view = build_measurements(conn)
    assert view.totals["input_tokens"] == 300
    assert view.totals["estimated_cost_usd"] == pytest.approx(0.002205)


def test_empty_llm_activity_is_not_usage_and_does_not_hide_session_totals(conn):
    session(conn, input_tokens=500)
    call(conn, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0, priced=False)
    conn.execute("UPDATE llm_calls SET raw_attrs_json='{}'")
    view = build_measurements(conn)
    assert view.totals["input_tokens"] == 500
    assert view.coverage["observed_usage"] == 1
    assert view.coverage["usage_grains"] == {"session": 1}
    assert view.model_mix[0]["model"] is None


@pytest.mark.parametrize("provenance", ["inferred", "config_default"])
def test_inferred_models_cannot_keep_historical_estimates(conn, provenance):
    from reflect.preparation_pipeline import reprice_sql_store

    session(conn, input_tokens=300, estimated_cost_usd=123)
    call(conn)
    conn.execute('UPDATE llm_calls SET raw_attrs_json=json_set(raw_attrs_json, \'$."reflect.model.provenance"\', ?)', (provenance,))
    before = build_measurements(conn)
    assert before.totals["estimated_cost_usd"] == 0
    assert before.coverage["cost_status"] == "unavailable"
    table = PricingTable({"fixture": ModelPricing("fixture", 3e-6, 15e-6, 3.75e-6, 0.3e-6)}, "fixture", 0)
    reprice_sql_store(conn, pricing_table=table, aliases={})
    assert conn.execute("SELECT estimated_cost_usd FROM sessions").fetchone()[0] == 0
    assert build_measurements(conn).totals["input_tokens"] == 300


def test_session_only_fallback_does_not_keep_inferred_or_parent_model_cost(conn):
    from reflect.preparation_pipeline import reprice_sql_store

    session(conn, input_tokens=300, estimated_cost_usd=123)
    step(conn, "s1", "inferred", "2026-11-01T10:00:00Z", "Stop", **{"gen_ai.request.model": "fixture", "reflect.model.provenance": "config_default"})
    session(conn, "child", input_tokens=500, estimated_cost_usd=456, source_ref="native_session:cursor:/tmp/agent-transcripts/s1/subagents/child.jsonl")
    table = PricingTable({"fixture": ModelPricing("fixture", 3e-6, 15e-6, 3.75e-6, 0.3e-6)}, "fixture", 0)
    reprice_sql_store(conn, pricing_table=table, aliases={})
    assert [row[0] for row in conn.execute("SELECT estimated_cost_usd FROM sessions ORDER BY id")] == [0, 0]
    assert build_measurements(conn).totals["input_tokens"] == 800


def test_cost_series_uses_observed_utc_event_day_and_shared_agent_totals(conn):
    conn.execute("INSERT INTO agents(id,name,created_at,updated_at) VALUES('claude','claude','2026-11-01','2026-11-01')")
    session(conn, agent_id="claude")
    call(conn, at="2026-11-01T01:00:00+02:00")
    view = build_measurements(conn, usage_only=True)
    assert view.cost_by_day == {"2026-10-31": pytest.approx(0.002205)}
    assert view.agent_cost_over_time == [{"day": "2026-10-31", "agent": "claude", "total_cost": pytest.approx(0.002205)}]
    assert view.sessions["s1"]["input_tokens"] == view.totals["input_tokens"]
    assert view.sessions["s1"]["estimated_cost_usd"] == view.totals["estimated_cost_usd"]


def test_session_duration_includes_observed_tools_outside_turn_boundaries(conn):
    main_turns(conn)
    step(conn, "s1", "tool", "2026-11-01T10:18:00Z", "PostToolUse", "t2")
    conn.execute("UPDATE steps SET type='tool_call' WHERE id='tool'")
    assert build_measurements(conn).metrics["session_duration_ms"] == 18 * 60 * 1000


@pytest.mark.parametrize(("models", "switches", "eligible"), [(["a"], 0, 0), ([None, None], None, 0), (["a", None, "b"], None, 0), (["a", "b", None, "a"], 1, 1)])
def test_model_switch_coverage_preserves_unknown_sequences(conn, models, switches, eligible):
    session(conn)
    for i, model in enumerate(models):
        tid = f"t{i}"
        step(conn, "s1", f"prompt{i}", f"2026-11-01T10:{i * 3:02}:00Z", "UserPromptSubmit", tid)
        call(conn, key=f"usage{i}", at=f"2026-11-01T10:{i * 3 + 1:02}:00Z", model=model, turn=tid)
        step(conn, "s1", f"stop{i}", f"2026-11-01T10:{i * 3 + 2:02}:00Z", "Stop", tid)
    view = build_measurements(conn)
    assert view.metrics["model_switches"] == switches
    assert view.coverage["eligible_model_transitions"] == eligible
    assert view.coverage["known_model_usage"] == sum(m is not None for m in models)


def test_partial_price_preserves_observed_sum_but_not_full_price_coverage(conn):
    session(conn)
    step(conn, "s1", "prompt", "2026-11-01T10:00:00Z", "UserPromptSubmit")
    call(conn)
    step(conn, "s1", "stop", "2026-11-01T10:02:00Z", "Stop")
    conn.execute('''UPDATE llm_calls SET raw_attrs_json=json_set(raw_attrs_json,
        '$."reflect.cost.source"','partial',
        '$."reflect.cost.breakdown".missing_price_buckets',json('["cache_creation"]'))''')
    view = build_measurements(conn, include_turns=True)
    assert view.totals["estimated_cost_usd"] == pytest.approx(0.002205)
    assert view.coverage["cost_status"] == "partial"
    assert view.coverage["priced_usage"] == 0
    assert view.coverage["partially_priced_usage"] == 1
    assert view.turns[0].cache_write_cost_usd is None


def test_model_price_without_usage_does_not_become_a_measured_zero(conn):
    session(conn)
    call(conn, input_tokens=0, cache_read=0, cache_write=0, output_tokens=0)
    # Explicit zero buckets are fully observed and can have genuine zero cost.
    assert build_measurements(conn).coverage["cost_status"] == "complete"
    conn.execute('''UPDATE llm_calls SET raw_attrs_json=json_remove(raw_attrs_json,
        '$."gen_ai.usage.input_tokens"','$."gen_ai.usage.output_tokens"',
        '$."gen_ai.usage.cache_read.input_tokens"','$."gen_ai.usage.cache_creation.input_tokens"')''')
    assert build_measurements(conn).coverage["cost_status"] == "unavailable"


@pytest.mark.parametrize("has_prompt", [True, False])
def test_observed_unfinished_or_tools_only_work_prevents_idle_claim(conn, has_prompt):
    main_turns(conn)
    session(conn, "child", parent_session_id="s1")
    if has_prompt:
        step(conn, "child", "childprompt", "2026-11-01T10:05:00Z", "UserPromptSubmit")
    step(conn, "child", "childwork", "2026-11-01T10:06:00Z", "PreToolUse")
    conn.execute("UPDATE steps SET type='tool_call',ended_at='2026-11-01T10:07:00Z' WHERE id='childwork'")
    scoped = build_measurements(conn, session_ids={"s1"}, include_turns=True)
    assert scoped.turns[1].idle_before_ms is None
    assert scoped.metrics["idle_total_ms"] is None
    assert scoped.coverage["eligible_idle_intervals"] == 0


@pytest.mark.parametrize("bucket,attribute", [
    ("input_tokens", "gen_ai.usage.input_tokens"),
    ("output_tokens", "gen_ai.usage.output_tokens"),
    ("cache_read_tokens", "gen_ai.usage.cache_read.input_tokens"),
    ("cache_creation_tokens", "gen_ai.usage.cache_creation.input_tokens"),
])
def test_bucket_coverage_distinguishes_observed_zero_from_missing_and_deduplicates(conn, bucket, attribute):
    session(conn)
    for key, request, at in (
        ("zero", "same", "2026-11-01T10:00:00Z"),
        ("duplicate", "same", "2026-11-01T10:00:01Z"),
        ("missing", "missing", "2026-11-01T10:01:00Z"),
        ("outside", "outside", "2026-11-02T00:00:00Z"),
    ):
        call(conn, key=key, request=request, at=at, input_tokens=0,
             output_tokens=0, cache_read=0, cache_write=0, priced=False)
    conn.execute(
        "UPDATE llm_calls SET raw_attrs_json=json_remove(raw_attrs_json, ?) WHERE id='missing'",
        (f'$."{attribute}"',),
    )
    view = build_measurements(
        conn, date_from="2026-11-01T00:00:00Z", date_to="2026-11-02T00:00:00Z", usage_only=True,
    )
    assert view.coverage["observed_usage"] == 2
    assert view.coverage["token_bucket_records"] == {
        name: 1 if name == bucket else 2
        for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
    }
    assert view.totals == {
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
        "cache_creation_tokens": 0, "reasoning_tokens": 0, "estimated_cost_usd": 0,
    }
    assert view.model_mix[0]["calls"] == 2
    assert view.model_mix[0]["cost_observations"] == 0


def test_model_cost_observations_include_priced_zero_and_partial_but_not_unpriced_calls(conn):
    session(conn)
    call(conn, key="priced", model="alpha")
    call(conn, key="free", model="alpha", input_tokens=0, output_tokens=0, cache_read=0, cache_write=0)
    call(conn, key="unpriced", model="alpha", priced=False)
    call(conn, key="partial", model="beta")
    call(conn, key="inferred", model="guessed")
    conn.execute('''UPDATE llm_calls SET raw_attrs_json=json_set(raw_attrs_json,
        '$."reflect.cost.source"','partial') WHERE id='partial' ''')
    conn.execute('''UPDATE llm_calls SET raw_attrs_json=json_set(raw_attrs_json,
        '$."reflect.model.provenance"','inferred') WHERE id='inferred' ''')
    view = build_measurements(conn, usage_only=True)
    models = {row["model"]: row for row in view.model_mix}
    assert (models["alpha"]["calls"], models["alpha"]["cost_observations"]) == (3, 2)
    assert (models["beta"]["calls"], models["beta"]["cost_observations"]) == (1, 1)
    assert (models[None]["calls"], models[None]["cost_observations"]) == (1, 0)
    assert models["alpha"]["cost_usd"] == pytest.approx(0.002205)
    assert models["beta"]["cost_usd"] == pytest.approx(0.002205)
    assert models[None]["cost_usd"] == 0
    assert view.totals["estimated_cost_usd"] == pytest.approx(0.00441)
    assert view.totals["input_tokens"] == 1200
    assert view.totals["output_tokens"] == 200
    assert view.coverage["observed_usage"] == 5
    assert view.coverage["cost_status"] == "partial"


def test_elapsed_means_and_nearest_rank_p95_include_observed_zero_not_empty_or_outside_sessions(conn):
    for seconds in range(21):
        sid = f"duration-{seconds}"
        session(conn, sid)
        step(conn, sid, f"prompt-{seconds}", "2026-11-01T10:00:00Z", "UserPromptSubmit")
        step(conn, sid, f"stop-{seconds}", f"2026-11-01T10:00:{seconds:02}Z", "Stop")
    session(conn, "empty")
    session(conn, "outside")
    step(conn, "outside", "outside-prompt", "2026-11-01T09:00:00Z", "UserPromptSubmit")
    step(conn, "outside", "outside-stop", "2026-11-01T09:59:00Z", "Stop")
    session(conn, "synthetic")
    for key, at, event in (("synthetic-prompt", "2026-11-01T10:00:00Z", "UserPromptSubmit"),
                           ("synthetic-stop", "2026-11-01T10:00:59Z", "Stop")):
        step(conn, "synthetic", key, at, event, **{"reflect.time.provenance": "synthetic"})
    view = build_measurements(conn, date_from="2026-11-01T10:00:00Z", date_to="2026-11-01T10:01:00Z")
    for kind in ("session", "turn"):
        assert view.metrics[f"mean_{kind}_elapsed_ms"] == 10_000
        assert view.metrics[f"p95_{kind}_elapsed_ms"] == 19_000
        assert view.coverage[f"eligible_{kind}_elapsed"] == 21
    assert view.metrics["session_duration_ms"] == 210_000
    assert view.metrics["completed_turns"] == 21
    assert view.totals["input_tokens"] == view.totals["estimated_cost_usd"] == 0


@pytest.mark.parametrize("date_from,date_to", [
    ("2026-11-01T10:00:00Z", "2026-11-01T10:00:10Z"),
    ("2026-11-01T10:00:05Z", "2026-11-01T10:00:11Z"),
])
def test_elapsed_window_never_pairs_a_turn_across_either_boundary(conn, date_from, date_to):
    session(conn)
    step(conn, "s1", "prompt", "2026-11-01T10:00:00Z", "UserPromptSubmit")
    step(conn, "s1", "stop", "2026-11-01T10:00:10Z", "Stop")
    view = build_measurements(conn, date_from=date_from, date_to=date_to)
    assert view.metrics["completed_turns"] == 0
    assert view.metrics["mean_turn_elapsed_ms"] is None
    assert view.metrics["p95_turn_elapsed_ms"] is None
    assert view.coverage["eligible_turn_elapsed"] == 0
    # A single observed instant has known zero elapsed time; it does not
    # inherit the session's lifetime or the missing end of the turn.
    assert view.metrics["mean_session_elapsed_ms"] == 0
    assert view.metrics["p95_session_elapsed_ms"] == 0
    assert view.coverage["eligible_session_elapsed"] == 1


def test_completed_tool_mean_uses_only_paired_turns_with_observed_results(conn):
    session(conn)
    for number in range(5):
        tid = f"t{number}"
        step(conn, "s1", f"prompt-{tid}", f"2026-11-01T10:{number * 3:02}:00Z", "UserPromptSubmit", tid)
        if number < 4:
            step(conn, "s1", f"stop-{tid}", f"2026-11-01T10:{number * 3 + 2:02}:00Z", "Stop", tid)
        for index in range((2, 4, 1, 1, 10)[number]):
            key = f"tool-{number}-{index}"
            at = f"2026-11-01T10:{number * 3 + 1:02}:00Z"
            attrs = {"gen_ai.client.turn_id": tid,
                     "gen_ai.client.hook.event": "PreToolUse" if number == 2 else "PostToolUse"}
            if number == 3:
                attrs["reflect.tool.identity_provenance"] = "synthetic"
            step(conn, "s1", key, at, "tool", tid)
            conn.execute(
                """INSERT INTO tool_calls(id, step_id, session_id, tool_name, logical_call_id,
                   status, raw_attrs_json, created_at, updated_at) VALUES (?, ?, 's1', 'test', ?,
                   'ok', ?, ?, ?)""", (key, key, key, json.dumps(attrs), at, at),
            )
    view = build_measurements(conn, include_turns=True)
    assert view.coverage["observed_prompt_turns"] == 5
    assert view.metrics["completed_turns"] == 4
    assert view.coverage["eligible_tool_turns"] == 2
    assert [turn.tool_calls for turn in view.turns] == [2, 4, None, None]
    assert view.metrics["completed_tool_calls"] == 6
    assert view.metrics["mean_tool_calls_per_turn"] == 3
    assert view.metrics["max_tool_calls_per_turn"] == 4

    conn.execute('''UPDATE tool_calls SET raw_attrs_json=json_set(raw_attrs_json,
        '$."gen_ai.client.hook.event"','PreToolUse')''')
    unknown = build_measurements(conn)
    assert unknown.coverage["eligible_tool_turns"] == 0
    assert unknown.metrics["completed_tool_calls"] is None
    assert unknown.metrics["mean_tool_calls_per_turn"] is None


def test_explicit_nonterminal_tool_result_cannot_count_as_completed(conn):
    session(conn)
    step(conn, "s1", "prompt", "2026-11-01T10:00:00Z", "UserPromptSubmit")
    step(conn, "s1", "stop", "2026-11-01T10:02:00Z", "Stop")
    step(conn, "s1", "tool", "2026-11-01T10:01:00Z", "PostToolUse")
    attrs = {"gen_ai.client.turn_id": "t1", "gen_ai.client.hook.event": "PostToolUse",
             "reflect.tool.terminal": False}
    conn.execute(
        """INSERT INTO tool_calls(id, step_id, session_id, tool_name, logical_call_id,
           status, output_hash, raw_attrs_json, created_at, updated_at)
           VALUES ('tool', 'tool', 's1', 'test', 'tool', 'ok', 'partial-output', ?,
                   '2026-11-01', '2026-11-01')""", (json.dumps(attrs),),
    )
    view = build_measurements(conn, include_turns=True)
    assert view.metrics["completed_turns"] == 1
    assert view.turns[0].tool_calls is None
    assert view.coverage["eligible_tool_turns"] == 0
    assert view.metrics["completed_tool_calls"] is None
    assert view.metrics["mean_tool_calls_per_turn"] is None


def test_tool_measurement_uses_covering_facts_including_terminal_flag(conn):
    session(conn)
    statements = []
    conn.set_trace_callback(statements.append)
    build_measurements(conn)
    conn.set_trace_callback(None)
    query = next(sql for sql in statements if "FROM tool_calls tc" in sql)
    plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + query)]
    assert any("COVERING INDEX idx_tool_calls_measurement_facts" in detail for detail in plan)
    # Matching SQLite expression projections are read from the index. Adding
    # metadata must not silently reopen every retained command/output blob.
    assert not any(row[1] == "Function" and str(row[5]).startswith("json_extract")
                   for row in conn.execute("EXPLAIN " + query))
