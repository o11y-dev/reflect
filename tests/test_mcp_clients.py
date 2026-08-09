"""Contracts for declared MCP client support."""

import json

import pytest

from reflect.agent_capabilities import AgentSupport, MCPClientSurface, get_agent_capability
from reflect.mcp_clients import (
    MCP_CLIENT_CAPABILITIES,
    MCP_CLIENT_CONFIGURATORS,
    configure_reflect_mcp,
    get_mcp_client_capability,
)


def test_mcp_client_matrix_uses_canonical_agent_capabilities() -> None:
    declared_agents = {
        capability.agent_name for capability in MCP_CLIENT_CAPABILITIES
    }

    assert len(declared_agents) == len(MCP_CLIENT_CAPABILITIES)
    assert "Antigravity" in declared_agents
    assert "Gemini CLI" not in declared_agents
    assert get_agent_capability("Gemini CLI").support is AgentSupport.HISTORICAL
    assert get_agent_capability("Antigravity").support is AgentSupport.PARTIAL


def test_headless_clients_have_complete_local_test_identity() -> None:
    for capability in MCP_CLIENT_CAPABILITIES:
        if capability.surface == MCPClientSurface.HEADLESS_CLI:
            assert capability.locally_testable
            assert capability.local_agent_name
            assert capability.executable
        else:
            assert not capability.locally_testable


def test_windsurf_is_explicitly_editor_config_only() -> None:
    capability = get_mcp_client_capability("Windsurf")

    assert capability is not None
    assert capability.surface == MCPClientSurface.EDITOR_CONFIG
    assert capability.config_surface == "~/.codeium/windsurf/mcp_config.json"


def test_mcp_configurator_matrix_covers_every_declared_client() -> None:
    assert {
        configurator.agent_name for configurator in MCP_CLIENT_CONFIGURATORS
    } == {
        capability.agent_name for capability in MCP_CLIENT_CAPABILITIES
    }


def test_cursor_configurator_preserves_existing_servers_and_is_idempotent(
    tmp_path,
) -> None:
    cursor_home = tmp_path / ".cursor"
    cursor_home.mkdir()
    config_path = cursor_home / "mcp.json"
    config_path.write_text(
        '{"mcpServers":{"existing":{"command":"existing-mcp","args":["serve"]}}}\n',
        encoding="utf-8",
    )

    first = configure_reflect_mcp(
        "Cursor",
        cursor_home,
        command="/usr/local/bin/reflect-mcp",
    )
    second = configure_reflect_mcp(
        "Cursor",
        cursor_home,
        command="/usr/local/bin/reflect-mcp",
    )

    assert first is not None and first.changed
    assert second is not None and not second.changed
    config = json.loads(config_path.read_text())
    assert config["mcpServers"]["existing"] == {
        "command": "existing-mcp",
        "args": ["serve"],
    }
    assert config["mcpServers"]["reflect"] == {
        "command": "/usr/local/bin/reflect-mcp",
        "args": [],
    }


def test_antigravity_configurator_initializes_an_empty_config(tmp_path) -> None:
    antigravity_home = tmp_path / ".gemini" / "antigravity-cli"
    config_path = antigravity_home.parent / "config" / "mcp_config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("", encoding="utf-8")

    result = configure_reflect_mcp(
        "Antigravity",
        antigravity_home,
        command="/usr/local/bin/reflect-mcp",
    )

    assert result is not None and result.changed
    assert json.loads(config_path.read_text()) == {
        "mcpServers": {
            "reflect": {
                "command": "/usr/local/bin/reflect-mcp",
                "args": [],
            }
        }
    }


def test_codex_configurator_preserves_reflect_server_options(tmp_path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(
        '[model]\nname = "gpt-test"\n\n'
        "[mcp_servers.reflect]\n"
        'command = "old-reflect-mcp"\n'
        'args = ["-m", "reflect.mcp"]\n'
        "startup_timeout_sec = 20\n",
        encoding="utf-8",
    )

    result = configure_reflect_mcp(
        "OpenAI Codex CLI",
        codex_home,
        command="/usr/local/bin/reflect-mcp",
    )

    assert result is not None and result.changed
    updated = config_path.read_text()
    assert 'name = "gpt-test"' in updated
    assert 'command = "/usr/local/bin/reflect-mcp"' in updated
    assert "args = []" in updated
    assert "startup_timeout_sec = 20" in updated


@pytest.mark.parametrize(
    ("agent_name", "agent_home", "relative_path", "container_key", "expected_server"),
    (
        (
            "Claude Code",
            ".claude",
            "../.claude.json",
            "mcpServers",
            {"command": "/usr/local/bin/reflect-mcp", "args": []},
        ),
        (
            "Cursor",
            ".cursor",
            "mcp.json",
            "mcpServers",
            {"command": "/usr/local/bin/reflect-mcp", "args": []},
        ),
        (
            "Antigravity",
            ".gemini/antigravity-cli",
            "../config/mcp_config.json",
            "mcpServers",
            {"command": "/usr/local/bin/reflect-mcp", "args": []},
        ),
        (
            "GitHub Copilot",
            ".copilot",
            "mcp-config.json",
            "mcpServers",
            {
                "type": "local",
                "command": "/usr/local/bin/reflect-mcp",
                "args": [],
                "tools": ["*"],
            },
        ),
        (
            "Windsurf",
            ".codeium/windsurf",
            "mcp_config.json",
            "mcpServers",
            {"command": "/usr/local/bin/reflect-mcp", "args": []},
        ),
        (
            "OpenCode",
            ".config/opencode",
            "opencode.json",
            "mcp",
            {
                "type": "local",
                "command": ["/usr/local/bin/reflect-mcp"],
                "enabled": True,
            },
        ),
    ),
)
def test_json_configurators_write_each_agent_contract(
    tmp_path,
    agent_name,
    agent_home,
    relative_path,
    container_key,
    expected_server,
) -> None:
    home = tmp_path / agent_home

    result = configure_reflect_mcp(
        agent_name,
        home,
        command="/usr/local/bin/reflect-mcp",
    )

    assert result is not None and result.changed
    assert result.path == (home / relative_path).resolve()
    config = json.loads(result.path.read_text())
    assert config[container_key]["reflect"] == expected_server
