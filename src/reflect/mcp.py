from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from reflect.changes import ChangeAction
from reflect.improvements.contracts import WorkflowMilestoneEvidence
from reflect.improvements.milestones import WorkflowMilestoneState
from reflect.improvements.models import (
    LoopKind,
    LoopStatus,
    SkillLifecycleState,
    WorkflowStatus,
)
from reflect.inspection import PatternType, SkillAvailability
from reflect.task_runs import MCPTaskOutcome

SERVER_INSTRUCTIONS = """
Reflect provides local telemetry evidence, reviewed workflows, scoped context, and exact usage.
At the start of every non-trivial repository task, call reflect_context once after identifying the
task and repository path, and before implementation or file changes. Follow a selected skill only
when execution_state is follow_allowed and its preconditions match. When
execution_state is retrieve_full_instructions, call the provided full_instructions_action first.
Registry lifecycle and installation fields do not override execution_state. Call reflect_context
again only when the goal, repository, or subsystem changes materially.
Read the actual session ID from the active caller's runtime environment (for Codex,
CODEX_THREAD_ID) and pass runtime_session_id and runtime_agent to context and completion.
Never guess an ID or use the newest captured session. Inspect link_state for missing identity.
After validation, call reflect_complete exactly once with the returned task_run_id.
accepted_pending means the task event is durable and awaits projection; do not start a replacement task.
When selected workflow instructions expose a workflow contract, record its named checkpoints with
reflect_record_milestone before reflect_complete; milestone self-report is not treated as
independently verified evidence.
Do this before the final response. Skip this flow for trivial factual lookups
and tasks that do not involve a repository. Treat provider memory as context rather than
Reflect-verified evidence, and never install or apply workflows without explicit operator approval.
Use the read-only inspection tools when you need registry, pattern, provenance, or task-link status.
Use reflect_impact before concluding that an installed workflow has no measured impact.
For a requested workflow mutation, call reflect_review_change and present its exact target, diff,
evidence, risks, and rollback plan. Do not expose or apply its approval token yet. If the user asks
for a revision, prepare a new review and present the replacement diff. Call reflect_apply_change
only after the user explicitly approves that exact review in the current conversation. Vague
positive feedback is not approval. Never use these tools as a generic command execution path.
""".strip()

mcp = FastMCP("Reflect", instructions=SERVER_INSTRUCTIONS)
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
MILESTONE_TOOL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
CHANGE_REVIEW_TOOL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
CHANGE_APPLY_TOOL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)


def _db_path() -> Path:
    explicit = os.environ.get("REFLECT_DB_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = Path(os.environ.get("REFLECT_HOME", Path.home() / ".reflect")).expanduser()
    return home / "state" / "reflect.db"


async def _request(method: str, path: str, arguments: dict) -> dict[str, Any]:
    from reflect.backend_client import BackendClient
    from reflect.backend_lifecycle import BackendLocator
    endpoint = await BackendLocator(_db_path()).ensure()
    client = BackendClient(endpoint)
    return await client.request(method, path, **({'params': arguments} if method == 'GET' else {'body': arguments}))


@mcp.tool(annotations=TASK_START_TOOL)
async def reflect_context(
    question: str,
    path: str = "",
    task_file: str = "",
    memory_provider: str = "local_sqlite",
    memory_limit: int = 5,
    runtime_session_id: str = "",
    runtime_agent: str = "",
) -> dict[str, Any]:
    'Call once at task start before implementation to get guidance and selected skills.'
    arguments = {'question': question, 'path': path, 'task_file': task_file, 'memory_provider': memory_provider, 'memory_limit': memory_limit, 'runtime_session_id': runtime_session_id, 'runtime_agent': runtime_agent}
    arguments['path'] = str(Path(path).expanduser().resolve()) if path else str(Path.cwd())
    if task_file:
        arguments['task_file'] = str(Path(task_file).expanduser().resolve())
    arguments = {key: value for key, value in arguments.items() if value not in ('', None)}
    return await _request('POST', "/api/context", arguments)


@mcp.tool(annotations=TASK_COMPLETE_TOOL)
async def reflect_complete(
    task_run_id: str,
    outcome: MCPTaskOutcome,
    verification_passed: bool | None = None,
    summary: str = "",
    runtime_session_id: str = "",
    runtime_agent: str = "",
) -> dict[str, Any]:
    'Call once after validation to record the task outcome and close the Reflect guidance run.'
    arguments = {'outcome': outcome, 'verification_passed': verification_passed, 'summary': summary, 'runtime_session_id': runtime_session_id, 'runtime_agent': runtime_agent}
    arguments = {key: value for key, value in arguments.items() if value not in ('', None)}
    return await _request('POST', f"/api/task-runs/{quote(task_run_id, safe='')}/completion", arguments)


@mcp.tool(annotations=MILESTONE_TOOL)
async def reflect_record_milestone(
    task_run_id: str,
    skill_version_id: str,
    milestone_id: str,
    state: WorkflowMilestoneState,
    idempotency_key: str,
    evidence: WorkflowMilestoneEvidence | None = None,
) -> dict[str, Any]:
    'Record one idempotent workflow checkpoint against the active Reflect task run.'
    arguments = {'skill_version_id': skill_version_id, 'milestone_id': milestone_id, 'state': state, 'idempotency_key': idempotency_key, 'evidence': evidence}
    arguments['evidence'] = evidence.model_dump(mode='json') if evidence else None
    arguments = {key: value for key, value in arguments.items() if value not in ('', None)}
    return await _request('POST', f"/api/task-runs/{quote(task_run_id, safe='')}/milestones", arguments)


@mcp.tool(annotations=READ_ONLY_TOOL)
async def reflect_improvements(
    limit: int = 20,
    offset: int = 0,
    detail: str = "summary",
    evidence_limit: int = 10,
    path: str = "",
    session_id: str = "",
    global_scope: bool = False,
    period: str = "",
) -> dict[str, Any]:
    'Read scoped findings, marking retained evidence while refresh is pending.'
    arguments = {'limit': limit, 'offset': offset, 'detail': detail, 'evidence_limit': evidence_limit, 'path': path, 'session_id': session_id, 'global_scope': global_scope, 'period': period}
    if path or (not session_id and not global_scope):
        arguments['path'] = str(Path(path).expanduser().resolve()) if path else str(Path.cwd())
    arguments = {key: value for key, value in arguments.items() if value not in ('', None)}
    return await _request('GET', "/api/improvements", arguments)


@mcp.tool(annotations=READ_ONLY_TOOL)
async def reflect_skills(
    query: str = "",
    lifecycle: SkillLifecycleState | None = None,
    availability: SkillAvailability = SkillAvailability.ANY,
    source_agent: str = "",
    minimum_evidence: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    'List or search indexed skills by lifecycle, installation, source agent, and evidence.'
    arguments = {'query': query, 'lifecycle': lifecycle, 'availability': availability, 'source_agent': source_agent, 'minimum_evidence': minimum_evidence, 'limit': limit}
    arguments = {key: value for key, value in arguments.items() if value not in ('', None)}
    return await _request('GET', "/api/skill-search", arguments)


@mcp.tool(annotations=READ_ONLY_TOOL)
async def reflect_patterns(
    pattern_type: PatternType = PatternType.ALL,
    query: str = "",
    workflow_status: WorkflowStatus | None = None,
    loop_kind: LoopKind | None = None,
    loop_status: LoopStatus | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    'Inspect existing workflow candidates and loops without running detectors.'
    arguments = {'pattern_type': pattern_type, 'query': query, 'workflow_status': workflow_status, 'loop_kind': loop_kind, 'loop_status': loop_status, 'limit': limit}
    arguments = {key: value for key, value in arguments.items() if value not in ('', None)}
    return await _request('GET', "/api/patterns", arguments)


@mcp.tool(annotations=READ_ONLY_TOOL)
async def reflect_impact(
    impact_id: str = "",
    workflow_id: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    'List impact checks or inspect one persisted before/after comparison.'
    arguments = {'impact_id': impact_id, 'workflow_id': workflow_id, 'limit': limit}
    arguments = {key: value for key, value in arguments.items() if value not in ('', None)}
    return await _request('GET', "/api/impact-checks", arguments)


@mcp.tool(annotations=READ_ONLY_TOOL)
async def reflect_guidance_efficiency(
    repo_id: str, task_archetype_id: str, model: str, limit: int = 100
) -> dict[str, Any]:
    'Compare verified tasks for one repository, task archetype and exact model.'
    arguments = {'repo_id': repo_id, 'task_archetype_id': task_archetype_id, 'model': model, 'limit': limit}
    arguments = {key: value for key, value in arguments.items() if value not in ('', None)}
    return await _request('GET', "/api/guidance-efficiency", arguments)


@mcp.tool(annotations=READ_ONLY_TOOL)
async def reflect_task_status(task_run_id: str) -> dict[str, Any]:
    'Inspect task completion and late-ingestion linkage without changing either.'
    arguments = {}
    arguments = {key: value for key, value in arguments.items() if value not in ('', None)}
    return await _request('GET', f"/api/task-runs/{quote(task_run_id, safe='')}", arguments)


@mcp.tool(annotations=CHANGE_REVIEW_TOOL)
async def reflect_review_change(
    action: ChangeAction,
    entity_id: str,
    project_root: str = "",
    revised_content: dict[str, Any] | None = None,
    expires_in_minutes: int = 30,
) -> dict[str, Any]:
    'Prepare an exact workflow approval, application, or rollback for user review.'
    arguments = {'action': action, 'entity_id': entity_id, 'project_root': project_root, 'revised_content': revised_content, 'expires_in_minutes': expires_in_minutes}
    arguments['project_root'] = str(Path(project_root).expanduser().resolve()) if project_root else str(Path.cwd())
    arguments = {key: value for key, value in arguments.items() if value not in ('', None)}
    return await _request('POST', "/api/change-reviews", arguments)


@mcp.tool(annotations=CHANGE_APPLY_TOOL)
async def reflect_apply_change(approval_token: str) -> dict[str, Any]:
    'Apply only the exact reviewed change after explicit user approval.'
    arguments = {'approval_token': approval_token}
    arguments = {key: value for key, value in arguments.items() if value not in ('', None)}
    return await _request('POST', "/api/change-reviews/apply", arguments)


@mcp.tool(annotations=READ_ONLY_TOOL)
async def reflect_explain(entity_id: str) -> dict[str, Any]:
    'Explain evidence, workflows, skills, task runs, change reviews, or memory.'
    arguments = {}
    arguments = {key: value for key, value in arguments.items() if value not in ('', None)}
    return await _request('GET', f"/api/entities/{quote(entity_id, safe='')}", arguments)


@mcp.tool(annotations=READ_ONLY_TOOL)
async def reflect_usage(
    session_id: str = "",
    global_scope: bool = False,
    period: str = "week",
    agent: str = "",
) -> dict[str, Any]:
    'Return exact local usage for one session or a global day, week, month, or all-time scope.'
    if not session_id and not global_scope:
        raise ValueError('Pass the caller session_id or select global_scope explicitly')
    arguments = {'session_id': session_id, 'global_scope': global_scope, 'period': period, 'agent': agent}
    arguments = {key: value for key, value in arguments.items() if value not in ('', None)}
    return await _request('POST', "/api/usage", arguments)


@mcp.tool(annotations=READ_ONLY_TOOL)
async def reflect_jobs(job_id: str = "", limit: int = 20) -> dict[str, Any]:
    """Inspect durable asynchronous work without resubmitting it."""
    if job_id:
        return await _request('GET', f'/api/jobs/{quote(job_id, safe="")}', {})
    return await _request('GET', '/api/jobs', {'limit': limit})


def main() -> None:
    """Run Reflect's standards-compliant local MCP server over stdio."""

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
