"""Token economy computation — moved unchanged from the legacy insights module."""
from __future__ import annotations

from reflect.models import TelemetryStats
from reflect.utils import _safe_ratio


def compute_token_economy(stats: TelemetryStats) -> dict:
    """Derive token-economy signals from local telemetry."""
    prompts = stats.events_by_type.get("UserPromptSubmit", 0)
    file_reads = stats.events_by_type.get("BeforeReadFile", 0)
    mcp_calls = stats.events_by_type.get("BeforeMCPExecution", 0)

    total_tokens = (
        stats.total_input_tokens
        + stats.total_output_tokens
        + stats.total_cache_creation_tokens
        + stats.total_cache_read_tokens
    )
    avg_input_per_prompt = _safe_ratio(stats.total_input_tokens, prompts)
    avg_output_per_prompt = _safe_ratio(stats.total_output_tokens, prompts)
    reads_per_prompt = _safe_ratio(file_reads, prompts)
    mcp_per_prompt = _safe_ratio(mcp_calls, prompts)
    cache_reuse_ratio = _safe_ratio(stats.total_cache_read_tokens, stats.total_input_tokens)
    cache_hit_pct = 100 * min(cache_reuse_ratio, 1.0)

    session_rows: list[dict] = []
    for sid in stats.sessions_seen:
        tok = stats.session_tokens.get(sid, {})
        total_session_tokens = (
            tok.get("input", 0)
            + tok.get("output", 0)
            + tok.get("cache_creation", 0)
            + tok.get("cache_read", 0)
        )
        prompt_count = sum(
            1 for span in stats.session_span_details.get(sid, [])
            if span.get("event") == "UserPromptSubmit"
        )
        if prompt_count == 0:
            prompt_count = stats.session_events.get(sid, 0) // 20
        session_rows.append({
            "sid": sid,
            "tokens": total_session_tokens,
            "prompts": prompt_count,
            "events": stats.session_events.get(sid, 0),
        })
    session_rows.sort(key=lambda row: row["tokens"], reverse=True)
    top_session_tokens = session_rows[0]["tokens"] if session_rows else 0
    top_session_share = 100 * _safe_ratio(top_session_tokens, total_tokens)
    high_context_sessions = sum(
        1 for row in session_rows
        if row["tokens"] >= 500_000 or row["prompts"] >= 25 or row["events"] >= 1500
    )

    total_model_events = sum(stats.models_by_count.values())
    heavy_model_events = 0
    for model, count in stats.models_by_count.items():
        m = model.lower()
        if "opus" in m or "pro" in m or "thinking" in m:
            heavy_model_events += count
    heavy_model_share = 100 * _safe_ratio(heavy_model_events, total_model_events)

    return {
        "total_tokens": total_tokens,
        "avg_input_per_prompt": avg_input_per_prompt,
        "avg_output_per_prompt": avg_output_per_prompt,
        "reads_per_prompt": reads_per_prompt,
        "mcp_per_prompt": mcp_per_prompt,
        "cache_hit_pct": cache_hit_pct,
        "cache_reuse_ratio": cache_reuse_ratio,
        "top_session_tokens": top_session_tokens,
        "top_session_share": top_session_share,
        "high_context_sessions": high_context_sessions,
        "heavy_model_share": heavy_model_share,
    }
