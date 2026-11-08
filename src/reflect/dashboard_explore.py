from __future__ import annotations

import shlex
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reflect.dashboard_queries import build_dashboard_payload
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
    if canonical_view == "usage":
        dashboard = build_dashboard_payload(
            db_path, q=q, session_id=session_id, agents=agents, model=model,
            status=status, range_name=range_name, lazy_heavy_tabs=True,
            include_comparison=False, base_tab_names=set(tab_names),
        )
        payload.update({key: dashboard[key] for key in (
            "snapshot_completed_at", "analysis_state", "analysis_completed_at", "analysis_window",
            "analysis_coverage", "inventory_scope", "inventory_scope_label", "range",
        )})
        for tab_name in tab_names:
            payload.update(dashboard["sqlite"]["tabs"]["tools" if tab_name == "usage_tools" else tab_name])
        return payload

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
        if has_scope:
            now = datetime.now(UTC)
            sessions = select_session_scope(
                conn, q=q, agents=agents, model=model, status=status,
                date_from=(now - timedelta(days=days)).isoformat() if days else None,
                date_to=now.isoformat() if days else None,
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
