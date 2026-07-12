from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import PurePosixPath

from reflect.views.report_tabs import (
    _attr,
    _extract_skill_name_from_path,
    _extract_skill_name_from_preview,
    _extract_skill_names_from_text,
    _extract_subagent_name_from_tool,
    _extract_subagent_names_from_text,
)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _stable_id(prefix: str, *parts: object) -> str:
    payload = ":".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha1(payload.encode()).hexdigest()}"


def _load_json_dict(value: object) -> dict:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


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


def _path_candidates(*values: object) -> list[str]:
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
        if any(char in cleaned for char in "{}[]"):
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
        if isinstance(value, str):
            for match in re.finditer(r"(?<![A-Za-z0-9_./-])((?:\.{0,2}/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)", value):
                add(match.group(1))
            for match in re.finditer(r"(?<![A-Za-z0-9_./-])([A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8})(?![A-Za-z0-9_./-])", value):
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
    if len(folders) > 6:
        folders = [folders[0], *folders[-5:]]
    return folders


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
            }
        )

    if re.search(r"\b(gh\s+pr\s+create|create_pull_request|pull request|opened pr|pr #\d+)\b", haystack):
        add("pr_opened", 0.75, text)
    if re.search(r"\b(git\s+commit|committed|commit made)\b", haystack):
        add("commit_made", 0.75, text)
    if re.search(r"\b(pytest|npm test|cargo test|go test|test session starts|tests? passed|\d+\s+passed)\b", haystack):
        add("tests_passed" if status != "error" and "failed" not in haystack else "tests_failed", 0.7, text)
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
        """,
        (
            json.dumps(attrs or {}, sort_keys=True),
            first_seen_at,
            last_seen_at,
            timestamp,
            node_id,
        ),
    )


def rebuild_graph(conn: sqlite3.Connection) -> dict[str, int]:
    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        timestamp = _now()
        nodes = 0
        edges = 0

        conn.execute(
            """
            DELETE FROM graph_edges
            WHERE source_node_id IN (SELECT id FROM graph_nodes WHERE kind = 'Folder' AND (label LIKE '{%' OR label LIKE '[%'))
               OR target_node_id IN (SELECT id FROM graph_nodes WHERE kind = 'Folder' AND (label LIKE '{%' OR label LIKE '[%'))
            """
        )
        conn.execute("DELETE FROM graph_nodes WHERE kind = 'Folder' AND (label LIKE '{%' OR label LIKE '[%')")

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
                    _, inserted = _insert_edge(
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

            for step in conn.execute("SELECT * FROM steps WHERE session_id = ? ORDER BY seq", (session["id"],)):
                step_attrs = _load_json_dict(step["raw_attrs_json"])
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

                prompt_text = str(_attr(step_attrs, "gen_ai.client.prompt", "gen_ai.client.prompt.text", "prompt") or "")
                file_path = str(
                    _attr(
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
                skill_names = set(_extract_skill_names_from_text(prompt_text))
                path_skill = _extract_skill_name_from_path(file_path)
                if path_skill:
                    skill_names.add(path_skill)
                for skill_name in sorted(skill_names):
                    skill_node, inserted = _insert_node(
                        conn,
                        kind="Skill",
                        label=skill_name,
                        attrs={"source": "step_text_or_path", "confidence": "medium"},
                        timestamp=timestamp,
                    )
                    nodes += int(inserted)
                    _, inserted = _insert_edge(
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

                event = str(_attr(step_attrs, "gen_ai.client.hook.event", "ide.hook.event") or step["summary"] or "")
                event_lc = event.lower()
                subagent_type = str(_attr(step_attrs, "gen_ai.client.subagent_type", "ide.subagent_type", "subagent.type") or "")
                is_subagent_event = subagent_type or "subagent" in event_lc
                if is_subagent_event:
                    subagent_name = subagent_type or "unknown"
                    subagent_node, inserted = _insert_node(
                        conn,
                        kind="Subagent",
                        label=subagent_name,
                        session_id=session["id"],
                        attrs={"source": "lifecycle", "event": event},
                        timestamp=timestamp,
                    )
                    nodes += int(inserted)
                    _, inserted = _insert_edge(
                        conn,
                        source_node_id=session_node,
                        target_node_id=subagent_node,
                        kind="spawned_subagent" if "stop" not in event_lc else "stopped_subagent",
                        session_id=session["id"],
                        attrs={"source": "step", "step_id": step["id"]},
                        first_seen_at=step["started_at"],
                        last_seen_at=step["ended_at"],
                        timestamp=timestamp,
                    )
                    edges += int(inserted)

                for subagent_name in sorted(_extract_subagent_names_from_text(prompt_text)):
                    subagent_node, inserted = _insert_node(
                        conn,
                        kind="Subagent",
                        label=subagent_name,
                        session_id=session["id"],
                        attrs={"source": "prompt_text", "confidence": "medium"},
                        timestamp=timestamp,
                    )
                    nodes += int(inserted)
                    _, inserted = _insert_edge(
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

            tool_attrs = _load_json_dict(tool["raw_attrs_json"])
            preview = tool["input_preview_redacted"] or ""
            output_preview = tool["output_preview_redacted"] or ""
            call_node, inserted = _insert_node(
                conn,
                kind="ToolCall",
                label=f"{tool['tool_name']}:{_short_id(tool['id'])}",
                session_id=tool["session_id"],
                attrs={
                    "tool_call_id": tool["id"],
                    "step_id": tool["step_id"],
                    "tool_name": tool["tool_name"],
                    "tool_type": tool["tool_type"],
                    "status": tool["status"],
                    "duration_ms": tool["duration_ms"],
                    "input_hash": tool["input_hash"],
                    "output_hash": tool["output_hash"],
                    "error_type": tool["error_type"],
                    "error_message": tool["error_message_redacted"],
                },
                first_seen_at=tool["created_at"],
                last_seen_at=tool["updated_at"],
                timestamp=timestamp,
            )
            nodes += int(inserted)
            _, inserted = _insert_edge(
                conn,
                source_node_id=session_node,
                target_node_id=call_node,
                kind="executed_tool_call",
                session_id=tool["session_id"],
                attrs={"status": tool["status"]},
                first_seen_at=tool["created_at"],
                last_seen_at=tool["updated_at"],
                timestamp=timestamp,
            )
            edges += int(inserted)
            _, inserted = _insert_edge(
                conn,
                source_node_id=call_node,
                target_node_id=tool_node,
                kind="instance_of_tool",
                session_id=tool["session_id"],
                attrs={"status": tool["status"]},
                first_seen_at=tool["created_at"],
                last_seen_at=tool["updated_at"],
                timestamp=timestamp,
            )
            edges += int(inserted)

            skill_names = set(_extract_skill_names_from_text(preview))
            if str(tool["tool_name"]).lower() == "skill":
                skill_name = _extract_skill_name_from_preview(preview)
                if skill_name:
                    skill_names.add(skill_name)
            for path in _path_candidates(
                _attr(
                    tool_attrs,
                    "gen_ai.client.file_path",
                    "gen_ai.client.tool.input.file_path",
                    "gen_ai.client.tool.input.path",
                    "tool.input.file_path",
                    "tool.input.path",
                    "file.path",
                    "path",
                ),
                preview,
            ):
                path_skill = _extract_skill_name_from_path(path)
                if path_skill:
                    skill_names.add(path_skill)
                path_node, inserted = _insert_node(
                    conn,
                    kind="Path",
                    label=path,
                    session_id=tool["session_id"],
                    attrs={"source": "tool_call", "tool_name": tool["tool_name"]},
                    timestamp=timestamp,
                )
                nodes += int(inserted)
                _, inserted = _insert_edge(
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
                for folder in _folder_candidates(path):
                    folder_node, inserted = _insert_node(
                        conn,
                        kind="Folder",
                        label=folder,
                        session_id=tool["session_id"],
                        attrs={"source": "tool_call_path", "tool_name": tool["tool_name"]},
                        timestamp=timestamp,
                    )
                    nodes += int(inserted)
                    _, inserted = _insert_edge(
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
                    _, inserted = _insert_edge(
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
                    _, inserted = _insert_edge(
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
                skill_node, inserted = _insert_node(
                    conn,
                    kind="Skill",
                    label=skill_name,
                    attrs={"source": "tool_call", "confidence": "medium"},
                    timestamp=timestamp,
                )
                nodes += int(inserted)
                _, inserted = _insert_edge(
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
                _, inserted = _insert_edge(
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

            subagent_name = _extract_subagent_name_from_tool(str(tool["tool_name"]), tool_attrs, preview)
            if subagent_name:
                subagent_node, inserted = _insert_node(
                    conn,
                    kind="Subagent",
                    label=subagent_name,
                    session_id=tool["session_id"],
                    attrs={"source": "tool_call", "status": tool["status"]},
                    timestamp=timestamp,
                )
                nodes += int(inserted)
                _, inserted = _insert_edge(
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
                _, inserted = _insert_edge(
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
                _, inserted = _insert_edge(
                    conn,
                    source_node_id=session_node,
                    target_node_id=outcome_node,
                    kind="achieved_outcome",
                    session_id=tool["session_id"],
                    attrs={"tool_call_id": tool["id"], "confidence": outcome["confidence"]},
                    first_seen_at=tool["created_at"],
                    last_seen_at=tool["updated_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)
                _, inserted = _insert_edge(
                    conn,
                    source_node_id=call_node,
                    target_node_id=outcome_node,
                    kind="produced_outcome",
                    session_id=tool["session_id"],
                    attrs={"confidence": outcome["confidence"]},
                    first_seen_at=tool["created_at"],
                    last_seen_at=tool["updated_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)

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
                _, inserted = _insert_edge(
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
                    _, inserted = _insert_edge(
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
                _, inserted = _insert_edge(
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
                _, inserted = _insert_edge(
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
                _, inserted = _insert_edge(
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
                    _, inserted = _insert_edge(
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
                        _, inserted = _insert_edge(
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
                _, inserted = _insert_edge(
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
                    _, inserted = _insert_edge(
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
                        _, inserted = _insert_edge(
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
                    target_node_id=spec_node,
                    kind="planned_spec",
                    session_id=memory["session_id"],
                    attrs={"memory_id": memory["id"]},
                    first_seen_at=memory["created_at"],
                    last_seen_at=memory["last_seen_at"],
                    timestamp=timestamp,
                )
                edges += int(inserted)

        conn.commit()
        return {"nodes": nodes, "edges": edges}
    finally:
        conn.row_factory = previous_row_factory


def refresh_graph(
    conn: sqlite3.Connection,
    session_ids: set[str],
) -> dict[str, int]:
    """Reuse the graph builder with high-volume canonical tables scoped to changed sessions."""
    scoped_ids = sorted(str(session_id) for session_id in session_ids if session_id)
    if not scoped_ids:
        return {"nodes": 0, "edges": 0, "refreshed_sessions": 0}

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
        "mcp_calls": "session_id",
        "memories": "session_id",
        "evidence": "session_id",
    }
    try:
        for table, session_column in scoped_tables.items():
            conn.execute(
                f"""
                CREATE TEMP VIEW {table} AS
                SELECT source.*
                FROM main.{table} source
                JOIN reflect_changed_sessions changed
                  ON changed.session_id = source.{session_column}
                """
            )
        result = rebuild_graph(conn)
        return {
            **result,
            "refreshed_sessions": len(scoped_ids),
        }
    finally:
        for table in reversed(scoped_tables):
            conn.execute(f"DROP VIEW IF EXISTS temp.{table}")
        conn.execute("DROP TABLE IF EXISTS reflect_changed_sessions")
