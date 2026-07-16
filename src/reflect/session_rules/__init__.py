"""Object-oriented per-session quality scoring."""

from reflect.session_rules.adapters import context_from_spans, context_from_summary
from reflect.session_rules.base import (
    BaseSessionRule,
    SessionRuleContext,
    SessionRuleDefinition,
    SessionRuleRegistry,
    SessionRuleResult,
    SessionRuleScorer,
)
from reflect.session_rules.rules import (
    DEFAULT_SESSION_RULE_REGISTRY,
    DEFAULT_SESSION_RULE_SCORER,
    DEFAULT_SESSION_RULES,
)

__all__ = [
    "BaseSessionRule",
    "DEFAULT_SESSION_RULE_REGISTRY",
    "DEFAULT_SESSION_RULE_SCORER",
    "DEFAULT_SESSION_RULES",
    "SessionRuleContext",
    "SessionRuleDefinition",
    "SessionRuleRegistry",
    "SessionRuleResult",
    "SessionRuleScorer",
    "context_from_spans",
    "context_from_summary",
]
