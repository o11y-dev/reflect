from __future__ import annotations

import sqlite3
from enum import StrEnum

from pydantic import Field

from reflect.improvements.loops import LoopService
from reflect.improvements.models import (
    LoopKind,
    LoopRecord,
    LoopStatus,
    SkillLifecycleState,
    SkillRecord,
    WorkflowCandidateRecord,
    WorkflowStatus,
)
from reflect.improvements.skills import SkillRegistryService
from reflect.improvements.workflows import WorkflowService
from reflect.schema.base import ReflectModel


class SkillAvailability(StrEnum):
    """Installation availability filter for agent-facing skill discovery."""

    ANY = "any"
    INSTALLED = "installed"
    NOT_INSTALLED = "not_installed"


class PatternType(StrEnum):
    """Evidence-backed pattern families exposed through MCP."""

    ALL = "all"
    WORKFLOW = "workflow"
    LOOP = "loop"


class SkillInspectionAnswer(ReflectModel):
    """Bounded, typed skill registry search result."""

    query: str = ""
    lifecycle: SkillLifecycleState | None = None
    availability: SkillAvailability = SkillAvailability.ANY
    source_agent: str | None = None
    minimum_evidence: int = Field(default=0, ge=0)
    skills: list[SkillRecord] = Field(default_factory=list)
    count: int = Field(default=0, ge=0)
    truncated: bool = False


class SkillSourceSession(ReflectModel):
    """Bounded source-session provenance for one skill version."""

    session_id: str
    relationship: str
    confidence: float = Field(ge=0, le=1)
    title: str | None = None
    agent: str | None = None
    started_at: str
    status: str
    workspace: str | None = None


class PatternInspectionAnswer(ReflectModel):
    """Bounded workflow-candidate and loop inspection result."""

    pattern_type: PatternType
    query: str = ""
    workflows: list[WorkflowCandidateRecord] = Field(default_factory=list)
    loops: list[LoopRecord] = Field(default_factory=list)
    workflow_count: int = Field(default=0, ge=0)
    loop_count: int = Field(default=0, ge=0)
    truncated: bool = False


class AgentInspectionService:
    """Read-only discovery over Reflect's durable skill and pattern ledgers."""

    _SOURCE_LIMIT = 500

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        skills: SkillRegistryService | None = None,
        workflows: WorkflowService | None = None,
        loops: LoopService | None = None,
    ) -> None:
        self.conn = conn
        self.skills_registry = skills or SkillRegistryService(conn)
        self.workflows = workflows or WorkflowService(conn)
        self.loops = loops or LoopService(conn)

    def search_skills(
        self,
        *,
        query: str = "",
        lifecycle: SkillLifecycleState | None = None,
        availability: SkillAvailability = SkillAvailability.ANY,
        source_agent: str | None = None,
        minimum_evidence: int = 0,
        limit: int = 20,
    ) -> SkillInspectionAnswer:
        """Search already-indexed skills without refreshing or mutating the registry."""

        bounded_limit = max(1, min(limit, 100))
        normalized_query = query.strip().casefold()
        normalized_agent = source_agent.strip().casefold() if source_agent else None
        records = self.skills_registry.list(
            lifecycle=lifecycle,
            include_stale=True,
            limit=self._SOURCE_LIMIT,
        )
        filtered = [
            skill
            for skill in records
            if self._skill_matches(
                skill,
                query=normalized_query,
                availability=availability,
                source_agent=normalized_agent,
                minimum_evidence=max(0, minimum_evidence),
            )
        ]
        if normalized_query:
            filtered.sort(key=lambda skill: self._skill_search_key(skill, normalized_query))
        return SkillInspectionAnswer(
            query=query.strip(),
            lifecycle=lifecycle,
            availability=availability,
            source_agent=source_agent.strip() if source_agent else None,
            minimum_evidence=max(0, minimum_evidence),
            skills=filtered[:bounded_limit],
            count=len(filtered),
            truncated=len(filtered) > bounded_limit or len(records) == self._SOURCE_LIMIT,
        )

    def inspect_patterns(
        self,
        *,
        pattern_type: PatternType = PatternType.ALL,
        query: str = "",
        workflow_status: WorkflowStatus | None = None,
        loop_kind: LoopKind | None = None,
        loop_status: LoopStatus | None = None,
        limit: int = 20,
    ) -> PatternInspectionAnswer:
        """Inspect existing workflow candidates and loops without running detectors."""

        bounded_limit = max(1, min(limit, 100))
        normalized_query = query.strip().casefold()
        workflows: list[WorkflowCandidateRecord] = []
        loops: list[LoopRecord] = []
        workflow_source_count = 0
        loop_source_count = 0

        if pattern_type in {PatternType.ALL, PatternType.WORKFLOW}:
            workflows = self.workflows.list(
                limit=self._SOURCE_LIMIT,
                statuses={workflow_status.value} if workflow_status else None,
            )
            workflow_source_count = len(workflows)
            workflows = [
                item
                for item in workflows
                if self._matches_text(
                    normalized_query,
                    item.id,
                    item.title,
                    item.hypothesis,
                    item.scope,
                    str(item.content.get("slug") or ""),
                    str(item.content.get("description") or ""),
                )
            ]
        if pattern_type in {PatternType.ALL, PatternType.LOOP}:
            loops = self.loops.list(
                kind=loop_kind,
                status=loop_status,
                limit=self._SOURCE_LIMIT,
            )
            loop_source_count = len(loops)
            loops = [
                item
                for item in loops
                if self._matches_text(
                    normalized_query,
                    item.id,
                    item.title,
                    item.summary,
                    item.scope_type,
                    item.scope_id,
                    item.tool_name or "",
                )
            ]

        workflow_count = len(workflows)
        loop_count = len(loops)
        return PatternInspectionAnswer(
            pattern_type=pattern_type,
            query=query.strip(),
            workflows=workflows[:bounded_limit],
            loops=loops[:bounded_limit],
            workflow_count=workflow_count,
            loop_count=loop_count,
            truncated=(
                workflow_count > bounded_limit
                or loop_count > bounded_limit
                or workflow_source_count == self._SOURCE_LIMIT
                or loop_source_count == self._SOURCE_LIMIT
            ),
        )

    def skill_source_sessions(
        self,
        skill_version_id: str,
        *,
        limit: int = 50,
    ) -> list[SkillSourceSession]:
        """Resolve version-specific session evidence without exposing raw event content."""

        rows = self.conn.execute(
            """
            SELECT s.id, e.relationship, e.confidence, s.title, a.name,
                   s.started_at, s.status, w.root_path
            FROM skill_evidence e
            JOIN sessions s ON s.id = e.entity_id
            LEFT JOIN agents a ON a.id = s.agent_id
            LEFT JOIN workspaces w ON w.id = s.workspace_id
            WHERE e.skill_version_id = ? AND e.entity_type = 'session'
            ORDER BY s.started_at DESC, s.id
            LIMIT ?
            """,
            (skill_version_id, max(1, min(limit, 100))),
        ).fetchall()
        return [
            SkillSourceSession(
                session_id=str(row[0]),
                relationship=str(row[1]),
                confidence=float(row[2]),
                title=str(row[3]) if row[3] else None,
                agent=str(row[4]) if row[4] else None,
                started_at=str(row[5]),
                status=str(row[6]),
                workspace=str(row[7]) if row[7] else None,
            )
            for row in rows
        ]

    @classmethod
    def _skill_matches(
        cls,
        skill: SkillRecord,
        *,
        query: str,
        availability: SkillAvailability,
        source_agent: str | None,
        minimum_evidence: int,
    ) -> bool:
        if availability is SkillAvailability.INSTALLED and not skill.installation_count:
            return False
        if availability is SkillAvailability.NOT_INSTALLED and skill.installation_count:
            return False
        if source_agent and str(skill.source_agent or "").casefold() != source_agent:
            return False
        if skill.evidence_count < minimum_evidence:
            return False
        return cls._matches_text(
            query,
            skill.id,
            skill.slug,
            skill.name,
            skill.description,
            skill.source_agent or "",
        )

    @staticmethod
    def _matches_text(query: str, *values: str) -> bool:
        if not query:
            return True
        haystack = " ".join(values).casefold()
        return all(token in haystack for token in query.split())

    @staticmethod
    def _skill_search_key(skill: SkillRecord, query: str) -> tuple[int, int, str, str]:
        slug = skill.slug.casefold()
        name = skill.name.casefold()
        rank = (
            0
            if slug == query
            else 1
            if slug.startswith(query)
            else 2
            if name.startswith(query)
            else 3
        )
        return (rank, -skill.evidence_count, skill.slug, skill.id)
