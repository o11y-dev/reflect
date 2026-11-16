"""The small evidence graph vocabulary, shared by stored and virtual nodes.

These constraints validate shape, never the truth of a relationship. Canonical
telemetry and execution assessments remain the authoritative evidence stores.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from typing import Any

NODE_KINDS = frozenset({
    "Agent", "Folder", "MCPServer", "Memory", "Outcome", "Path", "Repo",
    "Session", "Skill", "Spec", "Subagent", "Tool", "ToolCall", "Workspace", "Execution",
})
EDGE_ENDPOINTS = {
    "addressed_spec": {("Session", "Spec")},
    "contains_path": {("Repo", "Path")},
    "contains_touched_path": {("Folder", "Path")},
    "defines_spec": {("Repo", "Spec")},
    "delegated_to": {("Subagent", "Subagent")},
    "described_by_path": {("Spec", "Path"), ("Memory", "Path")},
    "drove_tool_call": {("Skill", "ToolCall")},
    "executed_tool_call": {("Session", "ToolCall")},
    "instance_of_tool": {("ToolCall", "Tool")},
    "launched_by_tool_call": {("Subagent", "ToolCall")},
    "planned_spec": {("Session", "Spec")},
    "ran_in_workspace": {("Session", "Workspace")},
    "ran_session": {("Agent", "Session")},
    "recorded_memory": {("Repo", "Memory"), ("Session", "Memory")},
    "spawned_session": {("Session", "Session")},
    "spawned_subagent": {("Session", "Subagent")},
    "stopped_subagent": {("Session", "Subagent")},
    "suggests_outcome": {("Session", "Outcome"), ("ToolCall", "Outcome")},
    "touched_folder": {("Session", "Folder"), ("ToolCall", "Folder")},
    "touched_path": {("ToolCall", "Path")},
    "used_mcp": {("Session", "MCPServer")},
    "used_skill": {("Session", "Skill")},
    "used_tool": {("Session", "Tool")},
    "worked_in_repo": {("Session", "Repo")},
    "has_execution": {("Session", "Execution")},
    "checked_by": {("Execution", "ToolCall")},
}


def graph_id(prefix: str, *parts: object) -> str:
    payload = ":".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha1(payload.encode()).hexdigest()}"


def tool_call_node(tool: Mapping[str, Any]) -> dict[str, Any]:
    """Project one canonical call; full source identity is independent of label."""
    call_id = str(tool["id"])
    return {
        "id": graph_id("node", "ToolCall", f"tool_call:{call_id}"),
        "kind": "ToolCall", "label": f"{tool['tool_name']}:{call_id[-10:]}",
        "session_id": tool["session_id"], "identity_key": f"tool_call:{call_id}",
        "first_seen_at": tool["created_at"], "last_seen_at": tool["updated_at"],
        "attrs": {"tool_call_id": call_id, "step_id": tool["step_id"],
                  "tool_name": tool["tool_name"], "tool_type": tool["tool_type"],
                  "status": tool["status"], "duration_ms": tool["duration_ms"],
                  "input_hash": tool["input_hash"], "output_hash": tool["output_hash"],
                  "error_type": tool["error_type"], "error_message": tool["error_message_redacted"],
                  "evidence_class": "observed", "source_kind": "tool_call", "source_id": call_id},
    }


def validate_graph(conn: sqlite3.Connection, *, edges_table: str = "graph_edges") -> None:
    """Validate distinct staged endpoint triples using one join, never N queries."""
    if edges_table not in {"graph_edges", "reflect_graph_seen_edges"}:
        raise ValueError("Unsupported graph validation table")
    invalid_nodes = {str(row[0]) for row in conn.execute("SELECT DISTINCT kind FROM graph_nodes")} - NODE_KINDS
    if invalid_nodes:
        raise ValueError(f"Unknown graph node kinds: {sorted(invalid_nodes)}")
    for kind, source, target in conn.execute(f"""
        SELECT DISTINCT edge.kind, source.kind, target.kind FROM {edges_table} edge
        LEFT JOIN graph_nodes source ON source.id=edge.source_node_id
        LEFT JOIN graph_nodes target ON target.id=edge.target_node_id
    """):
        if (source, target) not in EDGE_ENDPOINTS.get(kind, set()):
            raise ValueError(f"Invalid graph relationship: {source} --{kind}--> {target}")


def virtual_tool_graph(conn: sqlite3.Connection, session_ids: list[str] | None, *, limit: int = 40) -> tuple[list[dict], list[dict]]:
    """Bound canonical calls before projection; never deserialize raw payloads."""
    if session_ids == []:
        return [], []
    scope = "" if session_ids is None else f"WHERE tc.session_id IN ({','.join('?' for _ in session_ids)})"
    fields = "id,step_id,session_id,tool_name,tool_type,status,duration_ms,input_hash,output_hash,error_type,error_message_redacted,created_at,updated_at"
    # Rank compact covering-index entries before fetching call details. Selecting
    # the payload first lets SQLite read every matching call before its top-N sort.
    index = "idx_tool_calls_graph_recent" if session_ids is None else "idx_tool_calls_graph_session_recent"
    cursor = conn.execute(f"""WITH recent AS MATERIALIZED (
        SELECT tc.id,tc.created_at FROM tool_calls tc INDEXED BY {index}
        {scope} ORDER BY tc.created_at DESC,tc.id DESC LIMIT ?
    ) SELECT {','.join('tc.' + field for field in fields.split(','))}
        FROM recent JOIN tool_calls tc ON tc.id=recent.id
        ORDER BY recent.created_at DESC,recent.id DESC""",
        (*(session_ids or []), min(max(limit, 0), 40)))
    columns = [column[0] for column in cursor.description]
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    for row in cursor:
        tool = dict(zip(columns, row, strict=True))
        call = tool_call_node(tool)
        call["attrs_json"] = json.dumps(call.pop("attrs"), sort_keys=True)
        nodes[call["id"]] = call
        tool_id = graph_id("node", "Tool", tool["tool_name"], "")
        session_id = graph_id("node", "Session", tool["session_id"], tool["session_id"])
        nodes.setdefault(tool_id, {"id": tool_id, "kind": "Tool", "label": tool["tool_name"],
            "session_id": None, "attrs_json": json.dumps({"tool_type": tool["tool_type"]}),
            "first_seen_at": None, "last_seen_at": None})
        nodes.setdefault(session_id, {"id": session_id, "kind": "Session", "label": tool["session_id"],
            "session_id": tool["session_id"], "attrs_json": "{}", "first_seen_at": None, "last_seen_at": None})
        for kind, source, target in (("executed_tool_call", session_id, call["id"]), ("instance_of_tool", call["id"], tool_id)):
            edges.append({"source_node_id": source, "target_node_id": target, "kind": kind,
                "session_id": tool["session_id"], "weight": 1,
                "attrs_json": json.dumps({"status": tool["status"], "evidence_class": "observed",
                    "source_kind": "tool_call", "source_id": tool["id"]}),
                "first_seen_at": tool["created_at"], "last_seen_at": tool["updated_at"]})
    return list(nodes.values()), edges
