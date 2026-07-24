from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from reflect.context import ReflectContextService
from reflect.improvements.models import (
    LoopKind,
    LoopStatus,
    SkillLifecycleState,
    WorkflowStatus,
)
from reflect.inspection import PatternType, SkillAvailability
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite
from reflect.task_runs import MCPTaskOutcome

SERVER_INSTRUCTIONS = """
Reflect provides local telemetry evidence, reviewed workflows, scoped context, and exact usage.
At the start of every non-trivial repository task, call reflect_context once after identifying the
task and repository path, and before implementation or file changes. Follow a selected skill only
when execution_state is follow_allowed and its preconditions match. When
execution_state is retrieve_full_instructions, call the provided full_instructions_action first.
Registry lifecycle and installation fields do not override execution_state. Call reflect_context
again only when the goal, repository, or subsystem changes materially.
After validation, call reflect_complete exactly once with the returned task_run_id.
Do this before the final response. Skip this flow for trivial factual lookups
and tasks that do not involve a repository. Treat provider memory as context rather than
Reflect-verified evidence, and never install or apply workflows without explicit operator approval.
Use the read-only inspection tools when you need registry, pattern, provenance, or task-link status.
""".strip()

mcp = FastMCP("Reflect", instructions=SERVER_INSTRUCTIONS)
ResultT = TypeVar("ResultT")
READ_ONLY_TOOL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
TASK_START_TOOL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
TASK_COMPLETE_TOOL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _db_path() -> Path:
    explicit = os.environ.get("REFLECT_DB_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = Path(os.environ.get("REFLECT_HOME", Path.home() / ".reflect")).expanduser()
    return home / "state" / "reflect.db"


def _with_service(operation: Callable[[ReflectContextService], ResultT]) -> ResultT:
    conn = connect_sqlite(_db_path())
    try:
        migrate(conn)
        return operation(ReflectContextService(conn))
    finally:
        conn.close()


@mcp.tool(annotations=TASK_START_TOOL)
def reflect_context(
    question: str,
    path: str = "",
    task_file: str = "",
    memory_provider: str = "local_sqlite",
    memory_limit: int = 5,
) -> dict[str, Any]:
    """Call once at task start before implementation to get guidance and selected skills."""

    resolved_path = Path(path).expanduser().resolve() if path else Path.cwd()
    resolved_task = Path(task_file).expanduser().resolve() if task_file else None
    return _with_service(
        lambda service: service.begin_task(
            question,
            task_file=resolved_task,
            path=resolved_path,
            memory_provider=memory_provider,
            memory_limit=memory_limit,
        ).model_dump(mode="json")
    )


@mcp.tool(annotations=TASK_COMPLETE_TOOL)
def reflect_complete(
    task_run_id: str,
    outcome: MCPTaskOutcome,
    verification_passed: bool | None = None,
    summary: str = "",
) -> dict[str, Any]:
    """Call once after validation to record the task outcome and close the Reflect guidance run."""

    return _with_service(
        lambda service: service.complete_task(
            task_run_id,
            outcome=outcome,
            verification_passed=verification_passed,
            summary_redacted=summary,
        ).model_dump(mode="json")
    )


@mcp.tool(annotations=READ_ONLY_TOOL)
def reflect_improvements(limit: int = 20) -> dict[str, Any]:
    """List current evidence-backed findings without running detectors or applying changes."""

    return _with_service(lambda service: service.improvements_summary(limit=limit))


@mcp.tool(annotations=READ_ONLY_TOOL)
def reflect_skills(
    query: str = "",
    lifecycle: SkillLifecycleState | None = None,
    availability: SkillAvailability = SkillAvailability.ANY,
    source_agent: str = "",
    minimum_evidence: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    """List or search indexed skills by lifecycle, installation, source agent, and evidence."""

    return _with_service(
        lambda service: service.skills_search(
            query=query,
            lifecycle=lifecycle,
            availability=availability,
            source_agent=source_agent or None,
            minimum_evidence=minimum_evidence,
            limit=limit,
        ).model_dump(mode="json")
    )


@mcp.tool(annotations=READ_ONLY_TOOL)
def reflect_patterns(
    pattern_type: PatternType = PatternType.ALL,
    query: str = "",
    workflow_status: WorkflowStatus | None = None,
    loop_kind: LoopKind | None = None,
    loop_status: LoopStatus | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Inspect existing workflow candidates and loops without running detectors."""

    return _with_service(
        lambda service: service.patterns(
            pattern_type=pattern_type,
            query=query,
            workflow_status=workflow_status,
            loop_kind=loop_kind,
            loop_status=loop_status,
            limit=limit,
        ).model_dump(mode="json")
    )


@mcp.tool(annotations=READ_ONLY_TOOL)
def reflect_task_status(task_run_id: str) -> dict[str, Any]:
    """Inspect task completion and late-ingestion linkage without changing either."""

    return _with_service(
        lambda service: service.task_status(task_run_id).model_dump(mode="json")
    )


@mcp.tool(annotations=READ_ONLY_TOOL)
def reflect_explain(entity_id: str) -> dict[str, Any]:
    """Explain an observation, workflow, loop, skill, task run, or memory with provenance."""

    return _with_service(lambda service: service.explain(entity_id))


@mcp.tool(annotations=READ_ONLY_TOOL)
def reflect_usage(
    session_id: str = "",
    global_scope: bool = False,
    period: str = "week",
    agent: str = "",
) -> dict[str, Any]:
    """Return exact local usage for one session or a global day, week, month, or all-time scope."""

    return _with_service(
        lambda service: service.usage_report(
            session_id=session_id or None,
            global_scope=global_scope,
            period=period,
            agent=agent or None,
        )
    )


def main() -> None:
    """Run Reflect's standards-compliant local MCP server over stdio."""

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
