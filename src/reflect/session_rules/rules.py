"""Built-in session quality rules."""
from __future__ import annotations

from reflect.utils import _safe_ratio

from .base import (
    BaseSessionRule,
    SessionRuleContext,
    SessionRuleDefinition,
    SessionRuleRegistry,
    SessionRuleResult,
    SessionRuleScorer,
)


class CompletionSessionRule(BaseSessionRule):
    definition = SessionRuleDefinition(
        id="completion",
        version=1,
        name="Completion",
        description=(
            "Full credit when the session emits a normal completion event; "
            "partial credit for subagent-only completion."
        ),
        max_points=25.0,
        signals=("Stop", "SessionEnd", "SubagentStop"),
    )

    def score(self, context: SessionRuleContext) -> SessionRuleResult:
        if context.source == "summary":
            completed = context.status in {"ok", "completed", "success"}
            return self.result(
                25.0 if completed else 0.0,
                (
                    "Session status indicates completion."
                    if completed
                    else "Session status does not indicate completion."
                ),
                {"status": context.status, "completed": completed},
            )
        if context.has_stop:
            earned = 25.0
            summary = "Found a normal session completion event."
        elif context.has_subagent_stop:
            earned = 15.0
            summary = "Found a subagent completion event, but no full session stop."
        else:
            earned = 0.0
            summary = "No completion event was found in the scored spans."
        return self.result(
            earned,
            summary,
            {
                "has_stop": context.has_stop,
                "has_subagent_stop": context.has_subagent_stop,
            },
        )


class EfficiencySessionRule(BaseSessionRule):
    definition = SessionRuleDefinition(
        id="efficiency",
        version=1,
        name="Efficiency",
        description=(
            "Penalizes high token-per-tool usage and unusually large tool counts, "
            "using local distribution thresholds when available."
        ),
        max_points=20.0,
        signals=("tokens per tool", "tool count", "session token volume"),
    )

    def score(self, context: SessionRuleContext) -> SessionRuleResult:
        earned = 20.0
        profile = context.profile
        if context.tool_uses > 0:
            tokens_per_tool = context.total_tokens / context.tool_uses
            if profile and not profile.tokens_per_tool.is_sparse():
                severe_threshold = profile.tokens_per_tool.p95
                mild_threshold = profile.tokens_per_tool.p75
            else:
                severe_threshold = 25_000.0
                mild_threshold = 10_000.0

            if tokens_per_tool > severe_threshold:
                earned -= 15.0
                summary = "Tokens per tool exceeded the severe threshold."
            elif tokens_per_tool > mild_threshold:
                earned -= 7.0
                summary = "Tokens per tool exceeded the mild threshold."
            else:
                summary = "Tokens per tool stayed within the expected range."

            if profile and not profile.session_tool_count.is_sparse():
                if profile.session_tool_count.is_outlier_high(float(context.tool_uses)):
                    earned -= 5.0
                    summary += " Tool count was also high for this local profile."
            elif context.tool_uses > 30:
                earned -= 5.0
                summary += " Tool count exceeded the cold-start threshold."
            metrics = {
                "tool_uses": context.tool_uses,
                "total_tokens": context.total_tokens,
                "tokens_per_tool": round(tokens_per_tool, 2),
                "mild_threshold": round(float(mild_threshold), 2),
                "severe_threshold": round(float(severe_threshold), 2),
            }
        else:
            summary = "No tool calls were present; scoring used total token volume."
            if profile and not profile.session_total_tokens.is_sparse():
                if profile.session_total_tokens.is_outlier_high(float(context.total_tokens)):
                    earned -= 10.0
                    summary = (
                        "No tool calls were present and total tokens were high "
                        "for this local profile."
                    )
            elif context.total_tokens > 50_000:
                earned -= 10.0
                summary = "No tool calls were present and total tokens exceeded 50k."
            elif context.total_tokens > 20_000:
                earned -= 5.0
                summary = "No tool calls were present and total tokens exceeded 20k."
            metrics = {
                "tool_uses": context.tool_uses,
                "total_tokens": context.total_tokens,
            }
            if context.source == "summary":
                metrics.update(
                    {
                        "tokens_per_tool": 0.0,
                        "mild_threshold": 10_000,
                        "severe_threshold": 25_000,
                    }
                )
        return self.result(earned, summary, metrics)


class ToolReliabilitySessionRule(BaseSessionRule):
    definition = SessionRuleDefinition(
        id="tool_reliability",
        version=1,
        name="Tool reliability",
        description=(
            "Rewards clean tool execution and scales down as failed tool calls "
            "exceed the expected local failure rate."
        ),
        max_points=15.0,
        signals=("PostToolUseFailure", "tool failure rate"),
    )

    def score(self, context: SessionRuleContext) -> SessionRuleResult:
        profile = context.profile
        if context.tool_uses > 0:
            failure_rate = context.failures / context.tool_uses
            if profile and not profile.session_failure_count.is_sparse():
                normal_rate = _safe_ratio(
                    profile.session_failure_count.median,
                    profile.session_tool_count.median,
                )
                threshold = max(normal_rate * 3.0, 0.10)
            else:
                threshold = 0.15

            if failure_rate > threshold:
                earned = max(0.0, 15.0 - failure_rate * 100)
                summary = "Failure rate exceeded the threshold."
            elif context.failures == 0:
                earned = 15.0
                summary = "No failed tool calls were observed."
            else:
                earned = 15.0 * (1.0 - failure_rate / threshold)
                summary = "Failures were present but stayed under the threshold."
            metrics = {
                "failures": context.failures,
                "tool_uses": context.tool_uses,
                "failure_rate": round(failure_rate, 4),
                "threshold": round(float(threshold), 4),
            }
        else:
            earned = 15.0
            summary = "No tool calls were present, so no tool failures were observed."
            metrics = {
                "failures": context.failures,
                "tool_uses": context.tool_uses,
            }
            if context.source == "summary":
                metrics.update({"failure_rate": 0.0, "threshold": 0.15})
        return self.result(earned, summary, metrics)


class LoopDetectionSessionRule(BaseSessionRule):
    definition = SessionRuleDefinition(
        id="loop_detection",
        version=1,
        name="Loop detection",
        description=(
            "Penalizes repeated use of the same tool in adjacent steps, which "
            "usually indicates stalled exploration or retry loops."
        ),
        max_points=10.0,
        signals=("repeated consecutive tool calls",),
    )

    def score(self, context: SessionRuleContext) -> SessionRuleResult:
        if context.consecutive_pairs is None or context.consecutive_triples is None:
            return self.result(
                10.0,
                "No repeated-tool loop signal was present in the session summary.",
                {"tool_sequence_available": False},
            )
        penalty = min(
            10.0,
            context.consecutive_pairs * 2.0 + context.consecutive_triples * 1.0,
        )
        return self.result(
            10.0 - penalty,
            (
                "Repeated consecutive tool calls reduced this score."
                if penalty
                else "No repeated consecutive tool loops were detected."
            ),
            {
                "consecutive_pairs": context.consecutive_pairs,
                "consecutive_triples": context.consecutive_triples,
                "penalty": round(penalty, 2),
            },
        )


class DurationHealthSessionRule(BaseSessionRule):
    definition = SessionRuleDefinition(
        id="duration_health",
        version=1,
        name="Duration health",
        description=(
            "Gives partial credit when timing data is sparse, penalizes very "
            "short sessions and long outliers."
        ),
        max_points=10.0,
        signals=("session span timestamps",),
    )

    def score(self, context: SessionRuleContext) -> SessionRuleResult:
        if not context.timing_available:
            metrics: dict[str, object]
            if context.source == "summary":
                metrics = {"duration_ms": context.duration_ms}
            else:
                metrics = {"timestamp_count": context.timestamp_count or 0}
            return self.result(
                5.0,
                "Only partial timing data was available.",
                metrics,
            )

        earned = 10.0
        summary = "Duration stayed within the expected range."
        profile = context.profile
        if context.duration_ms < 30_000:
            earned -= 3.0
            summary = "Session was very short, so duration health was reduced."
        elif profile and not profile.session_duration_ms.is_sparse():
            if profile.session_duration_ms.is_outlier_high(context.duration_ms):
                excess = context.duration_ms - profile.session_duration_ms.upper_fence()
                max_excess = (
                    profile.session_duration_ms.max_val
                    - profile.session_duration_ms.upper_fence()
                )
                if max_excess > 0:
                    earned -= 7.0 * min(1.0, excess / max_excess)
                else:
                    earned -= 3.0
                summary = "Session duration was high for this local profile."
        elif context.duration_ms > 1_800_000:
            earned -= 5.0
            summary = "Session exceeded the 30 minute cold-start duration threshold."

        metrics = {"duration_ms": round(context.duration_ms, 1)}
        if context.source == "spans":
            metrics["timestamp_count"] = context.timestamp_count or 0
        return self.result(earned, summary, metrics)


class ErrorRecoverySessionRule(BaseSessionRule):
    definition = SessionRuleDefinition(
        id="error_recovery",
        version=1,
        name="Error recovery",
        description=(
            "Rewards sessions that recover after failed tool calls; sessions "
            "without failures receive baseline credit."
        ),
        max_points=10.0,
        signals=("failure followed by successful PostToolUse",),
    )

    def score(self, context: SessionRuleContext) -> SessionRuleResult:
        if context.failures == 0:
            earned = 7.0
            summary = "No failures were observed, so recovery gets baseline credit."
        elif context.recovered > 0:
            earned = 10.0 * min(1.0, context.recovered / context.failures)
            summary = "Failures were followed by successful tool results."
        else:
            earned = 0.0
            summary = "Failures were observed without a matching successful recovery."
        return self.result(
            earned,
            summary,
            {"failures": context.failures, "recovered": context.recovered},
        )


class ToolDiversitySessionRule(BaseSessionRule):
    definition = SessionRuleDefinition(
        id="tool_diversity",
        version=1,
        name="Tool diversity",
        description=(
            "Rewards sessions that use a reasonable mix of tools instead of a "
            "single repeated action."
        ),
        max_points=5.0,
        signals=("distinct tools",),
    )

    def score(self, context: SessionRuleContext) -> SessionRuleResult:
        if context.distinct_tools is None:
            return self.result(
                0.0,
                "Distinct per-session tool count was not present in the session summary.",
                {"distinct_tools_available": False},
            )
        if context.distinct_tools >= 5:
            earned = 5.0
        elif context.distinct_tools >= 3:
            earned = 3.0
        elif context.distinct_tools >= 1:
            earned = 1.0
        else:
            earned = 0.0
        return self.result(
            earned,
            f"Observed {context.distinct_tools} distinct tool(s).",
            {"distinct_tools": context.distinct_tools},
        )


class EditProductivitySessionRule(BaseSessionRule):
    definition = SessionRuleDefinition(
        id="edit_productivity",
        version=1,
        name="Edit productivity",
        description=(
            "Rewards sessions that convert exploration into edits, using the "
            "edit-to-read ratio when available."
        ),
        max_points=5.0,
        signals=("AfterFileEdit", "BeforeReadFile"),
    )

    def score(self, context: SessionRuleContext) -> SessionRuleResult:
        if context.edits is None or context.reads is None:
            return self.result(
                0.0,
                "Read/edit productivity events were not present in the session summary.",
                {"edit_events_available": False},
            )
        if context.edits > 0:
            if context.reads > 0:
                edit_ratio = context.edits / context.reads
                earned = 5.0 * min(1.0, edit_ratio / 0.5)
                summary = "Edits were scored against the edit-to-read ratio."
            else:
                earned = 5.0
                summary = "Edits were present without read-heavy exploration."
        elif context.reads > 0:
            earned = 1.0
            summary = "Reads were present, but no edit events were captured."
        else:
            earned = 0.0
            summary = "No read or edit productivity signal was captured."
        metrics: dict[str, object] = {
            "edits": context.edits,
            "reads": context.reads,
        }
        if context.reads > 0:
            metrics["edit_to_read_ratio"] = round(context.edits / context.reads, 4)
        return self.result(earned, summary, metrics)


DEFAULT_SESSION_RULES = (
    CompletionSessionRule(),
    EfficiencySessionRule(),
    ToolReliabilitySessionRule(),
    LoopDetectionSessionRule(),
    DurationHealthSessionRule(),
    ErrorRecoverySessionRule(),
    ToolDiversitySessionRule(),
    EditProductivitySessionRule(),
)
DEFAULT_SESSION_RULE_REGISTRY = SessionRuleRegistry(DEFAULT_SESSION_RULES)
DEFAULT_SESSION_RULE_SCORER = SessionRuleScorer(DEFAULT_SESSION_RULE_REGISTRY)


__all__ = [
    "CompletionSessionRule",
    "DEFAULT_SESSION_RULE_REGISTRY",
    "DEFAULT_SESSION_RULE_SCORER",
    "DEFAULT_SESSION_RULES",
    "DurationHealthSessionRule",
    "EditProductivitySessionRule",
    "EfficiencySessionRule",
    "ErrorRecoverySessionRule",
    "LoopDetectionSessionRule",
    "ToolDiversitySessionRule",
    "ToolReliabilitySessionRule",
]
