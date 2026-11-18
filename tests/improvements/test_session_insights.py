from datetime import UTC, datetime, timedelta

import pytest

from reflect.improvements.repository import ImprovementRepository
from reflect.improvements.session_insights import (
    SessionInsightRule,
    read_session_insights,
    session_inputs,
    session_insight_rules,
    top_session_insights,
)
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite
from reflect.views.measurements import MeasurementView, TurnMeasurement


def turn(index, **values):
    start = datetime(2026, 11, 1, tzinfo=UTC) + timedelta(minutes=index * 2)
    fields = {"session_id": "s1", "turn_id": str(index), "prompt_at": start.isoformat(), "ended_at": (start + timedelta(minutes=1)).isoformat(), "model": "opus", "requested_mode": "manual", "input_tokens": 500_000, "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 100, "cost_usd": 2.0, "input_cost_usd": 2.0, "cache_write_cost_usd": 0.0, "tool_calls": 30, "idle_before_ms": 60_000 if index else None, "complete_usage": True}
    return TurnMeasurement(**{**fields, **values})


def inputs(turns):
    known = sum(t.cost_usd for t in turns if t.cost_usd is not None)
    return session_inputs(MeasurementView(scope={"watermark": "2026-11-02"}, sessions={"s1": {"known_cost_usd": known}}, turns=turns))


@pytest.mark.parametrize(("name", "turns"), [
    ("very_long", [turn(i) for i in range(21)]),
    ("heavy_turns", [turn(0, input_tokens=5_000_000)]),
    ("tool_spiral", [turn(0)]),
    ("low_cache_hit", [turn(0)]),
    ("premium_model", [turn(0)]),
    ("model_switching", [turn(i, model="opus" if i % 2 else "sonnet") for i in range(4)]),
    ("idle_rebuild", [turn(0), turn(1, idle_before_ms=600_000)]),
])
def test_each_r5_rule_emits_a_session_finding_without_a_workflow(name, turns):
    rule = SessionInsightRule(name)
    result = rule.detect_inputs(inputs(turns))
    assert len(result) == 1
    finding = result[0]
    assert finding.scope_id == "s1" and finding.baseline_query["scope"] == "session_lifetime"
    assert finding.source_sessions[0].session_id == "s1"
    assert rule.propose(finding) is None


def test_threshold_boundaries_and_missing_inputs_abstain():
    assert not SessionInsightRule("very_long").detect_inputs(inputs([turn(i) for i in range(20)]))
    assert not SessionInsightRule("tool_spiral").detect_inputs(inputs([turn(0, tool_calls=25)]))
    assert not SessionInsightRule("low_cache_hit").detect_inputs(inputs([turn(0, input_tokens=450_000, cache_read_tokens=50_000)]))
    assert not SessionInsightRule("premium_model").detect_inputs(inputs([turn(0, requested_mode=None)]))
    assert not SessionInsightRule("heavy_turns").detect_inputs(inputs([turn(0, complete_usage=False)]))
    assert not SessionInsightRule("idle_rebuild").detect_inputs(inputs([turn(0), turn(1, idle_before_ms=300_000)]))


def test_cache_rebuild_is_a_scenario_and_switch_takes_precedence():
    result = inputs([turn(0, model="sonnet"), turn(1, model="opus", idle_before_ms=600_000)])["s1"]
    assert result["switch_rebuild_cost_usd"] == pytest.approx(1.7)
    assert result["idle_rebuild_cost_usd"] is None
    assert result["warm_share_assumption"] == 0.85
    assert result["known_cost_usd"] == 4


def test_observed_warm_share_and_unknown_models():
    data = inputs([turn(0), turn(1, input_tokens=100_000, cache_read_tokens=400_000), turn(2, idle_before_ms=600_000)])["s1"]
    assert data["idle_rebuild_cost_usd"] == pytest.approx(1.6)
    assert data["warm_share_assumption"] is None
    unknown = inputs([turn(0), turn(1, model=None), turn(2, model="sonnet")])["s1"]
    assert unknown["model_switches"] == 0


def test_missing_prices_preserve_unknown_and_full_known_cost_is_denominator():
    data = inputs([turn(0), turn(1, idle_before_ms=600_000, input_cost_usd=None)])["s1"]
    assert data["idle_rebuild_cost_usd"] is None
    view = MeasurementView(sessions={"s1": {"known_cost_usd": 10}}, turns=[turn(0)])
    data = session_inputs(view)["s1"]
    assert data["premium_cost_share"] is None


def test_top_three_is_deterministic_and_preserves_other_persistable_findings():
    data = inputs([turn(i, input_tokens=5_000_000) for i in range(21)])
    ranked = top_session_insights(data)["s1"]
    assert len(ranked) == 3
    assert [r["rule_id"] for r in ranked] == ["session_very_long", "session_heavy_turns", "session_tool_spiral"]
    assert SessionInsightRule("low_cache_hit").detect_inputs(data)


def test_persisted_reader_is_scoped_ranked_and_excludes_resolved_findings(tmp_path):
    conn = connect_sqlite(tmp_path / "test.db")
    migrate(conn)
    now = "2026-11-01T12:00:00Z"
    conn.execute("INSERT INTO sessions(id,started_at,created_at,updated_at) VALUES('s1',?,?,?)", (now, now, now))
    repository = ImprovementRepository(conn)
    rules = session_insight_rules()
    repository.sync_rule_definitions((r.definition for r in rules), now=now)
    data = inputs([turn(i, input_tokens=5_000_000) for i in range(21)])
    for rule in rules:
        for draft in rule.detect_inputs(data):
            repository.upsert_observation(draft, now=now)
    assert len(read_session_insights(conn)["s1"]) == 3
    assert not read_session_insights(conn, session_ids=set())
    assert read_session_insights(conn, session_ids={"s1"})["s1"][0]["rule_id"] == "session_very_long"
    conn.execute("UPDATE observations SET status='resolved' WHERE rule_id='session_very_long'")
    assert read_session_insights(conn)["s1"][0]["rule_id"] == "session_heavy_turns"
    conn.close()
