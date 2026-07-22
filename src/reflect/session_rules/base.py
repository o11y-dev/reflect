"""Typed contracts and registry for per-session quality rules."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from reflect.insights.types import DataProfile


@dataclass(frozen=True)
class SessionRuleDefinition:
    """Stable metadata for one independently replaceable quality dimension."""

    id: str
    version: int
    name: str
    description: str
    max_points: float
    signals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Session rule ID must not be empty")
        if self.version < 1:
            raise ValueError("Session rule version must be at least 1")
        if not self.name.strip():
            raise ValueError("Session rule name must not be empty")
        if self.max_points <= 0:
            raise ValueError("Session rule max_points must be positive")

    def to_payload(self) -> dict[str, object]:
        points: int | float = self.max_points
        if float(self.max_points).is_integer():
            points = int(self.max_points)
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "points": points,
            "signals": list(self.signals),
            "description": self.description,
        }


@dataclass(frozen=True)
class SessionRuleContext:
    """Normalized signals available while scoring one session.

    Optional fields distinguish a measured zero from a signal that was not
    present in a summary source. Rules can therefore award partial credit or
    explain missing evidence without depending on a renderer-specific row.
    """

    session_id: str
    source: Literal["spans", "summary"] = "spans"
    profile: DataProfile | None = None
    status: str = "unknown"
    has_stop: bool = False
    has_subagent_stop: bool = False
    tool_uses: int = 0
    total_tokens: int = 0
    failures: int = 0
    consecutive_pairs: int | None = None
    consecutive_triples: int | None = None
    duration_ms: float = 0.0
    timing_available: bool = False
    timestamp_count: int | None = None
    recovered: int = 0
    distinct_tools: int | None = None
    edits: int | None = None
    reads: int | None = None

    def __post_init__(self) -> None:
        if self.source not in {"spans", "summary"}:
            raise ValueError("Session rule context source must be 'spans' or 'summary'")
        numeric_fields = (
            "tool_uses",
            "total_tokens",
            "failures",
            "duration_ms",
            "recovered",
        )
        optional_numeric_fields = (
            "consecutive_pairs",
            "consecutive_triples",
            "timestamp_count",
            "distinct_tools",
            "edits",
            "reads",
        )
        for field_name in numeric_fields:
            if getattr(self, field_name) < 0:
                raise ValueError(f"Session rule context {field_name} must not be negative")
        for field_name in optional_numeric_fields:
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"Session rule context {field_name} must not be negative")


@dataclass(frozen=True)
class SessionRuleResult:
    """One rule's contribution to a session quality score."""

    rule_id: str
    rule_version: int
    earned: float
    summary: str
    metrics: dict[str, object] = field(default_factory=dict)

    def to_payload(self, definition: SessionRuleDefinition) -> dict[str, object]:
        earned = round(max(0.0, min(definition.max_points, self.earned)), 2)
        return {
            "name": definition.name,
            "earned": earned,
            "max": definition.max_points,
            "summary": self.summary,
            "metrics": self.metrics,
            "inputs": [
                {"name": key.replace("_", " "), "value": value}
                for key, value in self.metrics.items()
            ],
        }


class BaseSessionRule(ABC):
    """Base class for one deterministic per-session quality dimension."""

    definition: SessionRuleDefinition

    @abstractmethod
    def score(self, context: SessionRuleContext) -> SessionRuleResult:
        """Calculate this rule's contribution from normalized session signals."""

    def evaluate(self, context: SessionRuleContext) -> SessionRuleResult:
        """Score one context and enforce the shared result contract."""
        if not isinstance(self.definition, SessionRuleDefinition):
            raise TypeError(
                f"{type(self).__name__}.definition must be a SessionRuleDefinition"
            )
        if not isinstance(context, SessionRuleContext):
            raise TypeError("Session rules require a SessionRuleContext")
        result = self.score(context)
        if not isinstance(result, SessionRuleResult):
            raise TypeError(
                f"{type(self).__name__}.score() must return a SessionRuleResult"
            )
        expected = (self.definition.id, self.definition.version)
        actual = (result.rule_id, result.rule_version)
        if actual != expected:
            raise ValueError(
                f"{type(self).__name__} returned result identity "
                f"{actual[0]}@{actual[1]}; expected {expected[0]}@{expected[1]}"
            )
        return replace(
            result,
            earned=max(0.0, min(self.definition.max_points, result.earned)),
        )

    def result(
        self,
        earned: float,
        summary: str,
        metrics: dict[str, object] | None = None,
    ) -> SessionRuleResult:
        """Build a correctly identified result for this rule."""
        return SessionRuleResult(
            rule_id=self.definition.id,
            rule_version=self.definition.version,
            earned=earned,
            summary=summary,
            metrics=metrics or {},
        )


class SessionRuleRegistry:
    """Validated collection with one active rule per stable session-rule ID."""

    def __init__(self, rules: Iterable[BaseSessionRule] = ()) -> None:
        self._rules: dict[str, BaseSessionRule] = {}
        for rule in rules:
            self.register(rule)

    def __iter__(self) -> Iterator[BaseSessionRule]:
        return iter(self._rules.values())

    def __len__(self) -> int:
        return len(self._rules)

    @property
    def rules(self) -> tuple[BaseSessionRule, ...]:
        return tuple(self._rules.values())

    def register(
        self,
        rule: BaseSessionRule,
        *,
        replace: bool = False,
    ) -> BaseSessionRule:
        if not isinstance(rule, BaseSessionRule):
            raise TypeError("Registered session rules must extend BaseSessionRule")
        if not isinstance(rule.definition, SessionRuleDefinition):
            raise TypeError(
                f"{type(rule).__name__}.definition must be a SessionRuleDefinition"
            )
        rule_id = rule.definition.id
        if rule_id in self._rules and not replace:
            current = self._rules[rule_id]
            raise ValueError(
                f"Session rule {rule_id!r} is already registered at version "
                f"{current.definition.version}; pass replace=True to install a replacement"
            )
        self._rules[rule_id] = rule
        return rule

    def copy(self) -> SessionRuleRegistry:
        return SessionRuleRegistry(self.rules)

    def extended(
        self,
        *rules: BaseSessionRule,
        replace: bool = False,
    ) -> SessionRuleRegistry:
        registry = self.copy()
        for rule in rules:
            registry.register(rule, replace=replace)
        return registry


class SessionRuleScorer:
    """Run a registry and expose renderer-neutral score and rubric payloads."""

    def __init__(self, registry: SessionRuleRegistry) -> None:
        if not isinstance(registry, SessionRuleRegistry):
            raise TypeError("SessionRuleScorer requires a SessionRuleRegistry")
        self.registry = registry

    def results(self, context: SessionRuleContext) -> list[SessionRuleResult]:
        return [rule.evaluate(context) for rule in self.registry]

    def breakdown(self, context: SessionRuleContext) -> list[dict[str, object]]:
        return [
            result.to_payload(rule.definition)
            for rule, result in zip(self.registry, self.results(context), strict=True)
        ]

    def score(self, context: SessionRuleContext) -> float:
        score = sum(float(item["earned"]) for item in self.breakdown(context))
        return min(100.0, max(0.0, score))

    def rules_payload(self) -> list[dict[str, object]]:
        return [rule.definition.to_payload() for rule in self.registry]


__all__ = [
    "BaseSessionRule",
    "SessionRuleContext",
    "SessionRuleDefinition",
    "SessionRuleRegistry",
    "SessionRuleResult",
    "SessionRuleScorer",
]
