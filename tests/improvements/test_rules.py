from __future__ import annotations

import sqlite3

import pytest

from reflect.improvements import (
    BaseImprovementRule,
    ImprovementService,
    RuleDefinition,
    RuleRegistry,
    WorkflowBehaviorType,
    WorkflowDefinition,
)
from reflect.improvements.models import ObservationDraft, Severity
from reflect.store.sqlite import connect_sqlite


class ExpensiveSessionRule(BaseImprovementRule):
    definition = RuleDefinition(
        id="expensive_session",
        version=1,
        category="cost",
        title="Expensive sessions",
        description="Example extension rule for unusually expensive sessions.",
        detector_config={"minimum_cost_usd": 10},
        required_signals=["sessions.cost_usd"],
    )
    workflow = WorkflowDefinition(
        slug="cost-budget-check",
        behavior_type=WorkflowBehaviorType.VERIFICATION,
        steps=["Inspect the session cost evidence.", "Verify the configured cost budget."],
    )

    def detect(self, conn: sqlite3.Connection) -> list[ObservationDraft]:
        return [
            self.make_observation(
                identity=("local", "expensive_session"),
                title="Session cost exceeds the local policy",
                summary="One session exceeded the configured cost threshold.",
                metric_name="session_cost_usd",
                metric_value=12.5,
                metric_unit="usd",
                metric_direction="lower_is_better",
                baseline_value=10.0,
                baseline_query={"minimum_cost_usd": 10},
                impact_score=68,
                confidence=0.9,
            )
        ]


class ExpensiveSessionRuleV2(ExpensiveSessionRule):
    definition = ExpensiveSessionRule.definition.model_copy(update={"version": 2})


def test_base_rule_builds_and_validates_owned_observations():
    rule = ExpensiveSessionRule()

    finding = rule.evaluate(sqlite3.connect(":memory:"))[0]

    assert finding.rule_id == "expensive_session"
    assert finding.rule_version == 1
    assert finding.category == "cost"
    assert finding.scope_type == "user"
    assert finding.scope_id == "local"
    assert finding.severity == Severity.HIGH
    assert len(finding.fingerprint) == 24
    proposal = rule.propose(finding)
    assert proposal is not None
    assert proposal.content["behavior_type"] == "verification"
    assert proposal.content["suggested_artifact"] == "skill"
    assert proposal.content["source"]["kind"] == "rule_blueprint"
    assert proposal.content["source"]["rule_id"] == "expensive_session"


def test_rule_registry_rejects_duplicates_and_supports_explicit_replacement():
    registry = RuleRegistry([ExpensiveSessionRule()])

    with pytest.raises(ValueError, match="already registered"):
        registry.register(ExpensiveSessionRule())

    replaced = registry.extended(ExpensiveSessionRuleV2(), replace=True)
    assert registry.rules[0].definition.version == 1
    assert replaced.rules[0].definition.version == 2


def test_improvement_service_accepts_a_custom_rule_registry(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        service = ImprovementService(conn, rules=RuleRegistry([ExpensiveSessionRule()]))

        result = service.refresh()
        observation = conn.execute(
            "SELECT rule_id, rule_version, category, status FROM observations"
        ).fetchone()
        candidate = conn.execute(
            "SELECT status, provenance_json FROM workflow_candidates"
        ).fetchone()

        assert result["detected"] == 1
        assert observation == ("expensive_session", 1, "cost", "proposal_ready")
        assert candidate[0] == "pending"
        assert '"rule_id":"expensive_session"' in candidate[1]
    finally:
        conn.close()


def test_rule_without_workflow_keeps_an_observation_without_creating_a_candidate(tmp_path):
    class ObservationOnlyRule(ExpensiveSessionRule):
        workflow = None

    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        result = ImprovementService(conn, rules=[ObservationOnlyRule()]).refresh()

        assert result["detected"] == 1
        assert conn.execute("SELECT status FROM observations").fetchone()[0] == "new"
        assert conn.execute("SELECT COUNT(*) FROM workflow_candidates").fetchone()[0] == 0
    finally:
        conn.close()


def test_base_rule_rejects_findings_owned_by_another_rule():
    class InvalidRule(ExpensiveSessionRule):
        def detect(self, conn: sqlite3.Connection) -> list[ObservationDraft]:
            finding = super().detect(conn)[0]
            return [finding.model_copy(update={"rule_id": "another_rule"})]

    with pytest.raises(ValueError, match="expected expensive_session@1"):
        InvalidRule().evaluate(sqlite3.connect(":memory:"))


def test_base_rule_allows_the_same_fingerprint_in_different_scopes():
    class ScopedRule(ExpensiveSessionRule):
        def detect(self, conn: sqlite3.Connection) -> list[ObservationDraft]:
            return [
                self.make_observation(
                    identity=("same-pattern",),
                    repo_id=repo_id,
                    title="Same pattern in separate repositories",
                    summary="The identity is unique within its repository scope.",
                    metric_name="session_cost_usd",
                    metric_value=12.5,
                    metric_unit="usd",
                    metric_direction="lower_is_better",
                    impact_score=40,
                    confidence=0.8,
                )
                for repo_id in ("repo-a", "repo-b")
            ]

    assert len(ScopedRule().evaluate(sqlite3.connect(":memory:"))) == 2
