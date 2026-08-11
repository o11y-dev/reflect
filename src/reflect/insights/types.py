"""Distribution types for session-quality analysis."""
from __future__ import annotations

import statistics
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Distribution statistics
# ---------------------------------------------------------------------------

def _percentile_from_sorted(sorted_values: list[float], p: float) -> float:
    """Compute the p-th percentile from a pre-sorted list."""
    from math import ceil
    if not sorted_values:
        return 0.0
    idx = ceil(len(sorted_values) * p / 100) - 1
    return sorted_values[max(0, min(idx, len(sorted_values) - 1))]


@dataclass(frozen=True)
class DistributionStats:
    """Summary statistics for a numeric distribution."""
    count: int
    mean: float
    median: float
    p25: float
    p75: float
    p90: float
    p95: float
    min_val: float
    max_val: float
    stdev: float

    def is_sparse(self, min_count: int = 5) -> bool:
        """Not enough data points for statistical reasoning."""
        return self.count < min_count

    def iqr(self) -> float:
        return self.p75 - self.p25

    def upper_fence(self, k: float = 1.5) -> float:
        """Tukey upper fence: p75 + k * IQR."""
        return self.p75 + k * self.iqr()

    def is_outlier_high(self, value: float, k: float = 1.5) -> bool:
        return value > self.upper_fence(k)


_EMPTY_DIST = DistributionStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def compute_distribution(values: list[float]) -> DistributionStats:
    """Build DistributionStats from a list of numeric values."""
    if not values:
        return _EMPTY_DIST
    s = sorted(values)
    n = len(s)
    return DistributionStats(
        count=n,
        mean=sum(s) / n,
        median=_percentile_from_sorted(s, 50),
        p25=_percentile_from_sorted(s, 25),
        p75=_percentile_from_sorted(s, 75),
        p90=_percentile_from_sorted(s, 90),
        p95=_percentile_from_sorted(s, 95),
        min_val=s[0],
        max_val=s[-1],
        stdev=statistics.stdev(s) if n >= 2 else 0.0,
    )


# ---------------------------------------------------------------------------
# Data profile — computed once per analysis run
# ---------------------------------------------------------------------------

@dataclass
class DataProfile:
    """Distributions used by the registered session-quality rules."""

    session_total_tokens: DistributionStats = _EMPTY_DIST
    session_tool_count: DistributionStats = _EMPTY_DIST
    session_failure_count: DistributionStats = _EMPTY_DIST
    session_duration_ms: DistributionStats = _EMPTY_DIST
    tokens_per_tool: DistributionStats = _EMPTY_DIST
