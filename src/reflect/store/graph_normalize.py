from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _stable_id(prefix: str, *parts: object) -> str:
    payload = ":".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha1(payload.encode()).hexdigest()}"


def _insert_node(
    conn: sqlite3.Connection,
    *,
    kind: str,
    label: str,
    session_id: str | None = None,
    attrs: dict | None = None,
    first_seen_at: str | None = None,
    last_seen_at: str | None = None,
    timestamp: str,
) -> tuple[str, bool]:
    node_id = _stable_id("node", kind, label, session_id or "")
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes(
          id, kind, label, session_id, first_seen_at, last_seen_at,
          attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            node_id,
            kind,
            label,
            session_id,
            first_seen_at,
            last_seen_at,
            json.dumps(attrs or {}, sort_keys=True),
            timestamp,
            timestamp,
        ),
    )
    return node_id, cursor.rowcount != 0


def _insert_edge(
    conn: sqlite3.Connection,
    *,
    source_node_id: str,
    target_node_id: str,
    kind: str,
    session_id: str | None = None,
    attrs: dict | None = None,
    first_seen_at: str | None = None,
    last_seen_at: str | None = None,
    timestamp: str,
) -> tuple[str, bool]:
    edge_id = _stable_id("edge", kind, source_node_id, target_node_id, session_id or "")
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO graph_edges(
          id, source_node_id, target_node_id, kind, session_id, weight,
          first_seen_at, last_seen_at, attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            edge_id,
            source_node_id,
            target_node_id,
            kind,
            session_id,
            1,
            first_seen_at,
            last_seen_at,
            json.dumps(attrs or {}, sort_keys=True),
            timestamp,
            timestamp,
        ),
    )
    return edge_id, cursor.rowcount != 0


def rebuild_graph(conn: sqlite3.Connection) -> dict[str, int]:
    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        timestamp = _now()
        nodes = 0
        edges = 0

        for session in conn.execute("SELECT * FROM sessions ORDER BY started_at, id"):
            session_node, inserted = _insert_node(
                conn,
                kind="Session",
                label=session["id"],
                session_id=session["id"],
                attrs={"status": session["status"], "agent_id": session["agent_id"]},
                first_seen_at=session["started_at"],
                last_seen_at=session["ended_at"],
                timestamp=timestamp,
            )
            nodes += int(inserted)

            if session["agent_id"]:
                agent = conn.execute("SELECT * FROM agents WHERE id = ?", (session["agent_id"],)).fetchone()
                if agent:
                    agent_node, inserted = _insert_node(
                        conn,
                        kind="Agent",
                        label=agent["name"],
                        attrs={"agent_id": agent["id"], "kind": agent["kind"]},
                        timestamp=timestamp,
                    )
                    nodes += int(inserted)
                    _, inserted = _insert_edge(
                        conn,
                        source_node_id=agent_node,
                        target_node_id=session_node,
                        kind="ran_session",
                        session_id=session["id"],
                        first_seen_at=session["started_at"],
                        last_seen_at=session["ended_at"],
                        timestamp=timestamp,
                    )
                    edges += int(inserted)

            for step in conn.execute("SELECT * FROM steps WHERE session_id = ? ORDER BY seq", (session["id"],)):
                step_node, inserted = _insert_node(
                    conn,
                    kind="Step",
                    label=f"{step['seq']}:{step['type']}",
                    session_id=session["id"],
                    attrs={"step_id": step["id"], "type": step["type"], "status": step["status"]},
                    first_seen_at=step["started_at"],
                    last_seen_at=step["ended_at"],
                    timestamp=timestamp,
                )
                nodes += int(inserted)
                _, inserted = _insert_edge(
                    conn,
                    source_node_id=session_node,
                    target_node_id=step_node,
                    kind="has_step",
                    session_id=session["id"],
                    first_seen_at=step["started_at"],
                    last_seen_at=step["ended_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)

        for tool in conn.execute("SELECT * FROM tool_calls ORDER BY created_at, id"):
            session_node, _ = _insert_node(
                conn,
                kind="Session",
                label=tool["session_id"],
                session_id=tool["session_id"],
                timestamp=timestamp,
            )
            tool_node, inserted = _insert_node(
                conn,
                kind="Tool",
                label=tool["tool_name"],
                attrs={"tool_type": tool["tool_type"]},
                timestamp=timestamp,
            )
            nodes += int(inserted)
            _, inserted = _insert_edge(
                conn,
                source_node_id=session_node,
                target_node_id=tool_node,
                kind="used_tool",
                session_id=tool["session_id"],
                attrs={"status": tool["status"]},
                timestamp=timestamp,
            )
            edges += int(inserted)

        for mcp in conn.execute("SELECT * FROM mcp_calls WHERE server_name IS NOT NULL ORDER BY created_at, id"):
            session_node, _ = _insert_node(
                conn,
                kind="Session",
                label=mcp["session_id"],
                session_id=mcp["session_id"],
                timestamp=timestamp,
            )
            mcp_node, inserted = _insert_node(
                conn,
                kind="MCPServer",
                label=mcp["server_name"],
                attrs={"transport": mcp["transport"]},
                timestamp=timestamp,
            )
            nodes += int(inserted)
            _, inserted = _insert_edge(
                conn,
                source_node_id=session_node,
                target_node_id=mcp_node,
                kind="used_mcp",
                session_id=mcp["session_id"],
                attrs={"tool_name": mcp["tool_name"], "status": mcp["status"]},
                timestamp=timestamp,
            )
            edges += int(inserted)

        for memory in conn.execute("SELECT * FROM memories ORDER BY created_at, id"):
            memory_node, inserted = _insert_node(
                conn,
                kind="Memory",
                label=memory["id"],
                session_id=memory["session_id"],
                attrs={"scope": memory["scope"], "type": memory["type"], "sensitivity": memory["sensitivity"]},
                first_seen_at=memory["created_at"],
                last_seen_at=memory["last_seen_at"],
                timestamp=timestamp,
            )
            nodes += int(inserted)
            if memory["session_id"]:
                session_node, _ = _insert_node(
                    conn,
                    kind="Session",
                    label=memory["session_id"],
                    session_id=memory["session_id"],
                    timestamp=timestamp,
                )
                _, inserted = _insert_edge(
                    conn,
                    source_node_id=session_node,
                    target_node_id=memory_node,
                    kind="recorded_memory",
                    session_id=memory["session_id"],
                    timestamp=timestamp,
                )
                edges += int(inserted)

        conn.commit()
        return {"nodes": nodes, "edges": edges}
    finally:
        conn.row_factory = previous_row_factory
