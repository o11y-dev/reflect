from __future__ import annotations

import math
import sqlite3
import statistics
from collections import defaultdict

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
    RuleDefinition,
    WorkflowBehaviorType,
    WorkflowDefinition,
)

_stable_fingerprint = stable_fingerprint
_scope = scope_for_repository
_severity = severity_for_impact


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

    def detect(self, conn: sqlite3.Connection) -> list[ObservationDraft]:
        rows = conn.execute(
            """
            WITH tool_totals AS (
              SELECT s.repo_id,
                     lower(trim(tc.tool_name)) AS tool_key,
                     MIN(tc.tool_name) AS tool_name,
                     COUNT(*) AS total_calls
              FROM tool_calls tc
              JOIN sessions s ON s.id = tc.session_id
              GROUP BY s.repo_id, lower(trim(tc.tool_name))
            ), failed_tools AS (
              SELECT s.repo_id,
                     lower(trim(tc.tool_name)) AS tool_key,
                     MIN(tc.tool_name) AS tool_name,
                     COUNT(*) AS failures,
                     COUNT(DISTINCT tc.session_id) AS affected_sessions
              FROM tool_calls tc
              JOIN sessions s ON s.id = tc.session_id
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
            evidence_rows = conn.execute(
                """
                SELECT tc.id, tc.session_id, tc.step_id, tc.status, tc.error_type
                FROM tool_calls tc
                JOIN sessions s ON s.id = tc.session_id
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

    def detect(self, conn: sqlite3.Connection) -> list[ObservationDraft]:
        rows = conn.execute(
            """
            SELECT s.repo_id, tc.session_id, tc.tool_name, tc.input_hash,
                   COUNT(*) AS call_count
            FROM tool_calls tc
            JOIN sessions s ON s.id = tc.session_id
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
                        for row in evidence_rows
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

    def detect(self, conn: sqlite3.Connection) -> list[ObservationDraft]:
        rows = conn.execute(
            """
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
              FROM sessions s
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
                """
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
                  FROM sessions s
                  JOIN tool_calls tc ON tc.session_id = s.id
                  WHERE COALESCE(s.repo_id, '') = COALESCE(?, '')
                    AND lower(COALESCE(s.status, '')) IN ('completed', 'ok', 'success', 'unknown')
                  GROUP BY s.id
                )
                SELECT session_id, writes FROM classified
                WHERE writes > 0 AND verification_calls = 0
                ORDER BY writes DESC
                LIMIT 20
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
                        for row in evidence_rows
                    ],
                )
            )
        return findings


class ContextExplosionRule(BaseImprovementRule):
    definition = RuleDefinition(
        id="context_explosion",
        version=1,
        category="context",
        title="Context explosion",
        description="Finds sessions whose token volume is a robust outlier within the same repository.",
        detector_config={"minimum_tokens": 250000, "median_multiplier": 2.5},
        required_signals=["sessions.input_tokens", "sessions.output_tokens"],
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

    def detect(self, conn: sqlite3.Connection) -> list[ObservationDraft]:
        rows = conn.execute(
            """
            SELECT id, repo_id, input_tokens + output_tokens AS total_tokens
            FROM sessions
            WHERE input_tokens + output_tokens > 0
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
                            summary_redacted=f"Session used {tokens:,} input and output tokens",
                            attrs={"total_tokens": tokens, "threshold": threshold},
                        )
                        for session_id, tokens in sorted(outliers, key=lambda item: item[1], reverse=True)[:20]
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

    def detect(self, conn: sqlite3.Connection) -> list[ObservationDraft]:
        rows = conn.execute(
            """
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
              FROM sessions s
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
                """
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
                  FROM sessions s
                  JOIN tool_calls tc ON tc.session_id = s.id
                  WHERE COALESCE(s.repo_id, '') = COALESCE(?, '')
                  GROUP BY s.id
                )
                SELECT session_id, reads FROM classified
                WHERE reads >= 8 AND writes = 0
                ORDER BY reads DESC
                LIMIT 20
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

    def detect(self, conn: sqlite3.Connection) -> list[ObservationDraft]:
        rows = conn.execute(
            """
            SELECT s.repo_id, COUNT(*) AS corrections, COUNT(DISTINCT so.session_id) AS sessions
            FROM session_outcomes so
            JOIN sessions s ON s.id = so.session_id
            WHERE so.outcome = 'corrected'
              AND lower(COALESCE(s.status, '')) IN ('completed', 'ok', 'success', 'unknown')
            GROUP BY s.repo_id
            HAVING COUNT(*) >= 2
            ORDER BY corrections DESC
            """
        ).fetchall()
        findings: list[ObservationDraft] = []
        for repo_id, corrections, affected_sessions in rows:
            evidence_rows = conn.execute(
                """
                SELECT so.session_id, so.source
                FROM session_outcomes so JOIN sessions s ON s.id = so.session_id
                WHERE so.outcome = 'corrected'
                  AND COALESCE(s.repo_id, '') = COALESCE(?, '')
                ORDER BY so.updated_at DESC LIMIT 20
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

    def detect(self, conn: sqlite3.Connection) -> list[ObservationDraft]:
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
            FROM tool_calls tc JOIN sessions s ON s.id = tc.session_id
            WHERE {match}
            GROUP BY s.repo_id
            HAVING COUNT(*) >= 2
            ORDER BY violations DESC
            """
        ).fetchall()
        findings: list[ObservationDraft] = []
        for repo_id, violations, affected_sessions in rows:
            evidence_rows = conn.execute(
                f"""
                SELECT tc.id, tc.session_id, tc.step_id, tc.error_type
                FROM tool_calls tc JOIN sessions s ON s.id = tc.session_id
                WHERE COALESCE(s.repo_id, '') = COALESCE(?, '') AND ({match})
                ORDER BY tc.created_at DESC LIMIT 20
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

    def detect(self, conn: sqlite3.Connection) -> list[ObservationDraft]:
        rows = conn.execute(
            """
            SELECT repo_id, COUNT(*) AS sessions, SUM(recovered_failure_count) AS recoveries
            FROM sessions
            WHERE recovered_failure_count > 0
              AND lower(COALESCE(status, '')) IN ('completed', 'ok', 'success')
            GROUP BY repo_id HAVING COUNT(*) >= 2
            ORDER BY recoveries DESC
            """
        ).fetchall()
        findings: list[ObservationDraft] = []
        for repo_id, affected_sessions, recoveries in rows:
            evidence_rows = conn.execute(
                """
                SELECT id, recovered_failure_count FROM sessions
                WHERE COALESCE(repo_id, '') = COALESCE(?, '')
                  AND recovered_failure_count > 0
                  AND lower(COALESCE(status, '')) IN ('completed', 'ok', 'success')
                ORDER BY recovered_failure_count DESC LIMIT 20
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
                        for row in evidence_rows
                    ],
                )
            )
        return findings


class HighPerformingWorkflowRule(BaseImprovementRule):
    definition = RuleDefinition(
        id="high_performing_repeated_workflow",
        version=1,
        category="workflow",
        title="High-performing repeated workflow worth preserving",
        description="Finds repeated failure-free tool sequences within the same task archetype.",
        detector_config={"minimum_sessions": 3, "maximum_signature_tools": 8},
        required_signals=["tool_calls.tool_name", "session_task_archetypes.task_archetype_id"],
    )
    # Confirmed productive routines enter the loop ledger first. An agent may
    # later turn one into a versioned skill through an explicit build action.
    workflow = None

    def detect(self, conn: sqlite3.Connection) -> list[ObservationDraft]:
        rows = conn.execute(
            """
            SELECT s.id, s.repo_id, sta.task_archetype_id,
                   COALESCE((
                     SELECT GROUP_CONCAT(tool_name, ' > ') FROM (
                       SELECT lower(tc.tool_name) AS tool_name
                       FROM tool_calls tc WHERE tc.session_id = s.id
                       ORDER BY tc.created_at LIMIT 8
                     )
                   ), '') AS signature
            FROM sessions s
            JOIN session_task_archetypes sta ON sta.session_id = s.id
            WHERE s.failure_count = 0
              AND lower(COALESCE(s.status, '')) IN ('completed', 'ok', 'success')
            """
        ).fetchall()
        groups: dict[tuple[str | None, str, str], list[str]] = defaultdict(list)
        for session_id, repo_id, archetype_id, signature in rows:
            if signature:
                groups[(repo_id, str(archetype_id), str(signature))].append(str(session_id))
        findings: list[ObservationDraft] = []
        for (repo_id, archetype_id, signature), session_ids in groups.items():
            if len(session_ids) < 3:
                continue
            signature_tools = [tool.strip() for tool in signature.split(" > ") if tool.strip()]
            if len(set(signature_tools)) < 2:
                continue
            impact = min(78.0, 30.0 + len(session_ids) * 6.0)
            scope_type, scope_id = _scope(repo_id)
            findings.append(
                ObservationDraft(
                    rule_id=self.definition.id,
                    rule_version=self.definition.version,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    repo_id=repo_id,
                    fingerprint=_stable_fingerprint(archetype_id, signature),
                    category=self.definition.category,
                    title=f"A reliable {archetype_id} workflow repeats",
                    summary=(
                        f"{len(session_ids)} completed, failure-free session(s) used the bounded "
                        f"tool sequence: {signature}."
                    ),
                    metric_name="successful_workflow_sessions",
                    metric_value=float(len(session_ids)),
                    metric_unit="sessions",
                    metric_direction="higher_is_better",
                    baseline_value=float(len(session_ids)),
                    baseline_query={
                        "repo_id": repo_id,
                        "task_archetype_id": archetype_id,
                        "tool_signature": signature,
                    },
                    impact_score=impact,
                    severity=_severity(impact),
                    confidence=min(0.92, 0.58 + len(session_ids) * 0.06),
                    occurrence_count=len(session_ids),
                    affected_session_count=len(session_ids),
                    evidence=[
                        EvidenceRef(
                            entity_type="session",
                            entity_id=session_id,
                            session_id=session_id,
                            summary_redacted="Completed without recorded tool failures using the repeated sequence",
                        )
                        for session_id in session_ids[:20]
                    ],
                )
            )
        return findings


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

    def detect(self, conn: sqlite3.Connection) -> list[ObservationDraft]:
        rows = conn.execute(
            """
            SELECT s.repo_id, COUNT(DISTINCT so.session_id) AS sessions
            FROM session_outcomes so JOIN sessions s ON s.id = so.session_id
            WHERE so.outcome = 'no-change-correct'
            GROUP BY s.repo_id HAVING COUNT(DISTINCT so.session_id) >= 2
            ORDER BY sessions DESC
            """
        ).fetchall()
        findings: list[ObservationDraft] = []
        for repo_id, affected_sessions in rows:
            evidence_rows = conn.execute(
                """
                SELECT so.session_id FROM session_outcomes so
                JOIN sessions s ON s.id = so.session_id
                WHERE so.outcome = 'no-change-correct'
                  AND COALESCE(s.repo_id, '') = COALESCE(?, '')
                ORDER BY so.updated_at DESC LIMIT 20
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
                )
            )
        return findings


DEFAULT_RULE_REGISTRY = RuleRegistry(
    (
        RepeatedToolFailureRule(),
        RetryLoopRule(),
        MissingVerificationRule(),
        ContextExplosionRule(),
        RepeatedExplorationRule(),
        UserCorrectionRule(),
        IgnoredConstraintRule(),
        SuccessfulRecoveryRule(),
        HighPerformingWorkflowRule(),
        CorrectNoChangeRule(),
    )
)
DEFAULT_RULES: tuple[BaseImprovementRule, ...] = DEFAULT_RULE_REGISTRY.rules
