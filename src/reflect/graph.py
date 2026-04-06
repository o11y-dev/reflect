from __future__ import annotations

from collections import Counter

from reflect.utils import _sanitize_command_display


def _compute_tool_transitions(session_tool_seq: dict[str, list]) -> list[dict]:
    """Compute directed tool transition counts from per-session ordered sequences.

    Returns list of {from, to, count} sorted by count desc, top 40.
    """
    transitions: Counter = Counter()
    for _sid, seq in session_tool_seq.items():
        # Sort by timestamp and extract consecutive pairs
        ordered = [t for _, t, _ in sorted(seq, key=lambda x: x[0])]
        for a, b in zip(ordered, ordered[1:], strict=False):
            if a != b:
                transitions[(a, b)] += 1
    return [
        {"from": f, "to": t, "count": c}
        for (f, t), c in transitions.most_common(40)
    ]


def _compute_tool_cooccurrence(
    session_tool_seq: dict[str, list],
    top_tools: list[str],
) -> dict:
    """Compute how often pairs of tools appear in the same session.

    Returns {tools: [...], matrix: [[...], ...]} for top N tools.
    """
    n = min(len(top_tools), 12)
    tools = top_tools[:n]
    idx = {t: i for i, t in enumerate(tools)}

    matrix = [[0] * n for _ in range(n)]
    for _sid, seq in session_tool_seq.items():
        present = {t for _, t, _ in seq if t in idx}
        for a in present:
            for b in present:
                if a != b:
                    matrix[idx[a]][idx[b]] += 1
                else:
                    matrix[idx[a]][idx[b]] += 1  # diagonal = self-count
    return {"tools": tools, "matrix": matrix}


def _compute_latency_histograms(
    tool_durations_ms: dict[str, list[float]],
    top_tools: list[str],
) -> dict:
    """Bucket latencies per tool into 6 fixed ranges.

    Buckets: 0-10ms, 10-100ms, 100ms-1s, 1s-5s, 5s-10s, 10s+
    """
    BUCKETS = [10, 100, 1_000, 5_000, 10_000, float("inf")]
    LABELS = ["0-10ms", "10-100ms", "100ms-1s", "1s-5s", "5s-10s", "10s+"]
    result: dict[str, list[int]] = {}
    for tool in top_tools[:10]:
        durations = tool_durations_ms.get(tool, [])
        if not durations:
            continue
        buckets = [0] * len(BUCKETS)
        for d in durations:
            for i, b in enumerate(BUCKETS):
                if d < b:
                    buckets[i] += 1
                    break
        result[tool] = buckets
    return {"labels": LABELS, "tools": result}


def _compute_dep_graph(
    agents: dict,
    tools_by_count: Counter,
    mcp_servers: Counter,
    session_conversation: dict[str, list] | None = None,
    session_agents: dict[str, str] | None = None,
) -> dict:
    """Build agent → tool network data for the dashboard.

    Returns {nodes: [...], links: [...]} for D3 force-directed layout.
    """
    nodes = []
    links = []
    seen_nodes: set[str] = set()

    def add_node(nid: str, ntype: str, size: int):
        if nid not in seen_nodes:
            nodes.append({"id": nid, "type": ntype, "size": size})
            seen_nodes.add(nid)

    isolated_agents = []
    session_conversation = session_conversation or {}
    session_agents = session_agents or {}
    agent_mcp_tools: dict[str, Counter[str]] = {}
    mcp_tool_servers: Counter[tuple[str, str]] = Counter()
    mcp_tool_totals: Counter[str] = Counter()

    for session_id, events in session_conversation.items():
        agent_name = session_agents.get(session_id, "")
        for event in events:
            if event.get("type") != "mcp_call":
                continue
            tool_name = str(event.get("tool_name") or "").strip()
            server_name = str(event.get("server") or "").strip()
            if not tool_name or not server_name:
                continue
            mcp_tool_totals[tool_name] += 1
            mcp_tool_servers[(tool_name, server_name)] += 1
            if agent_name:
                agent_mcp_tools.setdefault(agent_name, Counter())[tool_name] += 1

    top_mcp_tools = {tool for tool, _ in mcp_tool_totals.most_common(12)}
    top_server_names = {server for server, _ in mcp_servers.most_common(8)}

    for agent_name, ag in agents.items():
        agent_links = []
        for tool, count in ag.tools_by_count.most_common(12):
            if tool in top_mcp_tools:
                continue
            add_node(tool, "tool", count)
            agent_links.append({"source": agent_name, "target": tool, "value": count})
        for tool, count in agent_mcp_tools.get(agent_name, Counter()).most_common(10):
            if tool not in top_mcp_tools:
                continue
            add_node(tool, "mcp_tool", mcp_tool_totals.get(tool, count))
            agent_links.append({"source": agent_name, "target": tool, "value": count})

        if agent_links:
            add_node(agent_name, "agent", ag.total_events)
            links.extend(agent_links)
        else:
            isolated_agents.append({"id": agent_name, "events": ag.total_events})

    for (tool_name, server_name), count in mcp_tool_servers.most_common(18):
        if tool_name not in top_mcp_tools or server_name not in top_server_names:
            continue
        add_node(tool_name, "mcp_tool", mcp_tool_totals.get(tool_name, count))
        add_node(server_name, "mcp_server", mcp_servers.get(server_name, count))
        links.append({"source": tool_name, "target": server_name, "value": count})

    isolated_agents.sort(key=lambda item: (-item["events"], item["id"]))
    top_mcp_servers = [
        {"id": server, "events": count}
        for server, count in mcp_servers.most_common(6)
    ]
    return {
        "nodes": nodes,
        "links": links,
        "isolated_agents": isolated_agents,
        "top_mcp_servers": top_mcp_servers,
    }


def _compute_session_timeline(
    session_span_details: dict[str, list],
    session_events: dict[str, int],
    top_n: int = 6,
) -> list[dict]:
    """Build per-session event timelines for the top N busiest sessions.

    Returns list of {session, spans: [{t_rel_ms, tool, dur, ok}]} — t_rel_ms
    is relative to the session start so swimlanes can be aligned.
    """
    top_sessions = sorted(session_events, key=lambda s: session_events[s], reverse=True)[:top_n]
    result = []
    for sid in top_sessions:
        spans = session_span_details.get(sid, [])
        if not spans:
            continue
        spans_sorted = sorted(spans, key=lambda s: s["t"])
        t0 = spans_sorted[0]["t"]
        events = [
            {
                "t": round((s["t"] - t0) / 1e6),  # ns → ms relative
                "tool": _sanitize_command_display(str(s["tool"] or "")),
                "dur": s["dur"],
                "ok": s["ok"],
            }
            for s in spans_sorted[:200]  # cap per session
        ]
        result.append({"session": sid[:10], "spans": events})
    return result


def _compute_weekly_trends(activity_by_day: Counter) -> list[dict]:
    """Group daily event counts by ISO week, compute week-over-week delta."""
    import datetime as _dt
    weeks: dict[str, dict] = {}
    for day_str, count in activity_by_day.items():
        try:
            d = _dt.date.fromisoformat(day_str)
        except ValueError:
            continue
        year, week, _ = d.isocalendar()
        key = f"{year}-W{week:02d}"
        if key not in weeks:
            weeks[key] = {"week": key, "events": 0, "days_active": 0}
        weeks[key]["events"] += count
        if count > 0:
            weeks[key]["days_active"] += 1

    result = sorted(weeks.values(), key=lambda w: w["week"])
    for i, w in enumerate(result):
        prev = result[i - 1]["events"] if i > 0 else 0
        w["delta"] = w["events"] - prev
        w["delta_pct"] = round(100 * (w["events"] - prev) / prev, 1) if prev else None
    return result
