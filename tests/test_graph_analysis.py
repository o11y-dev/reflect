"""Tests for graph analysis helpers."""

from collections import Counter

from conftest import DAY1, HOUR, MIN

from reflect.core import (
    AgentStats,
    _compute_dep_graph,
    _compute_latency_histograms,
    _compute_session_timeline,
    _compute_tool_cooccurrence,
    _compute_tool_transitions,
    _compute_weekly_trends,
)


class TestToolTransitions:
    def test_basic(self):
        seq = {
            "s1": [(DAY1, "Read", True), (DAY1 + MIN, "Edit", True), (DAY1 + 2*MIN, "Bash", True)],
        }
        result = _compute_tool_transitions(seq)
        pairs = {(r["from"], r["to"]) for r in result}
        assert ("Read", "Edit") in pairs
        assert ("Edit", "Bash") in pairs

    def test_self_loops_excluded(self):
        seq = {
            "s1": [(DAY1, "Read", True), (DAY1 + MIN, "Read", True)],
        }
        result = _compute_tool_transitions(seq)
        assert not any(r["from"] == r["to"] for r in result)

    def test_counts_aggregated(self):
        seq = {
            "s1": [(DAY1, "Read", True), (DAY1 + MIN, "Edit", True)],
            "s2": [(DAY1, "Read", True), (DAY1 + MIN, "Edit", True)],
        }
        result = _compute_tool_transitions(seq)
        read_edit = next(r for r in result if r["from"] == "Read" and r["to"] == "Edit")
        assert read_edit["count"] == 2

    def test_capped_at_40(self):
        seq = {}
        for i in range(50):
            seq[f"s{i}"] = [(DAY1, f"Tool{i}", True), (DAY1 + MIN, f"Tool{i+1}", True)]
        result = _compute_tool_transitions(seq)
        assert len(result) <= 40

    def test_empty_input(self):
        assert _compute_tool_transitions({}) == []

    def test_sorted_by_count_desc(self):
        seq = {
            "s1": [(DAY1, "A", True), (DAY1 + MIN, "B", True)],
            "s2": [(DAY1, "A", True), (DAY1 + MIN, "B", True)],
            "s3": [(DAY1, "X", True), (DAY1 + MIN, "Y", True)],
        }
        result = _compute_tool_transitions(seq)
        counts = [r["count"] for r in result]
        assert counts == sorted(counts, reverse=True)


class TestToolCooccurrence:
    def test_basic_matrix(self):
        seq = {
            "s1": [(DAY1, "Read", True), (DAY1 + MIN, "Edit", True)],
            "s2": [(DAY1, "Read", True), (DAY1 + MIN, "Grep", True)],
        }
        top_tools = ["Read", "Edit", "Grep"]
        result = _compute_tool_cooccurrence(seq, top_tools)
        assert result["tools"] == top_tools
        assert len(result["matrix"]) == 3
        # Read cooccurs with Edit in s1
        read_idx = top_tools.index("Read")
        edit_idx = top_tools.index("Edit")
        assert result["matrix"][read_idx][edit_idx] >= 1

    def test_limited_to_12(self):
        top_tools = [f"Tool{i}" for i in range(20)]
        result = _compute_tool_cooccurrence({}, top_tools)
        assert len(result["tools"]) <= 12

    def test_diagonal_self_count(self):
        seq = {"s1": [(DAY1, "Read", True), (DAY1 + MIN, "Edit", True)]}
        top_tools = ["Read", "Edit"]
        result = _compute_tool_cooccurrence(seq, top_tools)
        read_idx = top_tools.index("Read")
        # Diagonal should count self-presence
        assert result["matrix"][read_idx][read_idx] >= 1

    def test_empty_sessions(self):
        result = _compute_tool_cooccurrence({}, ["Read", "Edit"])
        assert result["tools"] == ["Read", "Edit"]
        assert all(v == 0 for row in result["matrix"] for v in row)


class TestLatencyHistograms:
    def test_bucket_assignment(self):
        durations = {
            "Read": [5.0, 50.0, 500.0, 3000.0, 7000.0, 15000.0],
        }
        result = _compute_latency_histograms(durations, ["Read"])
        buckets = result["tools"]["Read"]
        # 0-10ms, 10-100ms, 100ms-1s, 1s-5s, 5s-10s, 10s+
        assert buckets[0] == 1   # 5ms
        assert buckets[1] == 1   # 50ms
        assert buckets[2] == 1   # 500ms
        assert buckets[3] == 1   # 3000ms
        assert buckets[4] == 1   # 7000ms
        assert buckets[5] == 1   # 15000ms

    def test_limited_to_10_tools(self):
        top_tools = [f"Tool{i}" for i in range(15)]
        durations = {t: [100.0] for t in top_tools}
        result = _compute_latency_histograms(durations, top_tools)
        assert len(result["tools"]) <= 10

    def test_tool_without_durations_excluded(self):
        result = _compute_latency_histograms({"Read": []}, ["Read"])
        assert "Read" not in result["tools"]

    def test_labels_present(self):
        result = _compute_latency_histograms({}, [])
        assert "labels" in result
        assert len(result["labels"]) == 6


class TestDepGraph:
    def _make_agent(self, name, tools=None, mcp=None):
        ag = AgentStats(name=name)
        if tools:
            ag.tools_by_count = Counter(tools)
        if mcp:
            ag.mcp_servers = Counter(mcp)
        return ag

    def test_nodes_and_links(self):
        agents = {
            "claude": self._make_agent("claude",
                                       tools={"Read": 5, "Edit": 3},
                                       mcp={"mcp-gitlab": 2}),
        }
        tools_by_count = Counter({"Read": 5, "Edit": 3})
        mcp_servers = Counter({"mcp-gitlab": 2})
        result = _compute_dep_graph(agents, tools_by_count, mcp_servers)
        node_ids = {n["id"] for n in result["nodes"]}
        assert "claude" in node_ids
        assert "Read" in node_ids
        # MCP servers appear in top_mcp_servers, not nodes
        assert any(s["id"] == "mcp-gitlab" for s in result["top_mcp_servers"])

    def test_empty_agents(self):
        result = _compute_dep_graph({}, Counter(), Counter())
        assert "nodes" in result
        assert "links" in result

    def test_mcp_servers_become_graph_nodes_when_mapping_exists(self):
        agents = {
            "claude": self._make_agent("claude", tools={"Read": 5}),
        }
        result = _compute_dep_graph(
            agents,
            Counter({"Read": 5}),
            Counter({"mcp-gitlab": 2}),
            session_conversation={
                "s1": [
                    {"type": "mcp_call", "tool_name": "get_issue", "server": "mcp-gitlab"},
                    {"type": "mcp_call", "tool_name": "get_issue", "server": "mcp-gitlab"},
                ]
            },
            session_agents={"s1": "claude"},
        )
        node_ids = {n["id"] for n in result["nodes"]}
        links = {(lnk["source"], lnk["target"]) for lnk in result["links"]}

        assert "get_issue" in node_ids
        assert "mcp-gitlab" in node_ids
        assert ("claude", "get_issue") in links
        assert ("get_issue", "mcp-gitlab") in links


class TestSessionTimeline:
    def test_basic(self):
        details = {
            "s1": [
                {"t": DAY1 + 10*MIN, "tool": "Read", "dur": 50.0, "ok": True, "event": "PreToolUse"},
                {"t": DAY1 + 12*MIN, "tool": "Edit", "dur": 200.0, "ok": True, "event": "PreToolUse"},
            ]
        }
        events = {"s1": 2}
        result = _compute_session_timeline(details, events)
        assert len(result) == 1
        s = result[0]
        assert s["session"] == "s1"  # key is "session" not "sid"
        # First span at t=0
        assert s["spans"][0]["t"] == 0

    def test_relative_timestamps(self):
        base = DAY1 + HOUR
        details = {
            "s1": [
                {"t": base, "tool": "Read", "dur": 50.0, "ok": True, "event": "PreToolUse"},
                {"t": base + 5*MIN, "tool": "Edit", "dur": 200.0, "ok": True, "event": "PreToolUse"},
            ]
        }
        result = _compute_session_timeline(details, {"s1": 2})
        spans = result[0]["spans"]
        assert spans[0]["t"] == 0
        # Timestamps stored as ms relative to session start
        assert spans[1]["t"] == 5*MIN // 1_000_000  # convert ns to ms

    def test_sorted_by_event_count(self):
        details = {
            "s1": [{"t": DAY1, "tool": "Read", "dur": 10.0, "ok": True, "event": "PreToolUse"}],
            "s2": [
                {"t": DAY1, "tool": "Read", "dur": 10.0, "ok": True, "event": "PreToolUse"},
                {"t": DAY1 + MIN, "tool": "Edit", "dur": 20.0, "ok": True, "event": "PreToolUse"},
                {"t": DAY1 + 2*MIN, "tool": "Grep", "dur": 5.0, "ok": True, "event": "PreToolUse"},
            ],
        }
        events = {"s1": 1, "s2": 3}
        result = _compute_session_timeline(details, events)
        assert result[0]["session"] == "s2"  # most events first

    def test_empty_input(self):
        assert _compute_session_timeline({}, {}) == []


class TestWeeklyTrends:
    def test_basic(self):
        activity = Counter({"2026-03-24": 10, "2026-03-25": 15, "2026-03-26": 12})
        result = _compute_weekly_trends(activity)
        assert len(result) >= 1
        # All same week
        assert result[0]["events"] == 37

    def test_cross_week_delta(self):
        activity = Counter({
            "2026-03-23": 10,  # week 13
            "2026-03-30": 20,  # week 14
        })
        result = _compute_weekly_trends(activity)
        assert len(result) == 2
        assert result[0]["delta_pct"] is None  # first week no prev
        assert result[1]["delta"] == 10

    def test_invalid_date_skipped(self):
        activity = Counter({"not-a-date": 5, "2026-03-24": 10})
        result = _compute_weekly_trends(activity)
        total = sum(w["events"] for w in result)
        assert total == 10  # only valid date counted

    def test_empty_input(self):
        assert _compute_weekly_trends(Counter()) == []
