"""Declared MCP client surfaces for Reflect's implemented agents."""

from __future__ import annotations

import json as _json_stdlib
import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from reflect.utils import _json_loads


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


@dataclass(frozen=True)
class MCPConfigurationResult:
    """Result of merging the Reflect server into one agent configuration."""

    path: Path
    changed: bool


class MCPClientConfigurator(Protocol):
    """Agent-specific persistent MCP configuration strategy."""

    agent_name: str

    def config_path(self, agent_home: Path) -> Path:
        """Return the user-scoped configuration path for this agent."""

    def configure(
        self,
        agent_home: Path,
        *,
        command: str,
    ) -> MCPConfigurationResult:
        """Merge the Reflect MCP server without removing user-owned configuration."""


@dataclass(frozen=True)
class JsonMCPClientConfigurator:
    """Merge a Reflect server into an agent's JSON MCP map."""

    agent_name: str
    filename: str
    container_key: str = "mcpServers"
    parent_of_home: bool = False
    server_kind: str = "standard"

    def config_path(self, agent_home: Path) -> Path:
        base = agent_home.parent if self.parent_of_home else agent_home
        return base / self.filename

    def _server_config(self, command: str) -> dict[str, object]:
        if self.server_kind == "copilot":
            return {
                "type": "local",
                "command": command,
                "args": [],
                "tools": ["*"],
            }
        if self.server_kind == "opencode":
            return {
                "type": "local",
                "command": [command],
                "enabled": True,
            }
        return {"command": command, "args": []}

    def configure(
        self,
        agent_home: Path,
        *,
        command: str,
    ) -> MCPConfigurationResult:
        path = self.config_path(agent_home)
        if path.exists():
            data = _json_loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError(f"{path} must contain a JSON object")
        else:
            data = {}

        servers = data.get(self.container_key)
        if servers is None:
            servers = {}
            data[self.container_key] = servers
        if not isinstance(servers, dict):
            raise ValueError(f"{path} field {self.container_key!r} must be an object")

        desired = self._server_config(command)
        current = servers.get("reflect")
        merged = {**current, **desired} if isinstance(current, dict) else desired
        changed = current != merged
        if changed:
            servers["reflect"] = merged
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_json_stdlib.dumps(data, indent=2) + "\n", encoding="utf-8")
        return MCPConfigurationResult(path=path, changed=changed)


@dataclass(frozen=True)
class CodexMCPClientConfigurator:
    """Merge the Reflect server into Codex config.toml."""

    agent_name: str = "OpenAI Codex CLI"

    def config_path(self, agent_home: Path) -> Path:
        return agent_home / "config.toml"

    def configure(
        self,
        agent_home: Path,
        *,
        command: str,
    ) -> MCPConfigurationResult:
        path = self.config_path(agent_home)
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        parsed = tomllib.loads(original) if original.strip() else {}
        mcp_servers = parsed.get("mcp_servers")
        current = mcp_servers.get("reflect") if isinstance(mcp_servers, dict) else None
        if (
            isinstance(current, dict)
            and current.get("command") == command
            and current.get("args") in (None, [])
        ):
            return MCPConfigurationResult(path=path, changed=False)

        quoted_command = _json_stdlib.dumps(command)
        section_pattern = r"(?ms)^\[mcp_servers\.reflect\]\n.*?(?=^\[|\Z)"
        section_match = re.search(section_pattern, original)
        if section_match:
            section = section_match.group(0).rstrip()
            command_pattern = r"(?m)^command\s*=.*$"
            if re.search(command_pattern, section):
                replacement = re.sub(
                    command_pattern,
                    f"command = {quoted_command}",
                    section,
                    count=1,
                )
            else:
                replacement = (
                    "[mcp_servers.reflect]\n"
                    f"command = {quoted_command}\n"
                    + section.removeprefix("[mcp_servers.reflect]\n")
                ).rstrip()
            if isinstance(current, dict) and current.get("args") not in (None, []):
                args_pattern = (
                    r"(?ms)^args\s*=\s*\[.*?\]\s*"
                    r"(?=^[A-Za-z0-9_-]+\s*=|^\[|\Z)"
                )
                if not re.search(args_pattern, replacement):
                    raise ValueError(
                        f"Could not safely replace mcp_servers.reflect.args in {path}"
                    )
                replacement = re.sub(
                    args_pattern,
                    "args = []\n",
                    replacement,
                    count=1,
                ).rstrip()
            updated = (
                original[:section_match.start()]
                + replacement
                + "\n\n"
                + original[section_match.end():].lstrip("\n")
            )
        else:
            block = f"[mcp_servers.reflect]\ncommand = {quoted_command}\n"
            updated = block if not original.strip() else original.rstrip() + "\n\n" + block

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated.rstrip() + "\n", encoding="utf-8")
        return MCPConfigurationResult(path=path, changed=True)


MCP_CLIENT_CAPABILITIES: tuple[MCPClientCapability, ...] = (
    MCPClientCapability(
        agent_name="Claude Code",
        config_surface="~/.claude.json",
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
        config_surface="~/.copilot/mcp-config.json",
        surface=MCPClientSurface.HEADLESS_CLI,
        local_agent_name="copilot",
        executable="copilot",
    ),
    MCPClientCapability(
        agent_name="OpenAI Codex CLI",
        config_surface="~/.codex/config.toml",
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
        config_surface="~/.config/opencode/opencode.json",
        surface=MCPClientSurface.HEADLESS_CLI,
        local_agent_name="opencode",
        executable="opencode",
    ),
)

_MCP_CLIENTS_BY_AGENT = {
    capability.agent_name: capability for capability in MCP_CLIENT_CAPABILITIES
}

MCP_CLIENT_CONFIGURATORS: tuple[MCPClientConfigurator, ...] = (
    JsonMCPClientConfigurator(
        agent_name="Claude Code",
        filename=".claude.json",
        parent_of_home=True,
    ),
    JsonMCPClientConfigurator(agent_name="Cursor", filename="mcp.json"),
    JsonMCPClientConfigurator(agent_name="Gemini CLI", filename="settings.json"),
    JsonMCPClientConfigurator(
        agent_name="GitHub Copilot",
        filename="mcp-config.json",
        server_kind="copilot",
    ),
    CodexMCPClientConfigurator(),
    JsonMCPClientConfigurator(agent_name="Windsurf", filename="mcp_config.json"),
    JsonMCPClientConfigurator(
        agent_name="OpenCode",
        filename="opencode.json",
        container_key="mcp",
        server_kind="opencode",
    ),
)

_MCP_CONFIGURATORS_BY_AGENT = {
    configurator.agent_name: configurator for configurator in MCP_CLIENT_CONFIGURATORS
}


def get_mcp_client_capability(agent_name: str) -> MCPClientCapability | None:
    """Return the declared MCP client capability for an agent display name."""

    return _MCP_CLIENTS_BY_AGENT.get(agent_name)


def get_mcp_client_configurator(agent_name: str) -> MCPClientConfigurator | None:
    """Return the persistent MCP configuration strategy for an agent."""

    return _MCP_CONFIGURATORS_BY_AGENT.get(agent_name)


def configure_reflect_mcp(
    agent_name: str,
    agent_home: Path,
    *,
    command: str,
) -> MCPConfigurationResult | None:
    """Persist the Reflect MCP server for one supported detected agent."""

    configurator = get_mcp_client_configurator(agent_name)
    if configurator is None:
        return None
    return configurator.configure(agent_home, command=command)


def local_mcp_agent_names() -> tuple[str, ...]:
    """Return stable suite names for every headless local MCP client."""

    return tuple(
        capability.local_agent_name
        for capability in MCP_CLIENT_CAPABILITIES
        if capability.local_agent_name is not None
    )
