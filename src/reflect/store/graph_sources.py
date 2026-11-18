"""Source ownership for aggregated telemetry relationships.

Only relations with collapsed support need this ledger. Direct call links are
owned by the canonical ToolCall endpoint and do not duplicate their payload here.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from reflect.store.graph_ontology import graph_id


class GraphSourceWriter:
    def __init__(self, conn: sqlite3.Connection, emit):
        self.conn = conn
        self.emit = emit
        self.source_kind = ""
        self.source_id = ""
        self.source_order = ""

    def source(self, kind: str = "", identity: object = "", order: object = "") -> None:
        self.source_kind = kind
        self.source_id = str(identity)
        self.source_order = str(order)

    def edge(self, conn: sqlite3.Connection, **kwargs: Any) -> tuple[str, bool]:
        attrs = dict(kwargs.get("attrs") or {})
        if self.source_kind:
            attrs.update(source_kind=self.source_kind, source_id=self.source_id)
            attrs.setdefault("evidence_class", "inferred" if kwargs["kind"] in {
                "used_skill", "drove_tool_call", "suggests_outcome",
            } or attrs.get("source") == "prompt_text" else "observed")
            kwargs["attrs"] = attrs
        result = self.emit(conn, **kwargs)
        if not self.source_kind:
            return result
        call_node_id = graph_id("node", "ToolCall", f"tool_call:{self.source_id}")
        if self.source_kind == "tool_call" and call_node_id in {
            kwargs["source_node_id"], kwargs["target_node_id"],
        }:
            return result
        self.conn.execute("""INSERT INTO graph_edge_sources(
            edge_id,source_kind,source_id,source_order,weight,first_seen_at,last_seen_at,attrs_json)
            VALUES(?,?,?,?,1,?,?,?) ON CONFLICT(edge_id,source_kind,source_id) DO UPDATE SET
            weight=graph_edge_sources.weight+1,
            source_order=excluded.source_order, first_seen_at=excluded.first_seen_at,
            last_seen_at=excluded.last_seen_at,attrs_json=excluded.attrs_json
        """, (result[0], self.source_kind, self.source_id, self.source_order,
              kwargs.get("first_seen_at"), kwargs.get("last_seen_at"), json.dumps(attrs, sort_keys=True)))
        return result


def copy_graph_sources(conn: sqlite3.Connection, *, scoped: bool) -> None:
    scope = ("edge_id IN (SELECT id FROM main.graph_edges WHERE session_id IN "
             "(SELECT session_id FROM reflect_changed_sessions)) AND ") if scoped else ""
    conn.execute(f"""DELETE FROM main.graph_edge_sources WHERE {scope}
        (edge_id,source_kind,source_id) NOT IN
        (SELECT edge_id,source_kind,source_id FROM temp.graph_edge_sources)""")
    conn.execute("""INSERT INTO main.graph_edge_sources SELECT * FROM temp.graph_edge_sources WHERE 1
        ON CONFLICT(edge_id,source_kind,source_id) DO UPDATE SET
        source_order=excluded.source_order,weight=excluded.weight,first_seen_at=excluded.first_seen_at,
        last_seen_at=excluded.last_seen_at,attrs_json=excluded.attrs_json
        WHERE (graph_edge_sources.source_order,graph_edge_sources.weight,graph_edge_sources.first_seen_at,
               graph_edge_sources.last_seen_at,graph_edge_sources.attrs_json) IS NOT
              (excluded.source_order,excluded.weight,excluded.first_seen_at,excluded.last_seen_at,excluded.attrs_json)""")
