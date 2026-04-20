"""Convert Insight objects to legacy output formats for backward compatibility."""
from __future__ import annotations

from .types import Insight


def insights_to_strings(insights: list[Insight]) -> list[str]:
    """Convert Insight objects to the existing markdown-bold string format."""
    return [f"**{i.title}** — {i.body}" for i in insights]


def insights_to_example_tuples(insights: list[Insight]) -> list[tuple[str, str, str]]:
    """Convert example Insights to (title, before, after) tuples."""
    return [(i.title, i.before, i.after) for i in insights if i.kind == "example"]
