"""Unit tests for skill-extraction helper functions."""

from __future__ import annotations

import sqlite3
from collections import Counter

from reflect.core import (
    _build_graph_evidence,
    _build_skill_evidence_bundle,
    _build_skill_evidence_bundle_from_sql,
    _build_skills_extraction_prompt,
    _build_skills_extraction_prompt_from_bundle,
    _compress_tool_sequence,
    _extract_recovery_chains,
    _serialize_sessions_for_skills,
)
from reflect.models import TelemetryStats

# ---------------------------------------------------------------------------
# _compress_tool_sequence
# ---------------------------------------------------------------------------

class TestCompressToolSequence:
    def test_empty(self):
        assert _compress_tool_sequence([]) == []

    def test_single(self):
        assert _compress_tool_sequence(["Read"]) == ["Read"]

    def test_no_repeats(self):
        assert _compress_tool_sequence(["Read", "Grep", "Edit"]) == ["Read", "Grep", "Edit"]

    def test_consecutive_pair(self):
        assert _compress_tool_sequence(["Read", "Read"]) == ["Read×2"]

    def test_mixed_repeats(self):
        result = _compress_tool_sequence(["Read", "Read", "Read", "Grep", "Edit", "Bash", "Bash"])
        assert result == ["Read×3", "Grep", "Edit", "Bash×2"]

    def test_single_repeat_not_annotated(self):
        result = _compress_tool_sequence(["Read", "Grep", "Grep", "Edit"])
        assert result == ["Read", "Grep×2", "Edit"]

    def test_all_same(self):
        result = _compress_tool_sequence(["Bash"] * 5)
        assert result == ["Bash×5"]

    def test_alternating(self):
        result = _compress_tool_sequence(["A", "B", "A", "B"])
        assert result == ["A", "B", "A", "B"]


# ---------------------------------------------------------------------------
# _extract_recovery_chains
# ---------------------------------------------------------------------------

class TestExtractRecoveryChains:
    def test_empty(self):
        assert _extract_recovery_chains([]) == []

    def test_no_failures(self):
        spans = [
            {"tool": "Read", "ok": True, "event": "PreToolUse", "t": 1},
            {"tool": "Edit", "ok": True, "event": "PreToolUse", "t": 2},
        ]
        assert _extract_recovery_chains(spans) == []

    def test_basic_recovery(self):
        spans = [
            {"tool": "Bash", "ok": False, "event": "PostToolUseFailure", "t": 1},
            {"tool": "Read", "ok": True, "event": "PreToolUse", "t": 2},
        ]
        result = _extract_recovery_chains(spans)
        assert result == ["Bash✗→Read"]

    def test_skips_non_actionable_after_failure(self):
        """A Stop span between the failure and recovery should be skipped."""
        spans = [
            {"tool": "Bash", "ok": False, "event": "PostToolUseFailure", "t": 1},
            {"tool": "",     "ok": True,  "event": "Stop",              "t": 2},
            {"tool": "Read", "ok": True,  "event": "PreToolUse",        "t": 3},
        ]
        result = _extract_recovery_chains(spans)
        assert result == ["Bash✗→Read"]

    def test_sorts_by_timestamp(self):
        """Out-of-order spans are sorted before chain extraction."""
        spans = [
            {"tool": "Read", "ok": True,  "event": "PreToolUse",        "t": 30},
            {"tool": "Grep", "ok": True,  "event": "PreToolUse",        "t": 10},
            {"tool": "Bash", "ok": False, "event": "PostToolUseFailure", "t": 20},
        ]
        result = _extract_recovery_chains(spans)
        # Bash failed at t=20, next actionable is Read at t=30
        assert result == ["Bash✗→Read"]

    def test_no_actionable_after_failure(self):
        """Failure at end of session with no actionable follow-up."""
        spans = [
            {"tool": "Bash", "ok": False, "event": "PostToolUseFailure", "t": 1},
            {"tool": "",     "ok": True,  "event": "Stop",              "t": 2},
        ]
        assert _extract_recovery_chains(spans) == []

    def test_shell_execution_counts_as_actionable(self):
        spans = [
            {"tool": "Edit", "ok": False, "event": "PostToolUseFailure",    "t": 1},
            {"tool": "sh",   "ok": True,  "event": "BeforeShellExecution",  "t": 2},
        ]
        result = _extract_recovery_chains(spans)
        assert result == ["Edit✗→sh"]

    def test_mcp_execution_counts_as_actionable(self):
        spans = [
            {"tool": "query", "ok": False, "event": "PostToolUseFailure",   "t": 1},
            {"tool": "query", "ok": True,  "event": "BeforeMCPExecution",   "t": 2},
        ]
        result = _extract_recovery_chains(spans)
        assert result == ["query✗→query"]

    def test_missing_tool_name_skipped(self):
        """Spans without a tool name on the failure side are ignored."""
        spans = [
            {"tool": "",     "ok": False, "event": "PostToolUseFailure", "t": 1},
            {"tool": "Read", "ok": True,  "event": "PreToolUse",        "t": 2},
        ]
        assert _extract_recovery_chains(spans) == []

    def test_spans_without_t_sorted_last(self):
        """Spans missing 't' are placed after all timestamped spans."""
        spans = [
            {"tool": "Read", "ok": True,  "event": "PreToolUse",        },  # no t
            {"tool": "Bash", "ok": False, "event": "PostToolUseFailure", "t": 1},
            {"tool": "Grep", "ok": True,  "event": "PreToolUse",        "t": 2},
        ]
        result = _extract_recovery_chains(spans)
        assert result == ["Bash✗→Grep"]

    def test_multiple_recoveries(self):
        spans = [
            {"tool": "Bash", "ok": False, "event": "PostToolUseFailure", "t": 1},
            {"tool": "Read", "ok": True,  "event": "PreToolUse",        "t": 2},
            {"tool": "Edit", "ok": False, "event": "PostToolUseFailure", "t": 3},
            {"tool": "Grep", "ok": True,  "event": "PreToolUse",        "t": 4},
        ]
        result = _extract_recovery_chains(spans)
        assert result == ["Bash✗→Read", "Edit✗→Grep"]


# ---------------------------------------------------------------------------
# _serialize_sessions_for_skills
# ---------------------------------------------------------------------------

def _make_stats(
    *,
    session_id: str = "test-session-abc",
    tool_seq: list | None = None,
    shell_cmds: Counter | None = None,
    conv: list | None = None,
    spans: list | None = None,
    events: int = 10,
    tokens_in: int = 500,
    tokens_out: int = 200,
    quality: float = 72.0,
    completed: bool = True,
    recovered: int = 0,
) -> TelemetryStats:
    """Build a minimal TelemetryStats for serialization tests."""
    sid = session_id
    stats = TelemetryStats(
        session_files=1,
        span_files=0,
        total_events=events,
        events_by_type=Counter(),
        events_by_file={},
    )
    stats.sessions_seen = {sid}
    stats.session_events = {sid: events}
    stats.session_models = {sid: Counter({"claude-sonnet": 1})}
    stats.session_tokens = {sid: {"input": tokens_in, "output": tokens_out}}
    stats.session_tool_seq = {sid: tool_seq or []}
    stats.session_shell_commands = {sid: shell_cmds} if shell_cmds else {}
    stats.session_conversation = {sid: conv or []}
    stats.session_span_details = {sid: spans or []}
    stats.session_quality_scores = {sid: quality}
    stats.session_goal_completed = {sid: completed}
    stats.session_recovered_failures = {sid: recovered} if recovered else {}
    stats.sessions_with_telemetry = {sid}
    return stats


class TestSerializeSessionsForSkills:
    def test_basic_metadata_present(self):
        stats = _make_stats(events=42, tokens_in=1000, tokens_out=500)
        output = _serialize_sessions_for_skills(stats)
        assert "test-ses" in output  # first 8 chars of session id
        assert "events=42" in output
        assert "tokens=1500" in output

    def test_tool_flow_sorted_by_timestamp(self):
        # Out-of-order timestamps — should produce Read → Edit, not Edit → Read
        stats = _make_stats(tool_seq=[
            (200, "Edit", True),
            (100, "Read", True),
        ])
        output = _serialize_sessions_for_skills(stats)
        assert "tool_flow=Read → Edit" in output

    def test_tool_flow_compressed(self):
        stats = _make_stats(tool_seq=[
            (100, "Read", True),
            (200, "Read", True),
            (300, "Grep", True),
        ])
        output = _serialize_sessions_for_skills(stats)
        assert "tool_flow=Read×2 → Grep" in output

    def test_shell_cmds_present(self):
        stats = _make_stats(shell_cmds=Counter({"pytest tests/": 3, "git commit": 2}))
        output = _serialize_sessions_for_skills(stats)
        assert "shell_cmds=" in output
        assert "pytest tests/" in output

    def test_prompts_normalized_single_line(self):
        """Newlines in prompt previews must be collapsed to spaces."""
        stats = _make_stats(conv=[
            {"type": "prompt", "preview": "Fix the\nfailing test"},
        ])
        output = _serialize_sessions_for_skills(stats)
        assert "\n  prompts=" not in output or "Fix the failing test" in output
        # The key requirement: no literal newline inside a prompt snippet
        for line in output.splitlines():
            if "prompts=" in line:
                assert "\n" not in line

    def test_prompts_truncated_to_80_chars(self):
        long_prompt = "A" * 100
        stats = _make_stats(conv=[{"type": "prompt", "preview": long_prompt}])
        output = _serialize_sessions_for_skills(stats)
        # Should be present but capped at 80 chars
        assert "A" * 80 in output
        assert "A" * 81 not in output

    def test_prompts_at_most_3_shown(self):
        conv = [{"type": "prompt", "preview": f"prompt {i}"} for i in range(5)]
        stats = _make_stats(conv=conv)
        output = _serialize_sessions_for_skills(stats)
        # prompts field should contain at most 3 separated by " / "
        for line in output.splitlines():
            if "prompts=" in line:
                assert line.count(" / ") <= 2

    def test_error_recovery_present(self):
        spans = [
            {"tool": "Bash", "ok": False, "event": "PostToolUseFailure", "t": 1},
            {"tool": "Read", "ok": True,  "event": "PreToolUse",        "t": 2},
        ]
        stats = _make_stats(spans=spans)
        output = _serialize_sessions_for_skills(stats)
        assert "error_recovery=Bash✗→Read" in output

    def test_no_tool_flow_when_empty(self):
        stats = _make_stats(tool_seq=[])
        output = _serialize_sessions_for_skills(stats)
        assert "tool_flow" not in output

    def test_top_12_sessions_only(self):
        """Only up to the bounded skill-session limit should appear."""
        stats = TelemetryStats(
            session_files=1, span_files=0, total_events=25,
            events_by_type=Counter(), events_by_file={},
        )
        sids = [f"sess-{i:03d}" for i in range(25)]
        stats.sessions_seen = set(sids)
        stats.session_events = {sid: i for i, sid in enumerate(sids)}
        stats.session_models = {sid: Counter({"claude": 1}) for sid in sids}
        stats.session_tokens = {sid: {"input": 10, "output": 5} for sid in sids}
        stats.session_tool_seq = {}
        stats.session_shell_commands = {}
        stats.session_conversation = {}
        stats.session_span_details = {}
        stats.session_quality_scores = dict.fromkeys(sids, 50.0)
        stats.session_goal_completed = dict.fromkeys(sids, True)
        output = _serialize_sessions_for_skills(stats)
        session_lines = [ln for ln in output.splitlines() if ln.startswith("Session ")]
        assert len(session_lines) == 12


class TestBuildSkillEvidenceBundle:
    def test_bundle_includes_scores_and_targets(self):
        spans = [
            {"tool": "Read", "ok": True, "event": "PreToolUse", "t": 1},
            {"tool": "Read", "ok": True, "event": "PreToolUse", "t": 2},
            {"tool": "Bash", "ok": False, "event": "PostToolUseFailure", "t": 3},
            {"tool": "Read", "ok": True, "event": "PreToolUse", "t": 4},
        ]
        stats = _make_stats(
            tool_seq=[(1, "Read", True), (2, "Read", True), (3, "Bash", False), (4, "Read", True)],
            conv=[{"type": "prompt", "preview": "Fix the flaky CLI flow"}],
            spans=spans,
            quality=41.0,
            completed=False,
            recovered=1,
        )

        bundle = _build_skill_evidence_bundle(stats)

        assert bundle["schema_version"] == 1
        assert bundle["sessions"][0]["quality_score"] == 41.0
        assert bundle["sessions"][0]["goal_completed"] is False
        assert bundle["sessions"][0]["score_signals"]["tool_failures"] == 1
        assert any(
            target["kind"] == "reliability"
            for target in bundle["sessions"][0]["improvement_targets"]
        )

    def test_bundle_adds_deep_context_for_selected_session(self):
        spans = [
            {"tool": "Read", "ok": True, "event": "PreToolUse", "t": 1, "dur": 10.0},
            {"tool": "Bash", "ok": False, "event": "PostToolUseFailure", "t": 2, "dur": 20.0},
        ]
        stats = _make_stats(
            conv=[
                {"type": "prompt", "preview": "Investigate auth timeouts"},
                {"type": "tool_call", "preview": "cat auth.py", "tool_name": "Read"},
                {"type": "response", "preview": "I found the bug", "model": "claude-4.5"},
            ],
            spans=spans,
            quality=35.0,
            completed=False,
        )

        bundle = _build_skill_evidence_bundle(stats)

        deep_context = bundle["sessions"][0]["deep_context"]
        assert deep_context["conversation"][0]["type"] == "prompt"
        assert deep_context["spans"][0]["event"] == "PreToolUse"

    def test_bundle_tracks_recurring_patterns_across_sessions(self):
        stats = TelemetryStats(
            session_files=1,
            span_files=0,
            total_events=4,
            events_by_type=Counter(),
            events_by_file={},
        )
        for sid in ("sess-a", "sess-b"):
            stats.sessions_seen.add(sid)
            stats.session_events[sid] = 20
            stats.session_models[sid] = Counter({"claude": 1})
            stats.session_tokens[sid] = {"input": 100, "output": 50}
            stats.session_tool_seq[sid] = [(1, "Read", True), (2, "Grep", True)]
            stats.session_shell_commands[sid] = Counter({"pytest tests/": 1})
            stats.session_conversation[sid] = [{"type": "prompt", "preview": "Fix tests"}]
            stats.session_span_details[sid] = [{"tool": "Read", "ok": True, "event": "PreToolUse", "t": 1}]
            stats.session_quality_scores[sid] = 80.0
            stats.session_goal_completed[sid] = True

        bundle = _build_skill_evidence_bundle(stats)

        assert bundle["summary"]["recurring_tool_flows"][0]["count"] == 2
        assert bundle["summary"]["recurring_shell_commands"][0]["value"] == "pytest tests/"


class TestBuildSkillsExtractionPrompt:
    def test_prompt_contains_authoritative_json_bundle(self):
        stats = _make_stats(conv=[{"type": "prompt", "preview": "Fix the CLI"}])

        prompt = _build_skills_extraction_prompt("Base prompt", stats)

        assert "Evidence summary:" in prompt
        assert "Evidence JSON (authoritative):" in prompt
        assert '"schema_version": 1' in prompt
        assert "session://test-session-abc" in prompt

    def test_prompt_from_bundle_keeps_graph_evidence(self):
        bundle = {
            "schema_version": 1,
            "summary": {
                "included_sessions": 0,
                "deep_context_sessions": 0,
                "average_quality_score": 0.0,
                "recurring_tool_flows": [],
                "recurring_shell_commands": [],
                "recurring_recovery_chains": [],
                "recurring_improvement_targets": [],
            },
            "sessions": [],
            "graph_evidence": {
                "source": "sql-graph",
                "scoped_session_count": 2,
                "recurring_patterns": [
                    {
                        "id": "graph-01",
                        "edge_kind": "used_skill",
                        "source": {"kind": "Session", "label": "sess-a"},
                        "target": {"kind": "Skill", "label": "reflect-skills"},
                        "count": 3,
                        "session_support": 2,
                        "session_ids": ["sess-a", "sess-b"],
                    }
                ],
                "skill_clusters": [],
                "subagent_clusters": [],
            },
        }

        prompt = _build_skills_extraction_prompt_from_bundle("Base prompt", bundle)

        assert '"graph_evidence"' in prompt
        assert '"edge_kind": "used_skill"' in prompt
        assert "Graph recurring patterns:" in prompt


class TestBuildGraphEvidence:
    def test_graph_evidence_extracts_recurring_patterns(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE graph_nodes (
              id TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              label TEXT NOT NULL,
              session_id TEXT,
              first_seen_at TEXT,
              last_seen_at TEXT,
              attrs_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE graph_edges (
              id TEXT PRIMARY KEY,
              source_node_id TEXT NOT NULL,
              target_node_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              session_id TEXT,
              weight REAL NOT NULL DEFAULT 1,
              first_seen_at TEXT,
              last_seen_at TEXT,
              attrs_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        rows_nodes = [
            ("session-a", "Session", "sess-a", "sess-a"),
            ("session-b", "Session", "sess-b", "sess-b"),
            ("skill-rs", "Skill", "reflect-skills", None),
            ("subagent-review", "Subagent", "code-review", "sess-a"),
        ]
        for node_id, kind, label, session_id in rows_nodes:
            conn.execute(
                """
                INSERT INTO graph_nodes(id, kind, label, session_id, attrs_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, '{}', '2026-05-28T00:00:00+00:00', '2026-05-28T00:00:00+00:00')
                """,
                (node_id, kind, label, session_id),
            )

        rows_edges = [
            ("edge-1", "session-a", "skill-rs", "used_skill", "sess-a"),
            ("edge-2", "session-b", "skill-rs", "used_skill", "sess-b"),
            ("edge-3", "session-a", "subagent-review", "spawned_subagent", "sess-a"),
            ("edge-4", "session-b", "subagent-review", "spawned_subagent", "sess-b"),
        ]
        for edge_id, source, target, kind, session_id in rows_edges:
            conn.execute(
                """
                INSERT INTO graph_edges(id, source_node_id, target_node_id, kind, session_id, attrs_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, '{}', '2026-05-28T00:00:00+00:00', '2026-05-28T00:00:00+00:00')
                """,
                (edge_id, source, target, kind, session_id),
            )
        conn.commit()

        evidence = _build_graph_evidence(conn, session_ids={"sess-a", "sess-b"})

        assert evidence["source"] == "sql-graph"
        assert evidence["scoped_session_count"] == 2
        assert evidence["recurring_patterns"]
        assert any(item["name"] == "reflect-skills" for item in evidence["skill_clusters"])


class TestBuildSkillEvidenceBundleFromSql:
    def test_sql_bundle_includes_stats_and_graph(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute(
            """
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY,
              agent_id TEXT,
              status TEXT,
              quality_score REAL,
              recovered_failure_count INTEGER,
              input_tokens INTEGER,
              output_tokens INTEGER,
              started_at TEXT,
              created_at TEXT
            )
            """
        )
        conn.execute("CREATE TABLE steps (id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, type TEXT, summary TEXT, raw_attrs_json TEXT)")
        conn.execute(
            """
            CREATE TABLE tool_calls (
              id TEXT PRIMARY KEY,
              step_id TEXT,
              session_id TEXT,
              tool_name TEXT,
              status TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE llm_calls (
              id TEXT PRIMARY KEY,
              session_id TEXT,
              prompt_preview_redacted TEXT,
              response_model TEXT,
              request_model TEXT,
              created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE graph_nodes (
              id TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              label TEXT NOT NULL,
              session_id TEXT,
              first_seen_at TEXT,
              last_seen_at TEXT,
              attrs_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE graph_edges (
              id TEXT PRIMARY KEY,
              source_node_id TEXT NOT NULL,
              target_node_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              session_id TEXT,
              weight REAL NOT NULL DEFAULT 1,
              first_seen_at TEXT,
              last_seen_at TEXT,
              attrs_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )

        conn.execute("INSERT INTO agents(id, name) VALUES ('agent-a', 'Claude Code')")
        conn.execute(
            """
            INSERT INTO sessions(id, agent_id, status, quality_score, recovered_failure_count, input_tokens, output_tokens, started_at, created_at)
            VALUES
              ('sess-a', 'agent-a', 'ok', 82, 1, 300, 200, '2026-05-28T00:00:00+00:00', '2026-05-28T00:00:00+00:00'),
              ('sess-b', 'agent-a', 'ok', 79, 0, 200, 100, '2026-05-28T01:00:00+00:00', '2026-05-28T01:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO steps(id, session_id, seq, type, summary, raw_attrs_json)
            VALUES
              ('step-a1', 'sess-a', 1, 'shell_command', 'pytest -q', '{"gen_ai.client.command":"pytest -q"}'),
              ('step-a2', 'sess-a', 2, 'tool_call', 'read', '{}'),
              ('step-a3', 'sess-a', 3, 'tool_call', 'edit', '{}'),
              ('step-b1', 'sess-b', 1, 'tool_call', 'read', '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO tool_calls(id, step_id, session_id, tool_name, status)
            VALUES
              ('tc-a1', 'step-a2', 'sess-a', 'Read', 'ok'),
              ('tc-a2', 'step-a3', 'sess-a', 'Edit', 'error'),
              ('tc-b1', 'step-b1', 'sess-b', 'Read', 'ok')
            """
        )
        conn.execute(
            """
            INSERT INTO llm_calls(id, session_id, prompt_preview_redacted, response_model, request_model, created_at)
            VALUES
              ('llm-a', 'sess-a', 'Fix failing test', 'claude-sonnet', 'claude-sonnet', '2026-05-28T00:00:00+00:00'),
              ('llm-b', 'sess-b', 'Investigate flaky spec', 'claude-sonnet', 'claude-sonnet', '2026-05-28T01:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO graph_nodes(id, kind, label, session_id, attrs_json, created_at, updated_at)
            VALUES
              ('n-sa', 'Session', 'sess-a', 'sess-a', '{}', '2026-05-28T00:00:00+00:00', '2026-05-28T00:00:00+00:00'),
              ('n-sb', 'Session', 'sess-b', 'sess-b', '{}', '2026-05-28T00:00:00+00:00', '2026-05-28T00:00:00+00:00'),
              ('n-skill', 'Skill', 'reflect-skills', NULL, '{}', '2026-05-28T00:00:00+00:00', '2026-05-28T00:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO graph_edges(id, source_node_id, target_node_id, kind, session_id, attrs_json, created_at, updated_at)
            VALUES
              ('ge-1', 'n-sa', 'n-skill', 'used_skill', 'sess-a', '{}', '2026-05-28T00:00:00+00:00', '2026-05-28T00:00:00+00:00'),
              ('ge-2', 'n-sb', 'n-skill', 'used_skill', 'sess-b', '{}', '2026-05-28T00:00:00+00:00', '2026-05-28T00:00:00+00:00')
            """
        )
        conn.commit()

        bundle = _build_skill_evidence_bundle_from_sql(conn, session_ids={"sess-a", "sess-b"})

        assert bundle is not None
        assert bundle["selection_policy"]["evidence_source"] == "sql"
        assert bundle["sessions"]
        assert bundle["graph_evidence"]["source"] == "sql-graph"
        assert "graph_evidence" in bundle
