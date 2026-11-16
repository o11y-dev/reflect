"""Typed product resources shared by terminal, MCP and browser clients."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from reflect.application import ReflectApplication
from reflect.changes import ChangeAction
from reflect.context import ReflectContextService
from reflect.improvements.contracts import WorkflowMilestoneEvidence
from reflect.improvements.milestones import WorkflowMilestoneState
from reflect.improvements.models import LoopKind, LoopStatus, SkillLifecycleState, WorkflowStatus
from reflect.inspection import PatternType, SkillAvailability
from reflect.task_inbox import TaskEventInbox
from reflect.task_runs import MCPTaskOutcome


class RequestModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class CaptureStatusRequest(RequestModel):
    closed_paths: list[str]


class ContextRequest(RequestModel):
    question: str
    path: str
    task_file: str | None = None
    memory_provider: str = 'local_sqlite'
    memory_limit: int = Field(default=5, ge=0, le=100)
    runtime_session_id: str | None = None
    runtime_agent: str | None = None


class CompletionRequest(RequestModel):
    outcome: MCPTaskOutcome
    verification_passed: bool | None = None
    summary: str = ''
    runtime_session_id: str | None = None
    runtime_agent: str | None = None


class MilestoneRequest(RequestModel):
    skill_version_id: str
    milestone_id: str
    state: WorkflowMilestoneState
    idempotency_key: str
    evidence: WorkflowMilestoneEvidence | None = None


class ReviewRequest(RequestModel):
    action: ChangeAction
    entity_id: str
    project_root: str | None = None
    revised_content: dict | None = None
    expires_in_minutes: int = Field(default=30, ge=1, le=1440)


class ApplyReviewRequest(RequestModel):
    approval_token: str


class UsageRequest(RequestModel):
    session_id: str | None = None
    global_scope: bool = False
    period: Literal['day', 'week', 'month', 'all'] = 'week'
    agent: str | None = None


class ProductResources:
    def __init__(self, application: ReflectApplication, runner):
        self.application = application
        self.runner = runner
        self.inbox = TaskEventInbox(application.db_path)

    @contextmanager
    def context(self):
        with self.application.read() as service:
            yield ReflectContextService(service.conn, initialize_schema=False)

    def start_task(self, request: ContextRequest):
        arguments = request.model_dump()
        arguments['path'] = Path(arguments['path']).expanduser().resolve()
        arguments['task_file'] = Path(arguments['task_file']) if arguments['task_file'] else None
        with self.context() as service:
            answer, start = service.prepare_task(**arguments)
        self.inbox.start(start)
        self.runner.jobs.submit('task_projection', {})
        status = self.inbox.status(start.task_run_id)
        return answer.model_copy(update={'tracking_state': status.tracking_state,
                                         'link_state': status.link_state.value}).model_dump(mode='json')


def install_product_resources(app, runner):
    """Explicit resource routes; never expose arbitrary method or shell dispatch."""
    from fastapi import HTTPException

    application = ReflectApplication(runner.db_path)
    resources = ProductResources(application, runner)

    @app.post('/api/context')
    def context(request: ContextRequest):
        return resources.start_task(request)

    @app.post('/api/answers')
    def answer(request: ContextRequest):
        with resources.context() as service:
            return service.ask(request.question, path=Path(request.path),
                               task_file=Path(request.task_file) if request.task_file else None).model_dump(mode='json')

    @app.post('/api/task-runs/{task_id}/completion')
    def complete(task_id: str, request: CompletionRequest):
        with application.read():
            arguments = request.model_dump(mode='json')
            arguments['summary_redacted'] = arguments.pop('summary')
            result = resources.inbox.accept(task_id, 'complete', arguments)
            runner.jobs.submit('task_projection', {})
            status = resources.inbox.status(task_id)
            return {**status.model_dump(mode='json'), 'idempotent': result['idempotent'],
                    'linked_to_session': status.link_state == 'linked'}

    @app.post('/api/task-runs/{task_id}/milestones')
    def milestone(task_id: str, request: MilestoneRequest):
        with application.read():
            result = resources.inbox.accept(task_id, 'milestone', request.model_dump(mode='json'))
            runner.jobs.submit('task_projection', {})
            return result

    @app.get('/api/task-runs/{task_id}')
    def task_status(task_id: str):
        with application.read():
            return resources.inbox.status(task_id).model_dump(mode='json')

    @app.get('/api/demo/improvements')
    def demo_improvements(observation_id: str | None = None):
        import tempfile
        from contextlib import closing
        from importlib import resources as package_resources

        from reflect.improvements.scope import ImprovementScopeResolver
        from reflect.improvements.service import ImprovementService
        from reflect.preparation_pipeline import prepare_sql_report_db
        from reflect.store.sqlite import connect_sqlite
        with tempfile.TemporaryDirectory(prefix='reflect-demo-') as directory:
            database = Path(directory) / 'reflect.db'
            traces = Path(str(package_resources.files('reflect') / 'data/demo-traces.json'))
            prepare_sql_report_db(database, otlp_traces=traces, include_native_sessions=False)
            with closing(connect_sqlite(database)) as conn:
                service = ImprovementService(conn)
                scope = ImprovementScopeResolver(conn).global_period('all')
                result = service.improve(observation_id, refresh=False, scope=scope)
                return result.model_dump(mode='json')

    @app.get('/api/improvements')
    def improvements(path: str | None = None, session_id: str | None = None,
                     global_scope: bool = False, period: str | None = None,
                     limit: int = 20, offset: int = 0, detail: str = 'summary', evidence_limit: int = 10):
        with resources.context() as service:
            return service.improvements_summary(path=Path(path) if path else None, session_id=session_id,
                global_scope=global_scope, period=period, limit=limit, offset=offset,
                detail=detail, evidence_limit=evidence_limit)

    @app.get('/api/skill-search')
    def skill_search(query: str = '', lifecycle: SkillLifecycleState | None = None,
                     availability: SkillAvailability = SkillAvailability.ANY,
                     source_agent: str | None = None, minimum_evidence: int = 0, limit: int = 20):
        with resources.context() as service:
            return service.skills_search(query=query, lifecycle=lifecycle, availability=availability,
                source_agent=source_agent, minimum_evidence=minimum_evidence, limit=limit).model_dump(mode='json')

    @app.get('/api/patterns')
    def patterns(pattern_type: PatternType = PatternType.ALL, query: str = '',
                 workflow_status: WorkflowStatus | None = None, loop_kind: LoopKind | None = None,
                 loop_status: LoopStatus | None = None, limit: int = 20):
        with resources.context() as service:
            return service.patterns(pattern_type=pattern_type, query=query, workflow_status=workflow_status,
                loop_kind=loop_kind, loop_status=loop_status, limit=limit).model_dump(mode='json')

    @app.get('/api/impact-checks')
    def impact_checks(impact_id: str | None = None, workflow_id: str | None = None, limit: int = 20):
        with resources.context() as service:
            return service.impact(impact_id=impact_id, workflow_id=workflow_id, limit=limit)

    @app.get('/api/guidance-efficiency')
    def guidance(repo_id: str, task_archetype_id: str, model: str, limit: int = 100):
        with resources.context() as service:
            return service.guidance_efficiency(repo_id=repo_id, task_archetype_id=task_archetype_id,
                                               model=model, limit=limit)

    @app.get('/api/entities/{entity_id}')
    def explain(entity_id: str):
        with resources.context() as service:
            return service.explain(entity_id)

    @app.post('/api/usage')
    def usage(request: UsageRequest):
        if not request.session_id and not request.global_scope:
            raise ValueError('Pass session_id or select global_scope explicitly')
        with resources.context() as service:
            from reflect.store.rollups import UsageRollupReadinessProbe
            reasons = UsageRollupReadinessProbe(session_ids=[request.session_id] if request.session_id else None).inspect(service.conn)
            if reasons:
                raise ValueError('; '.join(reasons))
            return service.usage_report(**request.model_dump())

    @app.post('/api/change-reviews')
    def review(request: ReviewRequest):
        with application.read() as reader:
            try:
                candidate_id = reader.workflows.show(request.entity_id).id
            except KeyError:
                candidate_id = reader.skills.workflow_candidate_for(request.entity_id)
        with application.workflow_action(candidate_id, action_kind='rollback' if request.action == ChangeAction.ROLLBACK_WORKFLOW else 'review') as improvement:
            service = ReflectContextService(improvement.conn, initialize_schema=False)
            return service.review_change(**{**request.model_dump(),
                'project_root': Path(request.project_root) if request.project_root else None}).model_dump(mode='json')

    @app.post('/api/change-reviews/apply')
    def apply_review(request: ApplyReviewRequest):
        with application.state_action() as improvement:
            service = ReflectContextService(improvement.conn, initialize_schema=False)
            return service.apply_change(request.approval_token).model_dump(mode='json')

    @app.get('/api/workflows/{candidate_id}')
    def workflow(candidate_id: str):
        with application.read() as service:
            try:
                return service.workflows.show(candidate_id).model_dump(mode='json')
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from exc

    from fastapi import Request
    from fastapi.responses import JSONResponse

    from reflect.backend_operations import (
        BuildArguments,
        CostArguments,
        DiscoveryArguments,
        MemoryArguments,
        RefreshArguments,
        SyncArguments,
    )
    globals().update(Request=Request, BuildArguments=BuildArguments, CostArguments=CostArguments,
                     DiscoveryArguments=DiscoveryArguments, MemoryArguments=MemoryArguments,
                     RefreshArguments=RefreshArguments, SyncArguments=SyncArguments)

    def submitted(kind, arguments, request):
        job = runner.jobs.submit(kind, arguments, idempotency_key=request.headers.get('idempotency-key'))
        return JSONResponse(job.response(), status_code=202)

    # Replace the old configuration-only refresh route, keeping one canonical route.
    app.router.routes[:] = [route for route in app.router.routes if getattr(route, 'path', None) != '/api/refresh']

    @app.post('/api/refresh')
    def refresh(request: Request, body: RefreshArguments | None = None):
        if body is None and runner.refresh_arguments is None:
            raise HTTPException(409, 'Refresh is disabled for this server')
        arguments = body.model_dump() if body else runner.refresh_arguments
        return submitted('refresh', arguments, request)

    @app.post('/api/skills/discover')
    def discover(request: Request, body: DiscoveryArguments):
        arguments = body.model_dump()
        dependency = None
        explicit_source = any((body.demo, body.otlp_traces, body.sessions_dir, body.spans_dir))
        source_refresh = RefreshArguments(
            otlp_traces=body.otlp_traces, spans_dir=body.spans_dir,
            include_native_sessions=not explicit_source).model_dump()
        if body.refresh:
            dependency = ('refresh', source_refresh)
        elif application.publications.current() is None:
            dependency = ('refresh', source_refresh if explicit_source else runner.refresh_arguments or
                          RefreshArguments(include_native_sessions=False).model_dump())
        job = runner.jobs.submit('skill_discovery', arguments,
                                 idempotency_key=request.headers.get('idempotency-key'), dependency=dependency)
        return JSONResponse(job.response(), status_code=202)

    @app.post('/api/loops/{loop_id}/build')
    def build(loop_id: str, request: Request, body: BuildArguments):
        return submitted('loop_build', {**body.model_dump(), 'loop_id': loop_id}, request)

    @app.post('/api/skills/sync')
    def sync(request: Request, body: SyncArguments):
        return submitted('skills_sync', body.model_dump(), request)

    @app.post('/api/maintenance/cost')
    def cost(request: Request, body: CostArguments):
        return submitted('cost_repair', body.model_dump(), request)

    @app.post('/api/memories/sync')
    def memory_sync(request: Request, body: MemoryArguments):
        if not body.path:
            raise HTTPException(422, 'A local path is required')
        return submitted('memory_sync', body.model_dump(), request)

    @app.post('/api/memories/validate')
    def memory_validate(request: Request, body: MemoryArguments):
        if not body.memory_id and not body.candidate_id:
            raise HTTPException(422, 'A memory or candidate identifier is required')
        return submitted('memory_validate', body.model_dump(), request)

    @app.post('/api/memories/{memory_id}/forget')
    def memory_forget(memory_id: str, request: Request):
        return submitted('memory_forget', {'memory_id': memory_id}, request)

    @app.get('/api/memories/providers')
    def memory_providers():
        with resources.context() as service:
            return service.memory.provider_health()

    @app.get('/api/memories')
    def memories(path: str | None = None, all_memories: bool = False, type: str | None = None,
                 scope: str | None = None, source: str | None = None, provider: str | None = None,
                 stale: bool = False, validated: bool = False, unvalidated: bool = False, limit: int = 100):
        filters = {k:v for k,v in {'type':type, 'scope':scope, 'source':source, 'provider':provider,
                                  'stale':stale, 'validated':validated, 'unvalidated':unvalidated}.items() if v}
        with resources.context() as service:
            return service.memory.list_memories(path=Path(path) if path else None, all_memories=all_memories,
                                                filters=filters, limit=limit)

    @app.get('/api/memories/search')
    def memory_search(query: str, path: str | None = None, provider: str = 'local_sqlite',
                      type: str | None = None, scope: str | None = None, source: str | None = None, limit: int = 20):
        with resources.context() as service:
            return service.memory.search(query, path=Path(path) if path else None, provider=provider,
                                         filters={k:v for k,v in {'type':type,'scope':scope,'source':source}.items() if v}, limit=limit)

    @app.get('/api/memories/candidates')
    def memory_candidates(path: str, session_id: str = '', limit: int = 50):
        with resources.context() as service:
            return service.memory.candidates(path=Path(path), session_id=session_id, limit=limit, generate=False)

    @app.get('/api/memories/{memory_id}')
    def memory_detail(memory_id: str):
        with resources.context() as service:
            return service.memory.inspect(memory_id)

    @app.post('/api/workflow-imports')
    def import_workflow(body: WorkflowImportRequest):
        session_ids = []
        if body.source_workflow_id:
            with application.read() as service:
                ledger = service.repository.workflow_evidence_ledger(body.source_workflow_id, limit=200)
                session_ids = [item.session_id for item in ledger.provenance_sessions]
        with application.state_action() as service:
            skill = {**body.skill, **({"source_kind": "agent_authored"} if body.source_agent else {}),
                     **({"source_workflow_id": body.source_workflow_id} if body.source_workflow_id else {})}
            candidate_id = service.stage_extracted_skills([skill], session_ids=session_ids,
                                                          source_agent=body.source_agent)[0]
            service.conn.execute('INSERT OR IGNORE INTO workflow_review_ownership(candidate_id,evidence_revision_hash) '
                                 'SELECT id,revision_hash FROM workflow_candidates WHERE id=?', (candidate_id,))
            return {'id': candidate_id, 'installed': False}

    @app.post('/api/skills/{skill_id}/apply')
    def apply_skill(skill_id: str, body: ProjectRequest):
        from reflect.dashboard_improvements import DashboardImprovementAdapter
        with application.read() as service:
            candidate_id = service.skills.workflow_candidate_for(skill_id)
        return DashboardImprovementAdapter(runner.db_path).apply_workflow(candidate_id, project_root=Path(body.project_root))

    @app.post('/api/skills/{skill_id}/rollback')
    def rollback_skill(skill_id: str):
        from reflect.dashboard_improvements import DashboardImprovementAdapter
        with application.read() as service:
            candidate_id = service.skills.workflow_candidate_for(skill_id)
        return DashboardImprovementAdapter(runner.db_path).rollback_workflow(candidate_id)

    from reflect.backend_operations import DatabaseArguments
    globals()['DatabaseArguments'] = DatabaseArguments

    @app.post('/api/maintenance/database/{operation}')
    def database_maintenance(operation: Literal['ingest','normalize','graph','rollups','migrate','prune'], request: Request, body: DatabaseArguments):
        return submitted('database_' + operation, body.model_dump(), request)

    @app.post('/api/capture/status')
    def capture_status(body: CaptureStatusRequest):
        from dataclasses import asdict

        from reflect.store.doctor import inspect_segment_preparation
        return asdict(inspect_segment_preparation(runner.db_path, [Path(path) for path in body.closed_paths]))

    @app.get('/api/database/health')
    def database_health():
        import sqlite3
        from contextlib import closing

        from reflect.store.doctor import inspect_database
        from reflect.store.sqlite import connect_sqlite_read_only
        connection = connect_sqlite_read_only(runner.db_path, profile=None) if runner.db_path.exists() else sqlite3.connect(':memory:')
        with closing(connection) as conn:
            return inspect_database(conn)

    @app.get('/api/delivery')
    def delivery():
        with application.read() as service:
            rows = service.conn.execute("""SELECT COALESCE(
                NULLIF(json_extract(attrs_json, '$."gen_ai.client.name"'), ''),
                NULLIF(json_extract(attrs_json, '$."agent.name"'), ''),
                NULLIF(json_extract(attrs_json, '$."service.name"'), '')) AS agent,
                MAX(observed_at), COUNT(*) FROM raw_events WHERE source_type NOT LIKE 'native_%'
                GROUP BY agent HAVING agent IS NOT NULL AND agent <> ''""").fetchall()
            from reflect.agent_capabilities import normalize_agent_key
            return {normalize_agent_key(agent): [str(observed or ''), int(count)] for agent, observed, count in rows}

    @app.get('/api/completions/{entity}')
    def completions(entity: str, prefix: str = '', limit: int = 100):
        from reflect.completion_queries import CompletionCatalog
        if entity not in CompletionCatalog._queries:
            raise HTTPException(404, 'Unknown completion resource')
        with application.read() as service:
            return CompletionCatalog(limit=limit).complete(entity, prefix, service.conn)


class WorkflowImportRequest(RequestModel):
    skill: dict
    source_agent: str | None = None
    source_workflow_id: str | None = None


class ProjectRequest(RequestModel):
    project_root: str
