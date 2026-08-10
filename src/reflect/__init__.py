"""AI usage telemetry report package."""

from __future__ import annotations

from reflect.models import AgentStats, TelemetryStats


def analyze_telemetry(*args, **kwargs):
    from reflect.processing import analyze_telemetry as _analyze_telemetry

    return _analyze_telemetry(*args, **kwargs)


__all__ = ["AgentStats", "TelemetryStats", "analyze_telemetry"]
