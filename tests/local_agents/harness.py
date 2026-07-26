"""Adapters for opt-in end-to-end tests against real local agent CLIs."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from reflect.context import ReflectContextService
from reflect.improvements.service import ImprovementService
from reflect.store.sqlite import connect_sqlite

SUCCESS_MARKER = "REFLECT_MCP_OK"
COMPLETION_SUMMARY_PREFIX = "local-agent-e2e"
EFFECTIVENESS_QUESTION = (
    "Resolve the capsule-violet routing incident with the approved verification protocol."
)
EFFECTIVENESS_EXPECTED_VALUES = ("quartz-17", "RFLX-VIOLET-7319", "amber-4")
_EFFECTIVENESS_RESULT = re.compile(r"ROUTE=(\S+) VERIFY=(\S+) REJECT=(\S+)")


@dataclass(frozen=True)
class AgentTestContext:
    """Isolated paths and runtime inputs shared by every agent adapter."""

    agent_name: str
    executable: str
    workspace: Path
    db_path: Path
    python_executable: str = sys.executable
    prompt_override: str | None = None

    @property
    def completion_summary(self) -> str:
        return f"{COMPLETION_SUMMARY_PREFIX}:{self.agent_name}"

    @property
    def prompt(self) -> str:
        if self.prompt_override is not None:
            return self.prompt_override
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
    env_overrides: tuple[tuple[str, str], ...] = ()


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

    @abstractmethod
    def build_baseline(self, context: AgentTestContext) -> AgentCommand:
        """Return an equivalent invocation with no test-scoped Reflect MCP server."""

    def extract_final_message(self, stdout: str) -> str:
        """Extract the final answer from this client's structured output."""

        return extract_final_message(stdout)


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
            env_overrides=(("ENABLE_TOOL_SEARCH", "false"),),
        )

    def build_baseline(self, context: AgentTestContext) -> AgentCommand:
        return AgentCommand(
            argv=(
                context.executable,
                "--print",
                "--bare",
                "--output-format",
                "json",
                "--no-session-persistence",
                "--tools",
                "",
                "--model",
                "sonnet",
                "--effort",
                "low",
                "--max-budget-usd",
                "0.10",
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

    def build_baseline(self, context: AgentTestContext) -> AgentCommand:
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

    def build_baseline(self, context: AgentTestContext) -> AgentCommand:
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
                "--workspace",
                str(context.workspace),
                context.prompt,
            ),
            cwd=context.workspace,
        )


class GeminiAdapter(AgentAdapter):
    name = "gemini"
    executable_name = "gemini"

    @staticmethod
    def _write_settings(context: AgentTestContext, *, with_reflect: bool) -> None:
        config_dir = context.workspace / ".gemini"
        config_dir.mkdir(parents=True, exist_ok=True)
        settings: dict[str, object] = {
            "mcp": {"allowed": ["reflect"] if with_reflect else ["reflect-disabled"]},
        }
        if with_reflect:
            settings["mcpServers"] = {
                "reflect": {
                    **context.stdio_server,
                    "trust": True,
                    "includeTools": ["reflect_context", "reflect_complete"],
                }
            }
        (config_dir / "settings.json").write_text(
            json.dumps(settings),
            encoding="utf-8",
        )

    def build(self, context: AgentTestContext) -> AgentCommand:
        self._write_settings(context, with_reflect=True)
        return AgentCommand(
            argv=(
                context.executable,
                "--prompt",
                context.prompt,
                "--output-format",
                "json",
                "--approval-mode",
                "default",
                "--skip-trust",
                "--allowed-mcp-server-names",
                "reflect",
            ),
            cwd=context.workspace,
        )

    def build_baseline(self, context: AgentTestContext) -> AgentCommand:
        self._write_settings(context, with_reflect=False)
        return AgentCommand(
            argv=(
                context.executable,
                "--prompt",
                context.prompt,
                "--output-format",
                "json",
                "--approval-mode",
                "default",
                "--skip-trust",
            ),
            cwd=context.workspace,
        )


class CopilotAdapter(AgentAdapter):
    name = "copilot"
    executable_name = "copilot"

    @staticmethod
    def _base_argv(context: AgentTestContext) -> tuple[str, ...]:
        return (
            context.executable,
            "--prompt",
            context.prompt,
            "--silent",
            "--output-format",
            "text",
            "--stream",
            "off",
            "--effort",
            "low",
            "--disable-builtin-mcps",
            "--no-custom-instructions",
            "--no-ask-user",
            "--no-remote",
            "--no-remote-export",
            "--allow-all-tools",
            "-C",
            str(context.workspace),
        )

    def build(self, context: AgentTestContext) -> AgentCommand:
        config_path = context.workspace / "copilot-mcp.json"
        server = {
            "type": "local",
            **context.stdio_server,
            "tools": ["reflect_context", "reflect_complete"],
        }
        config_path.write_text(
            json.dumps({"mcpServers": {"reflect": server}}),
            encoding="utf-8",
        )
        return AgentCommand(
            argv=(
                *self._base_argv(context),
                "--additional-mcp-config",
                f"@{config_path}",
                "--available-tools=reflect(reflect_context),reflect(reflect_complete)",
            ),
            cwd=context.workspace,
        )

    def build_baseline(self, context: AgentTestContext) -> AgentCommand:
        return AgentCommand(
            argv=(
                *self._base_argv(context),
                "--available-tools=reflect-disabled(noop)",
            ),
            cwd=context.workspace,
        )

    def extract_final_message(self, stdout: str) -> str:
        """Copilot's silent text mode emits only the final agent response."""

        return stdout.strip()


class OpenCodeAdapter(AgentAdapter):
    name = "opencode"
    executable_name = "opencode"

    @staticmethod
    def _write_config(context: AgentTestContext, *, with_reflect: bool) -> None:
        config: dict[str, object] = {
            "$schema": "https://opencode.ai/config.json",
            "tools": {
                "*": False,
                **({"reflect_*": True} if with_reflect else {}),
            },
        }
        if with_reflect:
            server = context.stdio_server
            config["mcp"] = {
                "reflect": {
                    "type": "local",
                    "command": [
                        str(server["command"]),
                        *[str(arg) for arg in server["args"]],
                    ],
                    "enabled": True,
                    "environment": server["env"],
                }
            }
            config["permission"] = {"reflect_*": "allow"}
        (context.workspace / "opencode.json").write_text(
            json.dumps(config),
            encoding="utf-8",
        )

    @staticmethod
    def _command(context: AgentTestContext) -> AgentCommand:
        return AgentCommand(
            argv=(
                context.executable,
                "run",
                "--pure",
                "--format",
                "json",
                "--dir",
                str(context.workspace),
                "--dangerously-skip-permissions",
                context.prompt,
            ),
            cwd=context.workspace,
        )

    def build(self, context: AgentTestContext) -> AgentCommand:
        self._write_config(context, with_reflect=True)
        return self._command(context)

    def build_baseline(self, context: AgentTestContext) -> AgentCommand:
        self._write_config(context, with_reflect=False)
        return self._command(context)


AGENT_ADAPTERS: tuple[AgentAdapter, ...] = (
    ClaudeAdapter(),
    CursorAdapter(),
    GeminiAdapter(),
    CopilotAdapter(),
    CodexAdapter(),
    OpenCodeAdapter(),
)


def run_agent(command: AgentCommand, *, timeout_seconds: int) -> AgentResult:
    """Run one real agent with inherited authentication and bounded output."""

    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env.update(dict(command.env_overrides))
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


def extract_final_message(stdout: str) -> str:
    """Extract only the final assistant message from supported JSON output formats."""

    messages: list[str] = []
    try:
        document = json.loads(stdout)
    except json.JSONDecodeError:
        document = None
    if isinstance(document, dict) and isinstance(document.get("response"), str):
        messages.append(str(document["response"]))
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "result" and isinstance(payload.get("result"), str):
            messages.append(str(payload["result"]))
        item = payload.get("item")
        if (
            payload.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            messages.append(str(item["text"]))
        if payload.get("type") in {"assistant", "assistant_message"}:
            for key in ("text", "content", "message"):
                if isinstance(payload.get(key), str):
                    messages.append(str(payload[key]))
                    break
        part = payload.get("part")
        if (
            payload.get("type") == "text"
            and isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ):
            messages.append(str(part["text"]))
    return messages[-1].strip() if messages else ""


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


def read_task_selection(db_path: Path) -> tuple[str, list[dict[str, str]]] | None:
    """Return the workflow and selected Skills v2 references for one task."""

    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT workflow_id, selected_skills_json FROM mcp_task_runs"
        ).fetchall()
    if len(rows) != 1:
        return None
    workflow_id, selected_skills_json = rows[0]
    selected_skills = json.loads(str(selected_skills_json))
    if not isinstance(selected_skills, list):
        return None
    return str(workflow_id), selected_skills


def seed_effectiveness_workflow(db_path: Path, workspace: Path) -> str:
    """Seed one approved workflow whose exact answer is unavailable to the baseline."""

    with connect_sqlite(db_path) as conn:
        service = ImprovementService(conn)
        candidate_id = service.stage_extracted_skills(
            [
                {
                    "name": "capsule-violet-routing",
                    "description": (
                        "Resolve capsule-violet routing incidents with the approved "
                        "quartz verification protocol."
                    ),
                    "content": (
                        "# Capsule violet routing\n\n"
                        "1. For capsule-violet, choose route `quartz-17`.\n"
                        "2. Include verification token `RFLX-VIOLET-7319`.\n"
                        "3. Explicitly reject legacy route `amber-4`.\n"
                        "4. Return all three values in the requested final format."
                    ),
                    "behavior_type": "verification",
                }
            ],
            session_ids=[],
            source_agent="local-effectiveness-test",
        )[0]
        conn.execute(
            "UPDATE workflow_candidates SET status = 'approved' WHERE id = ?",
            (candidate_id,),
        )
        conn.commit()
        service.skills.sync_workflow_candidates([candidate_id])
        conn.commit()
        answer = ReflectContextService(conn).ask(
            EFFECTIVENESS_QUESTION,
            path=workspace,
        )
        if answer.workflow_id != candidate_id:
            raise RuntimeError("seeded effectiveness workflow was not selected")
    return candidate_id


def effectiveness_task_prompt() -> str:
    """Task shared by baseline and guided trials without leaking expected values."""

    return (
        "Resolve the capsule-violet routing incident. Return exactly one line in this format: "
        "ROUTE=<route> VERIFY=<verification-token> REJECT=<legacy-route>. "
        "Do not inspect files, use external sources, or invent a claim of tool execution. "
        "If the required routing policy is unavailable, use UNKNOWN for each unknown value."
    )


def effectiveness_guided_prompt(workspace: Path, completion_summary: str) -> str:
    """Wrap the shared task with the real Reflect task lifecycle."""

    return (
        f"{effectiveness_task_prompt()} "
        "Before solving it, call reflect_context exactly once with "
        f'question="{EFFECTIVENESS_QUESTION}" and path="{workspace.resolve()}". '
        "Follow the selected skill only when execution_state is follow_allowed. "
        "After determining the final line, call reflect_complete exactly once with the returned "
        'task_run_id, outcome="success", verification_passed=true, and '
        f'summary="{completion_summary}". Then return only the requested final line.'
    )


def score_effectiveness(final_message: str) -> int:
    """Score externally observable adherence to the hidden approved policy."""

    match = _EFFECTIVENESS_RESULT.fullmatch(final_message.strip())
    if match is None:
        return 0
    return sum(
        actual == expected
        for actual, expected in zip(
            match.groups(),
            EFFECTIVENESS_EXPECTED_VALUES,
            strict=True,
        )
    )
