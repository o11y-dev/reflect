from __future__ import annotations

import shlex
from collections import Counter
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reflect.store.pending_refresh import analysis_coverage, derived_analysis_state
from reflect.store.sqlite import connect_sqlite_read_only, snapshot_incomplete
from reflect.utils import _json_loads

EXPLORE_VIEW_TABS: dict[str, tuple[str, ...]] = {
    "usage": ("activity", "models", "costs", "usage_tools", "mcp"),
    "tools": ("tools", "mcp"),
    "graph": ("graph",),
    "context": ("specs", "memory", "privacy", "exports"),
}


def canonical_explore_view(view_name: str) -> str:
    return view_name.strip().lower().replace("-", "_")


def build_explore_payload(
    db_path: Path,
    view_name: str,
    *,
    session_id: str = "",
    q: str = "",
    agents: set[str] | None = None,
    model: str = "all",
    status: str = "all",
    range_name: str = "all",
) -> dict[str, object]:
    canonical_view = canonical_explore_view(view_name)
    tab_names = EXPLORE_VIEW_TABS.get(canonical_view)
    if tab_names is None:
        raise ValueError(f"Unknown Explore view: {view_name}")

    if canonical_view == "context":
        session_id, q, agents, model, status, range_name = "", "", set(), "all", "all", "all"
    has_scope = bool(
        q or session_id or agents or model != "all" or status != "all" or range_name != "all"
    )

    payload: dict[str, object] = {
        "sql_backed": True,
        "view": canonical_view,
        "scoped": has_scope,
        "session_id": session_id,
    }
    from reflect.views import build_report_tab
    from reflect.views.sessions import select_session_scope

    # Tool observations need published canonical usage, not workflow analysis.
    # Context inventories remain inspectable during normalization. All view
    # inputs and readiness metadata share this one read-only transaction.
    profile = None if canonical_view == "context" else "usage"
    with closing(connect_sqlite_read_only(db_path, profile=profile)) as conn:
        scoped_ids = None
        days = 1 if range_name == "24h" else (
            int(range_name[:-1]) if range_name.endswith("d") and range_name[:-1].isdigit() else None
        )
        now = datetime.now(UTC)
        date_from = (now - timedelta(days=days)).isoformat() if days else None
        date_to = now.isoformat() if days else None
        if has_scope or canonical_view == "usage":
            sessions = select_session_scope(
                conn, q=q, agents=agents, model=model, status=status, ids_only=True,
                date_from=date_from, date_to=date_to,
            )
            scoped_ids = {row["id"] for row in sessions if not session_id or row["id"] == session_id}
        coverage = analysis_coverage(conn, scoped_ids)
        relevant_coverage = {"graph": coverage["graph"]} if canonical_view == "graph" else coverage
        state = "ready" if canonical_view == "tools" else derived_analysis_state(
            relevant_coverage, snapshot_ready=not snapshot_incomplete(conn),
        )
        metadata = dict(conn.execute(
            "SELECT key,value FROM store_metadata WHERE key IN "
            "('last_successful_usage_refresh','last_successful_refresh','analysis_window_v1')"
        ))
        try:
            window = _json_loads(metadata["analysis_window_v1"]) if metadata.get("analysis_window_v1") else None
        except (TypeError, ValueError):
            window = None
        payload.update(
            analysis_state="ready" if state == "ready" else "pending",
            analysis_coverage=coverage, analysis_window=window,
            analysis_completed_at=metadata.get("last_successful_refresh"),
            snapshot_completed_at=(metadata.get("last_successful_usage_refresh") or metadata.get("last_successful_refresh"))
            if canonical_view == "tools" else metadata.get("last_successful_refresh") if state == "ready" else None,
            inventory_scope="global_stored_inventory" if canonical_view == "context" else "matching_session_lifetimes",
            inventory_scope_label="Stored context and system inventory across all sessions."
            if canonical_view == "context" else "Tool and graph inventories cover matching sessions in full.",
            range=range_name,
        )
        if canonical_view == "usage":
            from reflect.graph import _compute_weekly_trends
            from reflect.views.measurements import build_measurements
            from reflect.views.report_tabs import period_tool_activity

            measurement = build_measurements(
                conn, session_ids=scoped_ids, date_from=date_from, date_to=date_to,
                usage_only=True,
            )
            for tab_name in tab_names:
                content = build_report_tab(conn, tab_name, session_ids=scoped_ids, measurement=measurement)
                if tab_name == "usage_tools":
                    content = {key: value for key, value in content.items()
                               if key not in {"agents", "agent_comparison", "commands_with_template"}}
                payload.update(content)
            payload.update(
                tools_by_count={}, tool_percentiles=[], skills_by_count={},
                skill_reports_by_count={}, skill_mentions_by_count={}, skill_attempts_by_count={},
                weekly_trends=_compute_weekly_trends(Counter(payload["activity_by_day"])),
                agent_cost_over_time=measurement.agent_cost_over_time,
                analysis_state=state,
                snapshot_completed_at=max((value for key, value in metadata.items()
                    if key in {"last_successful_usage_refresh", "last_successful_refresh"}), default=None),
                inventory_scope_label=(
                    "Costs, tokens, and KPI totals use the selected period. Activity charts and tool inventories cover matching sessions in full."
                    if days else ""
                ),
            )
            if date_from and date_to:
                mcp_calls, subagent_launches = period_tool_activity(
                    conn, session_ids=scoped_ids, date_from=date_from, date_to=date_to,
                )
                payload.update(mcp_calls=mcp_calls, subagent_launches=subagent_launches,
                               subagent_total_starts=subagent_launches)
            return payload
        for tab_name in tab_names:
            content = build_report_tab(conn, tab_name, session_ids=scoped_ids)
            if canonical_view == "context":
                payload[tab_name] = content
            else:
                payload.update(content)
        if state != "ready":
            payload.update(evidence_state="recorded", publication={
                "state": "recorded", "reason": state,
                "loaded_at": datetime.now(UTC).isoformat(), "snapshot_completed_at": None,
            })
            if state == "historical_pending":
                command = f"reflect refresh --session {shlex.quote(session_id)}" if session_id else "reflect refresh --all-history"
                payload["analysis_pending_reason"] = f"Stored evidence is available; historical analysis requires `{command}`."
    return payload


def parse_dashboard_scope(query: Mapping[str, str]) -> tuple[str, str, set[str], str, str, str]:
    agents = {agent for agent in (query.get("agents") or "").split(",") if agent}
    return (
        (query.get("q") or "").strip(),
        (query.get("session") or "").strip(),
        agents,
        query.get("model") or "all",
        query.get("status") or "all",
        query.get("range") or "all",
    )
