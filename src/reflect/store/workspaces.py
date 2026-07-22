from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

WORKSPACE_ATTRIBUTE_KEYS = (
    "code.workspace.root",
    "gen_ai.client.cwd",
    "gen_ai.client.workspace_path",
    "gen_ai.client.workspace",
    "gen_ai.client.repository_root",
    "workspace_path",
    "workspace",
    "cwd",
)
PARENT_SESSION_ATTRIBUTE_KEYS = (
    "gen_ai.client.parent_session_id",
    "parent_session_id",
    "gen_ai.parent.session.id",
)

_SOURCE_SCORES = {
    "code.workspace.root": 360,
    "gen_ai.client.cwd": 340,
    "cwd": 320,
    "gen_ai.client.workspace_path": 300,
    "workspace_path": 280,
    "gen_ai.client.workspace": 240,
    "gen_ai.client.repository_root": 350,
    "workspace": 220,
}
_AGENT_STATE_DIRS = {
    ".agents",
    ".claude",
    ".codex",
    ".copilot",
    ".cursor",
    ".gemini",
    ".local",
    ".opencode",
    ".pi",
}
_CURSOR_PARENT_RE = re.compile(r"/agent-transcripts/([^/]+)/")


def _stable_id(prefix: str, *parts: object) -> str:
    payload = ":".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha1(payload.encode()).hexdigest()}"


def _path_hash(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def _normalized_absolute_path(value: object) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip().strip("'\"")
    if not candidate or "\x00" in candidate or "\n" in candidate or "://" in candidate:
        return ""
    candidate = os.path.expanduser(candidate)
    if not os.path.isabs(candidate):
        return ""
    return os.path.normpath(candidate)


def _is_agent_state_path(path: str) -> bool:
    try:
        relative = Path(path).relative_to(Path.home())
    except ValueError:
        return False
    return bool(relative.parts and relative.parts[0] in _AGENT_STATE_DIRS)


def _is_generic_workspace_root(path: str) -> bool:
    return path in {os.path.sep, os.path.normpath(str(Path.home()))}


@dataclass(frozen=True)
class WorkspaceIdentity:
    root_path: str
    label: str
    source_key: str
    confidence: float
    repo_root: str = ""
    repo_owner: str = ""
    repo_name: str = ""
    branch: str = ""
    remote_hash: str = ""

    @property
    def workspace_id(self) -> str:
        return _stable_id("workspace", self.root_path)

    @property
    def repo_id(self) -> str:
        return _stable_id("repo", "local", self.repo_root) if self.repo_root else ""


class WorkspaceResolver:
    """Resolve vendor-neutral workspace identity from optional telemetry attributes."""

    def __init__(self) -> None:
        self._repo_cache: dict[str, str] = {}

    def resolve(self, attrs: dict[str, Any]) -> WorkspaceIdentity | None:
        identity = self.resolve_candidates(
            (key, attrs.get(key)) for key in WORKSPACE_ATTRIBUTE_KEYS if attrs.get(key)
        )
        return self.enrich(identity, attrs)

    @staticmethod
    def enrich(
        identity: WorkspaceIdentity | None,
        attrs: dict[str, Any],
    ) -> WorkspaceIdentity | None:
        if identity is None:
            return None
        return replace(
            identity,
            repo_owner=str(attrs.get("vcs.repository.owner") or ""),
            repo_name=str(attrs.get("vcs.repository.name") or ""),
            branch=str(attrs.get("vcs.ref.head.name") or ""),
            remote_hash=str(attrs.get("gen_ai.client.repository.remote.sha256") or ""),
        )

    def resolve_candidates(
        self,
        candidates: Iterable[tuple[str, object]],
    ) -> WorkspaceIdentity | None:
        counts: dict[str, Counter[str]] = defaultdict(Counter)
        for source_key, value in candidates:
            normalized = _normalized_absolute_path(value)
            if normalized:
                counts[normalized][source_key] += 1
        if not counts:
            return None

        resolved: list[tuple[int, str, str, int, str]] = []
        for path, source_counts in counts.items():
            repo_root = self._discover_repo_root(path)
            if "gen_ai.client.repository_root" in source_counts:
                repo_root = path
            if (_is_agent_state_path(path) or _is_generic_workspace_root(path)) and not repo_root:
                continue
            source_key, occurrences = max(
                source_counts.items(),
                key=lambda item: (_SOURCE_SCORES.get(item[0], 0), item[1], item[0]),
            )
            selected_root = repo_root or path
            depth = len(Path(selected_root).parts)
            score = (
                (10_000 if repo_root else 0)
                + _SOURCE_SCORES.get(source_key, 0)
                + min(occurrences, 100) * 3
                + depth
            )
            resolved.append((score, selected_root, source_key, occurrences, repo_root))
        if not resolved:
            return None

        _, root_path, source_key, occurrences, repo_root = max(resolved)
        confidence = 0.98 if repo_root else min(0.94, 0.66 + min(occurrences, 28) / 100)
        return WorkspaceIdentity(
            root_path=root_path,
            label=Path(root_path).name or root_path,
            source_key=source_key,
            confidence=confidence,
            repo_root=repo_root,
        )

    def _discover_repo_root(self, path: str) -> str:
        cached = self._repo_cache.get(path)
        if cached is not None:
            return cached
        candidate = Path(path)
        if candidate.is_file():
            candidate = candidate.parent
        repo_root = ""
        for current in (candidate, *candidate.parents):
            if (current / ".git").exists():
                repo_root = os.path.normpath(str(current))
                break
            if current == Path.home():
                break
        self._repo_cache[path] = repo_root
        return repo_root


class SessionLineageResolver:
    """Resolve parent-session identity without coupling to one agent adapter."""

    def resolve(self, attrs: dict[str, Any], source_ref: str = "") -> str:
        for key in PARENT_SESSION_ATTRIBUTE_KEYS:
            value = str(attrs.get(key) or "").strip()
            if value:
                return value
        match = _CURSOR_PARENT_RE.search(source_ref or "")
        return match.group(1) if match else ""


class WorkspaceStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, identity: WorkspaceIdentity, timestamp: str) -> tuple[str, str]:
        repo_id = self._upsert_repo(identity, timestamp) if identity.repo_root else ""
        self.conn.execute(
            """
            INSERT INTO workspaces(
              id, root_path, path_hash, label, repo_id, source_key, confidence,
              raw_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path_hash) DO UPDATE SET
              root_path = excluded.root_path,
              label = excluded.label,
              repo_id = COALESCE(excluded.repo_id, workspaces.repo_id),
              source_key = excluded.source_key,
              confidence = MAX(workspaces.confidence, excluded.confidence),
              raw_json = excluded.raw_json,
              updated_at = excluded.updated_at
            """,
            (
                identity.workspace_id,
                identity.root_path,
                _path_hash(identity.root_path),
                identity.label,
                repo_id or None,
                identity.source_key,
                identity.confidence,
                json.dumps({"root_path": identity.root_path}, sort_keys=True),
                timestamp,
                timestamp,
            ),
        )
        row = self.conn.execute(
            "SELECT id, COALESCE(repo_id, '') FROM workspaces WHERE path_hash = ?",
            (_path_hash(identity.root_path),),
        ).fetchone()
        return (str(row[0]), str(row[1] or ""))

    def _upsert_repo(self, identity: WorkspaceIdentity, timestamp: str) -> str:
        existing = self.conn.execute(
            "SELECT id FROM repos WHERE full_name = ?",
            (identity.repo_root,),
        ).fetchone()
        repo_id = str(existing[0]) if existing else identity.repo_id
        self.conn.execute(
            """
            INSERT INTO repos(
              id, provider, owner, name, full_name, branch, path_hash,
              raw_json, created_at, updated_at
            ) VALUES (?, 'local', NULLIF(?, ''), ?, ?, NULLIF(?, ''), ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              owner = COALESCE(excluded.owner, repos.owner),
              name = COALESCE(NULLIF(excluded.name, ''), repos.name),
              branch = COALESCE(excluded.branch, repos.branch),
              path_hash = excluded.path_hash,
              raw_json = excluded.raw_json,
              updated_at = excluded.updated_at
            """,
            (
                repo_id,
                identity.repo_owner,
                identity.repo_name or Path(identity.repo_root).name or identity.repo_root,
                identity.repo_root,
                identity.branch,
                _path_hash(identity.repo_root),
                json.dumps(
                    {
                        "root_path": identity.repo_root,
                        "remote_sha256": identity.remote_hash,
                    },
                    sort_keys=True,
                ),
                timestamp,
                timestamp,
            ),
        )
        return repo_id


def backfill_session_context(
    conn: sqlite3.Connection,
    *,
    timestamp: str,
    changed_session_ids: set[str] | None = None,
    session_ids: set[str] | None = None,
    resolver: WorkspaceResolver | None = None,
) -> dict[str, int]:
    """Promote stored workspace and lineage attributes into canonical sessions."""

    resolver = resolver or WorkspaceResolver()
    lineage = SessionLineageResolver()
    store = WorkspaceStore(conn)
    invalid_sessions: set[str] = set()
    invalid_workspace_rows = conn.execute(
        "SELECT id, root_path FROM workspaces WHERE repo_id IS NULL"
    ).fetchall()
    invalid_workspace_ids = {
        str(row[0])
        for row in invalid_workspace_rows
        if _is_generic_workspace_root(str(row[1] or ""))
    }
    if invalid_workspace_ids:
        invalid_placeholders = ", ".join("?" for _ in invalid_workspace_ids)
        invalid_sessions = {
            str(row[0])
            for row in conn.execute(
                f"SELECT id FROM sessions WHERE workspace_id IN ({invalid_placeholders})",
                sorted(invalid_workspace_ids),
            )
        }
        conn.execute(
            f"""
            UPDATE sessions
            SET workspace_id = NULL, repo_id = NULL, updated_at = ?
            WHERE workspace_id IN ({invalid_placeholders})
            """,
            (timestamp, *sorted(invalid_workspace_ids)),
        )
        conn.execute(
            f"DELETE FROM workspaces WHERE id IN ({invalid_placeholders})",
            sorted(invalid_workspace_ids),
        )
        if changed_session_ids is not None:
            changed_session_ids.update(invalid_sessions)
    filters: list[str] = []
    params: list[str] = []
    if session_ids is not None:
        if not session_ids:
            return {"sessions_updated": 0, "workspaces": 0, "parents": 0}
        placeholders = ", ".join("?" for _ in session_ids)
        filters.append(f"id IN ({placeholders})")
        params.extend(sorted(session_ids))
    else:
        filters.append(
            "(workspace_id IS NULL OR "
            "(parent_session_id IS NULL AND source_ref LIKE '%/agent-transcripts/%/subagents/%'))"
        )
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    sessions = conn.execute(
        f"""
        SELECT id, source_ref, workspace_id, repo_id, parent_session_id
        FROM sessions
        {where}
        ORDER BY id
        """,
        params,
    ).fetchall()
    known_session_ids = {str(row[0]) for row in conn.execute("SELECT id FROM sessions")}
    updated_session_ids = set(invalid_sessions)
    workspace_updates = 0
    parent_updates = 0
    touched_workspaces: set[str] = set()
    for session in sessions:
        session_id = str(session[0])
        candidates: list[tuple[str, object]] = []
        parent_candidates: list[dict[str, Any]] = []
        repository_attrs: dict[str, Any] = {}
        for step in conn.execute(
            "SELECT raw_attrs_json FROM steps WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ):
            try:
                attrs = json.loads(str(step[0] or "{}"))
            except json.JSONDecodeError:
                continue
            if not isinstance(attrs, dict):
                continue
            for key in WORKSPACE_ATTRIBUTE_KEYS:
                if attrs.get(key):
                    candidates.append((key, attrs[key]))
            if any(attrs.get(key) for key in PARENT_SESSION_ATTRIBUTE_KEYS):
                parent_candidates.append(attrs)
            for key in (
                "vcs.repository.owner",
                "vcs.repository.name",
                "vcs.ref.head.name",
                "gen_ai.client.repository.remote.sha256",
            ):
                if attrs.get(key) and not repository_attrs.get(key):
                    repository_attrs[key] = attrs[key]

        identity = resolver.resolve_candidates(candidates)
        identity = resolver.enrich(identity, repository_attrs)
        workspace_id = str(session[2] or "")
        repo_id = str(session[3] or "")
        if identity is not None:
            workspace_id, resolved_repo_id = store.upsert(identity, timestamp)
            repo_id = resolved_repo_id or repo_id
            touched_workspaces.add(workspace_id)

        parent_id = str(session[4] or "")
        for attrs in parent_candidates:
            parent_id = lineage.resolve(attrs, str(session[1] or "")) or parent_id
            if parent_id:
                break
        if not parent_id:
            parent_id = lineage.resolve({}, str(session[1] or ""))
        if parent_id not in known_session_ids or parent_id == session_id:
            parent_id = str(session[4] or "")

        changed = (
            workspace_id != str(session[2] or "")
            or repo_id != str(session[3] or "")
            or parent_id != str(session[4] or "")
        )
        if not changed:
            continue
        conn.execute(
            """
            UPDATE sessions
            SET workspace_id = NULLIF(?, ''),
                repo_id = NULLIF(?, ''),
                parent_session_id = NULLIF(?, ''),
                updated_at = ?
            WHERE id = ?
            """,
            (workspace_id, repo_id, parent_id, timestamp, session_id),
        )
        updated_session_ids.add(session_id)
        workspace_updates += int(workspace_id != str(session[2] or ""))
        parent_updates += int(parent_id != str(session[4] or ""))
        if changed_session_ids is not None:
            changed_session_ids.add(session_id)
    return {
        "sessions_updated": len(updated_session_ids),
        "workspaces": len(touched_workspaces),
        "parents": parent_updates,
        "workspace_updates": workspace_updates,
    }
