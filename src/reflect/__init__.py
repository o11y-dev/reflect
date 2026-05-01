"""AI usage telemetry report package."""

from __future__ import annotations

from reflect.models import AgentStats, TelemetryStats


def analyze_telemetry(*args, **kwargs):
    from reflect.processing import analyze_telemetry as _analyze_telemetry

    return _analyze_telemetry(*args, **kwargs)


def render_report(*args, **kwargs):
    from reflect.report import render_report as _render_report

    return _render_report(*args, **kwargs)


__all__ = ["AgentStats", "TelemetryStats", "analyze_telemetry", "render_report"]
