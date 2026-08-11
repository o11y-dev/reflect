"""Agent-neutral MCP call identification and canonical-ledger repair."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol


def _first_text(attrs: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _load_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True, slots=True)
class MCPIdentity:
    server_name: str | None = None
    tool_name: str | None = None


class MCPIdentityStrategy(Protocol):
    def identify(self, attrs: dict[str, Any]) -> MCPIdentity | None: ...


class AttributeMCPIdentityStrategy:
    """Read hook attributes and current OpenTelemetry MCP attributes."""

    def identify(self, attrs: dict[str, Any]) -> MCPIdentity | None:
        server = _first_text(
            attrs,
            "gen_ai.client.mcp_server",
            "mcp.server.name",
            "mcp.server",
            "server.name",
        )
        tool = _first_text(
            attrs,
            "gen_ai.client.mcp_tool",
            "mcp.tool.name",
            "mcp.tool",
            "tool.name",
        )
        return MCPIdentity(server, tool) if server or tool else None


class PayloadMCPIdentityStrategy:
    """Read MCP identity from Cursor/Copilot/Gemini-style tool payloads."""

    def identify(self, attrs: dict[str, Any]) -> MCPIdentity | None:
        payload = _load_object(
            attrs.get("gen_ai.client.tool.input") or attrs.get("tool.input")
        )
        nested = payload.get("mcp") if isinstance(payload.get("mcp"), dict) else {}
        server = next(
            (
                value.strip()
                for value in (
                    payload.get("server"),
                    payload.get("serverName"),
                    payload.get("mcpServer"),
                    nested.get("server"),
                    nested.get("serverName"),
                )
                if isinstance(value, str) and value.strip()
            ),
            None,
        )
        tool = next(
            (
                value.strip()
                for value in (
                    payload.get("toolName"),
                    payload.get("tool"),
                    payload.get("mcpTool"),
                    nested.get("tool"),
                    nested.get("toolName"),
                )
                if isinstance(value, str) and value.strip()
            ),
            None,
        )
        return MCPIdentity(server, tool) if server or tool else None


class EncodedMCPToolNameStrategy:
    """Decode the ``mcp__server__tool`` convention shared by agent CLIs."""

    def identify(self, attrs: dict[str, Any]) -> MCPIdentity | None:
        tool_name = _first_text(
            attrs,
            "gen_ai.client.tool_name",
            "ide.tool_name",
            "tool.name",
        )
        if not tool_name or not tool_name.startswith("mcp__"):
            return None
        parts = tool_name.split("__", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            return None
        return MCPIdentity(parts[1], parts[2])


class MCPCallClassifier:
    """Compose swappable identity strategies without agent-specific branches."""

    def __init__(self, strategies: tuple[MCPIdentityStrategy, ...] = ()) -> None:
        self._strategies = strategies or (
            AttributeMCPIdentityStrategy(),
            PayloadMCPIdentityStrategy(),
            EncodedMCPToolNameStrategy(),
        )

    def identify(self, attrs: dict[str, Any]) -> MCPIdentity:
        server_name: str | None = None
        tool_name: str | None = None
        for strategy in self._strategies:
            identity = strategy.identify(attrs)
            if identity is None:
                continue
            server_name = server_name or identity.server_name
            tool_name = tool_name or identity.tool_name
            if server_name and tool_name:
                break
        return MCPIdentity(server_name, tool_name)

    def is_explicit_event(self, event_type: str, attrs: dict[str, Any]) -> bool:
        raw_event = str(event_type or "").lower()
        hook_event = str(
            attrs.get("gen_ai.client.hook.event")
            or attrs.get("ide.hook.event")
            or attrs.get("event.name")
            or ""
        ).lower()
        return (
            "mcp" in raw_event
            or "mcp" in hook_event
            or any(
                attrs.get(key)
                for key in (
                    "gen_ai.client.mcp_server",
                    "gen_ai.client.mcp_tool",
                    "mcp.server.name",
                    "mcp.server",
                )
            )
        )

    @staticmethod
    def is_invocation(attrs: dict[str, Any]) -> bool:
        event = str(
            attrs.get("gen_ai.client.hook.event")
            or attrs.get("ide.hook.event")
            or attrs.get("event.name")
            or ""
        ).rsplit(".", 1)[-1].lower()
        return event in {"pretooluse", "beforemcpexecution", "tool.execution_start"}

    @staticmethod
    def call_id(attrs: dict[str, Any]) -> str | None:
        return _first_text(
            attrs,
            "gen_ai.client.tool_use_id",
            "gen_ai.tool.call.id",
            "tool.call_id",
            "tool_call_id",
        )

    @staticmethod
    def session_id(attrs: dict[str, Any]) -> str | None:
        return _first_text(attrs, "gen_ai.client.mcp_session_id", "mcp.session.id")

    @staticmethod
    def protocol_version(attrs: dict[str, Any]) -> str | None:
        return _first_text(
            attrs,
            "gen_ai.client.mcp_protocol_version",
            "mcp.protocol.version",
        )

    @staticmethod
    def transport(attrs: dict[str, Any]) -> str | None:
        return _first_text(attrs, "gen_ai.client.mcp_transport", "mcp.transport")


class MCPCallBackfill:
    """Attach MCP metadata to canonical tool calls that predate direct classification."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        classifier: MCPCallClassifier | None = None,
    ) -> None:
        self.conn = conn
        self.classifier = classifier or DEFAULT_MCP_CLASSIFIER

    def run(
        self,
        *,
        session_ids: set[str] | None = None,
        changed_session_ids: set[str] | None = None,
    ) -> dict[str, int]:
        inserted = 0
        for where, params in self._session_scopes("tc", session_ids):
            rows = self.conn.execute(
                f"""
                SELECT
                  tc.id,
                  tc.session_id,
                  tc.tool_name,
                  tc.raw_attrs_json,
                  tc.created_at,
                  tc.updated_at
                FROM tool_calls AS tc
                LEFT JOIN mcp_calls AS mc ON mc.tool_call_id = tc.id
                WHERE mc.tool_call_id IS NULL
                  AND (
                    tc.tool_name GLOB 'mcp__*'
                    OR LOWER(tc.tool_name) IN (
                      'callmcptool', 'call_mcp_tool', 'mcp_tool', 'mcp'
                    )
                  )
                  {where}
                ORDER BY tc.created_at, tc.id
                """,
                params,
            ).fetchall()
            for row in rows:
                attrs = _load_object(row[3])
                attrs.setdefault("gen_ai.client.tool_name", row[2])
                identity = self.classifier.identify(attrs)
                if not identity.server_name:
                    continue
                cursor = self.conn.execute(
                    """
                    INSERT INTO mcp_calls(
                      tool_call_id, mcp_session_id, mcp_protocol_version, transport,
                      server_name, tool_name, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tool_call_id) DO NOTHING
                    """,
                    (
                        row[0],
                        self.classifier.session_id(attrs),
                        self.classifier.protocol_version(attrs),
                        self.classifier.transport(attrs),
                        identity.server_name,
                        identity.tool_name,
                        row[4],
                        row[5],
                    ),
                )
                if cursor.rowcount:
                    inserted += 1
                    if changed_session_ids is not None:
                        changed_session_ids.add(str(row[1]))
        return {
            "inserted": inserted,
            "skipped_duplicates": 0,
            "updated_status": 0,
        }

    @staticmethod
    def _session_scopes(
        alias: str,
        session_ids: set[str] | None,
    ) -> list[tuple[str, tuple[str, ...]]]:
        if session_ids is None:
            return [("", ())]
        ordered = sorted(session_ids)
        return [
            (
                f"AND {alias}.session_id IN ({','.join('?' for _ in batch)})",
                tuple(batch),
            )
            for offset in range(0, len(ordered), 400)
            if (batch := ordered[offset : offset + 400])
        ]

DEFAULT_MCP_CLASSIFIER = MCPCallClassifier()


__all__ = [
    "AttributeMCPIdentityStrategy",
    "DEFAULT_MCP_CLASSIFIER",
    "EncodedMCPToolNameStrategy",
    "MCPCallBackfill",
    "MCPCallClassifier",
    "MCPIdentity",
    "MCPIdentityStrategy",
    "PayloadMCPIdentityStrategy",
]
