"""Paired local-only trials that measure whether Reflect guidance changes outcomes."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from .harness import (
    AGENT_ADAPTERS,
    AgentAdapter,
    AgentTestContext,
    effectiveness_guided_prompt,
    effectiveness_task_prompt,
    extract_final_message,
    read_completed_task,
    read_task_selection,
    run_agent,
    score_effectiveness,
    seed_effectiveness_workflow,
)

pytestmark = [
    pytest.mark.local_agent_e2e,
    pytest.mark.skipif(
        os.environ.get("REFLECT_RUN_LOCAL_AGENT_E2E") != "1" or bool(os.environ.get("CI")),
        reason="effectiveness trials require an explicit local run and never execute in CI",
    ),
]


def _selected_agents() -> set[str]:
    raw = os.environ.get("REFLECT_LOCAL_AGENT_E2E_AGENTS", "claude,codex,cursor")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


@pytest.mark.parametrize(
    "adapter",
    [
        pytest.param(adapter, id=adapter.name)
        for adapter in AGENT_ADAPTERS
        if adapter.name in _selected_agents()
    ],
)
def test_reflect_guidance_improves_policy_adherence(
    adapter: AgentAdapter,
    tmp_path: Path,
) -> None:
    executable = shutil.which(adapter.executable_name)
    if executable is None:
        pytest.skip(f"{adapter.executable_name} is not installed")
    timeout_seconds = int(os.environ.get("REFLECT_LOCAL_AGENT_E2E_TIMEOUT", "180"))

    baseline_workspace = tmp_path / f"{adapter.name}-baseline"
    baseline_workspace.mkdir()
    baseline_context = AgentTestContext(
        agent_name=adapter.name,
        executable=executable,
        workspace=baseline_workspace,
        db_path=baseline_workspace / "unused.db",
        prompt_override=effectiveness_task_prompt(),
    )
    try:
        baseline = run_agent(
            adapter.build_baseline(baseline_context),
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"{adapter.name} baseline timed out after {exc.timeout}s")
    assert baseline.returncode == 0, baseline.diagnostic()
    baseline_message = extract_final_message(baseline.stdout)
    baseline_score = score_effectiveness(baseline_message)

    guided_workspace = tmp_path / f"{adapter.name}-guided"
    guided_workspace.mkdir()
    guided_db = guided_workspace / "reflect.db"
    candidate_id = seed_effectiveness_workflow(guided_db, guided_workspace)
    guided_summary = f"local-agent-effectiveness:{adapter.name}"
    guided_context = AgentTestContext(
        agent_name=adapter.name,
        executable=executable,
        workspace=guided_workspace,
        db_path=guided_db,
        prompt_override=effectiveness_guided_prompt(guided_workspace, guided_summary),
    )
    try:
        guided = run_agent(
            adapter.build(guided_context),
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"{adapter.name} guided run timed out after {exc.timeout}s")
    assert guided.returncode == 0, guided.diagnostic()
    guided_message = extract_final_message(guided.stdout)
    guided_score = score_effectiveness(guided_message)

    task = read_completed_task(guided_db)
    assert task is not None, guided.diagnostic()
    status, outcome, verification_passed, summary, workspace_path = task
    assert (status, outcome, verification_passed) == ("completed", "success", 1)
    assert summary == guided_summary
    assert Path(workspace_path) == guided_workspace.resolve()

    selection = read_task_selection(guided_db)
    assert selection is not None
    workflow_id, selected_skills = selection
    assert workflow_id == candidate_id
    assert len(selected_skills) == 1
    assert selected_skills[0]["slug"] == "capsule-violet-routing"

    print(
        f"{adapter.name}: baseline={baseline_score}/3 guided={guided_score}/3 "
        f"baseline_output={baseline_message!r} guided_output={guided_message!r}"
    )
    assert guided_score == 3, guided.diagnostic()
    assert guided_score > baseline_score, (
        f"Reflect did not improve the outcome for {adapter.name}: "
        f"baseline={baseline_score}/3 guided={guided_score}/3"
    )
