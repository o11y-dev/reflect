"""Contracts for declared MCP client support."""

from reflect import core
from reflect.mcp_clients import (
    MCP_CLIENT_CAPABILITIES,
    MCPClientSurface,
    get_mcp_client_capability,
)


def test_mcp_client_matrix_covers_every_implemented_agent() -> None:
    declared_agents = {
        capability.agent_name for capability in MCP_CLIENT_CAPABILITIES
    }

    assert len(declared_agents) == len(MCP_CLIENT_CAPABILITIES)
    assert declared_agents == set(core._IMPLEMENTED_AGENT_SUPPORT)


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
