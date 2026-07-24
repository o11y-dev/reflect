from __future__ import annotations

import sqlite3
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field

from reflect.improvements.models import AskAnswer
from reflect.improvements.service import ImprovementService
from reflect.memory import MemoryService
from reflect.schema.base import ReflectModel
from reflect.task_runs import (
    MCPSelectedSkillRef,
    MCPTaskOutcome,
    MCPTaskRunResult,
    MCPTaskRunService,
)
from reflect.usage import UsageService


class ContextMemory(ReflectModel):
    """A bounded memory result with explicit provenance."""

    id: str
    content: str
    type: str = ""
    scope: str = ""
    provider: str
    provenance: str
    confidence: float = 0.5
    score: float = 0.0
    validation_status: str = ""
    source_kind: str = ""
    source_ref: str = ""
    path: str = ""
    workspace_root: str = ""


class ContextNextAction(ReflectModel):
    """A bounded follow-up call an agent can make."""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    when: str


class SkillExecutionState(StrEnum):
    """Machine-readable decision for handling selected skill instructions."""

    FOLLOW_ALLOWED = "follow_allowed"
    RETRIEVE_FULL_INSTRUCTIONS = "retrieve_full_instructions"


class SkillInstallationState(StrEnum):
    """Whether Reflect has an active installation for the selected skill."""

    INSTALLED = "installed"
    NOT_INSTALLED = "not_installed"


class ContextSkill(ReflectModel):
    """A versioned skill selected for the current task."""

    skill_id: str
    version_id: str
    slug: str
    name: str
    description: str
    workflow_status: str
    registry_lifecycle_state: str
    execution_state: SkillExecutionState
    execution_reason: str
    installation_targets: list[str] = Field(default_factory=list)
    installation_state: SkillInstallationState
    installation_requires_operator_approval: bool
    content_hash: str
    instructions: str
    instructions_truncated: bool = False
    full_instructions_action: ContextNextAction | None = None


class ReflectContextAnswer(AskAnswer):
    """Task guidance enriched with scoped memory, without conflating provenance."""

    memories: list[ContextMemory] = Field(default_factory=list)
    task_run_id: str | None = None
    selected_skills: list[ContextSkill] = Field(default_factory=list)
    next_action: ContextNextAction | None = None


class TaskCompletionAnswer(MCPTaskRunResult):
    """Task completion result with a bounded optional improvement follow-up."""

    next_action: ContextNextAction


class ReflectContextService:
    """Compose Reflect evidence, workflows, usage, and memory for agent clients."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.improvements = ImprovementService(conn)
        self.memory = MemoryService(conn)
        self.usage = UsageService(conn)
        self.task_runs = MCPTaskRunService(conn, usage=self.usage)

    def ask(
        self,
        question: str,
        *,
        task_file: Path | None = None,
        path: Path | None = None,
        memory_provider: str = "local_sqlite",
        memory_limit: int = 5,
    ) -> ReflectContextAnswer:
        answer = self.improvements.ask(question, task_file=task_file, path=path)
        limitations = list(answer.limitations)
        try:
            rows = self.memory.search(
                question,
                path=path or Path.cwd(),
                provider=memory_provider,
                limit=max(1, min(memory_limit, 20)),
            )
        except Exception as exc:  # noqa: BLE001 - context must preserve local guidance
            rows = []
            limitations.append(f"Memory provider {memory_provider!r} was unavailable: {exc}")

        memories = [self._context_memory(row, memory_provider) for row in rows]
        memories = [memory for memory in memories if memory.validation_status != "stale"]
        unvalidated = sum(
            1
            for memory in memories
            if memory.provider == "local_sqlite" and memory.validation_status != "validated"
        )
        if unvalidated:
            limitations.append(
                f"{unvalidated} matching local memory item(s) are unvalidated context, not approved guidance."
            )

        answer_text = answer.answer
        confidence = answer.confidence
        if memories and not answer.evidence:
            answer_text = (
                f"Reflect found {len(memories)} scoped memory item(s), but no matching approved workflow. "
                "Treat memory as context and verify it against the current repository state."
            )
            confidence = min(0.8, sum(memory.confidence for memory in memories) / len(memories))
        elif memories:
            answer_text = (
                f"Reflect found {len(answer.evidence)} workflow or observation item(s) and "
                f"{len(memories)} scoped memory item(s). Use approved guidance first and treat memory "
                "as supporting context."
            )

        return ReflectContextAnswer(
            **answer.model_dump(exclude={"answer", "evidence", "confidence", "limitations"}),
            answer=answer_text,
            evidence=answer.evidence,
            confidence=confidence,
            limitations=list(dict.fromkeys(limitations)),
            memories=memories,
        )

    def begin_task(
        self,
        question: str,
        *,
        task_file: Path | None = None,
        path: Path | None = None,
        memory_provider: str = "local_sqlite",
        memory_limit: int = 5,
    ) -> ReflectContextAnswer:
        """Return task guidance and record a privacy-safe MCP guidance run."""

        resolved_path = (path or Path.cwd()).expanduser().resolve()
        resolved_task = task_file.expanduser().resolve() if task_file else None
        answer = self.ask(
            question,
            task_file=resolved_task,
            path=resolved_path,
            memory_provider=memory_provider,
            memory_limit=memory_limit,
        )
        selected_skills = self._selected_skills(answer.workflow_id)
        skill_refs = [
            MCPSelectedSkillRef(
                skill_id=skill.skill_id,
                version_id=skill.version_id,
                slug=skill.slug,
            )
            for skill in selected_skills
        ]
        task_run_id = self.task_runs.start(
            question=question,
            workspace_path=resolved_path,
            task_file_path=resolved_task,
            workflow_id=answer.workflow_id,
            selected_skills=skill_refs,
        )
        return answer.model_copy(
            update={
                "task_run_id": task_run_id,
                "selected_skills": selected_skills,
                "next_action": ContextNextAction(
                    tool="reflect_complete",
                    arguments={"task_run_id": task_run_id},
                    when="After completing task validation and before the final response.",
                ),
            }
        )

    def complete_task(
        self,
        task_run_id: str,
        *,
        outcome: MCPTaskOutcome | str,
        verification_passed: bool | None = None,
        summary_redacted: str = "",
    ) -> TaskCompletionAnswer:
        """Record the agent-reported outcome for one MCP guidance run."""

        result = self.task_runs.complete(
            task_run_id,
            outcome=outcome,
            verification_passed=verification_passed,
            summary_redacted=summary_redacted,
        )
        return TaskCompletionAnswer(
            **result.model_dump(mode="python"),
            next_action=ContextNextAction(
                tool="reflect_improvements",
                when=(
                    "When the task exposed a repeated success, failure, recovery pattern, "
                    "or workflow gap."
                ),
            ),
        )

    def improvements_summary(self, *, limit: int = 20) -> dict[str, Any]:
        findings = self.improvements.list_inbox_findings(limit=max(1, min(limit, 100)))
        return {
            "findings": [finding.model_dump(mode="json") for finding in findings],
            "count": len(findings),
            "provenance": "local_telemetry",
        }

    def explain(self, entity_id: str) -> dict[str, Any]:
        observation = self.improvements.repository.get_observation(entity_id)
        if observation is not None:
            return {
                "found": True,
                "kind": "observation",
                "provenance": "local_telemetry",
                "entity": observation.model_dump(mode="json"),
            }
        workflow = self.improvements.repository.get_candidate(entity_id)
        if workflow is not None:
            return {
                "found": True,
                "kind": "workflow",
                "provenance": "reflect_workflow_ledger",
                "entity": workflow.model_dump(mode="json"),
            }
        skill = self._skill_explanation(entity_id)
        if skill is not None:
            return skill
        memory = self.memory.inspect(entity_id)
        if memory is not None:
            source = memory.get("source_metadata") or {}
            return {
                "found": True,
                "kind": "memory",
                "provenance": "local_memory",
                "entity": {
                    "id": memory.get("id"),
                    "type": memory.get("type"),
                    "scope": memory.get("scope"),
                    "provider": memory.get("provider"),
                    "provider_memory_id": memory.get("provider_memory_id"),
                    "provider_status": memory.get("provider_status"),
                    "confidence": memory.get("confidence"),
                    "validation_status": memory.get("validation_status"),
                    "stale_reason": memory.get("stale_reason") or "",
                    "source_metadata": source,
                    "content": memory.get("content_preview_redacted") or "",
                },
            }
        return {"found": False, "reason": "entity_not_found", "entity_id": entity_id}

    def usage_report(
        self,
        *,
        session_id: str | None = None,
        global_scope: bool = False,
        period: str = "week",
        agent: str | None = None,
    ) -> dict[str, Any]:
        return self.usage.report(
            session_id=session_id,
            global_scope=global_scope,
            period=period,
            agent=agent,
        ).model_dump(mode="json")

    def _selected_skills(self, workflow_id: str | None) -> list[ContextSkill]:
        if not workflow_id:
            return []
        workflow = self.improvements.repository.get_candidate(workflow_id)
        if workflow is None or workflow.status.value not in {"approved", "active"}:
            return []
        self.improvements.skills.sync_workflow_candidates([workflow_id])
        try:
            skill = self.improvements.skills.skill_for_candidate(workflow_id)
            detail = self.improvements.skills.show(skill.id)
        except (KeyError, RuntimeError):
            return []
        current = next(
            (
                version
                for version in detail.versions
                if version.id == detail.skill.current_version_id
            ),
            detail.versions[0] if detail.versions else None,
        )
        if current is None:
            return []
        instructions_limit = 20_000
        instructions_truncated = len(current.content_markdown) > instructions_limit
        execution_state = (
            SkillExecutionState.RETRIEVE_FULL_INSTRUCTIONS
            if instructions_truncated
            else SkillExecutionState.FOLLOW_ALLOWED
        )
        execution_reason = (
            "Retrieve the complete versioned instructions before following this skill."
            if instructions_truncated
            else "The linked workflow is approved or active and its inline instructions are complete."
        )
        installation_targets = detail.skill.installation_targets
        return [
            ContextSkill(
                skill_id=detail.skill.id,
                version_id=current.id,
                slug=detail.skill.slug,
                name=detail.skill.name,
                description=detail.skill.description,
                workflow_status=workflow.status.value,
                registry_lifecycle_state=detail.skill.lifecycle_state.value,
                execution_state=execution_state,
                execution_reason=execution_reason,
                installation_targets=installation_targets,
                installation_state=(
                    SkillInstallationState.INSTALLED
                    if installation_targets
                    else SkillInstallationState.NOT_INSTALLED
                ),
                installation_requires_operator_approval=True,
                content_hash=current.content_hash,
                instructions=current.content_markdown[:instructions_limit],
                instructions_truncated=instructions_truncated,
                full_instructions_action=(
                    ContextNextAction(
                        tool="reflect_explain",
                        arguments={"entity_id": current.id},
                        when="Before following this skill because its inline instructions are truncated.",
                    )
                    if instructions_truncated
                    else None
                ),
            )
        ]

    def _skill_explanation(self, entity_id: str) -> dict[str, Any] | None:
        version_row = self.conn.execute(
            "SELECT skill_id FROM skill_versions WHERE id = ?",
            (entity_id,),
        ).fetchone()
        try:
            detail = self.improvements.skills.show(
                str(version_row[0]) if version_row is not None else entity_id
            )
        except KeyError:
            return None
        version = next(
            (
                item
                for item in detail.versions
                if item.id
                == (entity_id if version_row is not None else detail.skill.current_version_id)
            ),
            detail.versions[0] if detail.versions else None,
        )
        if version is None:
            return None
        return {
            "found": True,
            "kind": "skill_version" if version_row is not None else "skill",
            "provenance": "reflect_skill_registry",
            "entity": {
                "skill": detail.skill.model_dump(mode="json"),
                "version": version.model_dump(mode="json"),
                "instructions_truncated": False,
            },
        }

    @staticmethod
    def _context_memory(row: dict[str, Any], requested_provider: str) -> ContextMemory:
        source = row.get("source_metadata") or {}
        provider = str(row.get("provider") or requested_provider)
        return ContextMemory(
            id=str(row.get("id") or row.get("memory_id") or ""),
            content=str(row.get("content_preview_redacted") or row.get("content") or "")[:1000],
            type=str(row.get("type") or ""),
            scope=str(row.get("scope") or ""),
            provider=provider,
            provenance="local_memory" if provider == "local_sqlite" else "provider_memory",
            confidence=float(row.get("confidence") or 0.5),
            score=float(row.get("score") or 0.0),
            validation_status=str(row.get("validation_status") or ""),
            source_kind=str(source.get("source_kind") or row.get("source") or ""),
            source_ref=str(source.get("source_ref") or ""),
            path=str(source.get("path") or ""),
            workspace_root=str(source.get("workspace_root") or ""),
        )
