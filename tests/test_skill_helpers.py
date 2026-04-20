"""Unit tests for skill-extraction helper functions."""

from __future__ import annotations

from collections import Counter

from reflect.core import (
    _build_skill_evidence_bundle,
    _build_skills_extraction_prompt,
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
