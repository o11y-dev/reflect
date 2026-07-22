from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from pydantic import Field

from reflect.improvements.models import AskAnswer
from reflect.improvements.service import ImprovementService
from reflect.memory import MemoryService
from reflect.schema.base import ReflectModel
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


class ReflectContextAnswer(AskAnswer):
    """Task guidance enriched with scoped memory, without conflating provenance."""

    memories: list[ContextMemory] = Field(default_factory=list)


class ReflectContextService:
    """Compose Reflect evidence, workflows, usage, and memory for agent clients."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.improvements = ImprovementService(conn)
        self.memory = MemoryService(conn)
        self.usage = UsageService(conn)

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
