from __future__ import annotations

from reflect.improvements.contracts import (
    WorkflowContract,
    WorkflowEvidence,
    WorkflowMilestoneContract,
    evaluate_contract,
)
from reflect.improvements.workflow_roles import WorkflowSignal, workflow_roles


def _contract() -> WorkflowContract:
    return WorkflowContract(
        signature_hash="safe-write",
        milestones=[
            WorkflowMilestoneContract(id=role, role=role)
            for role in ("observe", "authorize", "act", "verify")
        ],
    )


def test_contract_evaluation_is_symmetric_and_rejects_failed_verification():
    contract = _contract()
    evidence = WorkflowEvidence(
        observed_roles=["observe", "authorize", "act", "verify"],
        status="completed",
        outcome="success",
        verification_passed=True,
    )

    before = evaluate_contract(contract, evidence)
    after = evaluate_contract(contract, evidence)
    failed = evaluate_contract(
        contract,
        evidence.model_copy(update={"verification_passed": False}),
    )

    assert before == after
    assert before.followed is True
    assert failed.followed is False
    assert failed.successful_outcome is False


def test_user_approval_is_part_of_the_canonical_workflow_signal_sequence():
    roles = workflow_roles(
        [
            WorkflowSignal(kind="tool", name="Read", preview="inspect target"),
            WorkflowSignal(
                kind="conversation",
                name="user",
                preview="Approved, go ahead",
                fingerprint="approval-hash",
            ),
            WorkflowSignal(kind="tool", name="Write", preview="post change"),
            WorkflowSignal(kind="tool", name="Read", preview="read back target"),
        ]
    )

    assert roles == ["observe", "authorize", "act", "verify"]
