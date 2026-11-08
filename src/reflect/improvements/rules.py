from __future__ import annotations

import math
import re
import sqlite3
import statistics
from collections import defaultdict

from reflect.improvements.analysis_scope import AnalysisScope
from reflect.improvements.base import (
    BaseImprovementRule,
    RuleRegistry,
    scope_for_repository,
    severity_for_impact,
    stable_fingerprint,
)
from reflect.improvements.models import (
    EvidenceRef,
    ObservationDraft,
    ObservationSessionRef,
    RuleDefinition,
    WorkflowBehaviorType,
    WorkflowDefinition,
    WorkflowProposal,
)
from reflect.improvements.session_insights import session_insight_rules

_stable_fingerprint = stable_fingerprint
_scope = scope_for_repository
_severity = severity_for_impact


def _qualified_workflow_slug(base: str, qualifier: str) -> str:
    """Keep rule-generated workflow names readable and collision-resistant."""

    normalized = re.sub(r"[^a-z0-9]+", "-", qualifier.strip().lower()).strip("-") or "tool"
    digest = _stable_fingerprint(qualifier)[:6]
    available = max(1, 63 - len(base) - len(digest) - 2)
    return f"{base}-{normalized[:available].rstrip('-')}-{digest}"


class RepeatedToolFailureRule(BaseImprovementRule):
    definition = RuleDefinition(
        id="repeated_tool_failure_chain",
        version=1,
        category="tool_failure",
        title="Repeated tool failure chains",
        description="Finds tools that fail repeatedly across comparable repository sessions.",
        detector_config={"minimum_failures": 2, "minimum_sessions": 2},
        required_signals=["tool_calls.status", "tool_calls.error_type"],
    )
    workflow = WorkflowDefinition(
        slug="tool-failure-recovery",
        behavior_type=WorkflowBehaviorType.RECOVERY,
        steps=[
            "Reproduce the first failure with the smallest safe command.",
            "Classify the failure before retrying: input, environment, dependency, or product defect.",
            "Change one relevant condition and record why it should affect the result.",
            "Run the bounded command again and stop after a repeated unchanged failure.",
            "Verify the intended postcondition, not only the tool exit status.",
        ],
    )

    def propose(self, finding: ObservationDraft) -> WorkflowProposal | None:
        """Name each tool-specific recovery separately and measure repeated retries."""

        proposal = super().propose(finding)
        if proposal is None:
            return None
        tool_name = str(finding.baseline_query.get("tool_name") or "tool")
        slug = _qualified_workflow_slug(self.workflow.slug, tool_name)
        return proposal.model_copy(
            update={
                "hypothesis": (
                    f"A reviewed {slug} workflow will reduce identical retries after "
                    f"{tool_name} failures. {finding.summary}"
                ),
                "content": {**proposal.content, "slug": slug},
                "target_metric": "identical_retry_calls",
                "target_value": None,
            }
        )

    def detect(self, conn: sqlite3.Connection, *, scope: AnalysisScope | None = None) -> list[ObservationDraft]:
        scope = scope or AnalysisScope()
        rows = conn.execute(
            f"""
            WITH tool_totals AS (
              SELECT s.repo_id,
                     lower(trim(tc.tool_name)) AS tool_key,
                     MIN(tc.tool_name) AS tool_name,
                     COUNT(*) AS total_calls
              FROM tool_calls tc
              JOIN {scope.sessions} s ON s.id = tc.session_id
              GROUP BY s.repo_id, lower(trim(tc.tool_name))
            ), failed_tools AS (
              SELECT s.repo_id,
                     lower(trim(tc.tool_name)) AS tool_key,
                     MIN(tc.tool_name) AS tool_name,
                     COUNT(*) AS failures,
                     COUNT(DISTINCT tc.session_id) AS affected_sessions
              FROM tool_calls tc
              JOIN {scope.sessions} s ON s.id = tc.session_id
              WHERE lower(COALESCE(tc.status, '')) IN ('error', 'failed', 'failure')
                 OR NULLIF(tc.error_type, '') IS NOT NULL
              GROUP BY s.repo_id, lower(trim(tc.tool_name))
              HAVING COUNT(*) >= 2 AND COUNT(DISTINCT tc.session_id) >= 2
            )
            SELECT f.repo_id, f.tool_key, f.tool_name,
                   f.failures, f.affected_sessions, t.total_calls
            FROM failed_tools f
            JOIN tool_totals t
              ON t.tool_key = f.tool_key
             AND COALESCE(t.repo_id, '') = COALESCE(f.repo_id, '')
            ORDER BY f.failures DESC, f.tool_name
            """
        ).fetchall()
        findings: list[ObservationDraft] = []
        for repo_id, tool_key, tool_name, failures, affected_sessions, total_calls in rows:
            source_rows = conn.execute(
                f"""
                SELECT tc.session_id, COUNT(*) AS failures
                FROM tool_calls tc
                JOIN {scope.sessions} s ON s.id = tc.session_id
                WHERE lower(trim(tc.tool_name)) = ?
                  AND COALESCE(s.repo_id, '') = COALESCE(?, '')
                  AND (
                    lower(COALESCE(tc.status, '')) IN ('error', 'failed', 'failure')
                    OR NULLIF(tc.error_type, '') IS NOT NULL
                  )
                GROUP BY tc.session_id
                ORDER BY failures DESC, tc.session_id
                """,
                (tool_key, repo_id),
            ).fetchall()
            evidence_rows = conn.execute(
                f"""
                SELECT tc.id, tc.session_id, tc.step_id, tc.status, tc.error_type
                FROM tool_calls tc
                JOIN {scope.sessions} s ON s.id = tc.session_id
                WHERE lower(trim(tc.tool_name)) = ?
                  AND COALESCE(s.repo_id, '') = COALESCE(?, '')
                  AND (
                    lower(COALESCE(tc.status, '')) IN ('error', 'failed', 'failure')
                    OR NULLIF(tc.error_type, '') IS NOT NULL
                )
                ORDER BY tc.created_at DESC
                LIMIT 20
                """,
                (tool_key, repo_id),
            ).fetchall()
            rate = float(failures) / max(1, int(total_calls))
            impact = min(
                100.0,
                18.0
                + min(32.0, math.log2(float(failures) + 1.0) * 5.0)
                + min(30.0, float(affected_sessions) * 2.5)
                + min(20.0, rate * 160.0),
            )
            scope_type, scope_id = _scope(repo_id)
            findings.append(
                ObservationDraft(
                    rule_id=self.definition.id,
                    rule_version=self.definition.version,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    repo_id=repo_id,
                    fingerprint=_stable_fingerprint(tool_key),
                    category=self.definition.category,
                    title=f"Repeated {tool_name} failures",
                    summary=(
                        f"{failures} failed {tool_name} calls across {affected_sessions} session(s); "
                        f"the observed failure rate is {rate:.1%}."
                    ),
                    metric_name="tool_failure_rate",
                    metric_value=rate,
                    metric_unit="ratio",
                    metric_direction="lower_is_better",
                    baseline_value=rate,
                    baseline_query={"repo_id": repo_id, "tool_name": tool_name},
                    impact_score=impact,
                    severity=_severity(impact),
                    confidence=min(0.95, 0.52 + affected_sessions * 0.06 + min(0.12, rate)),
                    occurrence_count=failures,
                    affected_session_count=affected_sessions,
                    evidence=[
                        EvidenceRef(
                            entity_type="tool_call",
                            entity_id=row[0],
                            tool_call_id=row[0],
                            session_id=row[1],
                            step_id=row[2],
                            summary_redacted=(
                                f"{tool_name} ended with status {row[3] or 'unknown'}"
                                + (f" ({row[4]})" if row[4] else "")
                            ),
                        )
                        for row in evidence_rows
                    ],
                    source_sessions=[
                        ObservationSessionRef(
                            session_id=str(row[0]),
                            occurrence_count=int(row[1]),
                            summary_redacted=(
                                f"Session contains {int(row[1])} failed {tool_name} call(s)"
                            ),
                            focus_entity_type="tool",
                            focus_entity_id=str(tool_name),
                        )
                        for row in source_rows
                    ],
                )
            )
        return findings


class RetryLoopRule(BaseImprovementRule):
    definition = RuleDefinition(
        id="retry_loop_without_state_change",
        version=1,
        category="reasoning",
        title="Retry loops without state change",
        description="Finds identical tool inputs repeated at least three times in one session.",
        detector_config={"minimum_identical_calls": 3},
        required_signals=["tool_calls.input_hash"],
    )
    # Loop evidence is promoted explicitly through ``reflect loops build``.
    # Detecting repetition alone must not create a workflow or skill candidate.
    workflow = None

    def detect(self, conn: sqlite3.Connection, *, scope: AnalysisScope | None = None) -> list[ObservationDraft]:
        scope = scope or AnalysisScope()
        rows = conn.execute(
            f"""
            SELECT s.repo_id, tc.session_id, tc.tool_name, tc.input_hash,
                   COUNT(*) AS call_count
            FROM tool_calls tc
            JOIN {scope.sessions} s ON s.id = tc.session_id
            WHERE NULLIF(tc.input_hash, '') IS NOT NULL
            GROUP BY s.repo_id, tc.session_id, tc.tool_name, tc.input_hash
            HAVING COUNT(*) >= 3
            ORDER BY call_count DESC, tc.tool_name, tc.session_id
            """
        ).fetchall()
        grouped: dict[tuple[str | None, str], list[tuple]] = defaultdict(list)
        display_names: dict[tuple[str | None, str], str] = {}
        for repo_id, session_id, tool_name, input_hash, call_count in rows:
            tool_key = str(tool_name).strip().lower()
            group_key = (repo_id, tool_key)
            display_names.setdefault(group_key, str(tool_name))
            grouped[group_key].append(
                (session_id, tool_name, input_hash, int(call_count))
            )
        findings: list[ObservationDraft] = []
        ordered_groups = sorted(
            grouped.items(),
            key=lambda item: (-sum(row[3] for row in item[1]), item[0][1]),
        )
        for (repo_id, tool_key), loop_rows in ordered_groups:
            tool_name = display_names[(repo_id, tool_key)]
            loop_count = len(loop_rows)
            repeated_calls = sum(row[3] for row in loop_rows)
            affected_sessions = len({row[0] for row in loop_rows})
            evidence_rows = loop_rows[:20]
            session_occurrences: dict[str, int] = defaultdict(int)
            for session_id, _tool_name, _input_hash, call_count in loop_rows:
                session_occurrences[str(session_id)] += int(call_count)
            impact = min(100.0, 40.0 + repeated_calls * 4.0 + affected_sessions * 6.0)
            scope_type, scope_id = _scope(repo_id)
            findings.append(
                ObservationDraft(
                    rule_id=self.definition.id,
                    rule_version=self.definition.version,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    repo_id=repo_id,
                    fingerprint=_stable_fingerprint(tool_key),
                    category=self.definition.category,
                    title=f"{tool_name} retries repeat without a changed input",
                    summary=(
                        f"{repeated_calls} calls form {loop_count} identical-input retry loop(s) "
                        f"across {affected_sessions} session(s)."
                    ),
                    metric_name="identical_retry_calls",
                    metric_value=float(repeated_calls),
                    metric_unit="calls",
                    metric_direction="lower_is_better",
                    baseline_value=float(repeated_calls),
                    baseline_query={"repo_id": repo_id, "tool_name": tool_key},
                    impact_score=impact,
                    severity=_severity(impact),
                    confidence=min(0.96, 0.7 + affected_sessions * 0.05),
                    occurrence_count=repeated_calls,
                    affected_session_count=affected_sessions,
                    evidence=[
                        EvidenceRef(
                            entity_type="retry_group",
                            entity_id=_stable_fingerprint(*row[:3]),
                            session_id=row[0],
                            summary_redacted=(
                                f"{row[1]} used the same redacted input hash {row[3]} times"
                            ),
                            attrs={"input_hash": row[2], "call_count": row[3]},
                        )
                        for row in evidence_rows[:20]
                    ],
                    source_sessions=[
                        ObservationSessionRef(
                            session_id=session_id,
                            occurrence_count=call_count,
                            summary_redacted=(
                                f"Session contains {call_count} identical-input retry call(s)"
                            ),
                            focus_entity_type="tool",
                            focus_entity_id=tool_key,
                        )
                        for session_id, call_count in sorted(
                            session_occurrences.items(),
                            key=lambda item: (-item[1], item[0]),
                        )
                    ],
                )
            )
        return findings


class MissingVerificationRule(BaseImprovementRule):
    definition = RuleDefinition(
        id="missing_or_late_verification",
        version=1,
        category="verification",
        title="Missing verification after changes",
        description="Finds completed sessions with write activity and no visible verification command.",
        detector_config={"verification_terms": ["test", "pytest", "ruff", "lint", "build", "compile"]},
        required_signals=["tool_calls.tool_name", "tool_calls.input_preview_redacted"],
    )
    workflow = WorkflowDefinition(
        slug="verify-before-done",
        behavior_type=WorkflowBehaviorType.VERIFICATION,
        steps=[
            "List the files and behavior changed in this task.",
            "Choose the narrowest relevant test, lint, compile, or schema check.",
            "Run the focused check before reporting completion.",
            "Run the broader repository gate when the change crosses module boundaries.",
            "Report exact validation commands and any remaining unverified risk.",
        ],
    )

    def detect(self, conn: sqlite3.Connection, *, scope: AnalysisScope | None = None) -> list[ObservationDraft]:
        scope = scope or AnalysisScope()
        rows = conn.execute(
            f"""
            WITH classified AS (
              SELECT s.id AS session_id, s.repo_id,
                     SUM(CASE WHEN lower(tc.tool_name) GLOB '*write*'
                                  OR lower(tc.tool_name) GLOB '*edit*'
                                  OR lower(tc.tool_name) GLOB '*patch*'
                              THEN 1 ELSE 0 END) AS writes,
                     SUM(CASE WHEN lower(tc.tool_name) GLOB '*test*'
                                  OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*pytest*'
                                  OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*ruff*'
                                  OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '* lint*'
                                  OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '* test*'
                                  OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*build*'
                                  OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*compile*'
                              THEN 1 ELSE 0 END) AS verification_calls
              FROM {scope.sessions} s
              JOIN tool_calls tc ON tc.session_id = s.id
              WHERE lower(COALESCE(s.status, '')) IN ('completed', 'ok', 'success', 'unknown')
              GROUP BY s.id, s.repo_id
            )
            SELECT repo_id, COUNT(*) AS affected_sessions, SUM(writes) AS writes
            FROM classified
            WHERE writes > 0 AND verification_calls = 0
            GROUP BY repo_id
            ORDER BY affected_sessions DESC
            """
        ).fetchall()
        findings: list[ObservationDraft] = []
        for repo_id, affected_sessions, writes in rows:
            evidence_rows = conn.execute(
                f"""
                WITH classified AS (
                  SELECT s.id AS session_id,
                         SUM(CASE WHEN lower(tc.tool_name) GLOB '*write*'
                                      OR lower(tc.tool_name) GLOB '*edit*'
                                      OR lower(tc.tool_name) GLOB '*patch*'
                                  THEN 1 ELSE 0 END) AS writes,
                         SUM(CASE WHEN lower(tc.tool_name) GLOB '*test*'
                                      OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*pytest*'
                                      OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*ruff*'
                                      OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '* lint*'
                                      OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '* test*'
                                      OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*build*'
                                      OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*compile*'
                                  THEN 1 ELSE 0 END) AS verification_calls
                  FROM {scope.sessions} s
                  JOIN tool_calls tc ON tc.session_id = s.id
                  WHERE COALESCE(s.repo_id, '') = COALESCE(?, '')
                    AND lower(COALESCE(s.status, '')) IN ('completed', 'ok', 'success', 'unknown')
                  GROUP BY s.id
                )
                SELECT session_id, writes FROM classified
                WHERE writes > 0 AND verification_calls = 0
                ORDER BY writes DESC
                """,
                (repo_id,),
            ).fetchall()
            impact = min(100.0, 30.0 + affected_sessions * 9.0 + writes * 2.0)
            scope_type, scope_id = _scope(repo_id)
            findings.append(
                ObservationDraft(
                    rule_id=self.definition.id,
                    rule_version=self.definition.version,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    repo_id=repo_id,
                    fingerprint=_stable_fingerprint("missing-verification"),
                    category=self.definition.category,
                    title="Changes finish without visible verification",
                    summary=(
                        f"{affected_sessions} completed session(s) made {writes} write/edit call(s) "
                        "without a visible test, lint, build, or compile command."
                    ),
                    metric_name="unverified_change_sessions",
                    metric_value=float(affected_sessions),
                    metric_unit="sessions",
                    metric_direction="lower_is_better",
                    baseline_value=float(affected_sessions),
                    baseline_query={"repo_id": repo_id},
                    impact_score=impact,
                    severity=_severity(impact),
                    confidence=min(0.88, 0.52 + affected_sessions * 0.06),
                    occurrence_count=affected_sessions,
                    affected_session_count=affected_sessions,
                    evidence=[
                        EvidenceRef(
                            entity_type="session",
                            entity_id=row[0],
                            session_id=row[0],
                            summary_redacted=f"Session contains {row[1]} write/edit call(s) and no visible verification call",
                            confidence=0.8,
                        )
                        for row in evidence_rows[:20]
                    ],
                    source_sessions=[
                        ObservationSessionRef(
                            session_id=str(row[0]),
                            occurrence_count=int(row[1]),
                            summary_redacted=(
                                f"Session contains {int(row[1])} write/edit call(s) "
                                "and no visible verification call"
                            ),
                            focus_entity_type="session",
                            focus_entity_id=str(row[0]),
                        )
                        for row in evidence_rows
                    ],
                )
            )
        return findings


class ContextExplosionRule(BaseImprovementRule):
    definition = RuleDefinition(
        id="context_explosion",
        version=3,
        category="context",
        title="Context explosion",
        description="Finds sessions whose token volume is a robust outlier within the same repository.",
        detector_config={"minimum_tokens": 250000, "median_multiplier": 2.5},
        required_signals=["sessions.input_tokens", "sessions.output_tokens", "sessions.cache_read_tokens", "sessions.cache_creation_tokens"],
    )
    workflow = WorkflowDefinition(
        slug="context-budget-checkpoints",
        behavior_type=WorkflowBehaviorType.EXPLORATION,
        steps=[
            "Define the goal, bounded file set, output, and done criteria before exploration.",
            "Summarize evidence at each milestone and drop raw output that is no longer needed.",
            "Split implementation from verification when the context crosses a task boundary.",
            "Start a fresh session with a concise handoff when the current context becomes noisy.",
        ],
    )

    def detect(self, conn: sqlite3.Connection, *, scope: AnalysisScope | None = None) -> list[ObservationDraft]:
        scope = scope or AnalysisScope()
        rows = conn.execute(
            f"""
            SELECT id, repo_id,
                   input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens
                   AS total_tokens
            FROM {scope.sessions}
            WHERE input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens
                  > 0
            ORDER BY repo_id, total_tokens
            """
        ).fetchall()
        by_repo: dict[str | None, list[tuple[str, int]]] = defaultdict(list)
        for session_id, repo_id, tokens in rows:
            by_repo[repo_id].append((session_id, int(tokens)))

        findings: list[ObservationDraft] = []
        for repo_id, sessions in by_repo.items():
            values = [tokens for _, tokens in sessions]
            if len(values) < 3:
                continue
            median = float(statistics.median(values))
            threshold = max(250_000.0, median * 2.5)
            outliers = [(session_id, tokens) for session_id, tokens in sessions if tokens >= threshold]
            if not outliers:
                continue
            total = sum(tokens for _, tokens in outliers)
            impact = min(100.0, 38.0 + len(outliers) * 7.0 + total / 250_000.0)
            scope_type, scope_id = _scope(repo_id)
            outlier_ratio = len(outliers) / len(sessions)
            title = (
                "A small set of sessions consumes runaway context"
                if outlier_ratio <= 0.1
                else "Many sessions consume unusually large context"
            )
            findings.append(
                ObservationDraft(
                    rule_id=self.definition.id,
                    rule_version=self.definition.version,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    repo_id=repo_id,
                    fingerprint=_stable_fingerprint("context-outliers"),
                    category=self.definition.category,
                    title=title,
                    summary=(
                        f"{len(outliers)} session(s) exceed the {threshold:,.0f}-token robust threshold; "
                        f"the repository median is {median:,.0f}."
                    ),
                    metric_name="context_outlier_sessions",
                    metric_value=float(len(outliers)),
                    metric_unit="sessions",
                    metric_direction="lower_is_better",
                    baseline_value=median,
                    baseline_query={"repo_id": repo_id, "method": "median_x_2.5"},
                    impact_score=impact,
                    severity=_severity(impact),
                    confidence=min(0.94, 0.58 + len(values) * 0.025),
                    occurrence_count=len(outliers),
                    affected_session_count=len(outliers),
                    evidence=[
                        EvidenceRef(
                            entity_type="session",
                            entity_id=session_id,
                            session_id=session_id,
                            summary_redacted=f"Session used {tokens:,} observed tokens including cache",
                            attrs={"total_tokens": tokens, "threshold": threshold},
                        )
                        for session_id, tokens in sorted(
                            outliers,
                            key=lambda item: item[1],
                            reverse=True,
                        )[:20]
                    ],
                    source_sessions=[
                        ObservationSessionRef(
                            session_id=session_id,
                            summary_redacted=f"Session used {tokens:,} observed tokens including cache",
                            focus_entity_type="session",
                            focus_entity_id=session_id,
                            attrs={"total_tokens": tokens, "threshold": threshold},
                        )
                        for session_id, tokens in outliers
                    ],
                )
            )
        return findings


class RepeatedExplorationRule(BaseImprovementRule):
    definition = RuleDefinition(
        id="repeated_exploration_known_paths",
        version=1,
        category="reasoning",
        title="Repeated exploration without a state change",
        description="Finds read-heavy sessions with no visible write or patch action.",
        detector_config={"minimum_reads": 8},
        required_signals=["tool_calls.tool_name"],
    )
    workflow = WorkflowDefinition(
        slug="map-then-probe",
        behavior_type=WorkflowBehaviorType.EXPLORATION,
        steps=[
            "Map likely entry points with one bounded file or symbol search.",
            "State the current hypothesis and the one missing fact needed to test it.",
            "Read only the smallest regions that can confirm or reject that hypothesis.",
            "After repeated reads, summarize findings and choose an edit, test, or explicit stop.",
        ],
    )

    def detect(self, conn: sqlite3.Connection, *, scope: AnalysisScope | None = None) -> list[ObservationDraft]:
        scope = scope or AnalysisScope()
        rows = conn.execute(
            f"""
            WITH classified AS (
              SELECT s.id AS session_id, s.repo_id,
                     SUM(CASE WHEN lower(tc.tool_name) GLOB '*read*'
                                  OR lower(tc.tool_name) GLOB '*find*'
                                  OR lower(tc.tool_name) GLOB '*search*'
                                  OR lower(tc.tool_name) GLOB '*grep*'
                                  OR lower(tc.tool_name) GLOB '*glob*'
                              THEN 1 ELSE 0 END) AS reads,
                     SUM(CASE WHEN lower(tc.tool_name) GLOB '*write*'
                                  OR lower(tc.tool_name) GLOB '*edit*'
                                  OR lower(tc.tool_name) GLOB '*patch*'
                              THEN 1 ELSE 0 END) AS writes
              FROM {scope.sessions} s
              JOIN tool_calls tc ON tc.session_id = s.id
              GROUP BY s.id, s.repo_id
            )
            SELECT repo_id, COUNT(*) AS affected_sessions, SUM(reads) AS reads
            FROM classified
            WHERE reads >= 8 AND writes = 0
            GROUP BY repo_id
            ORDER BY reads DESC
            """
        ).fetchall()
        findings: list[ObservationDraft] = []
        for repo_id, affected_sessions, reads in rows:
            evidence_rows = conn.execute(
                f"""
                WITH classified AS (
                  SELECT s.id AS session_id,
                         SUM(CASE WHEN lower(tc.tool_name) GLOB '*read*'
                                      OR lower(tc.tool_name) GLOB '*find*'
                                      OR lower(tc.tool_name) GLOB '*search*'
                                      OR lower(tc.tool_name) GLOB '*grep*'
                                      OR lower(tc.tool_name) GLOB '*glob*'
                                  THEN 1 ELSE 0 END) AS reads,
                         SUM(CASE WHEN lower(tc.tool_name) GLOB '*write*'
                                      OR lower(tc.tool_name) GLOB '*edit*'
                                      OR lower(tc.tool_name) GLOB '*patch*'
                                  THEN 1 ELSE 0 END) AS writes
                  FROM {scope.sessions} s
                  JOIN tool_calls tc ON tc.session_id = s.id
                  WHERE COALESCE(s.repo_id, '') = COALESCE(?, '')
                  GROUP BY s.id
                )
                SELECT session_id, reads FROM classified
                WHERE reads >= 8 AND writes = 0
                ORDER BY reads DESC
                """,
                (repo_id,),
            ).fetchall()
            impact = min(100.0, 28.0 + affected_sessions * 8.0 + reads * 1.2)
            scope_type, scope_id = _scope(repo_id)
            findings.append(
                ObservationDraft(
                    rule_id=self.definition.id,
                    rule_version=self.definition.version,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    repo_id=repo_id,
                    fingerprint=_stable_fingerprint("read-heavy-no-write"),
                    category=self.definition.category,
                    title="Exploration repeats without a visible state change",
                    summary=(
                        f"{affected_sessions} session(s) made {reads} read/search calls and no visible write action."
                    ),
                    metric_name="read_only_exploration_calls",
                    metric_value=float(reads),
                    metric_unit="calls",
                    metric_direction="lower_is_better",
                    baseline_value=float(reads),
                    baseline_query={"repo_id": repo_id, "minimum_reads": 8},
                    impact_score=impact,
                    severity=_severity(impact),
                    confidence=min(0.82, 0.48 + affected_sessions * 0.05),
                    occurrence_count=reads,
                    affected_session_count=affected_sessions,
                    evidence=[
                        EvidenceRef(
                            entity_type="session",
                            entity_id=row[0],
                            session_id=row[0],
                            summary_redacted=f"Session made {row[1]} read/search calls",
                            confidence=0.7,
                        )
                        for row in evidence_rows[:20]
                    ],
                    source_sessions=[
                        ObservationSessionRef(
                            session_id=str(row[0]),
                            occurrence_count=int(row[1]),
                            summary_redacted=(
                                f"Session made {int(row[1])} read/search calls"
                            ),
                            focus_entity_type="session",
                            focus_entity_id=str(row[0]),
                        )
                        for row in evidence_rows
                    ],
                )
            )
        return findings


class UserCorrectionRule(BaseImprovementRule):
    definition = RuleDefinition(
        id="user_correction_after_completion",
        version=1,
        category="outcome",
        title="User correction after completion",
        description="Finds repositories with repeated operator-confirmed corrections.",
        detector_config={"minimum_corrections": 2},
        required_signals=["session_outcomes.outcome", "sessions.status"],
    )
    workflow = WorkflowDefinition(
        slug="confirm-outcome-before-done",
        behavior_type=WorkflowBehaviorType.VERIFICATION,
        steps=[
            "Restate the requested outcome and the observable completion criteria.",
            "Verify the resulting behavior rather than relying on tool exit status.",
            "Compare the result with the original constraints before claiming completion.",
            "Report remaining uncertainty and request confirmation when intent is ambiguous.",
        ],
    )

    def detect(self, conn: sqlite3.Connection, *, scope: AnalysisScope | None = None) -> list[ObservationDraft]:
        scope = scope or AnalysisScope()
        rows = conn.execute(
            f"""
            SELECT s.repo_id, COUNT(*) AS corrections, COUNT(DISTINCT so.session_id) AS sessions
            FROM session_outcomes so
            JOIN {scope.sessions} s ON s.id = so.session_id
            WHERE so.outcome = 'corrected'
              AND lower(COALESCE(s.status, '')) IN ('completed', 'ok', 'success', 'unknown')
            GROUP BY s.repo_id
            HAVING COUNT(*) >= 2
            ORDER BY corrections DESC
            """
        ).fetchall()
        findings: list[ObservationDraft] = []
        for repo_id, corrections, affected_sessions in rows:
            source_rows = conn.execute(
                f"""
                SELECT so.session_id, COUNT(*) AS corrections
                FROM session_outcomes so
                JOIN {scope.sessions} s ON s.id = so.session_id
                WHERE so.outcome = 'corrected'
                  AND COALESCE(s.repo_id, '') = COALESCE(?, '')
                GROUP BY so.session_id
                ORDER BY corrections DESC, so.session_id
                """,
                (repo_id,),
            ).fetchall()
            evidence_rows = conn.execute(
                f"""
                SELECT so.session_id, so.source
                FROM session_outcomes so JOIN {scope.sessions} s ON s.id = so.session_id
                WHERE so.outcome = 'corrected'
                  AND COALESCE(s.repo_id, '') = COALESCE(?, '')
                ORDER BY so.updated_at DESC
                LIMIT 20
                """,
                (repo_id,),
            ).fetchall()
            impact = min(100.0, 45.0 + affected_sessions * 10.0)
            scope_type, scope_id = _scope(repo_id)
            findings.append(
                ObservationDraft(
                    rule_id=self.definition.id,
                    rule_version=self.definition.version,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    repo_id=repo_id,
                    fingerprint=_stable_fingerprint("operator-corrections"),
                    category=self.definition.category,
                    title="Completed work repeatedly requires operator correction",
                    summary=(
                        f"{corrections} explicit correction outcome(s) affected "
                        f"{affected_sessions} completed session(s)."
                    ),
                    metric_name="operator_correction_rate",
                    metric_value=float(corrections),
                    metric_unit="sessions",
                    metric_direction="lower_is_better",
                    baseline_value=float(corrections),
                    baseline_query={"repo_id": repo_id, "outcome": "corrected"},
                    impact_score=impact,
                    severity=_severity(impact),
                    confidence=min(0.98, 0.8 + affected_sessions * 0.03),
                    occurrence_count=corrections,
                    affected_session_count=affected_sessions,
                    evidence=[
                        EvidenceRef(
                            entity_type="session_outcome",
                            entity_id=f"{row[0]}:{row[1]}",
                            session_id=row[0],
                            summary_redacted="Operator explicitly marked the completed session as corrected",
                        )
                        for row in evidence_rows
                    ],
                    source_sessions=[
                        ObservationSessionRef(
                            session_id=str(row[0]),
                            occurrence_count=int(row[1]),
                            summary_redacted=(
                                "Operator explicitly marked the completed session as corrected"
                            ),
                            focus_entity_type="session_outcome",
                            focus_entity_id=str(row[0]),
                        )
                        for row in source_rows
                    ],
                )
            )
        return findings


class IgnoredConstraintRule(BaseImprovementRule):
    definition = RuleDefinition(
        id="constraint_or_instruction_ignored",
        version=1,
        category="policy",
        title="Constraint or instruction ignored",
        description="Finds repeated permission, sandbox, approval, or policy violations.",
        detector_config={"minimum_violations": 2},
        required_signals=["tool_calls.error_type", "tool_calls.error_message_redacted"],
    )
    workflow = WorkflowDefinition(
        slug="constraint-preflight",
        behavior_type=WorkflowBehaviorType.VERIFICATION,
        steps=[
            "List repository, sandbox, approval, and operator constraints before acting.",
            "Classify each planned mutation by scope and required authority.",
            "Stop before a denied boundary instead of retrying the same action.",
            "Ask for explicit approval when the required authority is missing.",
        ],
    )

    def detect(self, conn: sqlite3.Connection, *, scope: AnalysisScope | None = None) -> list[ObservationDraft]:
        scope = scope or AnalysisScope()
        match = """
          lower(COALESCE(tc.error_type, '')) GLOB '*permission*'
          OR lower(COALESCE(tc.error_type, '')) GLOB '*sandbox*'
          OR lower(COALESCE(tc.error_type, '')) GLOB '*policy*'
          OR lower(COALESCE(tc.error_type, '')) GLOB '*approval*'
          OR lower(COALESCE(tc.error_message_redacted, '')) GLOB '*permission denied*'
          OR lower(COALESCE(tc.error_message_redacted, '')) GLOB '*not allowed*'
        """
        rows = conn.execute(
            f"""
            SELECT s.repo_id, COUNT(*) AS violations, COUNT(DISTINCT tc.session_id) AS sessions
            FROM tool_calls tc JOIN {scope.sessions} s ON s.id = tc.session_id
            WHERE {match}
            GROUP BY s.repo_id
            HAVING COUNT(*) >= 2
            ORDER BY violations DESC
            """
        ).fetchall()
        findings: list[ObservationDraft] = []
        for repo_id, violations, affected_sessions in rows:
            source_rows = conn.execute(
                f"""
                SELECT tc.session_id, COUNT(*) AS violations
                FROM tool_calls tc
                JOIN {scope.sessions} s ON s.id = tc.session_id
                WHERE COALESCE(s.repo_id, '') = COALESCE(?, '') AND ({match})
                GROUP BY tc.session_id
                ORDER BY violations DESC, tc.session_id
                """,
                (repo_id,),
            ).fetchall()
            evidence_rows = conn.execute(
                f"""
                SELECT tc.id, tc.session_id, tc.step_id, tc.error_type
                FROM tool_calls tc JOIN {scope.sessions} s ON s.id = tc.session_id
                WHERE COALESCE(s.repo_id, '') = COALESCE(?, '') AND ({match})
                ORDER BY tc.created_at DESC
                LIMIT 20
                """,
                (repo_id,),
            ).fetchall()
            impact = min(100.0, 42.0 + violations * 7.0 + affected_sessions * 5.0)
            scope_type, scope_id = _scope(repo_id)
            findings.append(
                ObservationDraft(
                    rule_id=self.definition.id,
                    rule_version=self.definition.version,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    repo_id=repo_id,
                    fingerprint=_stable_fingerprint("constraint-violations"),
                    category=self.definition.category,
                    title="Tool actions repeatedly cross an enforced boundary",
                    summary=(
                        f"{violations} permission, sandbox, approval, or policy violation(s) "
                        f"occurred across {affected_sessions} session(s)."
                    ),
                    metric_name="constraint_violation_rate",
                    metric_value=float(violations),
                    metric_unit="calls",
                    metric_direction="lower_is_better",
                    baseline_value=float(violations),
                    baseline_query={"repo_id": repo_id},
                    impact_score=impact,
                    severity=_severity(impact),
                    confidence=min(0.94, 0.68 + affected_sessions * 0.05),
                    occurrence_count=violations,
                    affected_session_count=affected_sessions,
                    evidence=[
                        EvidenceRef(
                            entity_type="tool_call",
                            entity_id=row[0],
                            tool_call_id=row[0],
                            session_id=row[1],
                            step_id=row[2],
                            summary_redacted=f"Tool call hit an enforced boundary ({row[3] or 'policy'})",
                        )
                        for row in evidence_rows
                    ],
                    source_sessions=[
                        ObservationSessionRef(
                            session_id=str(row[0]),
                            occurrence_count=int(row[1]),
                            summary_redacted=(
                                f"Session hit {int(row[1])} enforced boundary violation(s)"
                            ),
                            focus_entity_type="policy",
                            focus_entity_id="constraint-violations",
                        )
                        for row in source_rows
                    ],
                )
            )
        return findings


class SuccessfulRecoveryRule(BaseImprovementRule):
    definition = RuleDefinition(
        id="successful_recovery_sequence",
        version=1,
        category="workflow",
        title="Successful recovery sequence worth preserving",
        description="Finds repositories with repeated recovered failures in completed sessions.",
        detector_config={"minimum_recovered_sessions": 2},
        required_signals=["sessions.recovered_failure_count", "sessions.status"],
    )
    workflow = WorkflowDefinition(
        slug="preserve-recovery-sequence",
        behavior_type=WorkflowBehaviorType.RECOVERY,
        steps=[
            "Capture the first failure and the hypothesis that explained it.",
            "Record the smallest state change that enabled recovery.",
            "Repeat the focused verification that confirmed the recovery.",
            "Reuse the sequence only when the same failure class and preconditions match.",
        ],
    )

    def detect(self, conn: sqlite3.Connection, *, scope: AnalysisScope | None = None) -> list[ObservationDraft]:
        scope = scope or AnalysisScope()
        rows = conn.execute(
            f"""
            SELECT repo_id, COUNT(*) AS sessions, SUM(recovered_failure_count) AS recoveries
            FROM {scope.sessions}
            WHERE recovered_failure_count > 0
              AND lower(COALESCE(status, '')) IN ('completed', 'ok', 'success')
            GROUP BY repo_id HAVING COUNT(*) >= 2
            ORDER BY recoveries DESC
            """
        ).fetchall()
        findings: list[ObservationDraft] = []
        for repo_id, affected_sessions, recoveries in rows:
            evidence_rows = conn.execute(
                f"""
                SELECT id, recovered_failure_count FROM {scope.sessions}
                WHERE COALESCE(repo_id, '') = COALESCE(?, '')
                  AND recovered_failure_count > 0
                  AND lower(COALESCE(status, '')) IN ('completed', 'ok', 'success')
                ORDER BY recovered_failure_count DESC
                """,
                (repo_id,),
            ).fetchall()
            impact = min(80.0, 25.0 + recoveries * 5.0 + affected_sessions * 4.0)
            scope_type, scope_id = _scope(repo_id)
            findings.append(
                ObservationDraft(
                    rule_id=self.definition.id,
                    rule_version=self.definition.version,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    repo_id=repo_id,
                    fingerprint=_stable_fingerprint("successful-recovery"),
                    category=self.definition.category,
                    title="Successful recovery behavior repeats across sessions",
                    summary=(
                        f"{affected_sessions} completed session(s) recovered from "
                        f"{recoveries} recorded failure(s)."
                    ),
                    metric_name="recovered_failure_sessions",
                    metric_value=float(affected_sessions),
                    metric_unit="sessions",
                    metric_direction="higher_is_better",
                    baseline_value=float(affected_sessions),
                    baseline_query={"repo_id": repo_id},
                    impact_score=impact,
                    severity=_severity(impact),
                    confidence=min(0.9, 0.62 + affected_sessions * 0.05),
                    occurrence_count=recoveries,
                    affected_session_count=affected_sessions,
                    evidence=[
                        EvidenceRef(
                            entity_type="session",
                            entity_id=row[0],
                            session_id=row[0],
                            summary_redacted=f"Completed session recovered {row[1]} failure(s)",
                        )
                        for row in evidence_rows[:20]
                    ],
                    source_sessions=[
                        ObservationSessionRef(
                            session_id=str(row[0]),
                            occurrence_count=int(row[1]),
                            summary_redacted=(
                                f"Completed session recovered {int(row[1])} failure(s)"
                            ),
                            focus_entity_type="session",
                            focus_entity_id=str(row[0]),
                        )
                        for row in evidence_rows
                    ],
                )
            )
        return findings


class HighPerformingWorkflowRule(BaseImprovementRule):
    definition = RuleDefinition(
        id="high_performing_repeated_workflow",
        version=3,
        category="workflow",
        title="Repeated procedure with observed checks",
        description="Finds scoped procedures with repeatable check evidence and preserves counterexamples.",
        detector_config={"minimum_source_sessions": 3, "minimum_checked_ratio": 0.6,
                         "maximum_execution_units": 300},
        required_signals=["execution_units.boundary_json", "execution_unit_steps.step_id"],
    )
    workflow = None

    def detect(self, conn: sqlite3.Connection, *, scope: AnalysisScope | None = None) -> list[ObservationDraft]:
        from reflect.improvements.procedures import ProcedureEvidenceRepository, procedure_contract

        scope = scope or AnalysisScope()
        bundle = ProcedureEvidenceRepository(conn).bundle(execution_table=scope.execution_units)
        findings = []
        for procedure in bundle["procedures"]:
            if not procedure["candidate_ready"]:
                continue
            signature = procedure_contract(procedure).signature_hash
            if conn.execute("""SELECT 1 FROM skill_installations si
                JOIN skill_versions sv ON sv.id=si.skill_version_id
                WHERE si.status='active'
                  AND json_extract(sv.workflow_json,'$.workflow_contract.signature_hash')=?
                LIMIT 1""", (signature,)).fetchone():
                continue
            roles = procedure["milestone_roles"]
            examples = [(example, polarity) for key, polarity in
                        (("supporting_examples", "supporting"), ("counterexamples", "counterexample"))
                        for example in procedure[key]]
            checked = procedure["verification_counts"].get("passed", 0)
            scope_type, scope_id = _scope(procedure["repo_id"])
            findings.append(ObservationDraft(
                rule_id=self.definition.id, rule_version=self.definition.version,
                scope_type=scope_type, scope_id=scope_id, repo_id=procedure["repo_id"],
                fingerprint=signature, category="workflow",
                title=f"A repeated {procedure['task_archetype_id']} procedure has check evidence",
                summary=(f"{checked}/{procedure['execution_count']} matching executions have current passing checks "
                         f"across {procedure['session_count']} sessions. Task outcomes remain separately assessed."),
                metric_name="procedure_checked_ratio", metric_value=procedure["checked_ratio"],
                metric_unit="ratio", metric_direction="higher_is_better",
                baseline_value=procedure["checked_ratio"],
                baseline_query={"repo_id": procedure["repo_id"], "workspace_id": procedure["workspace_id"],
                    "task_archetype_id": procedure["task_archetype_id"], "milestone_roles": roles,
                    "signature_hash": signature, "procedure": procedure, "selection": bundle["selection"]},
                impact_score=min(75, 30 + procedure["session_count"] * 5), severity=_severity(50),
                confidence=min(.9, .55 + len(procedure["checked_session_ids"]) * .05),
                occurrence_count=procedure["execution_count"], affected_session_count=procedure["session_count"],
                evidence=[EvidenceRef(entity_type="execution_unit", entity_id=example["execution_unit_id"],
                    session_id=example["session_id"], polarity=polarity,
                    summary_redacted=f"Matching operations and artifact scope; checks {example['verification']}; outcome {example['outcome']}.",
                    attrs=example) for example, polarity in examples],
                source_sessions=[ObservationSessionRef(session_id=sid,
                    summary_redacted="Contained a matching scoped procedure; check and outcome evidence are separate.")
                    for sid in procedure["session_ids"]],
            ))
        return findings

    def propose(self, finding: ObservationDraft) -> WorkflowProposal | None:
        procedure = finding.baseline_query.get("procedure") or {}
        if not procedure.get("candidate_ready"):
            return None
        signature = finding.baseline_query["signature_hash"]
        archetype = str(procedure["task_archetype_id"])
        verbs = {"inspect": "Inspect", "search": "Search", "modify": "Modify", "check": "Run the check for"}
        steps = [f"{verbs.get(item['operation'], item['operation'])} {item['target']}."
                 for item in procedure["operations"]]
        from reflect.improvements.procedures import procedure_contract
        contract = procedure_contract(procedure)
        return WorkflowProposal(
            title=f"Workflow: repeat the scoped {archetype} procedure",
            hypothesis=("This draft preserves a repeated procedure with observed checks. "
                        "Whether using it improves outcomes remains unmeasured. " + finding.summary),
            risk="low", content={"schema_version": 2,
                "slug": f"scoped-{archetype.replace('_', '-')}-{signature[:8]}",
                "behavior_type": WorkflowBehaviorType.PROVEN_PATTERN.value,
                "suggested_artifact": "skill", "description": f"Use for matching {archetype} work and artifact scope.",
                "steps": steps, "procedure": procedure,
                "abstain_when": ["The task, artifact areas, or check scope differs from the linked executions.",
                    "Required inputs are missing or the procedure evidence is incomplete.",
                    "A check fails repeatedly without a relevant state change; stop and report the blocker."],
                "verification": ["Run the relevant checks after the final affected edit and inspect their results.",
                    "Keep task acceptance and unknown outcomes separate from passing checks."],
                "workflow_contract": contract.model_dump(mode="json"),
                "source": {"kind": "rule_blueprint", "rule_id": self.definition.id,
                           "rule_version": self.definition.version}},
            target_metric="workflow_adherence", target_value=1.0, measurement_window=5,
        )

class CorrectNoChangeRule(BaseImprovementRule):
    definition = RuleDefinition(
        id="correct_no_change_outcome",
        version=1,
        category="outcome",
        title="Correct no-change behavior worth preserving",
        description="Finds repeated operator-confirmed sessions where abstaining was correct.",
        detector_config={"minimum_sessions": 2},
        required_signals=["session_outcomes.outcome"],
    )
    workflow = WorkflowDefinition(
        slug="verify-before-changing",
        behavior_type=WorkflowBehaviorType.PROVEN_PATTERN,
        steps=[
            "State the requested outcome and inspect the existing implementation first.",
            "Identify an observable defect or unmet requirement before editing.",
            "If the requirement is already satisfied, verify it and report no change.",
            "Do not create a diff solely to demonstrate activity.",
        ],
    )

    def detect(self, conn: sqlite3.Connection, *, scope: AnalysisScope | None = None) -> list[ObservationDraft]:
        scope = scope or AnalysisScope()
        rows = conn.execute(
            f"""
            SELECT s.repo_id, COUNT(DISTINCT so.session_id) AS sessions
            FROM session_outcomes so JOIN {scope.sessions} s ON s.id = so.session_id
            WHERE so.outcome = 'no-change-correct'
            GROUP BY s.repo_id HAVING COUNT(DISTINCT so.session_id) >= 2
            ORDER BY sessions DESC
            """
        ).fetchall()
        findings: list[ObservationDraft] = []
        for repo_id, affected_sessions in rows:
            source_rows = conn.execute(
                f"""
                SELECT so.session_id, COUNT(*) AS outcomes
                FROM session_outcomes so
                JOIN {scope.sessions} s ON s.id = so.session_id
                WHERE so.outcome = 'no-change-correct'
                  AND COALESCE(s.repo_id, '') = COALESCE(?, '')
                GROUP BY so.session_id
                ORDER BY outcomes DESC, so.session_id
                """,
                (repo_id,),
            ).fetchall()
            evidence_rows = conn.execute(
                f"""
                SELECT so.session_id FROM session_outcomes so
                JOIN {scope.sessions} s ON s.id = so.session_id
                WHERE so.outcome = 'no-change-correct'
                  AND COALESCE(s.repo_id, '') = COALESCE(?, '')
                ORDER BY so.updated_at DESC
                LIMIT 20
                """,
                (repo_id,),
            ).fetchall()
            impact = min(70.0, 25.0 + affected_sessions * 7.0)
            scope_type, scope_id = _scope(repo_id)
            findings.append(
                ObservationDraft(
                    rule_id=self.definition.id,
                    rule_version=self.definition.version,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    repo_id=repo_id,
                    fingerprint=_stable_fingerprint("correct-no-change"),
                    category=self.definition.category,
                    title="Correct abstention is a reusable repository behavior",
                    summary=(
                        f"{affected_sessions} session(s) were explicitly confirmed correct without a change."
                    ),
                    metric_name="correct_no_change_sessions",
                    metric_value=float(affected_sessions),
                    metric_unit="sessions",
                    metric_direction="higher_is_better",
                    baseline_value=float(affected_sessions),
                    baseline_query={"repo_id": repo_id, "outcome": "no-change-correct"},
                    impact_score=impact,
                    severity=_severity(impact),
                    confidence=min(0.98, 0.82 + affected_sessions * 0.03),
                    occurrence_count=affected_sessions,
                    affected_session_count=affected_sessions,
                    evidence=[
                        EvidenceRef(
                            entity_type="session_outcome",
                            entity_id=f"{row[0]}:no-change-correct",
                            session_id=row[0],
                            summary_redacted="Operator confirmed that making no change was correct",
                        )
                        for row in evidence_rows
                    ],
                    source_sessions=[
                        ObservationSessionRef(
                            session_id=str(row[0]),
                            occurrence_count=int(row[1]),
                            summary_redacted=(
                                "Operator confirmed that making no change was correct"
                            ),
                            focus_entity_type="session_outcome",
                            focus_entity_id=str(row[0]),
                        )
                        for row in source_rows
                    ],
                )
            )
        return findings


DEFAULT_RULE_REGISTRY = RuleRegistry(
    (
        RepeatedToolFailureRule(),
        MissingVerificationRule(),
        ContextExplosionRule(),
        RepeatedExplorationRule(),
        UserCorrectionRule(),
        IgnoredConstraintRule(),
        SuccessfulRecoveryRule(),
        HighPerformingWorkflowRule(),
        CorrectNoChangeRule(),
        *session_insight_rules(),
    )
)
DEFAULT_RULES: tuple[BaseImprovementRule, ...] = DEFAULT_RULE_REGISTRY.rules
