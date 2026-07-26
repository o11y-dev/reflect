"""Fast contract tests for the machine-only agent adapters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .harness import (
    AgentTestContext,
    ClaudeAdapter,
    CodexAdapter,
    CursorAdapter,
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
    config = json.loads((context.workspace / "claude-mcp.json").read_text())
    assert config["mcpServers"]["reflect"]["command"] == context.python_executable
    assert config["mcpServers"]["reflect"]["env"]["REFLECT_DB_PATH"] == str(
        context.db_path.resolve()
    )


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
