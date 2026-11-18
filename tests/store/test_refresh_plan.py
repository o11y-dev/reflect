import pytest

from reflect.store.refresh_plan import RefreshMode, plan_derived_refresh, session_batches


def test_refresh_plan_repairs_small_graph_and_rollup_gaps_incrementally():
    plan = plan_derived_refresh(
        changed_session_ids={"session-3"},
        all_session_ids={"session-1", "session-2", "session-3"},
        graph_session_ids={"session-1", "session-2"},
        rollup_session_ids={"session-1", "session-2"},
        graph_exists=True,
    )

    assert plan.cost_mode is RefreshMode.INCREMENTAL
    assert plan.graph_mode is RefreshMode.INCREMENTAL
    assert plan.rollup_mode is RefreshMode.INCREMENTAL
    assert plan.graph_session_ids == ("session-3",)
    assert plan.rollup_session_ids == ("session-3",)


def test_refresh_plan_rebuilds_absent_derived_state():
    plan = plan_derived_refresh(
        changed_session_ids={"session-1"},
        all_session_ids={"session-1"},
        graph_session_ids=set(),
        rollup_session_ids=set(),
        graph_exists=False,
    )

    assert plan.graph_mode is RefreshMode.FULL
    assert plan.rollup_mode is RefreshMode.FULL
    assert plan.graph_reason == "graph state is absent"
    assert plan.rollup_reason == "rollup state is absent"


def test_refresh_plan_rebuilds_only_the_stale_surface():
    sessions = {f"session-{index}" for index in range(401)}
    plan = plan_derived_refresh(
        changed_session_ids=set(),
        all_session_ids=sessions,
        graph_session_ids=sessions,
        rollup_session_ids=sessions | {"orphan"},
        graph_exists=True,
    )

    assert plan.cost_mode is RefreshMode.SKIP
    assert plan.graph_mode is RefreshMode.SKIP
    assert plan.rollup_mode is RefreshMode.FULL
    assert plan.rollup_reason == "1 orphan rollup row(s)"


def test_refresh_plan_skips_current_derived_state():
    plan = plan_derived_refresh(
        changed_session_ids=set(),
        all_session_ids={"session-1"},
        graph_session_ids={"session-1"},
        rollup_session_ids={"session-1"},
        graph_exists=True,
    )

    assert plan.cost_mode is RefreshMode.SKIP
    assert plan.graph_mode is RefreshMode.SKIP
    assert plan.rollup_mode is RefreshMode.SKIP


def test_refresh_plan_scopes_a_required_maintenance_rebuild_to_rollups():
    plan = plan_derived_refresh(
        changed_session_ids=set(),
        all_session_ids={"session-1"},
        graph_session_ids={"session-1"},
        rollup_session_ids={"session-1"},
        graph_exists=True,
        force_full_rollup_reason="migration maintenance is pending",
    )

    assert plan.cost_mode is RefreshMode.SKIP
    assert plan.graph_mode is RefreshMode.SKIP
    assert plan.rollup_mode is RefreshMode.FULL
    assert plan.graph_reason == "graph state is current"
    assert plan.rollup_reason == "migration maintenance is pending"


def test_refresh_plan_reprices_and_rebuilds_rollups_when_pricing_inputs_change():
    plan = plan_derived_refresh(
        changed_session_ids=set(),
        all_session_ids={"session-1"},
        graph_session_ids={"session-1"},
        rollup_session_ids={"session-1"},
        graph_exists=True,
        force_full_cost_reason="pricing rates or model aliases changed",
    )

    assert plan.cost_mode is RefreshMode.FULL
    assert plan.cost_reason == "pricing rates or model aliases changed"
    assert plan.graph_mode is RefreshMode.SKIP
    assert plan.rollup_mode is RefreshMode.FULL
    assert plan.rollup_reason == "pricing rates or model aliases changed"


def test_401_changed_sessions_remain_scoped_and_batched():
    changed = {f"changed-{index:03}" for index in range(401)}
    all_sessions = changed | {"unrelated-history"}
    plan = plan_derived_refresh(
        changed_session_ids=changed, all_session_ids=all_sessions,
        graph_session_ids=all_sessions, rollup_session_ids=all_sessions,
        graph_exists=True,
    )
    assert plan.cost_mode is plan.graph_mode is plan.rollup_mode is RefreshMode.INCREMENTAL
    assert set(plan.cost_session_ids) == set(plan.graph_session_ids) == set(plan.rollup_session_ids) == changed
    batches = list(session_batches(plan.graph_session_ids))
    assert [len(batch) for batch in batches] == [400, 1]
    assert sum(batches, ()) == tuple(sorted(changed))
    assert list(session_batches([])) == []
    with pytest.raises(ValueError, match="positive"):
        list(session_batches(changed, batch_size=0))


def test_missing_graph_is_bounded_without_limiting_accounting_or_late_changes():
    plan = plan_derived_refresh(
        changed_session_ids={"recent", "late-old"},
        all_session_ids={"recent", "late-old", "cold-history"},
        graph_session_ids=set(), rollup_session_ids={"cold-history"},
        graph_exists=False, graph_scope_session_ids={"recent"},
        graph_changed_session_ids={"recent", "late-old"},
    )
    assert plan.graph_mode is RefreshMode.INCREMENTAL
    assert plan.graph_session_ids == ("late-old", "recent")
    assert plan.cost_session_ids == plan.rollup_session_ids == ("late-old", "recent")


def test_empty_graph_window_does_not_trigger_absent_history_rebuild():
    plan = plan_derived_refresh(
        changed_session_ids={"imported-old"}, all_session_ids={"imported-old"},
        graph_session_ids=set(), rollup_session_ids=set(), graph_exists=False,
        graph_scope_session_ids=set(), graph_changed_session_ids=set(),
    )
    assert plan.graph_mode is RefreshMode.SKIP
    assert plan.graph_session_ids == ()
    assert plan.cost_session_ids == plan.rollup_session_ids == ("imported-old",)
