"""Build a DataProfile from TelemetryStats — computed once per analysis run."""
from __future__ import annotations

from reflect.models import TelemetryStats
from reflect.utils import _safe_ratio

from .economy import compute_token_economy
from .types import DataProfile, compute_distribution


def build_data_profile(stats: TelemetryStats) -> DataProfile:
    """Iterate sessions once, extract per-session metrics, build distributions."""
    total_tokens_list: list[float] = []
    input_tokens_list: list[float] = []
    output_tokens_list: list[float] = []
    tool_count_list: list[float] = []
    prompt_count_list: list[float] = []
    failure_count_list: list[float] = []
    duration_ms_list: list[float] = []
    tokens_per_tool_list: list[float] = []
    tools_per_prompt_list: list[float] = []
    reads_per_prompt_list: list[float] = []
    session_token_share_list: list[float] = []
    quality_score_list = [float(v) for v in stats.session_quality_scores.values()]

    grand_total_tokens = (
        stats.total_input_tokens
        + stats.total_output_tokens
        + stats.total_cache_creation_tokens
        + stats.total_cache_read_tokens
    )

    for sid in stats.sessions_seen:
        tok = stats.session_tokens.get(sid, {})
        inp = tok.get("input", 0)
        out = tok.get("output", 0)
        cc = tok.get("cache_creation", 0)
        cr = tok.get("cache_read", 0)
        total = inp + out + cc + cr
        spans = stats.session_span_details.get(sid, [])

        tool_count = sum(1 for s in spans if s.get("tool"))
        prompt_count = sum(1 for s in spans if s.get("event") == "UserPromptSubmit")
        failure_count = sum(1 for s in spans if not s.get("ok", True))
        read_count = sum(1 for s in spans if s.get("event") == "BeforeReadFile")

        timestamps = [s["t"] for s in spans if s.get("t")]
        duration = (max(timestamps) - min(timestamps)) / 1e6 if len(timestamps) >= 2 else 0.0

        total_tokens_list.append(float(total))
        input_tokens_list.append(float(inp))
        output_tokens_list.append(float(out))
        tool_count_list.append(float(tool_count))
        prompt_count_list.append(float(prompt_count))
        failure_count_list.append(float(failure_count))
        duration_ms_list.append(duration)

        if tool_count > 0:
            tokens_per_tool_list.append(total / tool_count)
        if prompt_count > 0:
            tools_per_prompt_list.append(tool_count / prompt_count)
            reads_per_prompt_list.append(read_count / prompt_count)
        if grand_total_tokens > 0:
            session_token_share_list.append(100.0 * total / grand_total_tokens)

    token_economy = compute_token_economy(stats)

    total_prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    total_tool_calls = stats.events_by_type.get("PreToolUse", 0)
    total_failures = stats.events_by_type.get("PostToolUseFailure", 0)

    total_model_events = sum(stats.models_by_count.values())
    heavy_model_events = sum(
        c for m, c in stats.models_by_count.items()
        if any(k in m.lower() for k in ("opus", "pro", "thinking"))
    )

    return DataProfile(
        session_total_tokens=compute_distribution(total_tokens_list),
        session_input_tokens=compute_distribution(input_tokens_list),
        session_output_tokens=compute_distribution(output_tokens_list),
        session_tool_count=compute_distribution(tool_count_list),
        session_prompt_count=compute_distribution(prompt_count_list),
        session_failure_count=compute_distribution(failure_count_list),
        session_duration_ms=compute_distribution(duration_ms_list),
        session_quality_scores=compute_distribution(quality_score_list),
        tokens_per_tool=compute_distribution(tokens_per_tool_list),
        tools_per_prompt=compute_distribution(tools_per_prompt_list),
        reads_per_prompt=compute_distribution(reads_per_prompt_list),
        session_token_share=compute_distribution(session_token_share_list),
        total_sessions=len(stats.sessions_seen),
        total_prompts=total_prompts,
        total_tool_calls=total_tool_calls,
        total_failures=total_failures,
        cache_reuse_ratio=_safe_ratio(stats.total_cache_read_tokens, stats.total_input_tokens),
        heavy_model_share=100.0 * _safe_ratio(heavy_model_events, total_model_events),
        token_economy=token_economy,
    )
