import json

import pytest

from reflect.store.mcp import MCPCallClassifier, MCPIdentity


@pytest.mark.parametrize(
    ("agent", "attrs", "expected"),
    [
        (
            "codex",
            {"mcp.server.name": "reflect", "tool.name": "reflect_context"},
            MCPIdentity("reflect", "reflect_context"),
        ),
        (
            "claude",
            {"gen_ai.client.tool_name": "mcp__reflect__reflect_context"},
            MCPIdentity("reflect", "reflect_context"),
        ),
        (
            "cursor",
            {
                "gen_ai.client.tool_name": "CallMcpTool",
                "gen_ai.client.tool.input": json.dumps(
                    {"server": "jira", "toolName": "search"}
                ),
            },
            MCPIdentity("jira", "search"),
        ),
        (
            "copilot",
            {
                "gen_ai.client.tool_name": "call_mcp_tool",
                "gen_ai.client.tool.input": json.dumps(
                    {"serverName": "github", "mcpTool": "search_code"}
                ),
            },
            MCPIdentity("github", "search_code"),
        ),
        (
            "gemini",
            {
                "gen_ai.client.tool_name": "mcp_tool",
                "gen_ai.client.tool.input": json.dumps(
                    {"mcp": {"server": "docs", "toolName": "lookup"}}
                ),
            },
            MCPIdentity("docs", "lookup"),
        ),
    ],
)
def test_classifier_supports_agent_neutral_mcp_shapes(agent, attrs, expected):
    attrs["gen_ai.client.name"] = agent
    assert MCPCallClassifier().identify(attrs) == expected


def test_classifier_composes_custom_identity_strategy():
    class CustomStrategy:
        def identify(self, attrs):
            return MCPIdentity(attrs.get("vendor.server"), attrs.get("vendor.tool"))

    classifier = MCPCallClassifier((CustomStrategy(),))

    assert classifier.identify(
        {"vendor.server": "custom", "vendor.tool": "inspect"}
    ) == MCPIdentity("custom", "inspect")
