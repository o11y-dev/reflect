"""Achievement badge builder."""
from __future__ import annotations

from reflect.models import TelemetryStats
from reflect.utils import _safe_ratio

from .economy import compute_token_economy
from .types import DataProfile


def build_achievement_badges(
    stats: TelemetryStats,
    profile: DataProfile | None = None,
) -> list[dict[str, str]]:
    """Build achievement badges. Uses profile for adaptive thresholds when available."""
    if profile is not None:
        economy = profile.token_economy
    else:
        economy = compute_token_economy(stats)

    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    pre_tool = stats.events_by_type.get("PreToolUse", 0)
    failures = stats.events_by_type.get("PostToolUseFailure", 0)
    completion_rate = 100 * _safe_ratio(
        sum(1 for done in stats.session_goal_completed.values() if done),
        len(stats.sessions_seen),
    )

    ratio = _safe_ratio(pre_tool, prompts)
    # Use distribution for "High Leverage" when available
    if profile and not profile.tools_per_prompt.is_sparse():
        leverage_threshold = profile.tools_per_prompt.p95
    else:
        leverage_threshold = 10.0

    badges: list[dict[str, str]] = []

    if stats.days_active >= 5:
        badges.append({"icon": "&#128293;", "name": "On a Roll", "sub": f"{stats.days_active} active days"})
    if len(stats.sessions_seen) >= 10:
        badges.append({"icon": "&#9889;", "name": "Power User", "sub": f"{len(stats.sessions_seen)} sessions"})
    if ratio >= leverage_threshold:
        badges.append({"icon": "&#128640;", "name": "High Leverage", "sub": f"{ratio:.1f}:1 tool ratio"})
    if stats.events_by_type.get("SubagentStart", 0) >= 10:
        badges.append({"icon": "&#129302;", "name": "Delegator", "sub": f"{stats.events_by_type.get('SubagentStart', 0)} subagents"})
    if stats.events_by_type.get("BeforeMCPExecution", 0) >= 100:
        badges.append({"icon": "&#128268;", "name": "MCP Heavy", "sub": f"{stats.events_by_type.get('BeforeMCPExecution', 0):,} MCP calls"})
    if failures == 0 and pre_tool > 10:
        badges.append({"icon": "&#9989;", "name": "Zero Failures", "sub": "clean tool execution"})
    if stats.events_by_type.get("BeforeShellExecution", 0) >= 50:
        badges.append({"icon": "&#128187;", "name": "Shell Ninja", "sub": f"{stats.events_by_type.get('BeforeShellExecution', 0):,} shell runs"})
    if len(stats.subagent_types) >= 5:
        badges.append({"icon": "&#128450;", "name": "Multi-Agent", "sub": f"{len(stats.subagent_types)} agent types"})
    cache_ratio = economy.get("cache_reuse_ratio", 0)
    if cache_ratio >= 0.15:
        if cache_ratio > 1.0:
            badges.append({"icon": "&#129534;", "name": "Cache Saver", "sub": f"{cache_ratio:.1f}x cached reuse"})
        else:
            badges.append({"icon": "&#129534;", "name": "Cache Saver", "sub": f"{economy.get('cache_hit_pct', 0):.0f}% hit rate"})
    if economy.get("top_session_share", 100) <= 15 and len(stats.sessions_seen) >= 5:
        badges.append({"icon": "&#127919;", "name": "Context Tamer", "sub": "token spend well distributed"})
    heavy = economy.get("heavy_model_share", 0)
    if 5 <= heavy <= 40 and stats.total_events >= 100:
        badges.append({"icon": "&#9878;", "name": "Model Mixer", "sub": "premium usage looks selective"})
    if completion_rate >= 70 and len(stats.sessions_seen) >= 3:
        badges.append({"icon": "&#127942;", "name": "Closer", "sub": f"{completion_rate:.0f}% completion"})
    if sum(stats.session_recovered_failures.values()) >= 3:
        badges.append({"icon": "&#128295;", "name": "Recovery Loop", "sub": f"{sum(stats.session_recovered_failures.values())} recovered failures"})

    if not badges:
        badges.append({"icon": "&#127793;", "name": "Getting Started", "sub": "keep going"})

    return badges
