"""Unit tests for skill-extraction helper functions.

Covers _compress_tool_sequence, _extract_recovery_chains, and
_serialize_sessions_for_skills to prevent regressions in the
trace-fingerprint serialization format.
"""

from __future__ import annotations

from collections import Counter

from reflect.core import (
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

    def test_top_20_sessions_only(self):
        """Only up to 20 sessions should appear in the output."""
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
        output = _serialize_sessions_for_skills(stats)
        session_lines = [ln for ln in output.splitlines() if ln.startswith("Session ")]
        assert len(session_lines) == 20
