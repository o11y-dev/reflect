from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class AgentStats:
    """Per-agent (IDE) metrics for side-by-side comparison."""
    name: str
    total_events: int = 0
    events_by_type: Counter[str] = field(default_factory=Counter)
    models_by_count: Counter[str] = field(default_factory=Counter)
    tools_by_count: Counter[str] = field(default_factory=Counter)
    tool_durations_ms: dict[str, list[float]] = field(default_factory=dict)
    sessions_seen: set[str] = field(default_factory=set)
    mcp_servers: Counter[str] = field(default_factory=Counter)
    subagent_types: Counter[str] = field(default_factory=Counter)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cache_read_tokens: int = 0
    # Evaluation metrics
    total_quality_score: float = 0.0
    completed_sessions: int = 0
    recovered_failures: int = 0


@dataclass
class TelemetryStats:
    session_files: int
    span_files: int
    total_events: int
    events_by_type: Counter[str]
    events_by_file: dict[str, int]

    # Enhanced fields
    models_by_count: Counter[str] = field(default_factory=Counter)
    tools_by_count: Counter[str] = field(default_factory=Counter)
    subagent_types: Counter[str] = field(default_factory=Counter)
    mcp_servers: Counter[str] = field(default_factory=Counter)
    sessions_seen: set[str] = field(default_factory=set)
    session_events: dict[str, int] = field(default_factory=dict)
    session_models: dict[str, Counter] = field(default_factory=dict)
    session_first_ts: dict[str, int] = field(default_factory=dict)
    tool_durations_ms: dict[str, list[float]] = field(default_factory=dict)
    activity_by_day: Counter[str] = field(default_factory=Counter)
    activity_by_hour: Counter[int] = field(default_factory=Counter)
    model_by_day: dict[str, Counter] = field(default_factory=dict)  # {date: Counter[model]}
    shell_commands: Counter[str] = field(default_factory=Counter)
    session_shell_commands: dict[str, Counter] = field(default_factory=dict)
    agents: dict[str, AgentStats] = field(default_factory=dict)
    # Graph analysis data
    session_tool_seq: dict[str, list] = field(default_factory=dict)  # {sid: [(ts_ns, tool, ok)]}
    session_span_details: dict[str, list] = field(default_factory=dict)  # {sid: [{t,tool,dur,ok}]}
    first_event_ts: str = ""
    last_event_ts: str = ""
    days_active: int = 0
    # Token usage
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cache_read_tokens: int = 0
    # Per-session token usage / provenance:
    # {session_id: {"input": int, "output": int, "source": str, "note": str}}
    session_tokens: dict = field(default_factory=dict)
    # MCP server availability: before/after counts per server
    mcp_server_before: Counter[str] = field(default_factory=Counter)
    mcp_server_after: Counter[str] = field(default_factory=Counter)
    # Subagent completion tracking: stops by type (when attribute available)
    subagent_stops_by_type: Counter[str] = field(default_factory=Counter)
    # Evaluation metrics
    session_quality_scores: dict[str, float] = field(default_factory=dict)
    session_goal_completed: dict[str, bool] = field(default_factory=dict)
    session_recovered_failures: dict[str, int] = field(default_factory=dict)
    session_tags: dict[str, set[str]] = field(default_factory=dict)
    # Per-session conversation events for the session browser
    session_conversation: dict[str, list[dict]] = field(default_factory=dict)
    # Map session_id → (agent_name, source_file_path) for lazy detail loading
    session_source: dict[str, tuple[str, str]] = field(default_factory=dict)
    sessions_with_telemetry: set[str] = field(default_factory=set)
