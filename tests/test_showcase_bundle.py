"""The public sample is an executable synthetic data contract, never a local export."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def builder():
    spec = importlib.util.spec_from_file_location("build_showcase", ROOT / "scripts/build_showcase.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bundle(builder):
    return builder.build_showcase()


def test_sample_totals_come_from_complete_canonical_records(bundle):
    overview = bundle["sqlite"]["overview"]
    usage = bundle["sqlite"]["tabs"]["usage"]
    sessions = bundle["sessions"]
    assert overview["session_count"] == len(sessions) == usage["unique_sessions"] == 14
    assert overview["tool_call_count"] == sum(row["tool_calls"] for row in sessions)
    assert overview["failure_count"] == sum(row["failures"] for row in sessions)
    assert overview["failure_count"] > 0
    assert usage["total_input_tokens"] == sum(row["input_tokens"] for row in sessions)
    assert usage["total_output_tokens"] == sum(row["output_tokens"] for row in sessions)


def test_every_scenario_and_inventory_has_inspectable_resources(bundle):
    demo = bundle["demo"]
    assert demo["schema_version"] == 1
    assert demo["default_scenario"] == "retry-recovery"
    assert len(demo["scenarios"]) == 3
    resources = demo["resources"]
    for scenario in demo["scenarios"]:
        for field, path in (("session_id", "session"), ("finding_id", "findings"),
                            ("workflow_id", "workflows"), ("skill_id", "skills"),
                            ("impact_id", "impact")):
            identifier = scenario[field]
            suffix = "/preview" if path == "workflows" else "/evidence" if path == "impact" else ""
            assert resources[f"/api/{path}/{identifier}{suffix}"]
        assert resources[f"/api/findings/{scenario['finding_id']}/evidence"]
        assert resources[f"/api/workflows/{scenario['workflow_id']}/evidence"]
        assert resources[f"/api/skills/{scenario['skill_id']}/usage-graph"]
    assert resources["/api/findings"]["findings"]
    assert resources["/api/workflows"]["workflows"]
    assert resources["/api/skills"]["skills"]
    assert resources["/api/impact"]["impact_checks"]
    assert resources["/api/explore/usage"]["sqlite"]["tabs"]["usage"]["unique_sessions"] == len(bundle["sessions"])
    for view in ("tools", "graph", "context"):
        assert resources[f"/api/explore/{view}"]["view"] == view


def test_synthetic_sample_preserves_unknowns_and_verification_chronology(bundle):
    assessments = bundle["demo"]["assessments"]
    assert assessments["demo-pass-then-edit"]["verification"] == "invalidated"
    # An ambiguous shell wrapper is deliberately unassessed, never a verified pass.
    assert assessments["demo-wrapper-failure"]["verification"] == "unknown"
    assert assessments["demo-wrapper-failure"]["outcome"] == "unknown"
    checks = bundle["demo"]["resources"]["/api/impact"]["impact_checks"]
    scenario = next(item for item in bundle["demo"]["scenarios"] if item["id"] == "insufficient-evidence")
    measurement = next(item for item in checks if item["id"] == scenario["impact_id"])
    assert measurement["verdict"] == "insufficient_data"
    assert measurement["after_value"] is None
    assert measurement["delta"] is None


def test_retry_comparison_uses_real_fixture_cohorts_and_skill_observations(bundle):
    demo = bundle["demo"]
    scenario = next(item for item in demo["scenarios"] if item["id"] == "retry-recovery")
    resources = demo["resources"]
    measurement = next(item for item in resources["/api/impact"]["impact_checks"] if item["id"] == scenario["impact_id"])
    assert measurement["before_count"] == 3
    assert measurement["after_count"] == 5
    assert measurement["metric_name"] == "tool_failure_rate"
    assert measurement["before_value"] == 0.5
    assert measurement["after_value"] == 0
    assert measurement["verdict"] == "improved"
    ledger = resources[f"/api/impact/{scenario['impact_id']}/evidence"]
    assert len(ledger["before_execution_units"]) == 3
    assert len(ledger["after_execution_units"]) == 5
    assert all(row["status"] == "failed" for row in scenario["preview"])
    skill = resources[f"/api/skills/{scenario['skill_id']}"]
    assert len(skill["usage_occurrences"]) == 5
    assert all(row["skill_version_id"] for row in skill["usage_occurrences"])
    for usage in skill["usage_occurrences"]:
        version = next(row for row in skill["versions"] if row["id"] == usage["skill_version_id"])
        assert version["created_at"] < usage["observed_at"]
        assert usage["evidence"]["version_attribution"] == "captured"


def test_finding_counts_match_their_retained_execution_evidence(bundle):
    resources = bundle["demo"]["resources"]
    for scenario in bundle["demo"]["scenarios"]:
        observation = resources[f"/api/findings/{scenario['finding_id']}"]
        ledger = resources[f"/api/findings/{scenario['finding_id']}/evidence"]
        assert observation["affected_session_count"] == ledger["provenance_session_count"]
        if scenario["id"] == "retry-recovery":
            assert observation["metric_name"] == "failed_tool_calls"
            assert observation["metric_unit"] == "calls"
            assert observation["metric_value"] == 9
            assert observation["affected_session_count"] == 3
        elif scenario["id"] == "preserve-routine":
            assert observation["metric_name"] == "checked_execution_units"
            assert observation["metric_value"] == 3


def test_sample_is_deterministic_private_and_committed(builder, bundle):
    encoded = builder.serialize(bundle)
    assert encoded == builder.serialize(builder.build_showcase())
    assert encoded == (ROOT / "docs/reports/showcase.json").read_text()
    for private_marker in ("/Users/", "/private/", "ridewithvia", "atlassian.net", "glpat-", "sk-ant-"):
        assert private_marker not in encoded
    assert bundle["demo"]["label"] == "Illustrative sample"
    assert bundle["demo"]["provenance"] == "synthetic"
    assert {item["agent"] for item in bundle["sessions"]} == {"codex", "claude", "cursor"}
    assert json.loads(encoded) == bundle
