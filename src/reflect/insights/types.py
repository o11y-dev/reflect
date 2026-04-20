"""Core types for the insights engine."""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Severity(IntEnum):
    """Ordinal severity. Higher = more important."""
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass(frozen=True)
class Insight:
    """A single structured insight produced by a signal function."""
    kind: str                  # "strength" | "observation" | "recommendation" | "example"
    title: str                 # Short heading
    body: str                  # Explanation / detail text
    category: str              # e.g. "efficiency", "cost", "reliability"
    severity: Severity
    confidence: float          # 0.0-1.0
    evidence: dict[str, Any] = field(default_factory=dict)
    # For examples only:
    before: str = ""
    after: str = ""

    @property
    def priority(self) -> float:
        """Sort key: higher = show first."""
        return float(self.severity) * self.confidence


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

    def lower_fence(self, k: float = 1.5) -> float:
        """Tukey lower fence: p25 - k * IQR."""
        return self.p25 - k * self.iqr()

    def is_outlier_high(self, value: float, k: float = 1.5) -> bool:
        return value > self.upper_fence(k)

    def is_outlier_low(self, value: float, k: float = 1.5) -> bool:
        return value < self.lower_fence(k)

    def z_score(self, value: float) -> float:
        if self.stdev <= 0:
            return 0.0
        return (value - self.mean) / self.stdev


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
    """Cached statistical summary of the user's own data distribution."""
    # Per-session distributions
    session_total_tokens: DistributionStats = _EMPTY_DIST
    session_input_tokens: DistributionStats = _EMPTY_DIST
    session_output_tokens: DistributionStats = _EMPTY_DIST
    session_tool_count: DistributionStats = _EMPTY_DIST
    session_prompt_count: DistributionStats = _EMPTY_DIST
    session_failure_count: DistributionStats = _EMPTY_DIST
    session_duration_ms: DistributionStats = _EMPTY_DIST
    session_quality_scores: DistributionStats = _EMPTY_DIST

    # Cross-session ratio distributions
    tokens_per_tool: DistributionStats = _EMPTY_DIST
    tools_per_prompt: DistributionStats = _EMPTY_DIST
    reads_per_prompt: DistributionStats = _EMPTY_DIST
    session_token_share: DistributionStats = _EMPTY_DIST

    # Aggregate counts
    total_sessions: int = 0
    total_prompts: int = 0
    total_tool_calls: int = 0
    total_failures: int = 0
    cache_reuse_ratio: float = 0.0
    heavy_model_share: float = 0.0

    # Token economy (computed once, reused everywhere)
    token_economy: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Confidence helper
# ---------------------------------------------------------------------------

def confidence_for(dist: DistributionStats, base: float = 0.8) -> float:
    """Scale confidence by data sufficiency."""
    if dist.count >= 10:
        return base
    elif dist.count >= 5:
        return base * 0.8
    else:
        return 0.5
