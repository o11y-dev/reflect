from reflect.store.refresh_plan import RefreshMode, plan_derived_refresh


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
