"""Signal registry and runner."""
from __future__ import annotations

from typing import Callable

from reflect.models import TelemetryStats

from ..types import DataProfile, Insight

SignalFn = Callable[[TelemetryStats, DataProfile], Insight | None]


def run_signals(
    stats: TelemetryStats,
    profile: DataProfile,
    kind: str,
) -> list[Insight]:
    """Run all registered signals for *kind*, return sorted by priority."""
    from . import examples as _examples
    from . import observations as _obs
    from . import recommendations as _recs
    from . import strengths as _str

    registry: dict[str, list[SignalFn]] = {
        "strength": _str.SIGNALS,
        "observation": _obs.SIGNALS,
        "recommendation": _recs.SIGNALS,
        "example": _examples.SIGNALS,
    }
    results: list[Insight] = []
    for fn in registry.get(kind, []):
        insight = fn(stats, profile)
        if insight is not None:
            results.append(insight)
    results.sort(key=lambda i: i.priority, reverse=True)
    return results
