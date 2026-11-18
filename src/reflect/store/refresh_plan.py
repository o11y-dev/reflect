from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from enum import StrEnum

DEFAULT_SESSION_BATCH_SIZE = 400


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
    cost_reason: str
    graph_reason: str
    rollup_reason: str

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["cost_mode"] = self.cost_mode.value
        payload["graph_mode"] = self.graph_mode.value
        payload["rollup_mode"] = self.rollup_mode.value
        return payload


def session_batches(
    session_ids: Iterable[str], *, batch_size: int = DEFAULT_SESSION_BATCH_SIZE,
) -> Iterator[tuple[str, ...]]:
    """Bound individual writes without expanding their scope to all history."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    ordered = sorted(set(session_ids))
    for offset in range(0, len(ordered), batch_size):
        yield tuple(ordered[offset:offset + batch_size])


def _mode_for_sessions(session_ids: set[str]) -> RefreshMode:
    return RefreshMode.INCREMENTAL if session_ids else RefreshMode.SKIP


def plan_derived_refresh(
    *,
    changed_session_ids: set[str],
    all_session_ids: set[str],
    graph_session_ids: set[str],
    rollup_session_ids: set[str],
    graph_exists: bool,
    graph_scope_session_ids: set[str] | None = None,
    graph_changed_session_ids: set[str] | None = None,
    force_full_cost_reason: str = "",
    force_full_rollup_reason: str = "",
) -> DerivedRefreshPlan:
    """Choose bounded graph and rollup repairs without coupling their lifecycles."""
    graph_scope = all_session_ids if graph_scope_session_ids is None else graph_scope_session_ids
    missing_graph = graph_scope - graph_session_ids
    missing_rollups = all_session_ids - rollup_session_ids
    orphan_rollups = rollup_session_ids - all_session_ids
    graph_changed = changed_session_ids if graph_changed_session_ids is None else graph_changed_session_ids
    graph_targets = graph_changed | missing_graph
    rollup_targets = changed_session_ids | missing_rollups

    if force_full_cost_reason and all_session_ids:
        cost_mode = RefreshMode.FULL
        cost_reason = force_full_cost_reason
    else:
        cost_mode = _mode_for_sessions(changed_session_ids)
        cost_reason = (
            f"{len(changed_session_ids)} changed session(s)"
            if changed_session_ids
            else "pricing state is current"
        )
    if all_session_ids and not graph_exists and graph_scope_session_ids is None:
        graph_mode = RefreshMode.FULL
        graph_reason = "graph state is absent"
    else:
        graph_mode = _mode_for_sessions(graph_targets)
        graph_reason = (
            f"{len(graph_targets)} changed or missing session(s)"
            if graph_targets
            else "graph state is current"
        )

    if force_full_cost_reason and all_session_ids:
        rollup_mode = RefreshMode.FULL
        rollup_reason = force_full_cost_reason
    elif force_full_rollup_reason:
        rollup_mode = RefreshMode.FULL
        rollup_reason = force_full_rollup_reason
    elif all_session_ids and not rollup_session_ids:
        rollup_mode = RefreshMode.FULL
        rollup_reason = "rollup state is absent"
    elif orphan_rollups:
        rollup_mode = RefreshMode.FULL
        rollup_reason = f"{len(orphan_rollups)} orphan rollup row(s)"
    else:
        rollup_mode = _mode_for_sessions(rollup_targets)
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
        cost_reason=cost_reason,
        graph_reason=graph_reason,
        rollup_reason=rollup_reason,
    )
