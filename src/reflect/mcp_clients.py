"""Declared MCP client surfaces for Reflect's implemented agents."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MCPClientSurface(StrEnum):
    """How an agent can connect to a local Reflect MCP server."""

    HEADLESS_CLI = "Headless CLI"
    EDITOR_CONFIG = "Editor config"


@dataclass(frozen=True)
class MCPClientCapability:
    """One implemented agent's MCP client and local-test contract."""

    agent_name: str
    config_surface: str
    surface: MCPClientSurface
    local_agent_name: str | None = None
    executable: str | None = None

    @property
    def locally_testable(self) -> bool:
        """Whether the machine-only suite can exercise this client headlessly."""

        return self.local_agent_name is not None and self.executable is not None


MCP_CLIENT_CAPABILITIES: tuple[MCPClientCapability, ...] = (
    MCPClientCapability(
        agent_name="Claude Code",
        config_surface="claude mcp / --mcp-config",
        surface=MCPClientSurface.HEADLESS_CLI,
        local_agent_name="claude",
        executable="claude",
    ),
    MCPClientCapability(
        agent_name="Cursor",
        config_surface=".cursor/mcp.json",
        surface=MCPClientSurface.HEADLESS_CLI,
        local_agent_name="cursor",
        executable="cursor-agent",
    ),
    MCPClientCapability(
        agent_name="Gemini CLI",
        config_surface=".gemini/settings.json",
        surface=MCPClientSurface.HEADLESS_CLI,
        local_agent_name="gemini",
        executable="gemini",
    ),
    MCPClientCapability(
        agent_name="GitHub Copilot",
        config_surface="--additional-mcp-config",
        surface=MCPClientSurface.HEADLESS_CLI,
        local_agent_name="copilot",
        executable="copilot",
    ),
    MCPClientCapability(
        agent_name="OpenAI Codex CLI",
        config_surface="codex mcp / inline config",
        surface=MCPClientSurface.HEADLESS_CLI,
        local_agent_name="codex",
        executable="codex",
    ),
    MCPClientCapability(
        agent_name="Windsurf",
        config_surface="~/.codeium/windsurf/mcp_config.json",
        surface=MCPClientSurface.EDITOR_CONFIG,
    ),
    MCPClientCapability(
        agent_name="OpenCode",
        config_surface="opencode.json",
        surface=MCPClientSurface.HEADLESS_CLI,
        local_agent_name="opencode",
        executable="opencode",
    ),
)

_MCP_CLIENTS_BY_AGENT = {
    capability.agent_name: capability for capability in MCP_CLIENT_CAPABILITIES
}


def get_mcp_client_capability(agent_name: str) -> MCPClientCapability | None:
    """Return the declared MCP client capability for an agent display name."""

    return _MCP_CLIENTS_BY_AGENT.get(agent_name)


def local_mcp_agent_names() -> tuple[str, ...]:
    """Return stable suite names for every headless local MCP client."""

    return tuple(
        capability.local_agent_name
        for capability in MCP_CLIENT_CAPABILITIES
        if capability.local_agent_name is not None
    )
