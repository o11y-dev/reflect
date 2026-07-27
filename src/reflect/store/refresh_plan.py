from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum

DEFAULT_INCREMENTAL_SESSION_LIMIT = 400


class RefreshMode(StrEnum):
    SKIP = "skip"
    INCREMENTAL = "incremental"
    FULL = "full"


@dataclass(frozen=True)
class DerivedRefreshPlan:
    cost_mode: RefreshMode
    graph_mode: RefreshMode
    rollup_mode: RefreshMode
    cost_session_ids: tuple[str, ...]
    graph_session_ids: tuple[str, ...]
    rollup_session_ids: tuple[str, ...]
    graph_reason: str
    rollup_reason: str

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["cost_mode"] = self.cost_mode.value
        payload["graph_mode"] = self.graph_mode.value
        payload["rollup_mode"] = self.rollup_mode.value
        return payload


def _mode_for_sessions(session_ids: set[str], *, limit: int) -> RefreshMode:
    if not session_ids:
        return RefreshMode.SKIP
    if len(session_ids) <= limit:
        return RefreshMode.INCREMENTAL
    return RefreshMode.FULL


def plan_derived_refresh(
    *,
    changed_session_ids: set[str],
    all_session_ids: set[str],
    graph_session_ids: set[str],
    rollup_session_ids: set[str],
    graph_exists: bool,
    incremental_limit: int = DEFAULT_INCREMENTAL_SESSION_LIMIT,
    force_full_rollup_reason: str = "",
) -> DerivedRefreshPlan:
    """Choose bounded graph and rollup repairs without coupling their lifecycles."""
    missing_graph = all_session_ids - graph_session_ids
    missing_rollups = all_session_ids - rollup_session_ids
    orphan_rollups = rollup_session_ids - all_session_ids
    graph_targets = changed_session_ids | missing_graph
    rollup_targets = changed_session_ids | missing_rollups

    cost_mode = _mode_for_sessions(changed_session_ids, limit=incremental_limit)
    if all_session_ids and not graph_exists:
        graph_mode = RefreshMode.FULL
        graph_reason = "graph state is absent"
    else:
        graph_mode = _mode_for_sessions(graph_targets, limit=incremental_limit)
        graph_reason = (
            f"{len(graph_targets)} changed or missing session(s)"
            if graph_targets
            else "graph state is current"
        )

    if force_full_rollup_reason:
        rollup_mode = RefreshMode.FULL
        rollup_reason = force_full_rollup_reason
    elif all_session_ids and not rollup_session_ids:
        rollup_mode = RefreshMode.FULL
        rollup_reason = "rollup state is absent"
    elif orphan_rollups:
        rollup_mode = RefreshMode.FULL
        rollup_reason = f"{len(orphan_rollups)} orphan rollup row(s)"
    else:
        rollup_mode = _mode_for_sessions(rollup_targets, limit=incremental_limit)
        rollup_reason = (
            f"{len(rollup_targets)} changed or missing session(s)"
            if rollup_targets
            else "rollup state is current"
        )

    return DerivedRefreshPlan(
        cost_mode=cost_mode,
        graph_mode=graph_mode,
        rollup_mode=rollup_mode,
        cost_session_ids=tuple(sorted(changed_session_ids)),
        graph_session_ids=tuple(sorted(graph_targets)),
        rollup_session_ids=tuple(sorted(rollup_targets)),
        graph_reason=graph_reason,
        rollup_reason=rollup_reason,
    )
