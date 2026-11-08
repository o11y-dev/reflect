"""Shared, read-only measurement of canonical observations.

Turns are observed prompt/terminal intervals, not execution units or assistant
messages. Missing attributes remain missing even where legacy SQL columns use 0.
"""
from __future__ import annotations

import sqlite3
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime
from math import ceil
from typing import Any

from pydantic import Field

from reflect.schema.base import ReflectModel
from reflect.telemetry_facts import TURN_TERMINAL_KEYS, event_time, observed_turn_terminal
from reflect.usage_facts import usage_facts
from reflect.utils import _json_loads


class TurnMeasurement(ReflectModel):
    session_id: str
    turn_id: str
    prompt_at: str
    ended_at: str
    model: str | None = None
    requested_mode: str | None = None
    input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    input_cost_usd: float | None = None
    cache_write_cost_usd: float | None = None
    tool_calls: int | None = None
    idle_before_ms: int | None = None
    complete_usage: bool = False


class MeasurementView(ReflectModel):
    scope: dict[str, Any] = Field(default_factory=dict)
    totals: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    model_mix: list[dict[str, Any]] = Field(default_factory=list)
    cost_breakdown: dict[str, float] = Field(default_factory=dict)
    cost_by_day: dict[str, float] = Field(default_factory=dict)
    agent_cost_over_time: list[dict[str, Any]] = Field(default_factory=list)
    sessions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    turns: list[TurnMeasurement] = Field(default_factory=list)


def _attrs(value: object, keys: tuple[str, ...] = ()) -> dict[str, Any]:
    try:
        result = _json_loads(str(value or "{}"))
        if isinstance(result, list) and keys:
            return {key: item for key, item in zip(keys, result, strict=True) if item is not None}
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _projection(column: str, keys: tuple[str, ...]) -> str:
    """Decode SQLite JSON once and return metadata without prompts/results."""
    paths = ",".join(f"'$.\"{key}\"'" for key in keys)
    return f"json_extract({column},{paths})"


_TURN_KEYS = ("gen_ai.client.turn_id", "turn.id", "turn_id", "generation.id", "gen_ai.client.generation_id")
_USAGE_KEYS = (
    "reflect.time.provenance", "gen_ai.request.id", "reflect.usage.identity",
    "reflect.usage.input_semantics", "reflect.usage.grain", "reflect.token.source",
    "reflect.cost.source", "reflect.cost.breakdown", "reflect.model.provenance",
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
    "gen_ai.usage.cache_read.input_tokens", "hook.cache_read_tokens",
    "gen_ai.usage.cache_creation.input_tokens", "hook.cache_write_tokens",
    "gen_ai.request.mode", "gen_ai.client.requested_mode", *_TURN_KEYS,
)
_BOUNDARY_KEYS = (*TURN_TERMINAL_KEYS, *_TURN_KEYS)
_TOOL_KEYS = (
    "reflect.tool.identity_provenance", "reflect.tool.terminal",
    "gen_ai.client.hook.event", "process.exit.code", *_TURN_KEYS,
)


def _rows(conn: sqlite3.Connection, sql: str, params: list | None = None) -> list[dict]:
    cursor = conn.execute(sql, params or [])
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor]


def _scope(column: str, session_ids: set[str] | None) -> tuple[str, list]:
    if session_ids is None:
        return "1=1", []
    ids = sorted(session_ids)
    return (f"{column} IN ({','.join('?' for _ in ids)})", ids) if ids else ("0=1", [])


def _turn_id(attrs: dict) -> str:
    return str(next((attrs[k] for k in _TURN_KEYS if attrs.get(k)), ""))


def _observed_time(attrs: dict) -> bool:
    return attrs.get("reflect.time.provenance") not in {"inferred", "synthetic"}


def build_measurements(
    conn: sqlite3.Connection, *, session_ids: set[str] | None = None,
    date_from: str | None = None, date_to: str | None = None,
    include_turns: bool = False, usage_only: bool = False,
) -> MeasurementView:
    """Measure one UTC half-open event window, or complete session lifetimes.

    Reads never fetch prices or change the database. Cost components come from
    the canonical pricing pass; unclassified historical sums retain coverage.
    """
    start, end = event_time(date_from), event_time(date_to)
    if date_from and start is None or date_to and end is None or start and end and start >= end:
        raise ValueError("Expected an increasing ISO-8601 date window")
    def inside(value: object) -> bool:
        timestamp = event_time(value)
        return bool(timestamp and (start is None or timestamp >= start) and (end is None or timestamp < end))

    clause, params = _scope("s.id", session_ids)
    session_rows = _rows(conn, f"SELECT s.id,s.started_at,s.ended_at,s.parent_session_id,s.token_provenance,s.input_tokens,s.output_tokens,s.cache_creation_tokens,s.cache_read_tokens,s.reasoning_tokens,s.estimated_cost_usd,COALESCE(a.name,'unknown') AS agent FROM sessions s LEFT JOIN agents a ON a.id=s.agent_id WHERE {clause}", params)
    ids = {r["id"] for r in session_rows}
    clause, params = _scope("lc.session_id", ids)
    # No joins to tools: each usage observation is counted at its own grain.
    calls = _rows(conn, f"""SELECT lc.id,lc.session_id,lc.provider,lc.request_model,lc.response_model,
        lc.input_tokens,lc.output_tokens,lc.cache_read_input_tokens,lc.cache_creation_input_tokens,
        lc.reasoning_output_tokens,lc.estimated_cost_usd,
        {_projection('lc.raw_attrs_json', _USAGE_KEYS)} AS raw_attrs_json,st.started_at AS event_at
        FROM llm_calls lc JOIN steps st ON st.id=lc.step_id
        WHERE {clause} ORDER BY st.started_at,lc.id""", params)
    totals = dict.fromkeys(("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "reasoning_tokens", "estimated_cost_usd"), 0)
    costs = dict.fromkeys(("input_cost_usd", "output_cost_usd", "cache_read_cost_usd", "cache_creation_cost_usd", "total_cost_usd"), 0.0)
    coverage: dict[str, Any] = {"observed_usage": 0, "priced_usage": 0, "complete_priced_usage": 0, "partially_priced_usage": 0, "known_model_usage": 0, "complete_input_usage": 0, "undated_usage": 0, "estimated_usage": 0, "legacy_cost_usage": 0, "unattributed_usage": 0, "undated_cost_usd": 0.0}
    bucket_records = dict.fromkeys(("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"), 0)
    coverage["token_bucket_records"] = bucket_records
    grains: Counter = Counter()
    by_model: dict[str, dict] = {}
    session_agents = {row["id"]: row["agent"] for row in session_rows}
    daily_costs: dict[str, float] = defaultdict(float)
    agent_daily_costs: dict[tuple[str, str], float] = defaultdict(float)
    price_coverage_keys = ("observed_usage", "priced_usage", "complete_priced_usage", "partially_priced_usage", "legacy_cost_usage")
    per_session = {sid: {**dict.fromkeys(totals, 0), **dict.fromkeys(price_coverage_keys, 0), "known_cost_usd": 0.0} for sid in ids}
    usage_by_turn: dict[tuple[str, str], list] = defaultdict(list)
    sessions_with_usage: set[str] = set()
    seen: set[tuple] = set()
    read_total = inclusive_total = 0
    watermark = max((r["ended_at"] or r["started_at"] or "" for r in session_rows), default="")
    for row in calls:
        attrs = _attrs(row["raw_attrs_json"], _USAGE_KEYS)
        facts = usage_facts(row, attrs)
        bucket, present = facts.buckets, facts.present
        if not present and not float(row["estimated_cost_usd"] or 0):
            # Prompt/assistant records may share llm_calls storage without a
            # usage observation. They remain activity, not missing-usage rows.
            continue
        sessions_with_usage.add(row["session_id"])
        if facts.identity in seen:
            continue
        seen.add(facts.identity)
        if not _observed_time(attrs) or event_time(row["event_at"]) is None:
            coverage["undated_usage"] += 1
            if start or end:
                continue
        elif not inside(row["event_at"]):
            continue
        grain = str(attrs.get("reflect.usage.grain") or "unknown")
        grains[grain] += 1
        coverage["observed_usage"] += 1
        for name in bucket_records:
            bucket_name = "cache_write_tokens" if name == "cache_creation_tokens" else name
            bucket_records[name] += int(bucket.get(bucket_name) is not None)
        info = per_session[row["session_id"]]
        info["observed_usage"] += 1
        estimated = attrs.get("reflect.token.source", "").startswith("estimated")
        coverage["estimated_usage"] += int(estimated)
        for target, column in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"), ("cache_read_tokens", "cache_read_input_tokens"), ("cache_creation_tokens", "cache_creation_input_tokens"), ("reasoning_tokens", "reasoning_output_tokens")):
            totals[target] += facts.tokens[column]
            info[target] += facts.tokens[column]
        complete = all(bucket[k] is not None for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens")) and not estimated
        if complete:
            coverage["complete_input_usage"] += 1
            read_total += bucket["cache_read_tokens"]
            inclusive_total += sum(bucket[k] for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
        source = "unavailable" if attrs.get("reflect.model.provenance") in {"inferred", "config_default"} else attrs.get("reflect.cost.source")
        breakdown = attrs.get("reflect.cost.breakdown")
        priced = source not in (None, "missing", "unavailable", "partial") and present
        partial_price = source == "partial" and present
        legacy = source is None and float(row["estimated_cost_usd"] or 0) > 0
        amount = float(row["estimated_cost_usd"] or 0) if priced or partial_price or legacy else None
        coverage["priced_usage"] += int(priced)
        coverage["complete_priced_usage"] += int(priced and all(v is not None for v in bucket.values()))
        coverage["partially_priced_usage"] += int(partial_price)
        coverage["legacy_cost_usage"] += int(legacy)
        info["priced_usage"] += int(priced)
        info["complete_priced_usage"] += int(priced and all(v is not None for v in bucket.values()))
        info["partially_priced_usage"] += int(partial_price)
        info["legacy_cost_usage"] += int(legacy)
        if amount is not None:
            totals["estimated_cost_usd"] += amount
            info["known_cost_usd"] += amount
            info["estimated_cost_usd"] += amount
            costs["total_cost_usd"] += amount
            dated = event_time(row["event_at"]) if _observed_time(attrs) else None
            if dated:
                day = dated.date().isoformat()
                daily_costs[day] += amount
                agent_daily_costs[(day, session_agents[row["session_id"]])] += amount
            else:
                coverage["undated_cost_usd"] += amount
        if (priced or partial_price) and isinstance(breakdown, dict):
            for key in costs:
                if key != "total_cost_usd":
                    costs[key] += float(breakdown.get(key) or 0)
        model = row["response_model"] or row["request_model"]
        if attrs.get("reflect.model.provenance") in {"inferred", "config_default"}:
            model = None
        coverage["known_model_usage"] += int(bool(model))
        mix = by_model.setdefault(model or "unknown", {"model": model, "calls": 0, "tokens": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "cost_observations": 0})
        mix["calls"] += 1
        mix["cost_observations"] += int(amount is not None)
        mix["input_tokens"] += facts.tokens["input_tokens"]
        mix["output_tokens"] += facts.tokens["output_tokens"]
        mix["tokens"] += sum(value for key, value in facts.tokens.items() if key != "reasoning_output_tokens")
        mix["cost_usd"] += amount or 0
        tid = _turn_id(attrs)
        if tid and grain not in {"session", "interval"}:
            usage_by_turn[(row["session_id"], tid)].append((bucket, model, attrs, amount, breakdown, complete))

    # Preserve retained lifetime aggregates only when canonical usage records
    # are absent. Never max them over corrected request totals or date them by
    # session start; these observations cannot support turn/window attribution.
    for row in session_rows:
        if row["id"] in sessions_with_usage or not any(row.get(key) for key in totals):
            continue
        estimated = row["token_provenance"] == "estimated_cursor_transcript"
        coverage["estimated_usage"] += int(estimated)
        coverage["undated_usage"] += 1
        coverage["unattributed_usage"] += 1
        if start or end:
            continue
        grains["session_estimate" if estimated else "session"] += 1
        coverage["observed_usage"] += 1
        info = per_session[row["id"]]
        info["observed_usage"] += 1
        for key in totals:
            totals[key] += row.get(key) or 0
            info[key] += row.get(key) or 0
        amount = float(row["estimated_cost_usd"] or 0)
        if amount > 0:
            coverage["legacy_cost_usage"] += 1
            info["legacy_cost_usage"] += 1
            info["known_cost_usd"] += amount
            costs["total_cost_usd"] += amount
            coverage["undated_cost_usd"] += amount
        mix = by_model.setdefault("unknown", {"model": None, "calls": 0, "tokens": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "cost_observations": 0})
        mix["calls"] += 1
        mix["cost_observations"] += int(amount > 0)
        mix["input_tokens"] += int(row["input_tokens"] or 0)
        mix["output_tokens"] += int(row["output_tokens"] or 0)
        mix["tokens"] += sum(int(row.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"))
        mix["cost_usd"] += amount
    coverage["usage_grains"] = dict(grains)
    for price_coverage in (coverage, *per_session.values()):
        observed = price_coverage["observed_usage"]
        price_coverage["cost_status"] = "complete" if observed and observed == price_coverage["complete_priced_usage"] else "partial" if price_coverage["priced_usage"] or price_coverage["partially_priced_usage"] or price_coverage["legacy_cost_usage"] else "unavailable"
    metrics: dict[str, Any] = {"completed_turns": 0, "mean_input_tokens": None, "max_tool_calls_per_turn": None, "cache_read_ratio": read_total/inclusive_total if inclusive_total else None, "inclusive_input_tokens": inclusive_total, "idle_total_ms": None, "idle_max_ms": None, "idle_over_five_minutes": None, "model_switches": None, "session_duration_ms": None}
    metrics.update(dict.fromkeys(("completed_tool_calls", "mean_tool_calls_per_turn", "mean_session_elapsed_ms", "p95_session_elapsed_ms", "mean_turn_elapsed_ms", "p95_turn_elapsed_ms")))
    view = MeasurementView(scope={"kind": "event_window" if start or end else "session_lifetime", "date_from": date_from, "date_to": date_to, "watermark": watermark}, totals=totals, coverage=coverage, metrics=metrics, model_mix=list(by_model.values()), cost_breakdown=costs, cost_by_day=dict(sorted(daily_costs.items())), agent_cost_over_time=[{"day": day, "agent": agent, "total_cost": amount} for (day, agent), amount in sorted(agent_daily_costs.items())], sessions=per_session)
    if usage_only or not ids:
        return view

    # Decode boundaries only for the reporting population first. Other
    # sessions can affect idle gaps, but cannot contribute measured turns.
    prompts: dict[tuple[str, str], str] = {}
    terminals: dict[tuple[str, str], str] = {}
    context_prompts: dict[tuple[str, str], datetime] = {}
    context_terminals: dict[tuple[str, str], datetime] = {}

    def collect_boundaries(boundary_ids: set[str]) -> None:
        clause, params = _scope("session_id", boundary_ids)
        boundaries = _rows(conn, f"""SELECT id,session_id,started_at,ended_at,summary,
            {_projection('raw_attrs_json', _BOUNDARY_KEYS)} AS raw_attrs_json
            FROM steps WHERE {clause} AND type IN ('conversation_event','lifecycle','llm_call')
            ORDER BY started_at,id""", params)
        for row in boundaries:
            attrs = _attrs(row["raw_attrs_json"], _BOUNDARY_KEYS)
            if not _observed_time(attrs):
                continue
            at = row["started_at"]
            observed_at = event_time(at)
            if observed_at is None:
                continue
            selected = row["session_id"] in ids and inside(at)
            event = str(attrs.get("gen_ai.client.hook.event") or attrs.get("event.name") or row["summary"] or "").lower().rsplit(".", 1)[-1]
            tid = _turn_id(attrs)
            if not tid:
                continue
            key = (row["session_id"], tid)
            if event in {"userpromptsubmit", "beforesubmitprompt", "user_message", "turn_start", "turnstarted", "task_started"}:
                context_prompts.setdefault(key, observed_at)
                if selected:
                    prompts.setdefault(key, at)
            if observed_turn_terminal(attrs):
                stopped_at = event_time(row["ended_at"] or at)
                if stopped_at:
                    context_terminals[key] = max(context_terminals.get(key, stopped_at), stopped_at)
                    if selected and (key not in terminals or stopped_at > event_time(terminals[key])):
                        terminals[key] = row["ended_at"] or at

    collect_boundaries(ids)
    first_terminal = min((event_time(value) for value in terminals.values()), default=None)
    last_prompt = max((event_time(value) for value in prompts.values()), default=None)
    if session_ids is not None and first_terminal and last_prompt and first_terminal < last_prompt:
        # The covering index supplies conservative per-session observed extents
        # without loading prompts/results. Keep whole overlapping sessions so a
        # turn spanning the entire gap (including unrelated or child work) is
        # still paired. Inclusive bounds preserve sub-millisecond overlaps.
        clause, params = _scope("session_id", ids)
        context_ids = {row[0] for row in conn.execute(f"""
            SELECT session_id FROM steps INDEXED BY idx_steps_measurement_times
            WHERE NOT ({clause}) AND type IN ('conversation_event','lifecycle','llm_call')
              AND COALESCE(json_extract(raw_attrs_json,'$."reflect.time.provenance"'),'observed')
                  NOT IN ('synthetic','inferred')
            GROUP BY session_id
            HAVING (MIN(julianday(started_at)) <= julianday(?)
                    AND MAX(julianday(COALESCE(ended_at,started_at))) >= julianday(?))
                OR SUM(julianday(started_at) IS NULL
                       OR julianday(COALESCE(ended_at,started_at)) IS NULL) > 0
        """, [*params, last_prompt.isoformat(), first_terminal.isoformat()])}
        if context_ids:
            collect_boundaries(context_ids)

    intervals = [(begin, context_terminals[key]) for key, begin in context_prompts.items()
                 if key in context_terminals and context_terminals[key] >= begin]
    # A missing Stop cannot erase observed work in another execution. Read
    # compact timestamps from the covering index, bounded by possible gaps;
    # actual tool activity also counts when no prompt/terminal pair exists.
    if first_terminal and last_prompt and first_terminal < last_prompt:
        activity = conn.execute("""
            SELECT started_at, ended_at FROM steps INDEXED BY idx_steps_measurement_times
            WHERE COALESCE(json_extract(raw_attrs_json,'$.\"reflect.time.provenance\"'),'observed')
                  NOT IN ('synthetic','inferred')
              AND julianday(started_at) < julianday(?)
              AND julianday(COALESCE(ended_at,started_at)) > julianday(?)
        """, (last_prompt.isoformat(), first_terminal.isoformat()))
        for began, ended in activity:
            begin, stop = event_time(began), event_time(ended or began)
            if begin and stop and stop >= begin:
                intervals.append((begin, stop))
    intervals.sort()
    interval_starts: list[datetime] = []
    latest_ends: list[datetime] = []
    for begin, stop in intervals:
        interval_starts.append(begin)
        latest_ends.append(max(stop, latest_ends[-1]) if latest_ends else stop)

    # Session duration includes observed tool/lifecycle events as well as turn
    # boundaries. Aggregate timestamps in SQL instead of decoding every step.
    clause, params = _scope("session_id", ids)
    time_filter = ""
    if date_from:
        time_filter += " AND at>=julianday(?)"
        params.append(date_from)
    if date_to:
        time_filter += " AND at<julianday(?)"
        params.append(date_to)
    durations = conn.execute(f"""
        WITH timed AS (
          SELECT session_id,julianday(started_at) AS a,julianday(ended_at) AS b
          FROM steps WHERE {clause}
            AND COALESCE(json_extract(raw_attrs_json,'$.\"reflect.time.provenance\"'),'observed') NOT IN ('synthetic','inferred')
        ), event_times AS (
          SELECT session_id,a AS at FROM timed UNION ALL SELECT session_id,b FROM timed
        ) SELECT session_id,MIN(at),MAX(at) FROM event_times
          WHERE at IS NOT NULL {time_filter} GROUP BY session_id
    """, params).fetchall()
    session_duration_ms = sum(round((r[2] - r[1]) * 86_400_000) for r in durations) if durations else None

    clause, params = _scope("tc.session_id", ids)
    tools = _rows(conn, f"""SELECT tc.session_id,tc.logical_call_id,tc.status,tc.output_hash,
        {_projection('tc.raw_attrs_json', _TOOL_KEYS)} AS raw_attrs_json
        FROM tool_calls tc WHERE {clause}""", params)
    tool_ids: dict[tuple[str, str], set] = defaultdict(set)
    ambiguous_tools: set[tuple[str, str]] = set()
    for row in tools:
        attrs = _attrs(row["raw_attrs_json"], _TOOL_KEYS)
        if attrs.get("reflect.tool.terminal") is False:
            continue
        key = (row["session_id"], _turn_id(attrs))
        if not row["logical_call_id"] or attrs.get("reflect.tool.identity_provenance") == "synthetic":
            ambiguous_tools.add(key)
        elif row["output_hash"] or attrs.get("process.exit.code") is not None or str(attrs.get("gen_ai.client.hook.event")).lower() in {"posttooluse", "posttoolusefailure", "aftershellexecution", "aftermcpexecution"}:
            tool_ids[key].add(row["logical_call_id"])
    turns = []
    for key, prompt in prompts.items():
        terminal = terminals.get(key)
        if not terminal or not inside(terminal) or event_time(terminal) < event_time(prompt):
            continue
        entries = usage_by_turn.get(key, [])
        complete = bool(entries) and all(e[5] for e in entries)
        models = {e[1] for e in entries}
        modes = {e[2].get("gen_ai.request.mode") or e[2].get("gen_ai.client.requested_mode") for e in entries}
        kwargs = {k: sum(e[0][k] for e in entries) if entries and all(e[0][k] is not None for e in entries) else None for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")}
        turns.append(TurnMeasurement(session_id=key[0], turn_id=key[1], prompt_at=prompt, ended_at=terminal, model=next(iter(models)) if len(models)==1 else None, requested_mode=next(iter(modes)) if len(modes)==1 else None, **kwargs, complete_usage=complete, cost_usd=sum(e[3] for e in entries) if entries and all(e[3] is not None for e in entries) else None, input_cost_usd=sum(float(e[4].get("input_cost_usd",0)) for e in entries) if entries and all(isinstance(e[4],dict) and "input" not in e[4].get("missing_price_buckets", []) for e in entries) else None, cache_write_cost_usd=sum(float(e[4].get("cache_creation_cost_usd",0)) for e in entries) if entries and all(isinstance(e[4],dict) and "cache_creation" not in e[4].get("missing_price_buckets", []) for e in entries) else None, tool_calls=len(tool_ids[key]) if key in tool_ids and key not in ambiguous_tools else None))
    turns.sort(key=lambda t:(t.session_id,event_time(t.prompt_at),t.turn_id))
    previous: dict[str, TurnMeasurement] = {}
    switches = eligible_transitions = 0
    for turn in turns:
        prev = previous.get(turn.session_id)
        if prev:
            a,b = event_time(prev.ended_at),event_time(turn.prompt_at)
            before = bisect_left(interval_starts, b) - 1
            overlaps = b > a and before >= 0 and latest_ends[before] > a
            if b >= a and not overlaps:
                turn.idle_before_ms = int((b-a).total_seconds()*1000)
            switches += int(bool(prev.model and turn.model and prev.model != turn.model))
            eligible_transitions += int(bool(prev.model and turn.model))
        previous[turn.session_id] = turn
    eligible = [t for t in turns if t.complete_usage]
    idle = [t.idle_before_ms for t in turns if t.idle_before_ms is not None]
    tool_counts = [t.tool_calls for t in turns if t.tool_calls is not None]
    observed_transitions = len(turns) - len(previous)
    switch_value = switches if eligible_transitions or observed_transitions == 0 and any(t.model for t in turns) else None
    metrics.update(completed_turns=len(turns), mean_input_tokens=sum(t.input_tokens+t.cache_read_tokens+t.cache_write_tokens for t in eligible)/len(eligible) if eligible else None, max_tool_calls_per_turn=max(tool_counts) if tool_counts else None, idle_total_ms=sum(idle) if idle else None, idle_max_ms=max(idle) if idle else None, idle_over_five_minutes=sum(i>300000 for i in idle) if idle else None, model_switches=switch_value, session_duration_ms=session_duration_ms)
    metrics.update(completed_tool_calls=sum(tool_counts) if tool_counts else None,
                   mean_tool_calls_per_turn=sum(tool_counts)/len(tool_counts) if tool_counts else None)
    # Reuse measured boundaries; no extra scan or per-session presentation rows.
    # p95 uses nearest rank and includes waiting between observed events.
    session_elapsed = sorted(round((row[2] - row[1]) * 86_400_000) for row in durations)
    turn_elapsed = sorted(round((event_time(turn.ended_at) - event_time(turn.prompt_at)).total_seconds() * 1000) for turn in turns)
    for kind, elapsed in (("session", session_elapsed), ("turn", turn_elapsed)):
        metrics[f"mean_{kind}_elapsed_ms"] = sum(elapsed)/len(elapsed) if elapsed else None
        metrics[f"p95_{kind}_elapsed_ms"] = elapsed[ceil(len(elapsed)*0.95)-1] if elapsed else None
        coverage[f"eligible_{kind}_elapsed"] = len(elapsed)
    coverage.update(eligible_usage_turns=len(eligible), observed_prompt_turns=len(prompts), eligible_tool_turns=len(tool_counts), eligible_idle_intervals=len(idle), eligible_model_transitions=eligible_transitions, observed_model_transitions=observed_transitions)
    view.metrics = metrics
    view.coverage = coverage
    view.turns = turns if include_turns else []
    return view
