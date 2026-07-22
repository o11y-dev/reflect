"""Agent-neutral MCP call identification and canonical-ledger repair."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
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


@dataclass(slots=True)
class MCPCallCandidate:
    step_id: str
    session_id: str
    status: str
    duration_ms: int | None
    started_at: str
    origin_kind: str | None
    attrs: dict[str, Any]
    raw_attrs_json: str
    identity: MCPIdentity
    call_id: str | None


class MCPCallBackfill:
    """Repair one canonical MCP call from overlapping native/hook evidence."""

    ORIGIN_PRIORITY = {
        "native_otlp_trace": 0,
        "hook_otlp_trace": 1,
        "native_session": 2,
    }

    def __init__(
        self,
        conn: sqlite3.Connection,
        classifier: MCPCallClassifier | None = None,
    ) -> None:
        self.conn = conn
        self.classifier = classifier or DEFAULT_MCP_CLASSIFIER
        self._existing_by_call: set[tuple[str, str]] = set()
        self._existing_by_tool: dict[
            tuple[str, str | None, str | None], list[MCPCallCandidate]
        ] = {}

    def run(
        self,
        *,
        session_ids: set[str] | None = None,
        changed_session_ids: set[str] | None = None,
    ) -> dict[str, int]:
        candidates = self._explicit_candidates(session_ids) + self._tool_candidates(session_ids)
        if candidates:
            self._load_existing({candidate.session_id for candidate in candidates})
            candidates.sort(
                key=lambda item: (
                    self.ORIGIN_PRIORITY.get(item.origin_kind or "", 3),
                    item.started_at,
                )
            )
        inserted = 0
        skipped_duplicates = 0
        timestamp = datetime.now(tz=UTC).isoformat()
        for candidate in candidates:
            if self._is_duplicate(candidate):
                skipped_duplicates += 1
                continue
            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO mcp_calls(
                  id, step_id, session_id, tool_call_id, mcp_session_id, mcp_protocol_version,
                  transport, server_name, tool_name, status, duration_ms,
                  raw_attrs_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._stable_id(candidate.step_id),
                    candidate.step_id,
                    candidate.session_id,
                    candidate.call_id,
                    self.classifier.session_id(candidate.attrs),
                    self.classifier.protocol_version(candidate.attrs),
                    self.classifier.transport(candidate.attrs),
                    candidate.identity.server_name,
                    candidate.identity.tool_name,
                    candidate.status,
                    candidate.duration_ms,
                    candidate.raw_attrs_json,
                    timestamp,
                    timestamp,
                ),
            )
            if cursor.rowcount:
                inserted += 1
                if changed_session_ids is not None:
                    changed_session_ids.add(candidate.session_id)
                self._remember(candidate)
        status_result = MCPCallResultReconciler(self.conn).run(
            session_ids=session_ids,
            changed_session_ids=changed_session_ids
        )
        return {
            "inserted": inserted,
            "skipped_duplicates": skipped_duplicates,
            "updated_status": status_result["updated"],
        }

    def _load_existing(self, session_ids: set[str]) -> None:
        self._existing_by_call.clear()
        self._existing_by_tool.clear()
        ordered_ids = sorted(session_ids)
        for offset in range(0, len(ordered_ids), 400):
            batch = ordered_ids[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = self.conn.execute(
                f"""
                SELECT mc.step_id, mc.session_id, mc.status, mc.duration_ms,
                       st.started_at, st.origin_kind, mc.server_name,
                       mc.tool_name, mc.tool_call_id
                FROM mcp_calls mc
                JOIN steps st ON st.id = mc.step_id
                WHERE mc.session_id IN ({placeholders})
                """,
                batch,
            )
            for row in rows:
                candidate = MCPCallCandidate(
                    step_id=row[0],
                    session_id=row[1],
                    status=row[2],
                    duration_ms=row[3],
                    started_at=row[4],
                    origin_kind=row[5],
                    attrs={},
                    raw_attrs_json="{}",
                    identity=MCPIdentity(row[6], row[7]),
                    call_id=row[8],
                )
                self._remember(candidate)

    def _explicit_candidates(
        self,
        session_ids: set[str] | None,
    ) -> list[MCPCallCandidate]:
        rows: list[sqlite3.Row | tuple[Any, ...]] = []
        for where, params in self._session_scopes("st", session_ids):
            rows.extend(self.conn.execute(
                f"""
                SELECT st.id, st.session_id, st.status, st.duration_ms, st.started_at,
                       st.origin_kind, st.raw_attrs_json
                FROM steps st
                LEFT JOIN mcp_calls mc ON mc.step_id = st.id
                WHERE mc.id IS NULL
                  AND (
                    LOWER(COALESCE(st.summary, '')) LIKE 'mcp.%'
                    OR json_extract(st.raw_attrs_json, '$."mcp.server.name"') IS NOT NULL
                  )
                  {where}
                """,
                params,
            ).fetchall())
        return [candidate for row in rows if (candidate := self._candidate(row))]

    def _tool_candidates(
        self,
        session_ids: set[str] | None,
    ) -> list[MCPCallCandidate]:
        rows: list[sqlite3.Row | tuple[Any, ...]] = []
        for where, params in self._session_scopes("st", session_ids):
            rows.extend(self.conn.execute(
                f"""
                SELECT tc.step_id, tc.session_id, tc.status, tc.duration_ms,
                       st.started_at, st.origin_kind, st.raw_attrs_json
                FROM tool_calls tc
                JOIN steps st ON st.id = tc.step_id
                LEFT JOIN mcp_calls mc ON mc.step_id = tc.step_id
                WHERE mc.id IS NULL
                  AND tc.tool_name IN (
                    'CallMcpTool', 'callmcptool', 'call_mcp_tool', 'mcp_tool', 'mcp'
                  )
                  {where}
                UNION ALL
                SELECT tc.step_id, tc.session_id, tc.status, tc.duration_ms,
                       st.started_at, st.origin_kind, st.raw_attrs_json
                FROM tool_calls tc
                JOIN steps st ON st.id = tc.step_id
                LEFT JOIN mcp_calls mc ON mc.step_id = tc.step_id
                WHERE mc.id IS NULL AND tc.tool_name GLOB 'mcp__*'
                  {where}
                """,
                (*params, *params),
            ).fetchall())
        candidates: list[MCPCallCandidate] = []
        for row in rows:
            attrs = _load_object(row[6])
            if not self.classifier.is_invocation(attrs):
                continue
            candidate = self._candidate(row, attrs=attrs)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

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

    def _candidate(
        self,
        row: sqlite3.Row | tuple[Any, ...],
        *,
        attrs: dict[str, Any] | None = None,
    ) -> MCPCallCandidate | None:
        attrs = attrs or _load_object(row[6])
        identity = self.classifier.identify(attrs)
        if not identity.server_name:
            return None
        return MCPCallCandidate(
            step_id=str(row[0]),
            session_id=str(row[1]),
            status=str(row[2]),
            duration_ms=row[3],
            started_at=str(row[4]),
            origin_kind=str(row[5]) if row[5] else None,
            attrs=attrs,
            raw_attrs_json=str(row[6]),
            identity=identity,
            call_id=self.classifier.call_id(attrs),
        )

    def _is_duplicate(self, candidate: MCPCallCandidate) -> bool:
        if candidate.call_id and (
            candidate.session_id,
            candidate.call_id,
        ) in self._existing_by_call:
            return True
        key = (
            candidate.session_id,
            candidate.identity.server_name,
            candidate.identity.tool_name,
        )
        for current in self._existing_by_tool.get(key, []):
            current_at = self._timestamp(current.started_at)
            candidate_at = self._timestamp(candidate.started_at)
            if current_at is None or candidate_at is None:
                continue
            close_in_time = abs((current_at - candidate_at).total_seconds()) <= 1
            authoritative_overlap = (
                current.origin_kind == "native_otlp_trace"
                or candidate.origin_kind == "native_session"
            )
            if close_in_time and authoritative_overlap:
                return True
        return False

    def _remember(self, candidate: MCPCallCandidate) -> None:
        if candidate.call_id:
            self._existing_by_call.add((candidate.session_id, candidate.call_id))
        key = (
            candidate.session_id,
            candidate.identity.server_name,
            candidate.identity.tool_name,
        )
        self._existing_by_tool.setdefault(key, []).append(candidate)

    @staticmethod
    def _timestamp(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _stable_id(step_id: str) -> str:
        return f"mcp_{hashlib.sha1(step_id.encode()).hexdigest()}"


class MCPCallResultReconciler:
    """Apply post-tool outcomes to canonical MCP invocation rows by call ID."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def run(
        self,
        *,
        session_ids: set[str] | None = None,
        changed_session_ids: set[str] | None = None,
    ) -> dict[str, int]:
        rows: list[sqlite3.Row | tuple[Any, ...]] = []
        for where, params in MCPCallBackfill._session_scopes("mc", session_ids):
            rows.extend(self.conn.execute(
                f"""
                SELECT mc.id, mc.session_id, tc.status, tc.duration_ms
                FROM tool_calls tc
                JOIN mcp_calls mc
                  ON mc.session_id = tc.session_id
                 AND mc.tool_call_id = COALESCE(
                   json_extract(tc.raw_attrs_json, '$."gen_ai.client.tool_use_id"'),
                   json_extract(tc.raw_attrs_json, '$."tool.call_id"'),
                   json_extract(tc.raw_attrs_json, '$.tool_call_id')
                 )
                WHERE mc.status = 'unknown'
                  AND json_extract(
                    tc.raw_attrs_json, '$."gen_ai.client.hook.event"'
                  ) IN ('PostToolUse', 'PostToolUseFailure', 'AfterMCPExecution')
                  AND (
                    tc.tool_name GLOB 'mcp__*'
                    OR tc.tool_name IN (
                      'CallMcpTool', 'callmcptool', 'call_mcp_tool', 'mcp_tool', 'mcp'
                    )
                  )
                  {where}
                ORDER BY tc.created_at DESC
                """,
                params,
            ).fetchall())
        updated = 0
        seen: set[str] = set()
        timestamp = datetime.now(tz=UTC).isoformat()
        for mcp_id, session_id, status, duration_ms in rows:
            if mcp_id in seen:
                continue
            seen.add(mcp_id)
            cursor = self.conn.execute(
                """
                UPDATE mcp_calls
                SET status = ?,
                    duration_ms = CASE
                      WHEN ? IS NULL THEN duration_ms
                      WHEN duration_ms IS NULL OR duration_ms < ? THEN ?
                      ELSE duration_ms
                    END,
                    updated_at = ?
                WHERE id = ? AND status = 'unknown'
                """,
                (status, duration_ms, duration_ms, duration_ms, timestamp, mcp_id),
            )
            if cursor.rowcount:
                updated += 1
                if changed_session_ids is not None:
                    changed_session_ids.add(str(session_id))
        return {"updated": updated}


DEFAULT_MCP_CLASSIFIER = MCPCallClassifier()


__all__ = [
    "AttributeMCPIdentityStrategy",
    "DEFAULT_MCP_CLASSIFIER",
    "EncodedMCPToolNameStrategy",
    "MCPCallBackfill",
    "MCPCallCandidate",
    "MCPCallClassifier",
    "MCPCallResultReconciler",
    "MCPIdentity",
    "MCPIdentityStrategy",
    "PayloadMCPIdentityStrategy",
]
