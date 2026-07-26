"""Fast contract tests for the machine-only agent adapters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reflect.context import ReflectContextService
from reflect.store.sqlite import connect_sqlite

from .harness import (
    EFFECTIVENESS_QUESTION,
    AgentTestContext,
    ClaudeAdapter,
    CodexAdapter,
    CursorAdapter,
    effectiveness_task_prompt,
    extract_final_message,
    score_effectiveness,
    seed_effectiveness_workflow,
)


@pytest.fixture
def context(tmp_path: Path) -> AgentTestContext:
    return AgentTestContext(
        agent_name="test-agent",
        executable="/usr/local/bin/test-agent",
        workspace=tmp_path,
        db_path=tmp_path / "reflect.db",
        python_executable="/opt/reflect/bin/python",
    )


def test_claude_adapter_uses_strict_one_run_mcp_config(
    context: AgentTestContext,
) -> None:
    command = ClaudeAdapter().build(context)

    assert "--bare" in command.argv
    assert "--strict-mcp-config" in command.argv
    assert "--no-session-persistence" in command.argv
    assert "mcp__reflect__reflect_context,mcp__reflect__reflect_complete" in command.argv
    assert "--tools" not in command.argv
    assert "sonnet" in command.argv
    assert dict(command.env_overrides)["ENABLE_TOOL_SEARCH"] == "false"
    config = json.loads((context.workspace / "claude-mcp.json").read_text())
    assert config["mcpServers"]["reflect"]["command"] == context.python_executable
    assert config["mcpServers"]["reflect"]["env"]["REFLECT_DB_PATH"] == str(
        context.db_path.resolve()
    )
    baseline = ClaudeAdapter().build_baseline(context)
    assert "--strict-mcp-config" not in baseline.argv
    assert "--tools" in baseline.argv


def test_codex_adapter_is_ephemeral_read_only_and_uses_inline_mcp_config(
    context: AgentTestContext,
) -> None:
    command = CodexAdapter().build(context)
    joined = " ".join(command.argv)

    assert "--ephemeral" in command.argv
    assert "--ignore-user-config" in command.argv
    assert "read-only" in command.argv
    assert "mcp_servers.reflect.command=" in joined
    assert "mcp_servers.reflect.env.REFLECT_DB_PATH=" in joined
    assert "mcp_servers.reflect" not in " ".join(
        CodexAdapter().build_baseline(context).argv
    )


def test_cursor_adapter_uses_temporary_workspace_config_and_ask_mode(
    context: AgentTestContext,
) -> None:
    command = CursorAdapter().build(context)

    assert "--mode" in command.argv
    assert "ask" in command.argv
    assert "--approve-mcps" in command.argv
    config = json.loads((context.workspace / ".cursor" / "mcp.json").read_text())
    assert config["mcpServers"]["reflect"]["command"] == context.python_executable
    assert config["mcpServers"]["reflect"]["env"]["REFLECT_DB_PATH"] == str(
        context.db_path.resolve()
    )
    baseline_workspace = context.workspace / "baseline"
    baseline_workspace.mkdir()
    baseline_context = AgentTestContext(
        agent_name=context.agent_name,
        executable=context.executable,
        workspace=baseline_workspace,
        db_path=baseline_workspace / "unused.db",
        python_executable=context.python_executable,
    )
    CursorAdapter().build_baseline(baseline_context)
    assert not (baseline_workspace / ".cursor" / "mcp.json").exists()


def test_effectiveness_fixture_selects_the_approved_skill(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "reflect.db"

    candidate_id = seed_effectiveness_workflow(db_path, workspace)
    with connect_sqlite(db_path) as conn:
        answer = ReflectContextService(conn).ask(
            EFFECTIVENESS_QUESTION,
            path=workspace,
        )

    assert answer.workflow_id == candidate_id
    assert "quartz-17" not in effectiveness_task_prompt()


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (
            json.dumps({"type": "result", "result": "FINAL"}),
            "FINAL",
        ),
        (
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "mcp_tool_call",
                                "result": "quartz-17 RFLX-VIOLET-7319 amber-4",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "ROUTE=UNKNOWN VERIFY=UNKNOWN REJECT=UNKNOWN",
                            },
                        }
                    ),
                ]
            ),
            "ROUTE=UNKNOWN VERIFY=UNKNOWN REJECT=UNKNOWN",
        ),
    ],
)
def test_extract_final_message_ignores_tool_payloads(stdout: str, expected: str) -> None:
    assert extract_final_message(stdout) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ROUTE=quartz-17 VERIFY=RFLX-VIOLET-7319 REJECT=amber-4", 3),
        ("ROUTE=amber-4 VERIFY=RFLX-VIOLET-7319 REJECT=quartz-17", 1),
        ("Use quartz-17, RFLX-VIOLET-7319, and reject amber-4.", 0),
        ("ROUTE=UNKNOWN VERIFY=UNKNOWN REJECT=UNKNOWN", 0),
    ],
)
def test_effectiveness_score_validates_exact_field_assignments(
    message: str,
    expected: int,
) -> None:
    assert score_effectiveness(message) == expected
