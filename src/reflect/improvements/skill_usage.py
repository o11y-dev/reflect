"""Source-owned skill observations; registry identity is never guessed from a name."""
from __future__ import annotations

import hashlib
import json
import shlex
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath

from reflect.telemetry_facts import extract_skill_names_from_text, first_attr
from reflect.utils import _json_loads


def _dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = _json_loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


@dataclass(frozen=True)
class SkillOccurrence:
    skill_id: str
    version_id: str | None
    relationship: str
    state: str
    path: str | None


class SkillUsageProjector:
    """Resolve captured identities using one refresh-local registry inventory."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.skills = {str(row[0]): str(row[1]) for row in conn.execute("SELECT id,slug FROM skills")}
        self.names: dict[str, set[str]] = defaultdict(set)
        for skill_id, slug in self.skills.items():
            self.names[slug].add(skill_id)
        self.paths: dict[str, set[str]] = defaultdict(set)
        for skill_id, path in conn.execute("SELECT skill_id,path FROM skill_installations"):
            self.paths[str(PurePosixPath(str(path)))].add(str(skill_id))
        self.versions = {str(row[0]): (str(row[1]), str(row[2])) for row in conn.execute(
            "SELECT id,skill_id,content_hash FROM skill_versions"
        )}

    def occurrences(self, attrs: dict, tool_name: str, status: str, workspace: str) -> list[SkillOccurrence]:
        payload = _dict(first_attr(attrs, "gen_ai.client.tool.input", "tool.input"))
        name = str(tool_name or first_attr(attrs, "gen_ai.client.tool_name", "tool.name") or "").lower().rsplit("__", 1)[-1]
        event = str(first_attr(attrs, "gen_ai.client.hook.event", "ide.hook.event") or "").lower().rsplit(".", 1)[-1]
        if not name and event == "beforereadfile":
            name, status = "read", "unknown"
        command = first_attr(attrs, "gen_ai.client.command", "command", "shell.command") or payload.get("cmd") or payload.get("command")
        if name in {"exec_command", "bash", "shell", "exec"} and isinstance(command, str):
            # A single read invocation is evidence; a filename mentioned in an
            # arbitrary script, search, echo, or composite command is not.
            if len(command) > 32_768 or any(char in command for char in ";&|<>\n`") or "$(" in command:
                return []
            try:
                tokens = shlex.split(command)
            except ValueError:
                return []
            if not tokens or PurePosixPath(tokens[0]).name not in {"cat", "head", "tail", "sed", "bat"}:
                return []
            from reflect.execution_assessment import explicit_exit
            code = explicit_exit({"raw_attrs_json": attrs})
            result = []
            for candidate in tokens[1:]:
                if candidate.endswith("/SKILL.md"):
                    read_attrs = {**attrs, "gen_ai.client.file_path": candidate}
                    result.extend(self.occurrences(read_attrs, "read", "ok" if code == 0 else "unknown", workspace))
            return result
        explicit_id = str(first_attr(attrs, "reflect.skill.id", "skill.id") or payload.get("skill_id") or "")
        path = str(first_attr(attrs, "gen_ai.client.file_path", "gen_ai.client.tool.input.file_path", "gen_ai.client.tool.input.path", "tool.input.file_path", "tool.input.path", "file.path", "path")
                   or payload.get("file_path") or payload.get("path") or "")
        if path and not path.startswith("/") and workspace:
            path = str(PurePosixPath(workspace) / path)
        path = str(PurePosixPath(path)) if path else ""
        version = str(first_attr(attrs, "reflect.skill.version_id", "skill.version_id") or payload.get("version_id") or "")
        if explicit_id:
            # An unknown captured identity cannot be reassigned by its name or
            # by a path that happens to be installed now.
            candidates = {explicit_id} if explicit_id in self.skills else set()
        elif path:
            candidates = self.paths.get(path, set())
        elif version in self.versions:
            candidates = {self.versions[version][0]}
        else:
            candidates = set()
        # A package name is an inferred mention, never a resolved invocation.
        names = extract_skill_names_from_text(first_attr(attrs, "gen_ai.client.prompt", "gen_ai.client.prompt.text", "prompt"))
        named = payload.get("skill") or first_attr(attrs, "gen_ai.client.skill.name", "skill.name")
        if isinstance(named, str):
            names.add(named)
        if len(candidates) != 1:
            if explicit_id or path or version:
                return []
            unambiguous = {next(iter(self.names[slug])) for slug in names if len(self.names[slug]) == 1}
            return [SkillOccurrence(skill_id, None, "mentioned", "inferred", None)
                    for skill_id in sorted(unambiguous)]
        skill_id = next(iter(candidates))
        captured_hash = str(first_attr(attrs, "reflect.skill.content_hash", "skill.content_hash", "file.content_hash") or "")
        version_id = version if self.versions.get(version, (None,))[0] == skill_id else None
        if not version_id and captured_hash:
            matches = [key for key, (owner, digest) in self.versions.items() if owner == skill_id and digest == captured_hash]
            version_id = matches[0] if len(matches) == 1 else None
        if name in {"skill", "invoke_skill", "use_skill"}:
            relationship = "invoked"
        elif path and "read" in name and status == "ok":
            relationship = "loaded"
        elif path and "read" in name:
            relationship = "load_attempted"
        else:
            relationship = "mentioned"
        return [SkillOccurrence(skill_id, version_id, relationship,
                                "inferred" if relationship == "mentioned" else "observed", path or None)]

    def refresh(self, *, session_ids: set[str] | None, step_ids: set[str] | None, now: str) -> int:
        if session_ids == set() or step_ids == set():
            return 0
        clauses, params = [], []
        if session_ids is not None:
            clauses.append(f"st.session_id IN ({','.join('?' for _ in session_ids)})")
            params.extend(sorted(session_ids))
        if step_ids is not None:
            clauses.append(f"st.id IN ({','.join('?' for _ in step_ids)})")
            params.extend(sorted(step_ids))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.conn.execute(f"""
            SELECT st.id, st.session_id, COALESCE(st.started_at,st.created_at), st.raw_attrs_json,
                   tc.id, tc.tool_name, tc.status, tc.raw_attrs_json, tc.input_preview_redacted,
                   w.root_path, eus.execution_unit_id
            FROM steps st
            JOIN sessions s ON s.id=st.session_id
            LEFT JOIN workspaces w ON w.id=s.workspace_id
            LEFT JOIN tool_calls tc ON tc.step_id=st.id AND tc.session_id=st.session_id
            LEFT JOIN execution_unit_steps eus ON eus.step_id=st.id AND eus.session_id=st.session_id
            {where} ORDER BY st.id,tc.id
        """, params)
        seen: set[str] = set()
        count = 0
        for step_id, session_id, at, raw, tool_id, name, status, tool_raw, preview, workspace, execution_id in rows:
            attrs = {**_dict(raw), **_dict(tool_raw)}
            if preview and not attrs.get("gen_ai.client.tool.input"):
                attrs["gen_ai.client.tool.input"] = preview
            for occurrence in self.occurrences(attrs, name or "", status or "unknown", workspace or ""):
                source_id = f"{step_id}:{tool_id or 'step'}:{occurrence.relationship}"
                identity = f"{occurrence.skill_id}:canonical_step:{source_id}"
                usage_id = "skill_usage_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
                if usage_id in seen:
                    continue
                seen.add(usage_id)
                evidence = json.dumps({"source": "canonical_step", "step_id": step_id,
                                      "tool_call_id": tool_id, "relationship": occurrence.relationship,
                                      "path": occurrence.path, "version_attribution": "captured" if occurrence.version_id else "unknown",
                                      "execution_attribution": "step_membership" if execution_id else "unknown"}, sort_keys=True)
                values = (occurrence.version_id, session_id, execution_id, occurrence.state, evidence, at)
                self.conn.execute("""INSERT INTO skill_usage(
                    id,skill_id,source_kind,source_id,skill_version_id,session_id,execution_unit_id,
                    state,evidence_json,observed_at,confidence,created_at,updated_at
                ) VALUES (?,?,'canonical_step',?,?,?,?,?,?,?, ?,?,?)
                ON CONFLICT(skill_id,source_kind,source_id) DO UPDATE SET
                    skill_version_id=excluded.skill_version_id,session_id=excluded.session_id,
                    execution_unit_id=excluded.execution_unit_id,state=excluded.state,
                    evidence_json=excluded.evidence_json,observed_at=excluded.observed_at,
                    confidence=excluded.confidence,updated_at=excluded.updated_at
                WHERE (skill_usage.skill_version_id,skill_usage.session_id,skill_usage.execution_unit_id,
                       skill_usage.state,skill_usage.evidence_json,skill_usage.observed_at) IS NOT
                      (excluded.skill_version_id,excluded.session_id,excluded.execution_unit_id,
                       excluded.state,excluded.evidence_json,excluded.observed_at)
                """, (usage_id, occurrence.skill_id, source_id, *values,
                      0.5 if occurrence.state == "inferred" else 1.0, now, now))
                count += 1
        # Reconcile only the authoritative scope, including deleted source steps.
        # Legacy observations have no replayable source occurrence. Their
        # absence from current steps cannot retract their recorded history.
        delete_clauses, delete_params = ["source_kind='canonical_step'"], []
        if step_ids is not None:
            delete_clauses.append(f"json_extract(evidence_json,'$.step_id') IN ({','.join('?' for _ in step_ids)})")
            delete_params.extend(sorted(step_ids))
        elif session_ids is not None:
            delete_clauses.append(f"session_id IN ({','.join('?' for _ in session_ids)})")
            delete_params.extend(sorted(session_ids))
        self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS reflect_seen_skill_usage(id TEXT PRIMARY KEY)")
        self.conn.execute("DELETE FROM reflect_seen_skill_usage")
        self.conn.executemany("INSERT INTO reflect_seen_skill_usage VALUES (?)", ((item,) for item in seen))
        self.conn.execute("DELETE FROM skill_usage WHERE " + " AND ".join(delete_clauses)
                          + " AND id NOT IN (SELECT id FROM reflect_seen_skill_usage)", delete_params)
        self.conn.execute("DROP TABLE reflect_seen_skill_usage")
        return count
