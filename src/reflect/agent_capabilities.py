"""Canonical product capabilities for every coding agent Reflect knows about."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class AgentSupport(StrEnum):
    SUPPORTED = "Supported"
    PARTIAL = "Partial"
    HISTORICAL = "Historical"
    PLANNED = "Planned"


class MCPClientSurface(StrEnum):
    HEADLESS_CLI = "Headless CLI"
    EDITOR_CONFIG = "Editor config"


@dataclass(frozen=True)
class MCPClientCapability:
    agent_name: str
    config_surface: str
    surface: MCPClientSurface
    local_agent_name: str | None = None
    executable: str | None = None

    @property
    def locally_testable(self) -> bool:
        return self.local_agent_name is not None and self.executable is not None


@dataclass(frozen=True)
class AgentCapability:
    key: str
    display_name: str
    home_parts: tuple[str, ...]
    env_names: tuple[str, ...]
    local_skill_path: str
    global_skill_path: str
    support: AgentSupport
    telemetry_path: str
    confidence: str
    recommendation: str
    aliases: tuple[str, ...] = ()
    hook_agent: str | None = None
    native_session_adapter: bool = False
    skill_cli: str | None = None
    skill_cli_flags: tuple[str, ...] = ()
    mcp: MCPClientCapability | None = None

    def home(self) -> Path:
        for env_name in self.env_names:
            if override := os.environ.get(env_name):
                return Path(override).expanduser()
        return Path.home().joinpath(*self.home_parts)

    @property
    def setup_names(self) -> tuple[str, ...]:
        return (self.display_name, self.key, *self.aliases)


AGENT_CAPABILITIES: tuple[AgentCapability, ...] = (
    AgentCapability(
        key="claude",
        display_name="Claude Code",
        aliases=("claude-code",),
        home_parts=(".claude",),
        env_names=("CLAUDE_HOME",),
        local_skill_path=".claude/skills/",
        global_skill_path="~/.claude/skills/",
        support=AgentSupport.SUPPORTED,
        telemetry_path="Native OTel + hooks + native sessions",
        confidence="High",
        recommendation="Use native OTel and hooks; Reflect also reads Claude transcripts for conversation detail.",
        hook_agent="claude",
        native_session_adapter=True,
        skill_cli="claude",
        skill_cli_flags=("--print",),
        mcp=MCPClientCapability(
            agent_name="Claude Code",
            config_surface="~/.claude.json",
            surface=MCPClientSurface.HEADLESS_CLI,
            local_agent_name="claude",
            executable="claude",
        ),
    ),
    AgentCapability(
        key="cursor",
        display_name="Cursor",
        aliases=("cursor-agent",),
        home_parts=(".cursor",),
        env_names=("CURSOR_HOME",),
        local_skill_path=".agents/skills/",
        global_skill_path="~/.cursor/skills/",
        support=AgentSupport.SUPPORTED,
        telemetry_path="Session/log adapters + optional hooks",
        confidence="Medium",
        recommendation="Use native session and log adapters for desktop; hooks cover supported CLI launches.",
        hook_agent="cursor",
        native_session_adapter=True,
        skill_cli="cursor-agent",
        skill_cli_flags=("--print", "--trust", "--mode", "ask"),
        mcp=MCPClientCapability(
            agent_name="Cursor",
            config_surface=".cursor/mcp.json",
            surface=MCPClientSurface.HEADLESS_CLI,
            local_agent_name="cursor",
            executable="cursor-agent",
        ),
    ),
    AgentCapability(
        key="antigravity",
        display_name="Antigravity",
        aliases=("agy", "antigravity-cli"),
        home_parts=(".gemini", "antigravity-cli"),
        env_names=("ANTIGRAVITY_HOME",),
        local_skill_path=".agents/skills/",
        global_skill_path="~/.gemini/config/skills/",
        support=AgentSupport.PARTIAL,
        telemetry_path="Reflect MCP task runs; native tool telemetry unverified",
        confidence="Medium",
        recommendation="Use agy with Reflect MCP. Antigravity does not currently expose a supported hook surface.",
        skill_cli="agy",
        skill_cli_flags=(
            "--output-format",
            "text",
            "--sandbox",
            "--disable-slash-commands",
            "--print",
        ),
        mcp=MCPClientCapability(
            agent_name="Antigravity",
            config_surface="~/.gemini/config/mcp_config.json",
            surface=MCPClientSurface.HEADLESS_CLI,
            local_agent_name="antigravity",
            executable="agy",
        ),
    ),
    AgentCapability(
        key="copilot",
        display_name="GitHub Copilot",
        aliases=("github-copilot",),
        home_parts=(".copilot",),
        env_names=("COPILOT_HOME",),
        local_skill_path=".agents/skills/",
        global_skill_path="~/.copilot/skills/",
        support=AgentSupport.SUPPORTED,
        telemetry_path="Native OTel + hooks + native sessions",
        confidence="High",
        recommendation="Prefer native Copilot OTel on OTLP HTTP; use hooks for additional governance events.",
        hook_agent="copilot",
        native_session_adapter=True,
        skill_cli="copilot",
        skill_cli_flags=("--prompt",),
        mcp=MCPClientCapability(
            agent_name="GitHub Copilot",
            config_surface="~/.copilot/mcp-config.json",
            surface=MCPClientSurface.HEADLESS_CLI,
            local_agent_name="copilot",
            executable="copilot",
        ),
    ),
    AgentCapability(
        key="codex",
        display_name="OpenAI Codex CLI",
        aliases=("codex-cli", "openai-codex"),
        home_parts=(".codex",),
        env_names=("CODEX_HOME",),
        local_skill_path=".agents/skills/",
        global_skill_path="~/.agents/skills/",
        support=AgentSupport.SUPPORTED,
        telemetry_path="Native OTel + native sessions",
        confidence="High",
        recommendation="Use native Codex OTel; Reflect reads Codex rollouts for conversations, tool calls, MCP, context, and memory evidence.",
        hook_agent="codex",
        native_session_adapter=True,
        skill_cli="codex",
        skill_cli_flags=("exec",),
        mcp=MCPClientCapability(
            agent_name="OpenAI Codex CLI",
            config_surface="~/.codex/config.toml",
            surface=MCPClientSurface.HEADLESS_CLI,
            local_agent_name="codex",
            executable="codex",
        ),
    ),
    AgentCapability(
        key="opencode",
        display_name="OpenCode",
        home_parts=(".config", "opencode"),
        env_names=("OPENCODE_HOME",),
        local_skill_path=".opencode/skills/",
        global_skill_path="~/.config/opencode/skills/",
        support=AgentSupport.SUPPORTED,
        telemetry_path="Native SQLite sessions + hooks",
        confidence="Medium",
        recommendation="Use OpenCode native sessions; hooks add supported lifecycle events.",
        hook_agent="opencode",
        native_session_adapter=True,
        skill_cli="opencode",
        skill_cli_flags=("run",),
        mcp=MCPClientCapability(
            agent_name="OpenCode",
            config_surface="~/.config/opencode/opencode.json",
            surface=MCPClientSurface.HEADLESS_CLI,
            local_agent_name="opencode",
            executable="opencode",
        ),
    ),
    AgentCapability(
        key="windsurf",
        display_name="Windsurf",
        home_parts=(".codeium", "windsurf"),
        env_names=("WINDSURF_HOME",),
        local_skill_path=".windsurf/skills/",
        global_skill_path="~/.codeium/windsurf/skills/",
        support=AgentSupport.SUPPORTED,
        telemetry_path="Hooks + configuration snapshots",
        confidence="Medium",
        recommendation="Use opentelemetry-hooks; native OTel is not currently declared.",
        hook_agent="windsurf",
        mcp=MCPClientCapability(
            agent_name="Windsurf",
            config_surface="~/.codeium/windsurf/mcp_config.json",
            surface=MCPClientSurface.EDITOR_CONFIG,
        ),
    ),
    AgentCapability(
        key="gemini",
        display_name="Gemini CLI",
        aliases=("gemini-cli",),
        home_parts=(".gemini",),
        env_names=("GEMINI_HOME", "GEMINI_DIR"),
        local_skill_path=".agents/skills/",
        global_skill_path="~/.gemini/skills/",
        support=AgentSupport.HISTORICAL,
        telemetry_path="Historical native OTel + session adapter",
        confidence="High",
        recommendation="Existing Gemini telemetry remains readable; use Antigravity for new local simulations.",
        native_session_adapter=True,
    ),
    *(
        AgentCapability(
            key=key,
            display_name=name,
            home_parts=home_parts,
            env_names=(env_name,),
            local_skill_path=local_skill_path,
            global_skill_path=global_skill_path,
            support=AgentSupport.PLANNED,
            telemetry_path="Not implemented",
            confidence="Planned",
            recommendation="Telemetry support is not yet verified; Reflect only inventories local configuration.",
        )
        for key, name, home_parts, env_name, local_skill_path, global_skill_path in (
            ("trae", "Trae", (".trae",), "TRAE_HOME", ".trae/skills/", "~/.trae/skills/"),
            ("cline", "Cline", (".agents",), "CLINE_HOME", ".agents/skills/", "~/.agents/skills/"),
            ("roo", "Roo Code", (".roo",), "ROO_HOME", ".roo/skills/", "~/.roo/skills/"),
            ("continue", "Continue", (".continue",), "CONTINUE_HOME", ".continue/skills/", "~/.continue/skills/"),
            ("goose", "Goose", (".config", "goose"), "GOOSE_HOME", ".goose/skills/", "~/.config/goose/skills/"),
            ("openhands", "OpenHands", (".openhands",), "OPENHANDS_HOME", ".openhands/skills/", "~/.openhands/skills/"),
            ("amp", "Amp", (".local", "share", "amp"), "AMP_HOME", ".agents/skills/", "~/.config/agents/skills/"),
            ("iflow", "iFlow", (".iflow",), "IFLOW_HOME", ".iflow/skills/", "~/.iflow/skills/"),
            ("pi", "Pi", (".pi",), "PI_HOME", ".pi/skills/", "~/.pi/agent/skills/"),
            ("openclaw", "OpenClaw", (".openclaw",), "OPENCLAW_HOME", "skills/", "~/.openclaw/skills/"),
        )
    ),
)

def _identity_token(name: object) -> str:
    return "-".join(str(name or "").strip().lower().replace("_", "-").split())


_BY_KEY = {
    _identity_token(name): capability
    for capability in AGENT_CAPABILITIES
    for name in capability.setup_names
}


def get_agent_capability(name: str) -> AgentCapability | None:
    return _BY_KEY.get(_identity_token(name))


def normalize_agent_key(name: object) -> str:
    normalized = _identity_token(name)
    capability = _BY_KEY.get(normalized)
    return capability.key if capability is not None else normalized


def setup_agent_capabilities() -> tuple[AgentCapability, ...]:
    return AGENT_CAPABILITIES


def skill_agent_capabilities() -> tuple[AgentCapability, ...]:
    return tuple(item for item in AGENT_CAPABILITIES if item.skill_cli is not None)


def mcp_agent_capabilities() -> tuple[AgentCapability, ...]:
    return tuple(item for item in AGENT_CAPABILITIES if item.mcp is not None)
