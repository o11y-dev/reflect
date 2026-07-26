"""Adapters for opt-in end-to-end tests against real local agent CLIs."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

SUCCESS_MARKER = "REFLECT_MCP_OK"
COMPLETION_SUMMARY_PREFIX = "local-agent-e2e"


@dataclass(frozen=True)
class AgentTestContext:
    """Isolated paths and runtime inputs shared by every agent adapter."""

    agent_name: str
    executable: str
    workspace: Path
    db_path: Path
    python_executable: str = sys.executable

    @property
    def completion_summary(self) -> str:
        return f"{COMPLETION_SUMMARY_PREFIX}:{self.agent_name}"

    @property
    def prompt(self) -> str:
        return (
            "Run this exact local Reflect MCP smoke test. "
            "Do not read or change files and do not call any non-Reflect tool. "
            "Call reflect_context exactly once with "
            f'question="local MCP smoke test for {self.agent_name}" and '
            f'path="{self.workspace.resolve()}". '
            "Then call reflect_complete exactly once with the returned task_run_id, "
            'outcome="success", verification_passed=true, and '
            f'summary="{self.completion_summary}". '
            f"After both tool calls succeed, answer with exactly {SUCCESS_MARKER}."
        )

    @property
    def stdio_server(self) -> dict[str, object]:
        return {
            "command": self.python_executable,
            "args": ["-m", "reflect.mcp"],
            "env": {
                "PYTHONUNBUFFERED": "1",
                "REFLECT_DB_PATH": str(self.db_path.resolve()),
            },
        }


@dataclass(frozen=True)
class AgentCommand:
    """One non-interactive agent invocation."""

    argv: tuple[str, ...]
    cwd: Path


@dataclass(frozen=True)
class AgentResult:
    """Captured local-agent process result with bounded diagnostics."""

    command: AgentCommand
    returncode: int
    stdout: str
    stderr: str

    def diagnostic(self, limit: int = 6000) -> str:
        combined = (
            f"command: {' '.join(self.command.argv[:8])} ...\n"
            f"exit: {self.returncode}\n"
            f"stdout:\n{self.stdout}\n"
            f"stderr:\n{self.stderr}"
        )
        if len(combined) <= limit:
            return combined
        return f"{combined[:limit]}\n... diagnostic truncated ..."


class AgentAdapter(ABC):
    """Build a safe, non-interactive command for one supported agent."""

    name: str
    executable_name: str

    @abstractmethod
    def build(self, context: AgentTestContext) -> AgentCommand:
        """Prepare any temporary configuration and return the agent command."""


class ClaudeAdapter(AgentAdapter):
    name = "claude"
    executable_name = "claude"

    def build(self, context: AgentTestContext) -> AgentCommand:
        config_path = context.workspace / "claude-mcp.json"
        config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "reflect": {
                            "type": "stdio",
                            **context.stdio_server,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return AgentCommand(
            argv=(
                context.executable,
                "--print",
                "--bare",
                "--output-format",
                "json",
                "--no-session-persistence",
                "--strict-mcp-config",
                "--mcp-config",
                str(config_path),
                "--permission-mode",
                "dontAsk",
                "--allowedTools",
                "mcp__reflect__reflect_context,mcp__reflect__reflect_complete",
                "--model",
                "sonnet",
                "--effort",
                "low",
                "--max-budget-usd",
                "0.25",
                context.prompt,
            ),
            cwd=context.workspace,
        )


class CodexAdapter(AgentAdapter):
    name = "codex"
    executable_name = "codex"

    def build(self, context: AgentTestContext) -> AgentCommand:
        server = context.stdio_server
        env = server["env"]
        assert isinstance(env, dict)
        return AgentCommand(
            argv=(
                context.executable,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--cd",
                str(context.workspace),
                "--json",
                "--config",
                'approval_policy="never"',
                "--config",
                f"mcp_servers.reflect.command={json.dumps(str(server['command']))}",
                "--config",
                'mcp_servers.reflect.args=["-m","reflect.mcp"]',
                "--config",
                "mcp_servers.reflect.env.PYTHONUNBUFFERED="
                f"{json.dumps(str(env['PYTHONUNBUFFERED']))}",
                "--config",
                "mcp_servers.reflect.env.REFLECT_DB_PATH="
                f"{json.dumps(str(env['REFLECT_DB_PATH']))}",
                context.prompt,
            ),
            cwd=context.workspace,
        )


class CursorAdapter(AgentAdapter):
    name = "cursor"
    executable_name = "cursor-agent"

    def build(self, context: AgentTestContext) -> AgentCommand:
        config_dir = context.workspace / ".cursor"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "mcp.json").write_text(
            json.dumps({"mcpServers": {"reflect": context.stdio_server}}),
            encoding="utf-8",
        )
        return AgentCommand(
            argv=(
                context.executable,
                "--print",
                "--output-format",
                "json",
                "--mode",
                "ask",
                "--sandbox",
                "enabled",
                "--trust",
                "--approve-mcps",
                "--workspace",
                str(context.workspace),
                context.prompt,
            ),
            cwd=context.workspace,
        )


AGENT_ADAPTERS: tuple[AgentAdapter, ...] = (
    ClaudeAdapter(),
    CodexAdapter(),
    CursorAdapter(),
)


def run_agent(command: AgentCommand, *, timeout_seconds: int) -> AgentResult:
    """Run one real agent with inherited authentication and bounded output."""

    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    completed = subprocess.run(
        command.argv,
        cwd=command.cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return AgentResult(
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def read_completed_task(db_path: Path) -> tuple[str, str, int | None, str, str] | None:
    """Read the single lifecycle record produced by the MCP smoke test."""

    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT status,
                   outcome,
                   verification_passed,
                   completion_summary_redacted,
                   workspace_path
            FROM mcp_task_runs
            ORDER BY started_at
            """
        ).fetchall()
    if len(rows) != 1:
        return None
    status, outcome, verification_passed, summary, workspace_path = rows[0]
    return (
        str(status),
        str(outcome),
        verification_passed,
        str(summary),
        str(workspace_path),
    )
