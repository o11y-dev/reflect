from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from reflect.store.graph_ontology import NODE_KINDS, graph_id, tool_call_node, validate_graph
from reflect.store.graph_sources import GraphSourceWriter, copy_graph_sources
from reflect.store.hook_facts import HookFactRepository
from reflect.telemetry_facts import (
    extract_skill_name_from_path,
    extract_skill_name_from_preview,
    extract_skill_names_from_text,
    extract_subagent_name_from_tool,
    extract_subagent_names_from_text,
    first_attr,
    subagent_fact,
)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


_stable_id = graph_id

def _load_json_dict(value: object) -> dict:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _memory_exposure_rows(conn: sqlite3.Connection, memory_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT session_id, MIN(created_at) AS first_seen_at,
               MAX(updated_at) AS last_seen_at
        FROM memory_exposures
        WHERE memory_id = ?
        GROUP BY session_id
        UNION ALL
        SELECT session_id, created_at, updated_at
        FROM memories
        WHERE id = ?
          AND COALESCE(session_id, '') <> ''
          AND NOT EXISTS (
            SELECT 1 FROM memory_exposures me
            WHERE me.memory_id = memories.id
              AND me.session_id = memories.session_id
          )
        ORDER BY session_id
        """,
        (memory_id, memory_id),
    ).fetchall()


def _short_id(value: object, length: int = 10) -> str:
    text = str(value or "")
    return text[-length:] if len(text) > length else text


def _spec_label_from_plan(memory: sqlite3.Row, attrs: dict) -> str:
    name = str(attrs.get("name") or "").strip()
    if name:
        base = name.removesuffix(".plan.md").removesuffix(".md")
        if base:
            return base
    preview = str(memory["content_preview_redacted"] or "").strip()
    if preview:
        return preview
    return str(memory["id"] or "cursor-plan")


def _relative_repo_path(path: str, workspace_root: str) -> str | None:
    normalized_path = PurePosixPath(path.replace("\\", "/")).as_posix().strip()
    normalized_root = PurePosixPath(workspace_root.replace("\\", "/")).as_posix().strip()
    if not normalized_path or not normalized_root:
        return None
    root_prefix = normalized_root.rstrip("/")
    if normalized_path == root_prefix:
        return None
    path_prefix = f"{root_prefix}/"
    if not normalized_path.startswith(path_prefix):
        return None
    relative = normalized_path[len(path_prefix):].strip("/")
    return relative or None


def _infer_repo_id_for_memory(
    conn: sqlite3.Connection,
    *,
    explicit_repo_id: str,
    memory_path: str,
    memory_attrs: dict,
) -> str:
    if explicit_repo_id:
        return explicit_repo_id
    candidates: list[str] = []
    path_candidates = [memory_path]
    workspace_root = str(memory_attrs.get("workspace_root") or "").strip()
    relative_path = _relative_repo_path(memory_path, workspace_root) if memory_path and workspace_root else None
    if relative_path and relative_path not in path_candidates:
        path_candidates.append(relative_path)
    for candidate_path in path_candidates:
        if not candidate_path:
            continue
        repo_hint = conn.execute(
            """
            SELECT repo_id
            FROM files
            WHERE path = ? AND COALESCE(repo_id, '') <> ''
            GROUP BY repo_id
            ORDER BY COUNT(*) DESC, repo_id ASC
            LIMIT 1
            """,
            (candidate_path,),
        ).fetchone()
        if repo_hint and str(repo_hint["repo_id"] or ""):
            candidates.append(str(repo_hint["repo_id"]))
            break
    if candidates:
        return candidates[0]
    if workspace_root:
        repo_rows = conn.execute(
            """
            SELECT repo_id
            FROM sessions
            WHERE COALESCE(repo_id, '') <> ''
            GROUP BY repo_id
            ORDER BY COUNT(*) DESC, repo_id ASC
            LIMIT 2
            """
        ).fetchall()
        if len(repo_rows) == 1:
            return str(repo_rows[0]["repo_id"] or "")
    return ""


def _path_candidates(*values: object, preview: str = "") -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        if not isinstance(value, str):
            return
        cleaned = value.strip().strip("'\"")
        if not cleaned or "REDACTED" in cleaned.upper():
            return
        if cleaned.startswith("{") or cleaned.startswith("["):
            try:
                decoded = json.loads(cleaned)
            except json.JSONDecodeError:
                return
            stack = [decoded]
            while stack:
                item = stack.pop()
                if isinstance(item, dict):
                    stack.extend(item.values())
                elif isinstance(item, list):
                    stack.extend(item)
                else:
                    add(item)
            return
        if len(cleaned) > 260:
            return
        if any(char in cleaned for char in "{}[]*?<>|") or any(char.isspace() for char in cleaned):
            return
        if not (
            "/" in cleaned
            or cleaned.startswith(".")
            or re.search(r"\.[A-Za-z0-9]{1,8}$", cleaned)
        ):
            return
        normalized = PurePosixPath(cleaned.replace("\\", "/")).as_posix()
        if normalized not in seen:
            seen.add(normalized)
            paths.append(normalized)

    for value in values:
        add(value)
    if preview:
        for match in re.finditer(
            r"(?<![A-Za-z0-9_./-])((?:\.{0,2}/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)",
            preview,
        ):
            candidate = match.group(1)
            if candidate.startswith(("/", "./", "../")) or re.search(
                r"\.[A-Za-z0-9]{1,8}$",
                candidate,
            ):
                add(candidate)
        for match in re.finditer(
            r"(?<![A-Za-z0-9_./-])([A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8})(?![A-Za-z0-9_./-])",
            preview,
        ):
            add(match.group(1))
    return paths


def _folder_candidates(path: str) -> list[str]:
    normalized = PurePosixPath(path.replace("\\", "/")).as_posix().strip()
    if not normalized or normalized in {".", "/"} or "/" not in normalized:
        return []
    absolute = normalized.startswith("/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if len(parts) <= 1:
        return []
    folder_parts = parts[:-1]
    folders: list[str] = []
    seen: set[str] = set()
    for depth in range(1, len(folder_parts) + 1):
        folder = "/".join(folder_parts[:depth])
        if absolute:
            folder = "/" + folder
        if folder not in seen:
            seen.add(folder)
            folders.append(folder)
    if absolute:
        generic_roots = {"/Users", "/home", Path.home().as_posix()}
        folders = [folder for folder in folders[-3:] if folder not in generic_roots]
    elif len(folders) > 6:
        folders = folders[-6:]
    return folders


def _workspace_relative_path(path: str, workspace_root: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/")).as_posix().strip()
    if not normalized or normalized in {".", "/"}:
        return ""
    if normalized.startswith("/"):
        return _relative_repo_path(normalized, workspace_root) or ""
    relative = normalized.removeprefix("./").strip("/")
    if not relative or relative == ".." or relative.startswith("../"):
        return ""
    return relative


def _outcomes_for_tool(tool_name: str, text: str, status: str) -> list[dict]:
    haystack = f"{tool_name}\n{text}".lower()
    outcomes: list[dict] = []

    def add(label: str, confidence: float, evidence: str) -> None:
        outcomes.append(
            {
                "label": label,
                "confidence": confidence,
                "status": status,
                "evidence": evidence[:220],
                "evidence_class": "inferred",
            }
        )

    # Git mutations are derived from paired operations in git_activity. A text
    # mention here must not create a competing factual outcome.
    if re.search(r"\b(reflect\s+report|report generated|wrote reports?/|reports?/[^ ]+\.html)\b", haystack):
        add("report_generated", 0.75, text)
    if re.search(r"\b(jira_update_issue|update_issue|issue updated|gh\s+issue\s+(edit|comment))\b", haystack):
        add("issue_updated", 0.7, text)
    return outcomes


def _insert_node(
    conn: sqlite3.Connection,
    *,
    kind: str,
    label: str,
    session_id: str | None = None,
    attrs: dict | None = None,
    first_seen_at: str | None = None,
    last_seen_at: str | None = None,
    identity_key: str = "",
    timestamp: str,
) -> tuple[str, bool]:
    if kind not in NODE_KINDS:
        raise ValueError(f"Unknown graph node kind: {kind}")
    node_id = (
        _stable_id("node", kind, identity_key)
        if identity_key
        else _stable_id("node", kind, label, session_id or "")
    )
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes(
          id, kind, label, session_id, first_seen_at, last_seen_at,
          attrs_json, identity_key, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            node_id,
            kind,
            label,
            session_id,
            first_seen_at,
            last_seen_at,
            json.dumps(attrs or {}, sort_keys=True),
            identity_key,
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
        INSERT INTO reflect_graph_seen_edges(
          id, source_node_id, target_node_id, kind, session_id, weight,
          first_seen_at, last_seen_at, attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          weight = reflect_graph_seen_edges.weight + 1,
          last_seen_at = COALESCE(excluded.last_seen_at, reflect_graph_seen_edges.last_seen_at),
          attrs_json = excluded.attrs_json,
          updated_at = excluded.updated_at
        RETURNING weight
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
    return edge_id, cursor.fetchone()[0] == 1


def _refresh_node(
    conn: sqlite3.Connection,
    *,
    node_id: str,
    attrs: dict | None,
    first_seen_at: str | None,
    last_seen_at: str | None,
    timestamp: str,
) -> None:
    conn.execute(
        """
        UPDATE graph_nodes
        SET attrs_json = ?,
            first_seen_at = COALESCE(first_seen_at, ?),
            last_seen_at = COALESCE(?, last_seen_at),
            updated_at = ?
        WHERE id = ?
          AND (attrs_json, first_seen_at, last_seen_at) IS NOT
              (?, COALESCE(first_seen_at, ?), COALESCE(?, last_seen_at))
        """,
        (
            json.dumps(attrs or {}, sort_keys=True),
            first_seen_at,
            last_seen_at,
            timestamp,
            node_id,
            json.dumps(attrs or {}, sort_keys=True),
            first_seen_at,
            last_seen_at,
        ),
    )


def _copy_changed_graph_rows(conn: sqlite3.Connection, source: str, target: str, *, edges: bool) -> None:
    """Apply one derived batch, preserving persistence timestamps on no-ops."""
    fields = (
        "source_node_id,target_node_id,kind,session_id,weight,first_seen_at,last_seen_at,attrs_json"
        if edges else "kind,label,session_id,first_seen_at,last_seen_at,attrs_json,identity_key"
    ).split(",")
    columns = ",".join(("id", *fields, "created_at", "updated_at"))
    assignments = ",".join(f"{field}=excluded.{field}" for field in (*fields, "updated_at"))
    current = target.rsplit(".", 1)[-1]
    conn.execute(f"""
        INSERT INTO {target}({columns}) SELECT {columns} FROM {source} WHERE 1
        ON CONFLICT(id) DO UPDATE SET {assignments}
        WHERE ({','.join(f'{current}.{field}' for field in fields)})
          IS NOT ({','.join(f'excluded.{field}' for field in fields)})
    """)


def _create_graph_stage(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TEMP TABLE graph_edge_sources AS SELECT * FROM main.graph_edge_sources WHERE 0")
    conn.execute("CREATE UNIQUE INDEX temp.reflect_stage_graph_sources ON graph_edge_sources(edge_id,source_kind,source_id)")
    for table in ("graph_nodes", "graph_edges"):
        conn.execute(f"CREATE TEMP TABLE {table} AS SELECT * FROM main.{table} WHERE 0")
        conn.execute(f"CREATE UNIQUE INDEX temp.reflect_stage_{table}_id ON {table}(id)")
    conn.execute("CREATE UNIQUE INDEX temp.reflect_stage_nodes_identity ON graph_nodes(kind,identity_key) WHERE identity_key<>''")
    conn.execute("CREATE UNIQUE INDEX temp.reflect_stage_nodes_label ON graph_nodes(kind,label,COALESCE(session_id,''),identity_key)")


def _preserve_execution_graph(conn: sqlite3.Connection, session_ids: list[str] | None = None) -> None:
    scope = "" if session_ids is None else " AND session_id IN (SELECT session_id FROM reflect_changed_sessions)"
    conn.execute(f"""INSERT OR IGNORE INTO temp.graph_nodes SELECT * FROM main.graph_nodes
        WHERE (kind='Execution' {scope}) OR id IN (
            SELECT target_node_id FROM main.graph_edges WHERE kind='checked_by' {scope})""")
    conn.execute(f"""INSERT OR IGNORE INTO temp.graph_edges SELECT * FROM main.graph_edges
        WHERE kind IN ('has_execution','checked_by') {scope}""")


def _drop_graph_stage(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE temp.graph_edge_sources")
    conn.execute("DROP TABLE temp.graph_edges")
    conn.execute("DROP TABLE temp.graph_nodes")


def rebuild_graph(
    conn: sqlite3.Connection,
    *,
    commit: bool = True,
) -> dict[str, int]:
    """Derive the complete graph once, then write only changed persistent rows."""
    if not commit and not conn.in_transaction:
        conn.execute("BEGIN")
    previous_temp_cache = int(conn.execute("PRAGMA temp.cache_size").fetchone()[0])
    conn.execute("SAVEPOINT reflect_graph_rebuild")
    try:
        # Give the default TEMP cache a 32 MiB target for hashed identities.
        # Preserve explicit caller settings and the existing storage mode.
        if previous_temp_cache == 0:
            conn.execute("PRAGMA temp.cache_size = -32768")
        _create_graph_stage(conn)
        _preserve_execution_graph(conn)
        result = _derive_graph(conn, commit=False)
        conn.execute("DELETE FROM main.graph_edges WHERE id NOT IN (SELECT id FROM temp.graph_edges)")
        conn.execute("DELETE FROM main.graph_nodes WHERE id NOT IN (SELECT id FROM temp.graph_nodes)")
        _copy_changed_graph_rows(conn, "temp.graph_nodes", "main.graph_nodes", edges=False)
        _copy_changed_graph_rows(conn, "temp.graph_edges", "main.graph_edges", edges=True)
        copy_graph_sources(conn, scoped=False)
        _drop_graph_stage(conn)
        conn.execute("RELEASE SAVEPOINT reflect_graph_rebuild")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT reflect_graph_rebuild")
        conn.execute("RELEASE SAVEPOINT reflect_graph_rebuild")
        raise
    finally:
        if previous_temp_cache == 0:
            conn.execute("PRAGMA temp.cache_size = 0")
    if commit:
        conn.commit()
    return result


def _finish_graph(conn: sqlite3.Connection, nodes: int, edges: int, *, commit: bool) -> dict[str, int]:
    validate_graph(conn, edges_table="reflect_graph_seen_edges")
    _copy_changed_graph_rows(conn, "temp.reflect_graph_seen_edges", "graph_edges", edges=True)
    if commit:
        conn.commit()
    return {"nodes": nodes, "edges": edges}


def _derive_graph(
    conn: sqlite3.Connection,
    *,
    commit: bool = True,
    source_only: bool = False,
) -> dict[str, int]:
    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        timestamp = _now()
        nodes = 0
        edges = 0
        writer = GraphSourceWriter(conn, _insert_edge)
        observed_agents: dict[str, dict[str, object]] = {}

        conn.execute("CREATE TEMP TABLE reflect_graph_seen_edges AS SELECT * FROM graph_edges WHERE 0")
        conn.execute("CREATE UNIQUE INDEX temp.reflect_graph_seen_edges_id ON reflect_graph_seen_edges(id)")

        session_contexts: dict[str, dict[str, str]] = {}
        for session in conn.execute("SELECT * FROM sessions ORDER BY started_at, id"):
            writer.source()
            session_id = str(session["id"])
            workspace_id = str(session["workspace_id"] or "")
            repo_id = str(session["repo_id"] or "")
            parent_session_id = str(session["parent_session_id"] or "")
            session_node, inserted = _insert_node(
                conn,
                kind="Session",
                label=session_id,
                session_id=session_id,
                attrs={
                    "status": session["status"],
                    "agent_id": session["agent_id"],
                    "workspace_id": workspace_id,
                    "repo_id": repo_id,
                    "parent_session_id": parent_session_id,
                },
                first_seen_at=session["started_at"],
                last_seen_at=session["ended_at"],
                timestamp=timestamp,
            )
            nodes += int(inserted)

            workspace = None
            if workspace_id:
                workspace = conn.execute(
                    "SELECT * FROM workspaces WHERE id = ?",
                    (workspace_id,),
                ).fetchone()
            if workspace:
                workspace_node, inserted = _insert_node(
                    conn,
                    kind="Workspace",
                    label=workspace["root_path"],
                    attrs={
                        "workspace_id": workspace["id"],
                        "label": workspace["label"],
                        "repo_id": workspace["repo_id"],
                        "source": workspace["source_key"],
                        "confidence": workspace["confidence"],
                    },
                    identity_key=str(workspace["id"]),
                    timestamp=timestamp,
                )
                nodes += int(inserted)
                _, inserted = writer.edge(
                    conn,
                    source_node_id=session_node,
                    target_node_id=workspace_node,
                    kind="ran_in_workspace",
                    session_id=session_id,
                    attrs={"confidence": workspace["confidence"]},
                    first_seen_at=session["started_at"],
                    last_seen_at=session["ended_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)
                session_contexts[session_id] = {
                    "workspace_id": workspace_id,
                    "workspace_root": str(workspace["root_path"] or ""),
                    "repo_id": repo_id,
                }

            if parent_session_id:
                parent = conn.execute(
                    "SELECT * FROM main.sessions WHERE id = ?",
                    (parent_session_id,),
                ).fetchone()
                if parent:
                    parent_node, inserted = _insert_node(
                        conn,
                        kind="Session",
                        label=parent_session_id,
                        session_id=parent_session_id,
                        attrs={
                            "status": parent["status"],
                            "agent_id": parent["agent_id"],
                            "workspace_id": str(parent["workspace_id"] or ""),
                            "repo_id": str(parent["repo_id"] or ""),
                            "parent_session_id": str(parent["parent_session_id"] or ""),
                        },
                        first_seen_at=parent["started_at"],
                        last_seen_at=parent["ended_at"],
                        timestamp=timestamp,
                    )
                    nodes += int(inserted)
                    _, inserted = writer.edge(
                        conn,
                        source_node_id=parent_node,
                        target_node_id=session_node,
                        kind="spawned_session",
                        session_id=session_id,
                        attrs={"source": "canonical_parent_session"},
                        first_seen_at=session["started_at"],
                        last_seen_at=session["ended_at"],
                        timestamp=timestamp,
                    )
                    edges += int(inserted)

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
                    _, inserted = writer.edge(
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

            if session["repo_id"]:
                repo = conn.execute("SELECT * FROM repos WHERE id = ?", (session["repo_id"],)).fetchone()
                if repo:
                    repo_node, inserted = _insert_node(
                        conn,
                        kind="Repo",
                        label=repo["full_name"],
                        attrs={
                            "repo_id": repo["id"],
                            "provider": repo["provider"],
                            "branch": repo["branch"],
                            "dirty": bool(repo["dirty"]),
                        },
                        timestamp=timestamp,
                    )
                    nodes += int(inserted)
                    _, inserted = writer.edge(
                        conn,
                        source_node_id=session_node,
                        target_node_id=repo_node,
                        kind="worked_in_repo",
                        session_id=session["id"],
                        first_seen_at=session["started_at"],
                        last_seen_at=session["ended_at"],
                        timestamp=timestamp,
                    )
                    edges += int(inserted)

            # The append guard admits only independent new source facts.
            # Delegation/context changes use the session repair path below.
            hook_facts = None if source_only else HookFactRepository(conn).load_agent_events(str(session["id"]))
            if hook_facts:
                observed_agents.update(hook_facts.agent_events_by_step)
            agent_types_by_id = hook_facts.agent_types_by_id() if hook_facts else {}
            for step in conn.execute(
                "SELECT id, seq, summary, started_at, ended_at, raw_attrs_json FROM steps WHERE session_id = ? ORDER BY seq, id",
                (session["id"],),
            ):
                writer.source("step", step["id"], f"0:{session['started_at']}:{session['id']}:{step['seq']:020d}:{step['id']}")
                step_attrs = _load_json_dict(step["raw_attrs_json"])
                prompt_text = str(first_attr(step_attrs, "gen_ai.client.prompt", "gen_ai.client.prompt.text", "prompt") or "")
                file_path = str(
                    first_attr(
                        step_attrs,
                        "gen_ai.client.file_path",
                        "gen_ai.client.tool.input.file_path",
                        "gen_ai.client.tool.input.path",
                        "tool.input.file_path",
                        "tool.input.path",
                        "file.path",
                        "path",
                    )
                    or ""
                )
                skill_names = set(extract_skill_names_from_text(prompt_text))
                path_skill = extract_skill_name_from_path(file_path)
                if path_skill:
                    skill_names.add(path_skill)
                for skill_name in sorted(skill_names):
                    skill_node, inserted = _insert_node(
                        conn,
                        kind="Skill",
                        label=skill_name,
                        attrs={"evidence_class": "context"},
                        timestamp=timestamp,
                    )
                    nodes += int(inserted)
                    _, inserted = writer.edge(
                        conn,
                        source_node_id=session_node,
                        target_node_id=skill_node,
                        kind="used_skill",
                        session_id=session["id"],
                        attrs={"source": "step", "step_id": step["id"]},
                        first_seen_at=step["started_at"],
                        last_seen_at=step["ended_at"],
                        timestamp=timestamp,
                    )
                    edges += int(inserted)

                agent_event = (hook_facts.agent_event_for_step(step["id"]) if hook_facts else None) or {}
                event = str(
                    agent_event.get("event_name")
                    or first_attr(step_attrs, "gen_ai.client.hook.event", "ide.hook.event")
                    or step["summary"]
                    or ""
                )
                event_lc = event.lower()
                subagent_type = str(
                    agent_event.get("agent_type")
                    or first_attr(
                        step_attrs,
                        "gen_ai.client.subagent_type",
                        "ide.subagent_type",
                        "subagent.type",
                    )
                    or ""
                )
                is_subagent_event = bool(agent_event) or event_lc.replace("_", "") in {"subagentstart", "subagentstop"}
                if is_subagent_event:
                    explicit = subagent_fact(step_attrs, event)
                    subagent_name = subagent_type or (explicit.label if explicit else "Role not recorded")
                    agent_id = str(
                        agent_event.get("agent_id")
                        or first_attr(step_attrs, "gen_ai.client.agent_id", "gen_ai.agent.id")
                        or ""
                    )
                    parent_agent_id = str(
                        agent_event.get("parent_agent_id")
                        or first_attr(step_attrs, "gen_ai.client.parent_agent_id")
                        or ""
                    )
                    subagent_node, inserted = _insert_node(
                        conn,
                        kind="Subagent",
                        label=subagent_name,
                        session_id=session["id"],
                        identity_key=(
                            f"{session['id']}:{agent_id or agent_event.get('event_id') or step['id']}"
                        ),
                        attrs={
                            "source": "lifecycle",
                            "event": event,
                            "agent_id": agent_id,
                            "parent_agent_id": parent_agent_id,
                        },
                        timestamp=timestamp,
                    )
                    nodes += int(inserted)
                    _, inserted = writer.edge(
                        conn,
                        source_node_id=session_node,
                        target_node_id=subagent_node,
                        kind="spawned_subagent" if "stop" not in event_lc else "stopped_subagent",
                        session_id=session["id"],
                        attrs={
                            "source": "step",
                            "step_id": step["id"],
                            "agent_id": agent_id,
                            "parent_agent_id": parent_agent_id,
                        },
                        first_seen_at=step["started_at"],
                        last_seen_at=step["ended_at"],
                        timestamp=timestamp,
                    )
                    edges += int(inserted)
                    if parent_agent_id and parent_agent_id in agent_types_by_id:
                        parent_node, inserted = _insert_node(
                            conn,
                            kind="Subagent",
                            label=agent_types_by_id[parent_agent_id],
                            session_id=session["id"],
                            identity_key=f"{session['id']}:{parent_agent_id}",
                            attrs={
                                "source": "lifecycle",
                                "agent_id": parent_agent_id,
                            },
                            timestamp=timestamp,
                        )
                        nodes += int(inserted)
                        _, inserted = writer.edge(
                            conn,
                            source_node_id=parent_node,
                            target_node_id=subagent_node,
                            kind="delegated_to",
                            session_id=session["id"],
                            attrs={"source": "hook_fact", "step_id": step["id"]},
                            first_seen_at=step["started_at"],
                            last_seen_at=step["ended_at"],
                            timestamp=timestamp,
                        )
                        edges += int(inserted)

                for subagent_name in sorted(extract_subagent_names_from_text(prompt_text)):
                    subagent_node, inserted = _insert_node(
                        conn,
                        kind="Subagent",
                        label=subagent_name,
                        session_id=session["id"],
                        attrs={"source": "prompt_text", "confidence": "medium"},
                        timestamp=timestamp,
                    )
                    nodes += int(inserted)
                    _, inserted = writer.edge(
                        conn,
                        source_node_id=session_node,
                        target_node_id=subagent_node,
                        kind="spawned_subagent",
                        session_id=session["id"],
                        attrs={"source": "prompt_text", "step_id": step["id"]},
                        first_seen_at=step["started_at"],
                        last_seen_at=step["ended_at"],
                        timestamp=timestamp,
                    )
                    edges += int(inserted)

        for tool in conn.execute("SELECT * FROM tool_calls ORDER BY created_at, id"):
            writer.source("tool_call", tool["id"], f"1:{tool['created_at']}:{tool['id']}")
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
            _, inserted = writer.edge(
                conn,
                source_node_id=session_node,
                target_node_id=tool_node,
                kind="used_tool",
                session_id=tool["session_id"],
                attrs={"status": tool["status"]},
                timestamp=timestamp,
            )
            edges += int(inserted)

            tool_attrs = _load_json_dict(tool["raw_attrs_json"])
            preview = tool["input_preview_redacted"] or ""
            output_preview = tool["output_preview_redacted"] or ""
            # Ordinary call topology is projected on demand from canonical rows.
            # Keep a call node only when semantic evidence needs it as an endpoint.
            call = tool_call_node(tool)
            call_node = call["id"]
            call_materialized = False

            def materialize_call(call=call) -> int:
                nonlocal call_materialized
                if call_materialized:
                    return 0
                call_materialized = True
                _, created = _insert_node(
                    conn, kind="ToolCall", label=call["label"], session_id=call["session_id"],
                    identity_key=call["identity_key"], attrs=call["attrs"],
                    first_seen_at=call["first_seen_at"], last_seen_at=call["last_seen_at"], timestamp=timestamp,
                )
                if not created:
                    _refresh_node(conn, node_id=call["id"], attrs=call["attrs"],
                                  first_seen_at=call["first_seen_at"], last_seen_at=call["last_seen_at"], timestamp=timestamp)
                return int(created)

            skill_names = set(extract_skill_names_from_text(preview))
            if str(tool["tool_name"]).lower() == "skill":
                skill_name = extract_skill_name_from_preview(preview)
                if skill_name:
                    skill_names.add(skill_name)
            explicit_path = first_attr(
                    tool_attrs,
                    "gen_ai.client.file_path",
                    "gen_ai.client.tool.input.file_path",
                    "gen_ai.client.tool.input.path",
                    "tool.input.file_path",
                    "tool.input.path",
                    "file.path",
                    "path",
                )
            for path in _path_candidates(explicit_path, preview=preview):
                nodes += materialize_call()
                path_skill = extract_skill_name_from_path(path)
                if path_skill:
                    skill_names.add(path_skill)
                context = session_contexts.get(str(tool["session_id"]), {})
                workspace_id = context.get("workspace_id", "")
                workspace_root = context.get("workspace_root", "")
                relative_path = (
                    _workspace_relative_path(path, workspace_root)
                    if workspace_id and workspace_root
                    else ""
                )
                shared_path = bool(relative_path)
                path_label = relative_path or path
                path_session_id = None if shared_path else tool["session_id"]
                path_identity = (
                    f"workspace:{workspace_id}:path:{relative_path}"
                    if shared_path
                    else ""
                )
                path_node, inserted = _insert_node(
                    conn,
                    kind="Path",
                    label=path_label,
                    session_id=path_session_id,
                    attrs={
                        "source": "tool_call",
                        "tool_name": tool["tool_name"],
                        "workspace_id": workspace_id,
                        "repo_id": context.get("repo_id", ""),
                        "relative_path": relative_path,
                    },
                    identity_key=path_identity,
                    timestamp=timestamp,
                )
                nodes += int(inserted)
                _, inserted = writer.edge(
                    conn,
                    source_node_id=call_node,
                    target_node_id=path_node,
                    kind="touched_path",
                    session_id=tool["session_id"],
                    attrs={"status": tool["status"], "tool_name": tool["tool_name"]},
                    first_seen_at=tool["created_at"],
                    last_seen_at=tool["updated_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)
                folder_paths = (
                    _folder_candidates(relative_path)
                    if shared_path
                    else ([] if workspace_id else _folder_candidates(path))
                )
                for folder in folder_paths:
                    folder_identity = (
                        f"workspace:{workspace_id}:folder:{folder}"
                        if shared_path
                        else ""
                    )
                    folder_node, inserted = _insert_node(
                        conn,
                        kind="Folder",
                        label=folder,
                        session_id=None if shared_path else tool["session_id"],
                        attrs={
                            "source": "tool_call_path",
                            "tool_name": tool["tool_name"],
                            "workspace_id": workspace_id,
                            "repo_id": context.get("repo_id", ""),
                            "relative_path": folder if shared_path else "",
                        },
                        identity_key=folder_identity,
                        timestamp=timestamp,
                    )
                    nodes += int(inserted)
                    _, inserted = writer.edge(
                        conn,
                        source_node_id=session_node,
                        target_node_id=folder_node,
                        kind="touched_folder",
                        session_id=tool["session_id"],
                        attrs={"status": tool["status"], "tool_name": tool["tool_name"]},
                        first_seen_at=tool["created_at"],
                        last_seen_at=tool["updated_at"],
                        timestamp=timestamp,
                    )
                    edges += int(inserted)
                    _, inserted = writer.edge(
                        conn,
                        source_node_id=call_node,
                        target_node_id=folder_node,
                        kind="touched_folder",
                        session_id=tool["session_id"],
                        attrs={"status": tool["status"], "tool_name": tool["tool_name"]},
                        first_seen_at=tool["created_at"],
                        last_seen_at=tool["updated_at"],
                        timestamp=timestamp,
                    )
                    edges += int(inserted)
                    _, inserted = writer.edge(
                        conn,
                        source_node_id=folder_node,
                        target_node_id=path_node,
                        kind="contains_touched_path",
                        session_id=tool["session_id"],
                        attrs={"status": tool["status"], "tool_name": tool["tool_name"]},
                        first_seen_at=tool["created_at"],
                        last_seen_at=tool["updated_at"],
                        timestamp=timestamp,
                    )
                    edges += int(inserted)

            for skill_name in sorted(skill_names):
                nodes += materialize_call()
                skill_node, inserted = _insert_node(
                    conn,
                    kind="Skill",
                    label=skill_name,
                    attrs={"evidence_class": "context"},
                    timestamp=timestamp,
                )
                nodes += int(inserted)
                _, inserted = writer.edge(
                    conn,
                    source_node_id=session_node,
                    target_node_id=skill_node,
                    kind="used_skill",
                    session_id=tool["session_id"],
                    attrs={"source": "tool_call", "tool_call_id": tool["id"]},
                    first_seen_at=tool["created_at"],
                    last_seen_at=tool["updated_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)
                _, inserted = writer.edge(
                    conn,
                    source_node_id=skill_node,
                    target_node_id=call_node,
                    kind="drove_tool_call",
                    session_id=tool["session_id"],
                    attrs={"source": "tool_call"},
                    first_seen_at=tool["created_at"],
                    last_seen_at=tool["updated_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)

            subagent_name = extract_subagent_name_from_tool(str(tool["tool_name"]), tool_attrs, preview)
            if subagent_name:
                observed_agent = observed_agents.get(str(tool["step_id"]), {})
                tool_fact = subagent_fact(tool_attrs, tool_name=tool["tool_name"], preview=preview)
                subagent_name = str(observed_agent.get("agent_type") or subagent_name)
                child_id = observed_agent.get("agent_id") or (tool_fact.agent_id if tool_fact else "")
                identity = child_id or observed_agent.get("event_id") or f"tool:{tool['logical_call_id'] or tool['id']}"
                nodes += materialize_call()
                subagent_node, inserted = _insert_node(
                    conn,
                    kind="Subagent",
                    label=subagent_name,
                    session_id=tool["session_id"],
                    identity_key=f"{tool['session_id']}:{identity}",
                    attrs={"source": "tool_call", "status": tool["status"]},
                    timestamp=timestamp,
                )
                nodes += int(inserted)
                _, inserted = writer.edge(
                    conn,
                    source_node_id=session_node,
                    target_node_id=subagent_node,
                    kind="spawned_subagent",
                    session_id=tool["session_id"],
                    attrs={"source": "tool_call", "tool_call_id": tool["id"]},
                    first_seen_at=tool["created_at"],
                    last_seen_at=tool["updated_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)
                _, inserted = writer.edge(
                    conn,
                    source_node_id=subagent_node,
                    target_node_id=call_node,
                    kind="launched_by_tool_call",
                    session_id=tool["session_id"],
                    attrs={"tool_name": tool["tool_name"], "status": tool["status"]},
                    first_seen_at=tool["created_at"],
                    last_seen_at=tool["updated_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)

            outcome_text = "\n".join(str(part or "") for part in (tool["tool_name"], preview, output_preview, tool["error_message_redacted"]))
            for outcome in _outcomes_for_tool(str(tool["tool_name"]), outcome_text, str(tool["status"])):
                nodes += materialize_call()
                outcome_node, inserted = _insert_node(
                    conn,
                    kind="Outcome",
                    label=outcome["label"],
                    session_id=tool["session_id"],
                    attrs=outcome,
                    first_seen_at=tool["created_at"],
                    last_seen_at=tool["updated_at"],
                    timestamp=timestamp,
                )
                nodes += int(inserted)
                _, inserted = writer.edge(
                    conn,
                    source_node_id=session_node,
                    target_node_id=outcome_node,
                    kind="suggests_outcome",
                    session_id=tool["session_id"],
                    attrs={"tool_call_id": tool["id"], "confidence": outcome["confidence"]},
                    first_seen_at=tool["created_at"],
                    last_seen_at=tool["updated_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)
                _, inserted = writer.edge(
                    conn,
                    source_node_id=call_node,
                    target_node_id=outcome_node,
                    kind="suggests_outcome",
                    session_id=tool["session_id"],
                    attrs={"confidence": outcome["confidence"]},
                    first_seen_at=tool["created_at"],
                    last_seen_at=tool["updated_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)

        for mcp in conn.execute(
            """
            SELECT
              mc.*,
              mc.tool_call_id AS id,
              tc.session_id,
              tc.status,
              tc.duration_ms
            FROM mcp_calls AS mc
            JOIN tool_calls AS tc ON tc.id = mc.tool_call_id
            WHERE mc.server_name IS NOT NULL
            ORDER BY mc.created_at, mc.tool_call_id
            """
        ):
            writer.source("tool_call", mcp["id"], f"1:{mcp['created_at']}:{mcp['id']}")
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
            _, inserted = writer.edge(
                conn,
                source_node_id=session_node,
                target_node_id=mcp_node,
                kind="used_mcp",
                session_id=mcp["session_id"],
                attrs={"tool_name": mcp["tool_name"], "status": mcp["status"]},
                timestamp=timestamp,
            )
            edges += int(inserted)

        if source_only:
            return _finish_graph(conn, nodes, edges, commit=commit)

        writer.source()
        for repo in conn.execute("SELECT * FROM repos ORDER BY full_name"):
            repo_node, inserted = _insert_node(
                conn,
                kind="Repo",
                label=repo["full_name"],
                attrs={
                    "repo_id": repo["id"],
                    "provider": repo["provider"],
                    "branch": repo["branch"],
                    "dirty": bool(repo["dirty"]),
                },
                timestamp=timestamp,
            )
            nodes += int(inserted)
            for file in conn.execute("SELECT * FROM files WHERE repo_id = ? ORDER BY path", (repo["id"],)):
                file_node, inserted = _insert_node(
                    conn,
                    kind="Path",
                    label=file["path"],
                    attrs={
                        "file_id": file["id"],
                        "repo_id": file["repo_id"],
                        "language": file["language"],
                        "extension": file["extension"],
                        "role": file["role"],
                        "read_count": file["read_count"],
                        "write_count": file["write_count"],
                        "sensitivity": file["sensitivity"],
                    },
                    timestamp=timestamp,
                )
                nodes += int(inserted)
                _, inserted = writer.edge(
                    conn,
                    source_node_id=repo_node,
                    target_node_id=file_node,
                    kind="contains_path",
                    attrs={"read_count": file["read_count"], "write_count": file["write_count"]},
                    timestamp=timestamp,
                )
                edges += int(inserted)

        for spec in conn.execute("SELECT * FROM specs ORDER BY updated_at, id"):
            spec_attrs = {
                "spec_id": spec["id"],
                "repo_id": spec["repo_id"],
                "status": spec["status"],
                "owner": spec["owner"],
                "source_path": spec["source_path"],
                "source": "spec_table",
            }
            spec_node, inserted = _insert_node(
                conn,
                kind="Spec",
                label=spec["title"],
                attrs=spec_attrs,
                first_seen_at=spec["created_at"],
                last_seen_at=spec["updated_at"],
                timestamp=timestamp,
            )
            if not inserted:
                _refresh_node(
                    conn,
                    node_id=spec_node,
                    attrs=spec_attrs,
                    first_seen_at=spec["created_at"],
                    last_seen_at=spec["updated_at"],
                    timestamp=timestamp,
                )
            nodes += int(inserted)
            if spec["repo_id"]:
                repo = conn.execute("SELECT * FROM repos WHERE id = ?", (spec["repo_id"],)).fetchone()
                if repo:
                    repo_node, inserted = _insert_node(
                        conn,
                        kind="Repo",
                        label=repo["full_name"],
                        attrs={
                            "repo_id": repo["id"],
                            "provider": repo["provider"],
                            "branch": repo["branch"],
                            "dirty": bool(repo["dirty"]),
                        },
                        timestamp=timestamp,
                    )
                    nodes += int(inserted)
                    _, inserted = writer.edge(
                        conn,
                        source_node_id=repo_node,
                        target_node_id=spec_node,
                        kind="defines_spec",
                        attrs={"status": spec["status"], "owner": spec["owner"]},
                        first_seen_at=spec["created_at"],
                        last_seen_at=spec["updated_at"],
                        timestamp=timestamp,
                    )
                    edges += int(inserted)
            spec_path = str(spec["source_path"] or "").strip()
            if spec_path:
                path_node, inserted = _insert_node(
                    conn,
                    kind="Path",
                    label=spec_path,
                    attrs={"source": "spec", "spec_id": spec["id"]},
                    timestamp=timestamp,
                )
                nodes += int(inserted)
                _, inserted = writer.edge(
                    conn,
                    source_node_id=spec_node,
                    target_node_id=path_node,
                    kind="described_by_path",
                    attrs={"source": "spec", "spec_id": spec["id"]},
                    first_seen_at=spec["created_at"],
                    last_seen_at=spec["updated_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)
            for evidence in conn.execute(
                """
                SELECT e.session_id, MIN(e.created_at) AS first_seen_at, MAX(e.updated_at) AS last_seen_at
                FROM evidence e
                JOIN requirements r ON r.id = e.requirement_id
                WHERE r.spec_id = ? AND COALESCE(e.session_id, '') <> ''
                GROUP BY e.session_id
                ORDER BY e.session_id
                """,
                (spec["id"],),
            ):
                session_node, _ = _insert_node(
                    conn,
                    kind="Session",
                    label=evidence["session_id"],
                    session_id=evidence["session_id"],
                    timestamp=timestamp,
                )
                _, inserted = writer.edge(
                    conn,
                    source_node_id=session_node,
                    target_node_id=spec_node,
                    kind="addressed_spec",
                    session_id=evidence["session_id"],
                    attrs={"spec_id": spec["id"], "status": spec["status"]},
                    first_seen_at=evidence["first_seen_at"],
                    last_seen_at=evidence["last_seen_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)

        for memory in conn.execute("SELECT * FROM memories WHERE type <> 'cursor_plan' ORDER BY created_at, id"):
            memory_attrs = _load_json_dict(memory["raw_attrs_json"])
            memory_path = str(memory_attrs.get("path") or "").strip()
            memory_repo_id = _infer_repo_id_for_memory(
                conn,
                explicit_repo_id=str(memory["repo_id"] or ""),
                memory_path=memory_path,
                memory_attrs=memory_attrs,
            )
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
            path_node_id: str | None = None
            if memory_path:
                path_node, inserted = _insert_node(
                    conn,
                    kind="Path",
                    label=memory_path,
                    session_id=memory["session_id"],
                    attrs={
                        "source": memory["source"],
                        "scope": memory["scope"],
                        "kind": memory["type"],
                    },
                    timestamp=timestamp,
                )
                nodes += int(inserted)
                path_node_id = path_node
                _, inserted = writer.edge(
                    conn,
                    source_node_id=memory_node,
                    target_node_id=path_node,
                    kind="described_by_path",
                    session_id=memory["session_id"],
                    attrs={"source": memory["source"], "scope": memory["scope"]},
                    first_seen_at=memory["created_at"],
                    last_seen_at=memory["last_seen_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)
            if memory_repo_id:
                repo = conn.execute("SELECT * FROM repos WHERE id = ?", (memory_repo_id,)).fetchone()
                if repo:
                    repo_node, inserted = _insert_node(
                        conn,
                        kind="Repo",
                        label=repo["full_name"],
                        attrs={
                            "repo_id": repo["id"],
                            "provider": repo["provider"],
                            "branch": repo["branch"],
                            "dirty": bool(repo["dirty"]),
                        },
                        timestamp=timestamp,
                    )
                    nodes += int(inserted)
                    _, inserted = writer.edge(
                        conn,
                        source_node_id=repo_node,
                        target_node_id=memory_node,
                        kind="recorded_memory",
                        session_id=memory["session_id"],
                        attrs={"scope": memory["scope"], "type": memory["type"], "source": memory["source"]},
                        first_seen_at=memory["created_at"],
                        last_seen_at=memory["last_seen_at"],
                        timestamp=timestamp,
                    )
                    edges += int(inserted)
                    if path_node_id:
                        _, inserted = writer.edge(
                            conn,
                            source_node_id=repo_node,
                            target_node_id=path_node_id,
                            kind="contains_path",
                            session_id=memory["session_id"],
                            attrs={"source": "memory", "scope": memory["scope"], "type": memory["type"]},
                            first_seen_at=memory["created_at"],
                            last_seen_at=memory["last_seen_at"],
                            timestamp=timestamp,
                        )
                        edges += int(inserted)
            for exposure in _memory_exposure_rows(conn, str(memory["id"])):
                session_node, _ = _insert_node(
                    conn,
                    kind="Session",
                    label=exposure["session_id"],
                    session_id=exposure["session_id"],
                    timestamp=timestamp,
                )
                _, inserted = writer.edge(
                    conn,
                    source_node_id=session_node,
                    target_node_id=memory_node,
                    kind="recorded_memory",
                    session_id=exposure["session_id"],
                    first_seen_at=exposure["first_seen_at"],
                    last_seen_at=exposure["last_seen_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)

        for memory in conn.execute("SELECT * FROM memories WHERE type = 'cursor_plan' ORDER BY created_at, id"):
            memory_attrs = _load_json_dict(memory["raw_attrs_json"])
            memory_path = str(memory_attrs.get("path") or "").strip()
            memory_repo_id = _infer_repo_id_for_memory(
                conn,
                explicit_repo_id=str(memory["repo_id"] or ""),
                memory_path=memory_path,
                memory_attrs=memory_attrs,
            )
            spec_label = _spec_label_from_plan(memory, memory_attrs)
            spec_attrs = {
                "memory_id": memory["id"],
                "repo_id": memory_repo_id,
                "source": "cursor_plan",
                "scope": memory["scope"],
                "path": memory_attrs.get("path"),
            }
            spec_node, inserted = _insert_node(
                conn,
                kind="Spec",
                label=spec_label,
                session_id=memory["session_id"],
                attrs=spec_attrs,
                first_seen_at=memory["created_at"],
                last_seen_at=memory["last_seen_at"],
                timestamp=timestamp,
            )
            if not inserted:
                _refresh_node(
                    conn,
                    node_id=spec_node,
                    attrs=spec_attrs,
                    first_seen_at=memory["created_at"],
                    last_seen_at=memory["last_seen_at"],
                    timestamp=timestamp,
                )
            nodes += int(inserted)
            path_node_id: str | None = None
            if memory_path:
                path_node, inserted = _insert_node(
                    conn,
                    kind="Path",
                    label=memory_path,
                    session_id=memory["session_id"],
                    attrs={"source": "cursor_plan", "memory_id": memory["id"]},
                    timestamp=timestamp,
                )
                nodes += int(inserted)
                path_node_id = path_node
                _, inserted = writer.edge(
                    conn,
                    source_node_id=spec_node,
                    target_node_id=path_node,
                    kind="described_by_path",
                    session_id=memory["session_id"],
                    attrs={"source": "cursor_plan", "memory_id": memory["id"]},
                    first_seen_at=memory["created_at"],
                    last_seen_at=memory["last_seen_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)
            if memory_repo_id:
                repo = conn.execute("SELECT * FROM repos WHERE id = ?", (memory_repo_id,)).fetchone()
                if repo:
                    repo_node, inserted = _insert_node(
                        conn,
                        kind="Repo",
                        label=repo["full_name"],
                        attrs={
                            "repo_id": repo["id"],
                            "provider": repo["provider"],
                            "branch": repo["branch"],
                            "dirty": bool(repo["dirty"]),
                        },
                        timestamp=timestamp,
                    )
                    nodes += int(inserted)
                    _, inserted = writer.edge(
                        conn,
                        source_node_id=repo_node,
                        target_node_id=spec_node,
                        kind="defines_spec",
                        session_id=memory["session_id"],
                        attrs={"source": "cursor_plan", "memory_id": memory["id"]},
                        first_seen_at=memory["created_at"],
                        last_seen_at=memory["last_seen_at"],
                        timestamp=timestamp,
                    )
                    edges += int(inserted)
                    if path_node_id:
                        _, inserted = writer.edge(
                            conn,
                            source_node_id=repo_node,
                            target_node_id=path_node_id,
                            kind="contains_path",
                            session_id=memory["session_id"],
                            attrs={"source": "cursor_plan", "memory_id": memory["id"]},
                            first_seen_at=memory["created_at"],
                            last_seen_at=memory["last_seen_at"],
                            timestamp=timestamp,
                        )
                        edges += int(inserted)
            for exposure in _memory_exposure_rows(conn, str(memory["id"])):
                session_node, _ = _insert_node(
                    conn,
                    kind="Session",
                    label=exposure["session_id"],
                    session_id=exposure["session_id"],
                    timestamp=timestamp,
                )
                _, inserted = writer.edge(
                    conn,
                    source_node_id=session_node,
                    target_node_id=spec_node,
                    kind="planned_spec",
                    session_id=exposure["session_id"],
                    attrs={"memory_id": memory["id"]},
                    first_seen_at=exposure["first_seen_at"],
                    last_seen_at=exposure["last_seen_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)

        return _finish_graph(conn, nodes, edges, commit=commit)
    finally:
        conn.execute("DROP TABLE IF EXISTS temp.reflect_graph_seen_edges")
        conn.row_factory = previous_row_factory


def refresh_graph(
    conn: sqlite3.Connection,
    session_ids: set[str],
    *,
    changed_step_ids: set[str] | None = None,
    changed_tool_ids: set[str] | None = None,
) -> dict[str, int]:
    """Reuse the graph builder with high-volume canonical tables scoped to changed sessions."""
    if changed_step_ids is not None:
        from reflect.store.graph_delta import try_append_graph
        appended = try_append_graph(conn, session_ids, changed_step_ids, changed_tool_ids)
        if appended is not None:
            return appended
    scoped_ids = sorted(str(session_id) for session_id in session_ids if session_id)
    if not scoped_ids:
        return {"nodes": 0, "edges": 0, "refreshed_sessions": 0}

    placeholders = ", ".join("?" for _ in scoped_ids)
    descendants = conn.execute(
        f"""
        WITH RECURSIVE relationships(parent_id, child_id) AS (
          SELECT parent_session_id, id FROM main.sessions WHERE parent_session_id IS NOT NULL
          UNION
          SELECT parent.session_id, child.session_id FROM main.graph_edges edge
          JOIN main.graph_nodes parent ON parent.id=edge.source_node_id
          JOIN main.graph_nodes child ON child.id=edge.target_node_id
          WHERE edge.kind='spawned_session'
          UNION
          SELECT exposure.session_id, memory.session_id
          FROM main.memory_exposures exposure
          JOIN main.memories memory ON memory.id=exposure.memory_id
          WHERE COALESCE(memory.session_id,'')<>''
          UNION
          SELECT edge.session_id, memory.session_id
          FROM main.graph_edges edge
          JOIN main.graph_nodes memory ON memory.id=edge.target_node_id
          WHERE edge.kind IN ('recorded_memory','planned_spec')
            AND COALESCE(memory.session_id,'')<>''
        ), child_sessions(id) AS (
          SELECT child_id FROM relationships WHERE parent_id IN ({placeholders})
          UNION
          SELECT relationships.child_id FROM relationships
          JOIN child_sessions parent ON relationships.parent_id = parent.id
        )
        SELECT id FROM child_sessions
        """,
        scoped_ids,
    ).fetchall()
    scoped_ids = sorted({*scoped_ids, *(str(row[0]) for row in descendants)})

    previous_temp_cache = int(conn.execute("PRAGMA temp.cache_size").fetchone()[0])
    conn.execute("SAVEPOINT reflect_graph_refresh")
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS reflect_changed_sessions(session_id TEXT PRIMARY KEY)"
    )
    conn.execute("DELETE FROM reflect_changed_sessions")
    conn.executemany(
        "INSERT INTO reflect_changed_sessions(session_id) VALUES (?)",
        ((session_id,) for session_id in scoped_ids),
    )
    scoped_tables = {
        "sessions": "id",
        "steps": "session_id",
        "tool_calls": "session_id",
        "memories": "session_id",
        "evidence": "session_id",
    }
    try:
        if previous_temp_cache == 0:
            conn.execute("PRAGMA temp.cache_size = -32768")
        _create_graph_stage(conn)
        _preserve_execution_graph(conn, scoped_ids)
        conn.execute("""CREATE TEMP TABLE reflect_graph_shared_candidates AS
            SELECT node.id FROM main.graph_nodes node
            WHERE node.session_id IS NULL AND EXISTS (
                SELECT 1 FROM main.graph_edges edge
                WHERE (edge.source_node_id=node.id OR edge.target_node_id=node.id)
                  AND edge.session_id IN (SELECT session_id FROM reflect_changed_sessions)
            )""")
        # Preserve existing first-writer metadata for shared nodes still owned
        # by other histories, plus complete parent/exposure Session references.
        conn.execute("""
            INSERT OR IGNORE INTO temp.graph_nodes SELECT node.* FROM main.graph_nodes node
            WHERE (node.session_id IS NULL AND (
                node.kind NOT IN ('Workspace','Folder','Path') OR EXISTS (
                    SELECT 1 FROM main.graph_edges edge
                    WHERE (edge.source_node_id=node.id OR edge.target_node_id=node.id)
                      AND (edge.session_id IS NULL OR edge.session_id NOT IN
                          (SELECT session_id FROM reflect_changed_sessions))
                )
            )) OR (node.kind='Session' AND node.session_id NOT IN
                (SELECT session_id FROM reflect_changed_sessions))
        """)
        for table, session_column in scoped_tables.items():
            # Ownerless context artifacts can be exposed to any selected
            # session. Session-owned artifacts are covered by scope closure.
            ownerless = f"OR COALESCE(source.{session_column},'')=''" if table == "memories" else ""
            conn.execute(
                f"""
                CREATE TEMP VIEW {table} AS
                SELECT source.*
                FROM main.{table} source
                WHERE source.{session_column} IN
                    (SELECT session_id FROM reflect_changed_sessions) {ownerless}
                """
            )
        conn.execute(
            """
            CREATE TEMP VIEW mcp_calls AS
            SELECT
              source.*,
              source.tool_call_id AS id,
              tc.session_id,
              tc.status,
              tc.duration_ms
            FROM main.mcp_calls AS source
            JOIN main.tool_calls AS tc ON tc.id = source.tool_call_id
            JOIN reflect_changed_sessions AS changed
              ON changed.session_id = tc.session_id
            """
        )
        result = _derive_graph(conn, commit=False)
        conn.execute("""DELETE FROM main.graph_edges
            WHERE (session_id IN (SELECT session_id FROM reflect_changed_sessions)
              OR source_node_id IN (SELECT id FROM main.graph_nodes WHERE session_id IN
                    (SELECT session_id FROM reflect_changed_sessions))
              OR target_node_id IN (SELECT id FROM main.graph_nodes WHERE session_id IN
                    (SELECT session_id FROM reflect_changed_sessions)))
              AND id NOT IN (SELECT id FROM temp.graph_edges)""")
        conn.execute("""DELETE FROM main.graph_nodes
            WHERE session_id IN (SELECT session_id FROM reflect_changed_sessions)
              AND id NOT IN (SELECT id FROM temp.graph_nodes)""")
        _copy_changed_graph_rows(conn, "temp.graph_nodes", "main.graph_nodes", edges=False)
        _copy_changed_graph_rows(conn, "temp.graph_edges", "main.graph_edges", edges=True)
        copy_graph_sources(conn, scoped=True)
        conn.execute("""DELETE FROM main.graph_nodes
            WHERE session_id IS NULL AND kind IN ('Workspace','Folder','Path','Tool','Agent','MCPServer','Skill')
              AND id IN (SELECT id FROM reflect_graph_shared_candidates)
              AND NOT EXISTS (SELECT 1 FROM main.graph_edges
                WHERE source_node_id=graph_nodes.id OR target_node_id=graph_nodes.id)""")
        _drop_graph_stage(conn)
        conn.execute("RELEASE SAVEPOINT reflect_graph_refresh")
        conn.commit()
        return {
            **result,
            "refreshed_sessions": len(scoped_ids),
        }
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT reflect_graph_refresh")
        conn.execute("RELEASE SAVEPOINT reflect_graph_refresh")
        raise
    finally:
        if previous_temp_cache == 0:
            conn.execute("PRAGMA temp.cache_size = 0")
        for table in reversed(scoped_tables):
            conn.execute(f"DROP VIEW IF EXISTS temp.{table}")
        conn.execute("DROP VIEW IF EXISTS temp.mcp_calls")
        conn.execute("DROP TABLE IF EXISTS temp.reflect_graph_shared_candidates")
        conn.execute("DROP TABLE IF EXISTS reflect_changed_sessions")
