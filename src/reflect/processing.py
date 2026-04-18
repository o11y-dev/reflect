from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from reflect.models import AgentStats, TelemetryStats

if TYPE_CHECKING:
    pass


# These are duplicated here to avoid importing from core.py (which imports from us).
# They stay in sync with the core.py definitions.
def _default_sessions_dir() -> Path:
    from reflect.parsing import HOOK_HOME, REFLECT_HOME
    p = REFLECT_HOME / "state" / "sessions"
    if p.is_dir():
        return p
    return HOOK_HOME / ".state" / "sessions"


def _default_spans_dir() -> Path:
    from reflect.parsing import HOOK_HOME, REFLECT_HOME
    p = REFLECT_HOME / "state" / "local_spans"
    if p.is_dir():
        return p
    return HOOK_HOME / ".state" / "local_spans"


def _process_span(
    span: dict,
    events_by_type: Counter,
    models: Counter,
    tools: Counter,
    mcp_servers: Counter,
    subagent_types: Counter,
    sessions_seen: set,
    timestamps_ns: list,
    tool_durations_ms: dict,
    activity_by_day: Counter,
    activity_by_hour: Counter,
    model_by_day: dict,
    session_events: dict,
    session_models: dict,
    session_first_ts: dict,
    shell_commands: Counter,
    session_shell_commands: dict,
    agents: dict,
    session_tool_seq: dict,
    session_span_details: dict,
    token_totals: dict | None = None,
    session_tokens: dict | None = None,
    mcp_server_before: Counter | None = None,
    mcp_server_after: Counter | None = None,
    subagent_stops_by_type: Counter | None = None,
    session_conversation: dict[str, list[dict]] | None = None,
) -> None:
    """Process a single flat span dict and update counters."""
    if subagent_stops_by_type is None:
        subagent_stops_by_type = Counter()
    from reflect.parsing import (
        _extract_event,
        _extract_model_name,
        _extract_session_id,
        _shorten_mcp_server,
    )

    attrs = span.get("attributes") or {}

    event = _extract_event(span)
    if event:
        events_by_type[event] += 1

    model = _extract_model_name(attrs)
    if model:
        models[model] += 1

    tool_name = attrs.get("gen_ai.client.tool_name")
    if tool_name:
        tools[tool_name] += 1

    mcp_server = attrs.get("gen_ai.client.mcp_server")
    if mcp_server:
        short_server = _shorten_mcp_server(mcp_server)
        mcp_servers[short_server] += 1
        if event == "BeforeMCPExecution" and mcp_server_before is not None:
            mcp_server_before[short_server] += 1
        elif event == "AfterMCPExecution" and mcp_server_after is not None:
            mcp_server_after[short_server] += 1

    subagent_type = attrs.get("gen_ai.client.subagent_type")
    if subagent_type:
        if event == "SubagentStart":
            subagent_types[subagent_type] += 1
        elif event == "SubagentStop" and subagent_stops_by_type is not None:
            subagent_stops_by_type[subagent_type] += 1

    session_id = _extract_session_id(attrs)
    if session_id:
        sessions_seen.add(session_id)
        session_events[session_id] = session_events.get(session_id, 0) + 1
        if model:
            if session_id not in session_models:
                session_models[session_id] = Counter()
            session_models[session_id][model] += 1

    ts_ns = span.get("start_time_ns")
    if ts_ns is not None:
        ts_int = int(ts_ns)
        timestamps_ns.append(ts_int)
        # Activity by day and hour
        dt = datetime.fromtimestamp(ts_int / 1e9, tz=UTC)
        day_key = dt.strftime("%Y-%m-%d")
        activity_by_day[day_key] += 1
        activity_by_hour[dt.hour] += 1
        if model:
            if day_key not in model_by_day:
                model_by_day[day_key] = Counter()
            model_by_day[day_key][model] += 1
        # Track first timestamp per session
        if session_id and (session_id not in session_first_ts or ts_int < session_first_ts[session_id]):
            session_first_ts[session_id] = ts_int

    # Collect tool/command durations for percentile calculation
    start_ns = span.get("start_time_ns")
    end_ns = span.get("end_time_ns")
    if tool_name and start_ns and end_ns:
        duration_ms = (int(end_ns) - int(start_ns)) / 1e6
        if duration_ms >= 0:
            tool_durations_ms.setdefault(tool_name, []).append(duration_ms)

    # Track shell commands — two sources:
    # 1. BeforeShellExecution (Cursor): gen_ai.client.command
    # 2. PreToolUse Bash/Shell (Claude Code): gen_ai.client.tool.input (when capture enabled)
    cmd = ""
    if event == "BeforeShellExecution":
        cmd = attrs.get("gen_ai.client.command", "")
    elif tool_name in ("Shell", "Bash") and event == "PreToolUse":
        cmd = attrs.get("gen_ai.client.tool.input", "")
    if isinstance(cmd, str) and cmd:
        short = cmd[:60] + ("..." if len(cmd) > 60 else "")
        shell_commands[short] += 1
        if session_id:
            if session_id not in session_shell_commands:
                session_shell_commands[session_id] = Counter()
            session_shell_commands[session_id][short] += 1

    # Graph analysis: record per-session tool sequences
    start_ns = span.get("start_time_ns")
    end_ns = span.get("end_time_ns")
    if session_id and tool_name and event == "PreToolUse" and start_ns:
        is_ok = True
        session_tool_seq.setdefault(session_id, []).append(
            (int(start_ns), tool_name, is_ok)
        )
    # Record tool/shell/MCP invocations and results for per-session timelines and
    # failure tracking.  PostToolUse/PostToolUseFailure events carry result status;
    # BeforeShellExecution spans derive their label from gen_ai.client.command
    # (no tool_name present for shell events).
    _DETAIL_EVENTS = {
        "PreToolUse", "PostToolUse", "PostToolUseFailure",
        "BeforeShellExecution", "BeforeMCPExecution",
    }
    if session_id and start_ns and event in _DETAIL_EVENTS:
        dur = 0.0
        if end_ns:
            dur = (int(end_ns) - int(start_ns)) / 1e6
        session_span_details.setdefault(session_id, []).append({
            "t": int(start_ns),
            "tool": tool_name or attrs.get("gen_ai.client.mcp_tool", attrs.get("gen_ai.client.command", "?")),
            "dur": round(dur, 1),
            "ok": event != "PostToolUseFailure",
            "event": event,
        })
    if session_id and event in ("Stop", "SubagentStop", "SessionEnd") and start_ns:
        dur = 0.0
        if end_ns:
            dur = (int(end_ns) - int(start_ns)) / 1e6
        session_span_details.setdefault(session_id, []).append({
            "t": int(start_ns),
            "tool": tool_name or "",
            "dur": round(dur, 1),
            "ok": True,
            "event": event,
        })

    # Token usage extraction
    def _int_attr(key: str) -> int:
        v = attrs.get(key)
        if v is None:
            return 0
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    input_tok = _int_attr("gen_ai.usage.input_tokens")
    output_tok = _int_attr("gen_ai.usage.output_tokens")
    cache_create_tok = _int_attr("gen_ai.usage.cache_creation.input_tokens")
    cache_read_tok = _int_attr("gen_ai.usage.cache_read.input_tokens")

    if token_totals is not None and (input_tok or output_tok or cache_create_tok or cache_read_tok):
        token_totals["input"] += input_tok
        token_totals["output"] += output_tok
        token_totals["cache_creation"] += cache_create_tok
        token_totals["cache_read"] += cache_read_tok

    if session_tokens is not None and session_id and (input_tok or output_tok or cache_create_tok or cache_read_tok):
        if session_id not in session_tokens:
            session_tokens[session_id] = {
                "input": 0,
                "output": 0,
                "cache_creation": 0,
                "cache_read": 0,
                "source": "local_telemetry",
                "note": "",
            }
        session_tokens[session_id]["input"] += input_tok
        session_tokens[session_id]["output"] += output_tok
        session_tokens[session_id]["cache_creation"] += cache_create_tok
        session_tokens[session_id]["cache_read"] += cache_read_tok
        session_tokens[session_id].setdefault("source", "local_telemetry")
        session_tokens[session_id].setdefault("note", "")

    # Conversation events for session browser
    if session_conversation is not None and session_id and event:
        conv_event: dict | None = None
        _ts_ms = int(int(start_ns)) // 1_000_000 if start_ns else 0
        if event == "SessionStart":
            conv_event = {"type": "session_start", "ts": _ts_ms}
        elif event == "UserPromptSubmit":
            preview = str(attrs.get("gen_ai.client.prompt", ""))[:200]
            conv_event = {"type": "prompt", "ts": _ts_ms, "preview": preview}
        elif event == "Stop":
            preview = str(attrs.get("gen_ai.client.output", ""))[:200]
            conv_event = {
                "type": "response", "ts": _ts_ms,
                "preview": preview,
                "model": model or "",
                "input_tokens": input_tok, "output_tokens": output_tok,
                "cache_read_tokens": cache_read_tok,
            }
        elif event == "PreToolUse":
            preview = str(attrs.get("gen_ai.client.tool.input", ""))[:200]
            conv_event = {"type": "tool_call", "ts": _ts_ms, "tool_name": tool_name or "", "preview": preview}
        elif event in ("PostToolUse", "PostToolUseFailure"):
            dur = 0.0
            if start_ns and end_ns:
                dur = (int(end_ns) - int(start_ns)) / 1e6
            conv_event = {
                "type": "tool_result", "ts": _ts_ms,
                "tool_name": tool_name or "", "success": event == "PostToolUse",
                "duration_ms": round(dur, 1),
            }
        elif event == "SubagentStart":
            conv_event = {"type": "subagent_start", "ts": _ts_ms, "subagent_type": subagent_type or ""}
        elif event == "SubagentStop":
            conv_event = {"type": "subagent_stop", "ts": _ts_ms, "subagent_type": subagent_type or ""}
        elif event == "SessionEnd":
            conv_event = {"type": "session_end", "ts": _ts_ms}
        elif event in ("BeforeMCPExecution", "AfterMCPExecution"):
            mcp_tool = attrs.get("gen_ai.client.mcp_tool", "")
            conv_event = {
                "type": "mcp_call" if event == "BeforeMCPExecution" else "mcp_result",
                "ts": _ts_ms, "tool_name": mcp_tool,
                "server": _shorten_mcp_server(mcp_server) if mcp_server else "",
            }
        if conv_event is not None:
            session_conversation.setdefault(session_id, []).append(conv_event)

    # Per-agent (IDE) stats
    agent_name = attrs.get("gen_ai.client.name") or attrs.get("ide.name") or attrs.get("service.name") or "unknown"
    if agent_name not in agents:
        agents[agent_name] = AgentStats(name=agent_name)
    ag = agents[agent_name]
    ag.total_events += 1
    if event:
        ag.events_by_type[event] += 1
    if model:
        ag.models_by_count[model] += 1
    if tool_name:
        ag.tools_by_count[tool_name] += 1
    if mcp_server:
        ag.mcp_servers[_shorten_mcp_server(mcp_server)] += 1
    if subagent_type and event == "SubagentStart":
        ag.subagent_types[subagent_type] += 1
    if session_id:
        ag.sessions_seen.add(session_id)
    if tool_name and start_ns and end_ns:
        duration_ms = (int(end_ns) - int(start_ns)) / 1e6
        if duration_ms >= 0:
            ag.tool_durations_ms.setdefault(tool_name, []).append(duration_ms)
    ag.total_input_tokens += input_tok
    ag.total_output_tokens += output_tok
    ag.total_cache_creation_tokens += cache_create_tok
    ag.total_cache_read_tokens += cache_read_tok


def analyze_telemetry(
    sessions_dir: Path,
    spans_dir: Path,
    otlp_traces_file: Path | None = None,
    since: datetime | None = None,
) -> TelemetryStats:
    from reflect.insights import compute_session_quality
    from reflect.parsing import (
        _canonical_otlp_traces_path,
        _enrich_missing_session_models_from_logs,
        _extract_session_id,
        _infer_otlp_logs_file,
        _load_json_lines,
        _load_otlp_traces,
        _load_rich_session_spans,
        _load_session_model_hints,
        _materialize_local_otlp_traces,
    )

    session_files = sorted(sessions_dir.glob("*.json")) if sessions_dir.exists() else []
    span_files = sorted(spans_dir.glob("*.jsonl")) if spans_dir.exists() else []
    materialized_local_otlp: Path | None = None
    if (
        sessions_dir == _default_sessions_dir()
        and spans_dir == _default_spans_dir()
        and (
            otlp_traces_file is None
            or otlp_traces_file == _canonical_otlp_traces_path()
            or not otlp_traces_file.exists()
        )
    ):
        materialized_local_otlp = _materialize_local_otlp_traces(sessions_dir, spans_dir)
        if materialized_local_otlp is not None:
            otlp_traces_file = materialized_local_otlp
    # Always discover rich session files for session_source mapping
    # (needed for "Load full transcript" in the web UI)
    _load_rich_for_source_map = (sessions_dir == _default_sessions_dir())

    # Only process rich session SPANS when no other data sources exist
    use_rich_sessions = (
        _load_rich_for_source_map
        and not span_files
        and not (otlp_traces_file and otlp_traces_file.exists())
    )

    events_by_type: Counter[str] = Counter()
    events_by_file: dict[str, int] = {}
    models: Counter[str] = Counter()
    tools: Counter[str] = Counter()
    subagent_types: Counter[str] = Counter()
    mcp_servers: Counter[str] = Counter()
    sessions_seen: set[str] = set()
    session_events: dict[str, int] = {}
    session_models: dict[str, Counter] = {}
    session_first_ts: dict[str, int] = {}
    timestamps_ns: list[int] = []
    tool_durations_ms: dict[str, list[float]] = {}
    activity_by_day: Counter[str] = Counter()
    activity_by_hour: Counter[int] = Counter()
    model_by_day: dict[str, Counter] = {}
    shell_commands: Counter[str] = Counter()
    session_shell_commands: dict[str, Counter] = {}
    agents: dict[str, AgentStats] = {}
    session_tool_seq: dict[str, list] = {}
    session_span_details: dict[str, list] = {}
    token_totals: dict[str, int] = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    session_tokens: dict[str, dict] = {}
    mcp_server_before: Counter[str] = Counter()
    mcp_server_after: Counter[str] = Counter()
    subagent_stops_by_type: Counter[str] = Counter()
    session_conversation: dict[str, list[dict]] = {}
    session_source: dict[str, tuple[str, str]] = {}
    sessions_with_telemetry: set[str] = set()
    total_events = 0
    since_ns: int = int(since.timestamp() * 1_000_000_000) if since else 0

    proc_args = (
        events_by_type, models, tools, mcp_servers,
        subagent_types, sessions_seen, timestamps_ns,
        tool_durations_ms, activity_by_day, activity_by_hour, model_by_day,
        session_events, session_models, session_first_ts,
        shell_commands, session_shell_commands, agents, session_tool_seq, session_span_details,
        token_totals, session_tokens,
        mcp_server_before, mcp_server_after, subagent_stops_by_type,
    )
    proc_kwargs = {"session_conversation": session_conversation}

    # Source 1: Legacy JSONL span files
    if materialized_local_otlp is None:
        for span_file in span_files:
            file_events = 0
            for span in _load_json_lines(span_file):
                ts = span.get("start_time_ns", 0)
                if since_ns and ts and int(ts) < since_ns:
                    continue
                sid = _extract_session_id(span.get("attributes") or {})
                if sid:
                    sessions_with_telemetry.add(sid)
                _process_span(span, *proc_args, **proc_kwargs)
                file_events += 1

            events_by_file[span_file.name] = file_events
            total_events += file_events

    # Source 2: OTLP JSON from collector file exporter
    if otlp_traces_file and otlp_traces_file.exists():
        # A synthesized OTLP file only counts as real telemetry when it came from
        # existing span files or from a non-default explicit input, not when it was
        # materialized solely from native local session stores.
        otlp_is_real_telemetry = (
            materialized_local_otlp is None
            or bool(span_files)
            or sessions_dir != _default_sessions_dir()
        )
        file_events = 0
        for span in _load_otlp_traces(otlp_traces_file, since_ns=since_ns):
            if otlp_is_real_telemetry:
                sid = _extract_session_id(span.get("attributes") or {})
                if sid:
                    sessions_with_telemetry.add(sid)
            _process_span(span, *proc_args, **proc_kwargs)
            file_events += 1

        events_by_file[otlp_traces_file.name] = file_events
        total_events += file_events

    # Source 3: Rich local agent session stores (session-first fallback)
    if use_rich_sessions and materialized_local_otlp is None:
        rich_spans, rich_counts, rich_source_map = _load_rich_session_spans()
        filtered_count = 0
        for span in rich_spans:
            ts = span.get("start_time_ns", 0)
            if since_ns and ts and int(ts) < since_ns:
                continue
            _process_span(span, *proc_args, **proc_kwargs)
            filtered_count += 1
        events_by_file.update(rich_counts)
        total_events += filtered_count
        session_source.update(rich_source_map)

    # Source 3b: Load rich session spans for agents not covered by OTLP/local spans
    # (e.g. Copilot sessions that only exist in native files, not in local_spans)
    # Also populates session_source for "Load full transcript" in the web UI.
    # For sessions already in sessions_seen, still merge token data to fill gaps left by OTLP.
    if _load_rich_for_source_map and not use_rich_sessions:
        rich_spans, rich_counts, rich_source_map = _load_rich_session_spans()
        session_source.update(rich_source_map)
        # Snapshot which sessions already have OTLP token data — don't double-count those.
        otlp_token_sessions = set(session_tokens.keys())
        filtered_count = 0
        for span in rich_spans:
            attrs = span.get("attributes") or {}
            sid = _extract_session_id(attrs)
            ts = span.get("start_time_ns", 0)
            event = attrs.get("gen_ai.client.hook.event") or ""
            agent_name = attrs.get("gen_ai.client.name") or attrs.get("ide.name") or attrs.get("service.name") or ""
            tool_name = attrs.get("gen_ai.client.tool_name") or attrs.get("gen_ai.client.mcp_tool") or ""
            mcp_server = attrs.get("gen_ai.client.mcp_server") or ""
            if since_ns and ts and int(ts) < since_ns:
                continue
            if sid and sid in sessions_seen:
                # Copilot/Gemini native session stores often carry richer tool lifecycle
                # data than the OTLP session shell events currently exported. Merge those
                # activity spans even for sessions already discovered via OTLP so graphs
                # and per-agent tool stats are not left empty.
                merge_native_activity = (
                    agent_name in {"copilot", "gemini"}
                    and (
                        bool(tool_name)
                        or bool(mcp_server)
                        or event in {
                            "PreToolUse",
                            "PostToolUse",
                            "PostToolUseFailure",
                            "BeforeMCPExecution",
                            "AfterMCPExecution",
                        }
                    )
                )
                if merge_native_activity:
                    _process_span(span, *proc_args, **proc_kwargs)
                    filtered_count += 1
                    continue
                # Session already covered by OTLP/local spans — merge token data only
                # if OTLP didn't already provide token data for this session.
                if sid not in otlp_token_sessions:
                    in_tok = attrs.get("gen_ai.usage.input_tokens")
                    out_tok = attrs.get("gen_ai.usage.output_tokens")
                    cc_tok = attrs.get("gen_ai.usage.cache_creation.input_tokens")
                    cr_tok = attrs.get("gen_ai.usage.cache_read.input_tokens")
                    if in_tok or out_tok or cc_tok or cr_tok:
                        def _to_int(v: object) -> int:
                            try:
                                return int(v)  # type: ignore[arg-type]
                            except (ValueError, TypeError):
                                return 0
                        it, ot, ct, rt = _to_int(in_tok), _to_int(out_tok), _to_int(cc_tok), _to_int(cr_tok)
                        entry = session_tokens.setdefault(
                            sid,
                            {
                                "input": 0,
                                "output": 0,
                                "cache_creation": 0,
                                "cache_read": 0,
                                "source": "local_telemetry",
                                "note": "",
                            },
                        )
                        entry["input"] += it
                        entry["output"] += ot
                        entry["cache_creation"] += ct
                        entry["cache_read"] += rt
                        entry.setdefault("source", "local_telemetry")
                        entry.setdefault("note", "")
                        token_totals["input"] += it
                        token_totals["output"] += ot
                        token_totals["cache_creation"] += ct
                        token_totals["cache_read"] += rt
                continue
            _process_span(span, *proc_args, **proc_kwargs)
            filtered_count += 1
        if filtered_count:
            events_by_file.update(rich_counts)
            total_events += filtered_count

    _enrich_missing_session_models_from_logs(
        _infer_otlp_logs_file(otlp_traces_file),
        sessions_seen,
        session_models,
    )
    session_model_hints = _load_session_model_hints(session_files)
    for session_id, model in session_model_hints.items():
        if session_id in sessions_seen and not session_models.get(session_id):
            session_models[session_id] = Counter({model: 1})

    # Date range from timestamps
    first_ts = last_ts = ""
    days_active = 0
    if timestamps_ns:
        min_ts = min(timestamps_ns)
        max_ts = max(timestamps_ns)
        min_dt = datetime.fromtimestamp(min_ts / 1e9, tz=UTC)
        max_dt = datetime.fromtimestamp(max_ts / 1e9, tz=UTC)
        first_ts = min_dt.strftime("%Y-%m-%d %H:%M UTC")
        last_ts = max_dt.strftime("%Y-%m-%d %H:%M UTC")
        unique_days = {datetime.fromtimestamp(t / 1e9, tz=UTC).date() for t in timestamps_ns}
        days_active = len(unique_days)

    # Aggregate recovery stats
    session_recovered_failures: dict[str, int] = {}
    for sid, spans in session_span_details.items():
        recovered = 0
        last_failed = False
        for s in spans:
            if s["event"] == "PostToolUseFailure":
                last_failed = True
            elif last_failed and s["ok"] and s["event"] == "PostToolUse":
                recovered += 1
                last_failed = False
            elif s["ok"]:
                last_failed = False
        if recovered:
            session_recovered_failures[sid] = recovered

    # Compute quality scores
    session_quality_scores: dict[str, float] = {}
    session_goal_completed: dict[str, bool] = {}
    for sid in sessions_seen:
        spans = session_span_details.get(sid, [])
        tokens = session_tokens.get(sid, {})
        score = compute_session_quality(sid, spans, tokens)
        session_quality_scores[sid] = score
        session_goal_completed[sid] = any(
            s["event"] in ("Stop", "SubagentStop", "SessionEnd") for s in spans
        )

    # Aggregate quality back to agents
    for _agent_name, ag in agents.items():
        for sid in ag.sessions_seen:
            if sid in session_quality_scores:
                ag.total_quality_score += session_quality_scores[sid]
                if session_goal_completed.get(sid):
                    ag.completed_sessions += 1
                if sid in session_recovered_failures:
                    ag.recovered_failures += session_recovered_failures[sid]

    return TelemetryStats(
        session_files=len(session_files),
        span_files=len(span_files),
        total_events=total_events,
        events_by_type=events_by_type,
        events_by_file=events_by_file,
        models_by_count=models,
        tools_by_count=tools,
        subagent_types=subagent_types,
        mcp_servers=mcp_servers,
        sessions_seen=sessions_seen,
        session_events=session_events,
        session_models=session_models,
        session_first_ts=session_first_ts,
        tool_durations_ms=tool_durations_ms,
        activity_by_day=activity_by_day,
        activity_by_hour=activity_by_hour,
        model_by_day=model_by_day,
        shell_commands=shell_commands,
        session_shell_commands=session_shell_commands,
        agents=agents,
        session_tool_seq=session_tool_seq,
        session_span_details=session_span_details,
        first_event_ts=first_ts,
        last_event_ts=last_ts,
        days_active=days_active,
        total_input_tokens=token_totals["input"],
        total_output_tokens=token_totals["output"],
        total_cache_creation_tokens=token_totals["cache_creation"],
        total_cache_read_tokens=token_totals["cache_read"],
        session_tokens=session_tokens,
        mcp_server_before=mcp_server_before,
        mcp_server_after=mcp_server_after,
        subagent_stops_by_type=subagent_stops_by_type,
        session_quality_scores=session_quality_scores,
        session_goal_completed=session_goal_completed,
        session_recovered_failures=session_recovered_failures,
        session_conversation={
            sid: sorted(evts, key=lambda e: e.get("ts", 0))
            for sid, evts in session_conversation.items()
        },
        session_source=session_source,
        sessions_with_telemetry=sessions_with_telemetry,
    )
