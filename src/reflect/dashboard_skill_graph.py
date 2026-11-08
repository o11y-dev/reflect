"""Bounded skill usage exploration over canonical source-owned occurrences."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from reflect.improvements.skills import SkillRegistryService
from reflect.store.sqlite import snapshot_incomplete


class SkillGraphNode(BaseModel):
    id: str
    kind: str
    label: str
    data: dict = Field(default_factory=dict)


class SkillGraphEdge(BaseModel):
    source: str
    target: str
    kind: str
    evidence_class: str = "derived"


def build_skill_usage_graph(
    conn: sqlite3.Connection, skill_id: str, *, days: int | None = 7,
    repo_id: str | None = None, agents: set[str] | None = None,
    version: str | None = None, offset: int = 0, limit: int = 20,
    execution_unit_id: str | None = None, session_id: str | None = None,
    evidence_offset: int = 0,
    now: datetime | None = None,
) -> dict:
    """Read one consistent filtered population; pagination never changes counts."""
    if (days is not None and not 1 <= days <= 3650) or offset < 0 or evidence_offset < 0 or not 1 <= limit <= 20:
        raise ValueError("days must be 1..3650 or all, offset nonnegative, and limit 1..20")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    date_from = (now - timedelta(days=days)).isoformat() if days is not None else None
    date_to = now.isoformat()
    conn.execute("SAVEPOINT skill_usage_graph_read")
    try:
        resolved_id = SkillRegistryService(conn, initialize_schema=False).resolve_id(skill_id)
        skill = conn.execute("SELECT id,name FROM skills WHERE id=?", (resolved_id,)).fetchone()
        skill_id, skill_name = str(skill[0]), str(skill[1])
        pending = snapshot_incomplete(conn)
        versions = [{"id": row[0], "label": f"Version {row[1]}"} for row in conn.execute(
            "SELECT id,version FROM skill_versions WHERE skill_id=? ORDER BY version DESC", (skill_id,))]
        completed = conn.execute("SELECT value FROM store_metadata WHERE key='last_successful_refresh'").fetchone()
        clauses = ["u.skill_id=?", "julianday(u.observed_at)<julianday(?)"]
        params: list = [skill_id, date_to]
        if date_from is not None:
            clauses.append("julianday(u.observed_at)>=julianday(?)")
            params.append(date_from)
        if repo_id:
            clauses.append("s.repo_id=?")
            params.append(repo_id)
        if agents:
            clauses.append(f"a.name IN ({','.join('?' for _ in agents)})")
            params.extend(sorted(agents))
        if version == "unknown":
            clauses.append("u.skill_version_id IS NULL")
        elif version:
            clauses.append("u.skill_version_id=?")
            params.append(version)
        if execution_unit_id:
            clauses.append("u.execution_unit_id=?")
            params.append(execution_unit_id)
        if session_id:
            clauses.append("u.session_id=?")
            params.append(session_id)
            if not execution_unit_id:
                # Expanding a session represents its unresolved occurrences,
                # not every separately attributed execution in that session.
                clauses.append("(eu.id IS NULL OR eu.source='mcp_report')")
        source = """FROM skill_usage u JOIN sessions s ON s.id=u.session_id
                    LEFT JOIN agents a ON a.id=s.agent_id
                    LEFT JOIN execution_units eu ON eu.id=u.execution_unit_id
                    LEFT JOIN repos r ON r.id=s.repo_id"""
        where = " WHERE " + " AND ".join(clauses)
        group_key = "CASE WHEN eu.id IS NOT NULL AND eu.source<>'mcp_report' THEN eu.id ELSE 'session:'||u.session_id END"
        count_row = conn.execute(f"""SELECT COUNT(*),COUNT(DISTINCT {group_key}),
            COUNT(DISTINCT CASE WHEN eu.source<>'mcp_report' THEN eu.id END),
            COUNT(DISTINCT u.session_id),COUNT(DISTINCT s.repo_id) {source} {where}""", params).fetchone()
        by_evidence = dict(conn.execute(f"SELECT u.state,COUNT(*) {source} {where} GROUP BY u.state", params))
        groups = conn.execute(f"""SELECT {group_key} AS owner,MAX(u.observed_at) AS observed_at,
            COUNT(*) AS occurrences,MIN(u.session_id),MIN(s.title),MIN(a.name),MIN(s.repo_id),MIN(r.name),
            CASE WHEN MIN(eu.source)<>'mcp_report' THEN MIN(eu.id) END
            {source} {where} GROUP BY 1 ORDER BY 2 DESC,1 LIMIT ? OFFSET ?""", [*params, limit, offset]).fetchall()
        group_ids = [str(row[0]) for row in groups]
        occurrences: dict[str, list[dict]] = {key: [] for key in group_ids}
        # Three source references per group, or twenty when a group is expanded.
        source_limit = 20 if execution_unit_id or session_id else 3
        if group_ids:
            selected = ",".join("?" for _ in group_ids)
            detail_rows = conn.execute(f"""WITH ranked AS (
                SELECT {group_key} AS owner,u.id,u.execution_unit_id,u.session_id,u.skill_version_id,
                       u.source_kind,u.source_id,u.state,u.observed_at,u.evidence_json,
                       ROW_NUMBER() OVER (PARTITION BY {group_key} ORDER BY u.observed_at DESC,u.id) AS rank
                {source} {where} AND {group_key} IN ({selected})
            ) SELECT owner,id,execution_unit_id,session_id,skill_version_id,source_kind,source_id,
                     state,observed_at,evidence_json FROM ranked WHERE rank>? AND rank<=? ORDER BY owner,rank""",
                [*params, *group_ids, evidence_offset, evidence_offset + source_limit]).fetchall()
            for row in detail_rows:
                try:
                    evidence = json.loads(row[9])
                except (ValueError, TypeError):
                    evidence = {}
                occurrences[row[0]].append({"usage_id": row[1], "execution_unit_id": row[2],
                    "session_id": row[3], "skill_version_id": row[4], "source_kind": row[5],
                    "source_id": row[6], "evidence_class": row[7], "observed_at": row[8],
                    "relationship": evidence.get("relationship", "mentioned"), "evidence": evidence})
        nodes = {skill_id: SkillGraphNode(id=skill_id, kind="Skill", label=skill_name)}
        edges: dict[tuple, SkillGraphEdge] = {}
        records = []
        for owner, at, count, sid, title, agent, rid, repo, eid in groups:
            repo_node = f"repo:{rid}" if rid else "repo:unknown"
            nodes.setdefault(repo_node, SkillGraphNode(id=repo_node, kind="Repo", label=repo or "Repository unknown"))
            nodes[owner] = SkillGraphNode(id=owner, kind="Execution" if eid else "Session",
                label=title or str(eid or sid), data={"session_id": sid, "execution_unit_id": eid,
                    "agent": agent, "occurrences": count, "observed_at": at})
            edges[(skill_id, repo_node, "scope")] = SkillGraphEdge(source=skill_id, target=repo_node, kind="usage_scope")
            edges[(repo_node, owner, "contains")] = SkillGraphEdge(source=repo_node, target=owner, kind="contains_execution" if eid else "contains_session")
            record = {"id": owner, "execution_unit_id": eid, "session_id": sid,
                      "title": title, "agent": agent, "repo_id": rid, "repository": repo,
                      "observed_at": at, "occurrence_count": count, "evidence": occurrences[owner]}
            records.append(record)
            for item in occurrences[owner]:
                key = (owner, item["relationship"], item["evidence_class"])
                edges[key] = SkillGraphEdge(source=skill_id, target=owner,
                    kind=item["relationship"], evidence_class=item["evidence_class"])
                if (execution_unit_id or session_id) and len(nodes) < 75:
                    evidence_id = item["usage_id"]
                    nodes[evidence_id] = SkillGraphNode(id=evidence_id, kind="Evidence",
                        label=item["relationship"].replace("_", " "), data=item)
                    edges[(owner, evidence_id, "evidence")] = SkillGraphEdge(source=owner,
                        target=evidence_id, kind="supported_by", evidence_class=item["evidence_class"])
        next_offset = offset + len(groups) if offset + len(groups) < count_row[1] else None
        return {"skill_id": skill_id, "skill_name": skill_name,
            "nodes": [node.model_dump() for node in nodes.values()],
            "edges": [edge.model_dump() for edge in list(edges.values())[:150]], "records": records,
            "counts": {"occurrences": count_row[0], "groups": count_row[1], "executions": count_row[2],
                       "sessions": count_row[3], "repositories": count_row[4], "by_evidence": by_evidence},
            "pagination": {"offset": offset, "limit": limit, "next_offset": next_offset,
                "evidence_offset": evidence_offset,
                "next_evidence_offset": evidence_offset + source_limit if (execution_unit_id or session_id)
                    and any(row[2] > evidence_offset + source_limit for row in groups) else None},
            "scope": {"days": days, "date_from": date_from, "date_to": date_to,
                      "repo_id": repo_id, "agents": sorted(agents or []), "version": version},
            "versions": versions,
            "analysis_state": "pending" if pending else "ready", "evidence_state": "recorded" if pending else "published",
            "publication": {"state": "recorded" if pending else "current", "recorded_at": max((row[1] for row in groups), default=None),
                            "snapshot_completed_at": completed[0] if completed else None},
            "limitations": ["Skill references do not prove the procedure was followed or caused an outcome.",
                            "Missing execution and version attribution remains unknown."]}
    finally:
        conn.execute("RELEASE SAVEPOINT skill_usage_graph_read")
