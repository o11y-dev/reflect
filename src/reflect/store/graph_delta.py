"""Bounded append repair using canonical source ownership.

Earlier-result, delegation and identity changes deliberately use the existing
session repair until their wider semantic dependency can be bounded exactly.
"""
from __future__ import annotations

import sqlite3


def try_append_graph(
    conn: sqlite3.Connection, session_ids: set[str], step_ids: set[str], tool_ids: set[str] | None,
) -> dict[str, int] | None:
    from reflect.store.graph_normalize import (
        _copy_changed_graph_rows,
        _create_graph_stage,
        _derive_graph,
        _drop_graph_stage,
    )

    if not step_ids:
        return {"nodes": 0, "edges": 0, "refreshed_sessions": 0, "source_scoped_sessions": 0}
    placeholders = ",".join("?" for _ in step_ids)
    selected_steps = sorted(step_ids)
    steps = conn.execute(f"SELECT id,session_id,seq,raw_attrs_json,summary,type FROM main.steps WHERE id IN ({placeholders})", selected_steps).fetchall()
    if len(steps) != len(step_ids) or any(str(row[1]) not in session_ids for row in steps):
        return None
    # Late events may revise prior delegation/context or first-writer metadata.
    if any("subagent" in f"{row[3]} {row[4]}".lower() for row in steps):
        return None
    # Context changes can update an existing artifact or exposure owned by
    # another session. The full session repair closes those dependencies;
    # source-only derivation deliberately does not project memory artifacts.
    if any(row[5] == "memory_event" for row in steps):
        return None
    if conn.execute(f"SELECT 1 FROM main.agent_events WHERE step_id IN ({placeholders}) LIMIT 1", selected_steps).fetchone():
        return None
    for sid in sorted(session_ids):
        selected = [int(row[2]) for row in steps if str(row[1]) == sid]
        if not selected or not conn.execute("SELECT 1 FROM main.graph_nodes WHERE kind='Session' AND session_id=?", (sid,)).fetchone():
            return None
        if conn.execute(f"SELECT 1 FROM main.steps WHERE session_id=? AND seq>=? AND id NOT IN ({placeholders}) LIMIT 1", (sid, min(selected), *selected_steps)).fetchone():
            return None
    selected_tools = sorted({*(tool_ids or ()), *(
        str(row[0]) for row in conn.execute(f"SELECT id FROM main.tool_calls WHERE step_id IN ({placeholders})", selected_steps)
    )})
    if tool_ids and conn.execute(f"""SELECT 1 FROM main.tool_calls
        WHERE id IN ({','.join('?' for _ in tool_ids)})
          AND step_id NOT IN ({placeholders}) LIMIT 1""", (*sorted(tool_ids), *selected_steps)).fetchone():
        return None
    if selected_tools and any(
        str(row[0]).lower() in {"task", "agent", "spawn_agent", "send_message", "wait_agent"}
        or "subagent" in f"{row[1]} {row[2]}".lower()
        for row in conn.execute(f"""SELECT tool_name,raw_attrs_json,input_preview_redacted FROM main.tool_calls
            WHERE id IN ({','.join('?' for _ in selected_tools)})""", selected_tools)
    ):
        return None
    for kind, ids in (("step", selected_steps), ("tool_call", selected_tools)):
        if ids and conn.execute(f"SELECT 1 FROM main.graph_edge_sources WHERE source_kind=? AND source_id IN ({','.join('?' for _ in ids)}) LIMIT 1", (kind, *ids)).fetchone():
            return None

    previous_cache = int(conn.execute("PRAGMA temp.cache_size").fetchone()[0])
    conn.execute("SAVEPOINT reflect_graph_append")
    try:
        if previous_cache == 0:
            conn.execute("PRAGMA temp.cache_size=-32768")
        conn.execute("CREATE TEMP TABLE reflect_changed_sessions(session_id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO reflect_changed_sessions VALUES (?)", ((sid,) for sid in sorted(session_ids)))
        conn.execute("CREATE TEMP TABLE reflect_graph_changed_steps(id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO reflect_graph_changed_steps VALUES (?)", ((sid,) for sid in selected_steps))
        conn.execute("CREATE TEMP TABLE reflect_graph_changed_tools(id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO reflect_graph_changed_tools VALUES (?)", ((tid,) for tid in selected_tools))
        _create_graph_stage(conn)
        conn.execute("""INSERT INTO temp.graph_nodes SELECT * FROM main.graph_nodes
            WHERE session_id IS NULL OR (kind='Session' AND session_id NOT IN
                (SELECT session_id FROM reflect_changed_sessions))""")
        conn.execute("CREATE TEMP VIEW sessions AS SELECT * FROM main.sessions WHERE id IN (SELECT session_id FROM reflect_changed_sessions)")
        conn.execute("CREATE TEMP VIEW steps AS SELECT * FROM main.steps WHERE id IN (SELECT id FROM reflect_graph_changed_steps)")
        conn.execute("CREATE TEMP VIEW tool_calls AS SELECT * FROM main.tool_calls WHERE id IN (SELECT id FROM reflect_graph_changed_tools)")
        result = _derive_graph(conn, commit=False, source_only=True)
        _merge_append_support(conn)
        _copy_changed_graph_rows(conn, "temp.graph_nodes", "main.graph_nodes", edges=False)
        _copy_changed_graph_rows(conn, "temp.graph_edges", "main.graph_edges", edges=True)
        conn.execute("INSERT INTO main.graph_edge_sources SELECT * FROM temp.graph_edge_sources")
        _drop_graph_stage(conn)
        for view in ("tool_calls", "steps", "sessions"):
            conn.execute(f"DROP VIEW temp.{view}")
        for table in ("reflect_graph_changed_tools", "reflect_graph_changed_steps", "reflect_changed_sessions"):
            conn.execute(f"DROP TABLE temp.{table}")
        conn.execute("RELEASE SAVEPOINT reflect_graph_append")
        conn.commit()
        return {**result, "refreshed_sessions": len(session_ids), "source_scoped_sessions": len(session_ids)}
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT reflect_graph_append")
        conn.execute("RELEASE SAVEPOINT reflect_graph_append")
        raise
    finally:
        if previous_cache == 0:
            conn.execute("PRAGMA temp.cache_size=0")


def _merge_append_support(conn: sqlite3.Connection) -> None:
    """Merge weights arithmetically and select winning evidence through indexes."""
    for edge_id, count in conn.execute("SELECT edge_id,SUM(weight) FROM temp.graph_edge_sources GROUP BY edge_id").fetchall():
        old = conn.execute("SELECT weight FROM main.graph_edges WHERE id=?", (edge_id,)).fetchone()
        if old is None:
            continue
        candidates: dict[str, list] = {"first": [], "last": [], "attrs": []}
        for schema in ("main", "temp"):
            for key, order, predicate in (("first", "ASC", ""), ("last", "DESC", "AND last_seen_at IS NOT NULL"), ("attrs", "DESC", "")):
                row = conn.execute(f"""SELECT source_order,first_seen_at,last_seen_at,attrs_json
                    FROM {schema}.graph_edge_sources WHERE edge_id=? {predicate}
                    ORDER BY source_order {order} LIMIT 1""", (edge_id,)).fetchone()
                if row is not None:
                    candidates[key].append(tuple(row))
        first = min(candidates["first"], key=lambda row: row[0])
        winner = max(candidates["attrs"], key=lambda row: row[0])
        last = max(candidates["last"], key=lambda row: row[0])[2] if candidates["last"] else None
        conn.execute("""UPDATE temp.graph_edges SET weight=?,first_seen_at=?,last_seen_at=?,attrs_json=?
            WHERE id=?""", (float(old[0]) + count, first[1], last, winner[3], edge_id))
