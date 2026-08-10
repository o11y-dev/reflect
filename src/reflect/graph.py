"""Small graph-shaped derivations shared by SQL dashboard views."""

from __future__ import annotations

import datetime as dt
from collections import Counter


def _compute_weekly_trends(activity_by_day: Counter) -> list[dict]:
    """Group daily event counts by ISO week and compute week-over-week change."""
    weeks: dict[str, dict] = {}
    for day, count in activity_by_day.items():
        try:
            date = dt.date.fromisoformat(day)
        except ValueError:
            continue
        year, week, _ = date.isocalendar()
        key = f"{year}-W{week:02d}"
        row = weeks.setdefault(key, {"week": key, "events": 0, "days_active": 0})
        row["events"] += count
        row["days_active"] += count > 0

    result = sorted(weeks.values(), key=lambda row: row["week"])
    for index, row in enumerate(result):
        previous = result[index - 1]["events"] if index else 0
        row["delta"] = row["events"] - previous
        row["delta_pct"] = (
            round(100 * row["delta"] / previous, 1)
            if previous
            else None
        )
    return result
