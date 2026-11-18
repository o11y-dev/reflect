"""Project current execution assessments without interpreting or reassessing them."""
from __future__ import annotations

import sqlite3

from reflect.store.graph_ontology import graph_id, tool_call_node, validate_graph


def refresh_execution_graph(
    conn: sqlite3.Connection, session_ids: set[str] | None = None, *,
    execution_unit_ids: set[str] | None = None, commit: bool = False,
) -> dict[str, int]:
    """Call after execution/contract assessment and before publication.

    `checked_by` records check evidence, not successful verification. Missing or
    ambiguous canonical call attribution does not manufacture a check endpoint.
    """
    from reflect.store.graph_normalize import (
        _copy_changed_graph_rows,
        _insert_edge,
        _insert_node,
        _load_json_dict,
        _now,
        _refresh_node,
    )

    if session_ids == set() or execution_unit_ids == set():
        return {"executions": 0, "checks": 0}
    scope = "" if session_ids is None else f" AND eu.session_id IN ({','.join('?' for _ in session_ids)})"
    params = tuple(sorted(session_ids)) if session_ids is not None else ()
    unit_scope = "" if execution_unit_ids is None else f" AND eu.id IN ({','.join('?' for _ in execution_unit_ids)})"
    unit_params = tuple(sorted(execution_unit_ids)) if execution_unit_ids is not None else ()
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    now = _now()
    if not commit and not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT reflect_execution_graph")
    try:
        conn.execute("CREATE TEMP TABLE reflect_graph_seen_edges AS SELECT * FROM graph_edges WHERE 0")
        conn.execute("CREATE UNIQUE INDEX temp.reflect_graph_seen_edges_id ON reflect_graph_seen_edges(id)")
        conn.execute("CREATE TEMP TABLE reflect_execution_graph_nodes(id TEXT PRIMARY KEY)")
        conn.execute("CREATE TEMP TABLE reflect_execution_graph_scope(id TEXT PRIMARY KEY)")
        node_scope = "" if session_ids is None else f" AND session_id IN ({','.join('?' for _ in session_ids)})"
        identity_scope = "" if execution_unit_ids is None else f" AND identity_key IN ({','.join('?' for _ in execution_unit_ids)})"
        conn.execute(f"""INSERT INTO reflect_execution_graph_scope
            SELECT id FROM graph_nodes WHERE kind='Execution' {node_scope} {identity_scope}""",
            (*params, *(f"execution:{unit_id}" for unit_id in unit_params)))
        conn.execute("""CREATE TEMP TABLE reflect_execution_graph_old_calls AS
            SELECT target_node_id id FROM graph_edges WHERE kind='checked_by'
            AND source_node_id IN (SELECT id FROM reflect_execution_graph_scope)""")
        executions = checks = 0
        for unit in conn.execute(f"""SELECT eu.* FROM execution_units eu
            WHERE EXISTS (SELECT 1 FROM execution_unit_steps member WHERE member.execution_unit_id=eu.id)
            {scope} {unit_scope} ORDER BY eu.started_at,eu.id""", (*params, *unit_params)).fetchall():
            assessment = _load_json_dict(unit["boundary_json"]).get("assessment") or {}
            if not assessment:
                continue
            assessment = dict(assessment)
            session_node = graph_id("node", "Session", unit["session_id"], unit["session_id"])
            if not conn.execute("SELECT 1 FROM graph_nodes WHERE id=?", (session_node,)).fetchone():
                continue
            attrs = {"execution_unit_id": unit["id"], "evidence_class": "derived",
                     "repo_id": unit["repo_id"], "workspace_id": unit["workspace_id"],
                     "source": unit["source"], "assessment": assessment,
                     "verification_scope": "execution_interval"}
            node_id, created = _insert_node(
                conn, kind="Execution", label=str(unit["id"]), session_id=unit["session_id"],
                identity_key=f"execution:{unit['id']}", attrs=attrs,
                first_seen_at=unit["started_at"], last_seen_at=unit["ended_at"], timestamp=now,
            )
            if not created:
                _refresh_node(conn, node_id=node_id, attrs=attrs, first_seen_at=unit["started_at"],
                              last_seen_at=unit["ended_at"], timestamp=now)
            conn.execute("INSERT INTO reflect_execution_graph_nodes VALUES (?)", (node_id,))
            conn.execute("INSERT OR IGNORE INTO reflect_execution_graph_scope VALUES (?)", (node_id,))
            executions += 1
            _insert_edge(conn, source_node_id=session_node, target_node_id=node_id, kind="has_execution",
                         session_id=unit["session_id"], attrs={"source_kind": "execution_unit", "source_id": unit["id"],
                         "evidence_class": "derived"}, first_seen_at=unit["started_at"], last_seen_at=unit["ended_at"], timestamp=now)
            observations = {str(check["step_id"]): dict(check) for check in assessment.get("checks", [])}
            for step_id in assessment.get("pending_check_step_ids", []):
                observations.setdefault(str(step_id), {"step_id": step_id, "exit_code": None})["pending"] = True
            for step_id, check in observations.items():
                calls = conn.execute("""SELECT tc.* FROM tool_calls tc
                    JOIN execution_unit_steps member ON member.step_id=tc.step_id
                    WHERE tc.step_id=? AND tc.session_id=? AND member.execution_unit_id=? LIMIT 2""",
                    (step_id, unit["session_id"], unit["id"])).fetchall()
                if len(calls) != 1:
                    continue
                call = tool_call_node(calls[0])
                _, created = _insert_node(conn, kind=call["kind"], label=call["label"], session_id=call["session_id"],
                             identity_key=call["identity_key"], attrs=call["attrs"],
                             first_seen_at=call["first_seen_at"], last_seen_at=call["last_seen_at"], timestamp=now)
                if not created:
                    _refresh_node(conn, node_id=call["id"], attrs=call["attrs"],
                                  first_seen_at=call["first_seen_at"], last_seen_at=call["last_seen_at"], timestamp=now)
                _insert_edge(conn, source_node_id=node_id, target_node_id=call["id"], kind="checked_by",
                             session_id=unit["session_id"], attrs={**check, "source_kind": "execution_unit",
                             "source_id": unit["id"], "evidence_class": "derived",
                             "assessment_digest": assessment.get("input_digest")},
                             first_seen_at=check.get("started_at"), last_seen_at=check.get("at"), timestamp=now)
                checks += 1
        validate_graph(conn, edges_table="reflect_graph_seen_edges")
        conn.execute("""DELETE FROM graph_edges WHERE (
            (kind='has_execution' AND target_node_id IN (SELECT id FROM reflect_execution_graph_scope)) OR
            (kind='checked_by' AND source_node_id IN (SELECT id FROM reflect_execution_graph_scope)))
            AND id NOT IN (SELECT id FROM reflect_graph_seen_edges)""")
        conn.execute("""DELETE FROM graph_nodes WHERE id IN (SELECT id FROM reflect_execution_graph_scope)
            AND id NOT IN (SELECT id FROM reflect_execution_graph_nodes)""")
        _copy_changed_graph_rows(conn, "temp.reflect_graph_seen_edges", "graph_edges", edges=True)
        conn.execute("""DELETE FROM graph_nodes WHERE kind='ToolCall'
            AND id IN (SELECT id FROM reflect_execution_graph_old_calls)
            AND NOT EXISTS (SELECT 1 FROM graph_edges WHERE source_node_id=graph_nodes.id OR target_node_id=graph_nodes.id)""")
        conn.execute("DROP TABLE temp.reflect_graph_seen_edges")
        conn.execute("DROP TABLE temp.reflect_execution_graph_nodes")
        conn.execute("DROP TABLE temp.reflect_execution_graph_scope")
        conn.execute("DROP TABLE temp.reflect_execution_graph_old_calls")
        conn.execute("RELEASE SAVEPOINT reflect_execution_graph")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT reflect_execution_graph")
        conn.execute("RELEASE SAVEPOINT reflect_execution_graph")
        raise
    finally:
        conn.row_factory = previous_factory
    if commit:
        conn.commit()
    return {"executions": executions, "checks": checks}
