"""Opt-in cross-agent blog handoff through a freshly installed Reflect wheel."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from .harness import (
    AGENT_ADAPTERS,
    AgentAdapter,
    AgentTestContext,
    FreshInstallEnvironment,
    blog_draft_prompt,
    blog_revision_prompt,
    run_agent,
    validate_blog_draft,
    validate_blog_revision,
)

pytestmark = [
    pytest.mark.local_agent_e2e,
    pytest.mark.skipif(
        os.environ.get("REFLECT_RUN_LOCAL_AGENT_E2E") != "1" or bool(os.environ.get("CI")),
        reason="real-agent tests require an explicit local run and never execute in CI",
    ),
]


def _selected_adapters() -> list[AgentAdapter]:
    raw = os.environ.get(
        "REFLECT_LOCAL_AGENT_E2E_AGENTS",
        ",".join(adapter.name for adapter in AGENT_ADAPTERS),
    )
    selected = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return [adapter for adapter in AGENT_ADAPTERS if adapter.name in selected]


def _run_blog_agent(
    adapter: AgentAdapter,
    context: AgentTestContext,
    *,
    timeout_seconds: int,
):
    try:
        result = run_agent(adapter.build(context), timeout_seconds=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"{adapter.name} timed out after {exc.timeout}s")
    assert result.returncode == 0, result.diagnostic()
    return result, adapter.extract_final_message(result.stdout)


def test_fresh_install_agents_draft_and_cross_revise_from_reflect_memory(
    tmp_path: Path,
) -> None:
    adapters = _selected_adapters()
    assert adapters, "select at least one local agent"
    for adapter in adapters:
        assert shutil.which(adapter.executable_name), (
            f"required local agent executable is missing: {adapter.executable_name}"
        )

    repo_root = Path(__file__).resolve().parents[2]
    fresh = FreshInstallEnvironment.install(repo_root, tmp_path / "fresh-install")
    memory_workspace = fresh.root / "memory-workspace"
    memory_id = fresh.seed_blog_memory(memory_workspace)
    timeout_seconds = int(os.environ.get("REFLECT_LOCAL_AGENT_E2E_TIMEOUT", "180"))

    drafts: dict[str, str] = {}
    for adapter in adapters:
        workspace = fresh.root / "agents" / f"draft-{adapter.name}"
        workspace.mkdir(parents=True)
        summary = f"blog-draft:{adapter.name}"
        context = AgentTestContext(
            agent_name=adapter.name,
            executable=str(shutil.which(adapter.executable_name)),
            workspace=workspace,
            context_path=memory_workspace,
            db_path=fresh.db_path,
            python_executable=str(fresh.python_executable),
            runtime_session_id=f"blog-draft-{adapter.name}",
            completion_summary_override=summary,
            prompt_override=blog_draft_prompt(memory_workspace, summary),
        )
        result, draft = _run_blog_agent(
            adapter,
            context,
            timeout_seconds=timeout_seconds,
        )
        assert not validate_blog_draft(draft), (
            f"{adapter.name} draft failed: {validate_blog_draft(draft)}\n"
            f"{result.diagnostic()}"
        )
        drafts[adapter.name] = draft

    revisions: dict[str, str] = {}
    for index, author in enumerate(adapters):
        reviewer = adapters[(index + 1) % len(adapters)]
        workspace = fresh.root / "agents" / f"revision-{reviewer.name}-of-{author.name}"
        workspace.mkdir(parents=True)
        summary = f"blog-revision:{reviewer.name}:from:{author.name}"
        context = AgentTestContext(
            agent_name=reviewer.name,
            executable=str(shutil.which(reviewer.executable_name)),
            workspace=workspace,
            context_path=memory_workspace,
            db_path=fresh.db_path,
            python_executable=str(fresh.python_executable),
            runtime_session_id=f"blog-revision-{reviewer.name}-from-{author.name}",
            completion_summary_override=summary,
            prompt_override=blog_revision_prompt(
                memory_workspace,
                drafts[author.name],
                summary,
            ),
        )
        result, revision = _run_blog_agent(
            reviewer,
            context,
            timeout_seconds=timeout_seconds,
        )
        assert not validate_blog_revision(revision), (
            f"{reviewer.name} revision failed: {validate_blog_revision(revision)}\n"
            f"{result.diagnostic()}"
        )
        revisions[reviewer.name] = revision

    assert len(drafts) == len(adapters)
    assert len(revisions) == len(adapters)
    with sqlite3.connect(fresh.db_path) as conn:
        rows = conn.execute(
            """
            SELECT runtime_session_id, status, outcome, verification_passed,
                   selected_memories_json
            FROM mcp_task_runs
            ORDER BY started_at
            """
        ).fetchall()
        assert len(rows) == len(adapters) * 2
        assert len({str(row[0]) for row in rows}) == len(rows)
        assert all(tuple(row[1:4]) == ("completed", "success", 1) for row in rows)
        assert all(
            [item["memory_id"] for item in json.loads(str(row[4]))] == [memory_id]
            for row in rows
        )
