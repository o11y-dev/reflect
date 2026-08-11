"""Tests for the weekly activity trend used by SQL dashboard views."""

from collections import Counter

from reflect.graph import _compute_weekly_trends


def test_weekly_trends_group_events_and_compute_delta() -> None:
    activity = Counter(
        {
            "2026-03-23": 10,
            "2026-03-24": 15,
            "2026-03-30": 20,
        }
    )

    result = _compute_weekly_trends(activity)

    assert [row["events"] for row in result] == [25, 20]
    assert result[0]["delta_pct"] is None
    assert result[1]["delta"] == -5


def test_weekly_trends_skip_invalid_dates() -> None:
    result = _compute_weekly_trends(Counter({"not-a-date": 5, "2026-03-24": 10}))

    assert sum(row["events"] for row in result) == 10
    assert _compute_weekly_trends(Counter()) == []
