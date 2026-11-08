import json

import pytest

from reflect.execution_assessment import EditObservation, assess_execution, check_command
from reflect.improvements.contracts import (
    WorkflowContract,
    WorkflowEvidence,
    WorkflowMilestoneContract,
    evaluate_contract,
)
from reflect.improvements.workflow_roles import WorkflowSignal, workflow_role


def tool(step, command, at, code=None, scope=None):
    attrs = {"gen_ai.client.command": command}
    if code is not None:
        attrs["process.exit.code"] = code
    if scope is not None:
        attrs["reflect.check.scope"] = scope
    return {"step_id": step, "started_at": at, "raw_attrs_json": json.dumps(attrs)}


@pytest.mark.parametrize("command", [
    "rg -n pytest docs", "echo pytest", "pytest --help", "pytest --collect-only",
    "pytest && echo done", "python -c 'print(\"pytest\")'", "pytest; true",
])
def test_mentions_and_composite_commands_are_not_successful_checks(command):
    assert check_command(command) is None


def test_embedded_payloads_and_scripts_are_rejected_before_shell_parsing(monkeypatch):
    def unexpected_parse(_command):
        raise AssertionError("unsupported scripts must not reach shlex")

    monkeypatch.setattr("reflect.execution_assessment.shlex.split", unexpected_parse)
    oversized = "pytest --payload '" + "x" * (1024 * 1024) + "'"
    assert check_command(oversized) is None
    assert check_command("pytest\npython -c 'embedded script'") is None
    assessment = assess_execution(
        tools=[tool("large", oversized, "10:01", 0)],
        edits=[], closed=True, input_identity=["unit"],
    )
    assert assessment["verification"] == "unknown"
    assert assessment["checks"] == []


def test_matching_rerun_recovers_but_does_not_prove_semantic_completion():
    result = assess_execution(
        tools=[tool("fail", "pytest tests/a.py", "10:01", 1), tool("pass", "pytest tests/a.py", "10:02", 0)],
        edits=[], closed=True, input_identity=["unit"],
    )
    assert result["verification"] == "passed"
    assert result["checks"][0]["step_id"] == "pass"
    assert result["outcome"] == "unknown"


def test_different_passing_check_does_not_erase_failure():
    result = assess_execution(
        tools=[tool("fail", "pytest tests/a.py", "10:01", 1), tool("pass", "ruff check .", "10:02", 0)],
        edits=[], closed=True, input_identity=["unit"],
    )
    assert result["verification"] == "failed"


def test_running_rerun_does_not_erase_observed_failure():
    result = assess_execution(
        tools=[tool("fail", "pytest", "10:01", 1), tool("running", "pytest", "10:02")],
        edits=[], closed=False, input_identity=["unit"],
    )
    assert result["verification"] == "failed"
    assert result["pending_check_step_ids"] == ["running"]


@pytest.mark.parametrize(("paths", "expected"), [({"src/a.py"}, "invalidated"), ({"docs/readme.md"}, "passed"), (set(), "invalidated")])
def test_only_relevant_or_unknown_edits_invalidate_scoped_verification(paths, expected):
    result = assess_execution(
        tools=[tool("pass", "pytest", "10:01", 0, ["src/a.py"])],
        edits=[EditObservation("edit", "10:02", frozenset(paths))], closed=True, input_identity=["unit"],
    )
    assert result["verification"] == expected
    assert result["checks"][0]["invalidated_by"] == (["edit"] if expected == "invalidated" else [])


def test_missing_exit_and_repeated_derivation_are_honest_and_stable():
    args = {"tools": [tool("poll", "pytest", "10:01")], "edits": [], "closed": False, "input_identity": ["unit"]}
    first = assess_execution(**args)
    assert first == assess_execution(**args)
    assert first["verification"] == "unknown"
    assert first["terminal_check_count"] == 0


def test_unknown_outcome_and_verification_cannot_satisfy_contract():
    contract = WorkflowContract(signature_hash="test", milestones=[WorkflowMilestoneContract(id="verify")])
    unknown = evaluate_contract(contract, WorkflowEvidence(status="completed", observed_roles=["verify"]))
    assert not unknown.followed
    assert not unknown.successful_outcome


def test_workflow_roles_use_operation_not_file_name_or_search_text():
    assert workflow_role(WorkflowSignal(kind="tool", name="apply_patch", preview="tests/test_capture.py"), acted=False) == "act"
    assert workflow_role(WorkflowSignal(kind="tool", name="exec_command", preview='{"cmd":"rg -n pytest docs"}'), acted=False) is None
    assert workflow_role(WorkflowSignal(kind="tool", name="exec_command", preview='{"cmd":"pytest"}'), acted=False) == "verify"


def test_environment_change_is_not_a_matching_recovery():
    result = assess_execution(
        tools=[tool("fail", "MODE=a pytest", "10:01", 1), tool("pass", "MODE=b pytest", "10:02", 0)],
        edits=[], closed=True, input_identity=["unit"],
    )
    assert result["verification"] == "failed"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_edits_during_a_check_invalidate_its_scope(exit_code):
    check = {**tool("check", "pytest", "10:01", exit_code), "ended_at": "10:03"}
    result = assess_execution(tools=[check], edits=[EditObservation("edit", "10:02", frozenset({"src/app.py"}))],
        closed=True, input_identity=["unit"])
    assert result["verification"] == "invalidated"
