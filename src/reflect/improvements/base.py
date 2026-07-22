from __future__ import annotations

import hashlib
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from typing import Any

from reflect.improvements.models import (
    EvidenceRef,
    ObservationDraft,
    RuleDefinition,
    Severity,
    WorkflowDefinition,
    WorkflowProposal,
    WorkflowSourceKind,
)


def stable_fingerprint(*parts: object) -> str:
    """Build a stable, privacy-safe identity for one recurring finding."""
    if not parts:
        raise ValueError("A rule fingerprint requires at least one identity part")
    value = "\x1f".join(str(part).strip().lower() for part in parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def scope_for_repository(repo_id: str | None) -> tuple[str, str]:
    """Use repository scope when known, otherwise fall back to local-user scope."""
    return ("repository", repo_id) if repo_id else ("user", "local")


def severity_for_impact(impact_score: float) -> Severity:
    """Map the shared 0-100 impact scale to the durable severity contract."""
    if impact_score >= 85:
        return Severity.CRITICAL
    if impact_score >= 65:
        return Severity.HIGH
    if impact_score >= 35:
        return Severity.MEDIUM
    return Severity.LOW


class BaseImprovementRule(ABC):
    """Base class for deterministic, evidence-backed improvement rules.

    Subclasses declare a versioned ``definition`` and implement ``detect``.
    ``make_observation`` fills the rule identity, category, scope, fingerprint,
    and severity consistently so custom rules only own their domain query and
    finding-specific metrics.
    """

    definition: RuleDefinition
    workflow: WorkflowDefinition | None = None

    @abstractmethod
    def detect(self, conn: sqlite3.Connection) -> list[ObservationDraft]:
        """Return deterministic findings from canonical Reflect SQLite state."""

    def evaluate(self, conn: sqlite3.Connection) -> list[ObservationDraft]:
        """Run the detector and enforce the cross-rule persistence contract."""
        if not isinstance(self.definition, RuleDefinition):
            raise TypeError(f"{type(self).__name__}.definition must be a RuleDefinition")
        findings = self.detect(conn)
        if not isinstance(findings, list):
            raise TypeError(f"{type(self).__name__}.detect() must return list[ObservationDraft]")

        identities: set[tuple[str, str, str]] = set()
        for finding in findings:
            if not isinstance(finding, ObservationDraft):
                raise TypeError(f"{type(self).__name__}.detect() returned a non-ObservationDraft value")
            if finding.rule_id != self.definition.id or finding.rule_version != self.definition.version:
                raise ValueError(
                    f"{type(self).__name__} returned finding identity "
                    f"{finding.rule_id}@{finding.rule_version}; expected "
                    f"{self.definition.id}@{self.definition.version}"
                )
            if finding.category != self.definition.category:
                raise ValueError(
                    f"{type(self).__name__} returned category {finding.category!r}; "
                    f"expected {self.definition.category!r}"
                )
            identity = (finding.scope_type, finding.scope_id, finding.fingerprint)
            if identity in identities:
                raise ValueError(
                    f"{type(self).__name__} returned duplicate finding identity "
                    f"{finding.scope_type}:{finding.scope_id}:{finding.fingerprint}"
                )
            identities.add(identity)
        return findings

    def propose(self, finding: ObservationDraft) -> WorkflowProposal | None:
        """Build this rule's optional actionable workflow for one finding."""
        if self.workflow is None:
            return None
        if finding.rule_id != self.definition.id:
            raise ValueError(
                f"{type(self).__name__} cannot propose a workflow for {finding.rule_id!r}"
            )
        workflow = self.workflow
        direction = "increase" if finding.metric_direction == "higher_is_better" else "reduce"
        baseline = finding.baseline_value
        if baseline is None:
            target_value = None
        elif finding.metric_direction == "higher_is_better":
            target_value = float(baseline) * 1.2
        elif finding.metric_name.endswith(("rate", "ratio")):
            target_value = max(0.0, float(baseline) * 0.6)
        else:
            target_value = max(0.0, float(baseline) * 0.7)
        return WorkflowProposal(
            title=f"Workflow: {finding.title}",
            hypothesis=(
                f"A reviewed {workflow.slug} workflow will {direction} "
                f"{finding.metric_name}. {finding.summary}"
            ),
            risk=workflow.risk,
            content={
                "schema_version": 1,
                "slug": workflow.slug,
                "behavior_type": workflow.behavior_type.value,
                "suggested_artifact": workflow.suggested_artifact.value,
                "description": workflow.description or f"Use when Reflect detects: {finding.title.lower()}.",
                "steps": workflow.steps,
                "abstain_when": workflow.abstain_when,
                "verification": workflow.verification,
                "source": {
                    "kind": WorkflowSourceKind.RULE_BLUEPRINT.value,
                    "observation_title": finding.title,
                    "rule_id": self.definition.id,
                    "rule_version": self.definition.version,
                },
            },
            target_metric=finding.metric_name,
            target_value=target_value,
            measurement_window=workflow.measurement_window,
        )

    def make_observation(
        self,
        *,
        identity: Iterable[object],
        title: str,
        summary: str,
        metric_name: str,
        metric_value: float,
        metric_unit: str,
        metric_direction: str,
        impact_score: float,
        confidence: float,
        occurrence_count: int = 1,
        affected_session_count: int = 1,
        repo_id: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        baseline_value: float | None = None,
        baseline_query: dict[str, Any] | None = None,
        actionability: str = "review",
        evidence: Iterable[EvidenceRef] = (),
    ) -> ObservationDraft:
        """Create a valid draft owned by this rule definition."""
        identity_parts = tuple(identity)
        if (scope_type is None) != (scope_id is None):
            raise ValueError("scope_type and scope_id must be provided together")
        if scope_type is None or scope_id is None:
            scope_type, scope_id = scope_for_repository(repo_id)
        return ObservationDraft(
            rule_id=self.definition.id,
            rule_version=self.definition.version,
            scope_type=scope_type,
            scope_id=scope_id,
            repo_id=repo_id,
            fingerprint=stable_fingerprint(*identity_parts),
            category=self.definition.category,
            title=title,
            summary=summary,
            metric_name=metric_name,
            metric_value=metric_value,
            metric_unit=metric_unit,
            metric_direction=metric_direction,
            baseline_value=baseline_value,
            baseline_query=baseline_query or {},
            impact_score=impact_score,
            severity=severity_for_impact(impact_score),
            confidence=confidence,
            occurrence_count=occurrence_count,
            affected_session_count=affected_session_count,
            actionability=actionability,
            evidence=list(evidence),
        )


# Preserve the original public name while making the base-class role explicit.
ImprovementRule = BaseImprovementRule


class RuleRegistry:
    """Validated collection of one active implementation per stable rule ID."""

    def __init__(self, rules: Iterable[BaseImprovementRule] = ()):
        self._rules: dict[str, BaseImprovementRule] = {}
        for rule in rules:
            self.register(rule)

    def __iter__(self) -> Iterator[BaseImprovementRule]:
        return iter(self._rules.values())

    def __len__(self) -> int:
        return len(self._rules)

    @property
    def rules(self) -> tuple[BaseImprovementRule, ...]:
        return tuple(self._rules.values())

    def register(self, rule: BaseImprovementRule, *, replace: bool = False) -> BaseImprovementRule:
        if not isinstance(rule, BaseImprovementRule):
            raise TypeError("Registered rules must extend BaseImprovementRule")
        if not isinstance(rule.definition, RuleDefinition):
            raise TypeError(f"{type(rule).__name__}.definition must be a RuleDefinition")
        rule_id = rule.definition.id
        if rule_id in self._rules and not replace:
            current = self._rules[rule_id]
            raise ValueError(
                f"Rule {rule_id!r} is already registered at version {current.definition.version}; "
                "pass replace=True to install a replacement implementation"
            )
        self._rules[rule_id] = rule
        return rule

    def copy(self) -> RuleRegistry:
        return RuleRegistry(self.rules)

    def extended(self, *rules: BaseImprovementRule, replace: bool = False) -> RuleRegistry:
        registry = self.copy()
        for rule in rules:
            registry.register(rule, replace=replace)
        return registry


__all__ = [
    "BaseImprovementRule",
    "ImprovementRule",
    "RuleRegistry",
    "scope_for_repository",
    "severity_for_impact",
    "stable_fingerprint",
]
