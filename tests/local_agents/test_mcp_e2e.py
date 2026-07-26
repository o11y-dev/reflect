"""Opt-in end-to-end tests using real MCP-capable agent CLIs."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from .harness import (
    AGENT_ADAPTERS,
    SUCCESS_MARKER,
    AgentAdapter,
    AgentTestContext,
    read_completed_task,
    run_agent,
)

pytestmark = [
    pytest.mark.local_agent_e2e,
    pytest.mark.skipif(
        os.environ.get("REFLECT_RUN_LOCAL_AGENT_E2E") != "1" or bool(os.environ.get("CI")),
        reason="real-agent tests require an explicit local run and never execute in CI",
    ),
]


def _selected_agents() -> set[str]:
    raw = os.environ.get(
        "REFLECT_LOCAL_AGENT_E2E_AGENTS",
        ",".join(adapter.name for adapter in AGENT_ADAPTERS),
    )
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


@pytest.mark.parametrize(
    "adapter",
    [
        pytest.param(adapter, id=adapter.name)
        for adapter in AGENT_ADAPTERS
        if adapter.name in _selected_agents()
    ],
)
def test_real_agent_completes_reflect_mcp_lifecycle(
    adapter: AgentAdapter,
    tmp_path: Path,
) -> None:
    executable = shutil.which(adapter.executable_name)
    if executable is None:
        pytest.skip(f"{adapter.executable_name} is not installed")

    workspace = tmp_path / adapter.name
    workspace.mkdir()
    db_path = workspace / "reflect.db"
    context = AgentTestContext(
        agent_name=adapter.name,
        executable=executable,
        workspace=workspace,
        db_path=db_path,
    )
    command = adapter.build(context)
    timeout_seconds = int(os.environ.get("REFLECT_LOCAL_AGENT_E2E_TIMEOUT", "180"))

    try:
        result = run_agent(command, timeout_seconds=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"{adapter.name} timed out after {exc.timeout}s")

    assert result.returncode == 0, result.diagnostic()
    assert adapter.extract_final_message(result.stdout) == SUCCESS_MARKER, result.diagnostic()

    task = read_completed_task(db_path)
    assert task is not None, result.diagnostic()
    status, outcome, verification_passed, summary, workspace_path = task
    assert status == "completed"
    assert outcome == "success"
    assert verification_passed == 1
    assert summary == context.completion_summary
    assert Path(workspace_path) == workspace.resolve()
