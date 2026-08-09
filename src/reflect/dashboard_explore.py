from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from reflect.dashboard_queries import build_dashboard_payload


def _sql_dashboard_tab_payload(
    db_path: Path,
    tab_name: str,
    *,
    session_id: str = "",
) -> dict[str, object]:
    from reflect.store.sqlite import connect_sqlite_read_only
    from reflect.views.report_tabs import build_report_tab

    scoped_ids = {session_id} if session_id else None
    conn = connect_sqlite_read_only(db_path)
    try:
        payload = build_report_tab(conn, tab_name, session_ids=scoped_ids)
    finally:
        conn.close()
    return {
        "sql_backed": True,
        "tab": tab_name.strip().lower().replace("-", "_"),
        "scoped": scoped_ids is not None,
        "session_id": session_id,
        **payload,
    }


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
    filtered_tabs = None
    if has_scope:
        filtered_tabs = build_dashboard_payload(
            db_path,
            q=q,
            session_id=session_id,
            agents=agents,
            model=model,
            status=status,
            range_name=range_name,
            lazy_heavy_tabs=True,
            include_comparison=False,
            base_tab_names=set(tab_names),
        )["sqlite"]["tabs"]
    for tab_name in tab_names:
        if filtered_tabs is not None:
            content = filtered_tabs["tools" if tab_name == "usage_tools" else tab_name]
        else:
            tab_payload = _sql_dashboard_tab_payload(db_path, tab_name, session_id=session_id)
            content = {
                key: value
                for key, value in tab_payload.items()
                if key not in {"sql_backed", "tab", "scoped", "session_id"}
            }
        if canonical_view in {"usage", "tools", "graph"}:
            payload.update(content)
        else:
            payload[tab_name] = content
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
