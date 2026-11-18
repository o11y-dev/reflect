import asyncio
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner
from test_dashboard_sql_api import _seed_sql_report_db

from reflect.backend_api import install_backend
from reflect.backend_client import BackendClient, BackendEndpoint
from reflect.backend_lifecycle import BackendLocator
from reflect.backend_runtime import BackendJobRunner, action_store_path
from reflect.core import main
from reflect.dashboard_server import build_dashboard_app
from reflect.jobs import JobRegistry
from reflect.store.snapshots import PublishedSnapshotStore
from reflect.store.sqlite import connect_sqlite, mark_snapshot
from reflect.store.user_state import UserStateStore
from reflect.store.user_state_migration import UserStateMigration


@pytest.fixture
def product_backend(tmp_path, monkeypatch):
    db = tmp_path / 'reflect.db'
    _seed_sql_report_db(db)
    UserStateMigration(db, UserStateStore(action_store_path(db))).run()
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=True)
    PublishedSnapshotStore(db, reserve_bytes=0).publish(evidence_revision='first')
    runner = BackendJobRunner(db, refresh_arguments={'otlp_traces': None, 'include_native_sessions': False})
    runner.jobs.initialize()
    app = build_dashboard_app(docs_dir=tmp_path, db_path=db)
    install_backend(app, runner, token='test', port=8765)
    transport = httpx.ASGITransport(app=app)
    endpoint = BackendEndpoint('http://127.0.0.1:8765', 'test', str(db))
    async def ensure(self):
        assert self.db_path == db.resolve()
        return endpoint
    monkeypatch.setattr(BackendLocator, 'ensure', ensure)
    original = BackendClient.__init__
    def client_init(self, target, **kwargs):
        original(self, target, transport=transport)
    monkeypatch.setattr(BackendClient, '__init__', client_init)
    monkeypatch.setenv('REFLECT_DB_PATH', str(db))
    return db, runner, BackendClient(endpoint)


def test_usage_cli_mcp_and_api_share_evidence_during_writer(product_backend):
    from reflect.mcp import reflect_usage
    db, _, client = product_backend
    with closing(connect_sqlite(db)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        expected = asyncio.run(client.request('POST', '/api/usage', body={'global_scope': True, 'period': 'all'}))
        mcp = asyncio.run(reflect_usage(global_scope=True, period='all'))
        cli = CliRunner().invoke(main, ['usage', '--global', '--period', 'all', '--json', '--db-path', str(db)])
        assert cli.exit_code == 0, cli.output
        import json
        actual = json.loads(cli.output)
        assert actual['totals'] == expected['totals'] == mcp['totals']
        writer.rollback()


def test_refresh_cli_waits_for_durable_job_by_default(product_backend, monkeypatch):
    from reflect.mcp import reflect_jobs
    from reflect.backend_runtime import _execute_job
    db, runner, _ = product_backend
    spool = db.parent / 'otlp'
    spool.mkdir()
    traces = spool / 'otel-traces.active.jsonl'
    traces.touch()
    spans = db.parent / 'spans'
    spans.mkdir()

    def execute_waiting_job(_db, job_id):
        assert runner.jobs.claim(job_id, owner='test')
        _execute_job(db, runner.jobs.path, job_id, 'test')
        return runner.jobs.get(job_id).response()

    monkeypatch.setattr('reflect.cli.backend.wait_for_job', execute_waiting_job)
    result = CliRunner().invoke(main, [
        'refresh', '--no-native-sessions', '--json', '--db-path', str(db),
        '--otlp-traces', str(traces), '--spans-dir', str(spans),
    ])
    assert result.exit_code == 0, result.output
    import json
    payload = json.loads(result.output)
    assert payload['state'] == 'succeeded'
    assert JobRegistry(runner.jobs.path).get(payload['job_id']).kind == 'refresh'
    inspected = asyncio.run(reflect_jobs(payload['job_id']))
    assert inspected['job_id'] == payload['job_id']
    assert inspected['state'] == 'succeeded'


def test_refresh_cli_can_return_queued_job_without_waiting(product_backend):
    from reflect.mcp import reflect_jobs
    db, runner, _ = product_backend
    result = CliRunner().invoke(main, ['refresh', '--no-wait', '--no-native-sessions', '--json', '--db-path', str(db)])
    assert result.exit_code == 0, result.output
    import json
    payload = json.loads(result.output)
    assert payload['state'] == 'queued'
    assert JobRegistry(runner.jobs.path).get(payload['job_id']).kind == 'refresh'
    inspected = asyncio.run(reflect_jobs(payload['job_id']))
    assert inspected['state'] == 'queued'


def test_invalid_long_operation_arguments_never_queue(product_backend):
    db, runner, client = product_backend
    from reflect.backend_client import BackendError
    with pytest.raises(BackendError) as exc:
        asyncio.run(client.request('POST', '/api/refresh', body={'execute': 'arbitrary shell'}))
    assert exc.value.status_code == 422
    assert runner.jobs.list() == []


def test_product_clients_do_not_open_sqlite():
    import ast
    root = Path(__file__).parents[1] / 'src/reflect'
    paths = [root / name for name in ('core.py', 'mcp.py', 'shell_completion.py')]
    paths.extend((root / 'cli').glob('*.py'))
    prohibited = ('sqlite3', 'reflect.store', 'reflect.application', 'reflect.preparation_pipeline')
    for path in paths:
        tree = ast.parse(path.read_text())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imports.append(node.module or '')
            elif isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
        assert not [module for module in imports if module.startswith(prohibited)], path



def test_created_workflow_can_be_applied_then_measured_after_refresh(product_backend, tmp_path):
    import subprocess

    from reflect.backend_runtime import _execute_job
    db, runner, client = product_backend
    root = tmp_path / 'project'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    created = asyncio.run(client.request('POST', '/api/workflows', body={'content': {
        'slug':'measure-saved-work', 'description':'Verify changed work', 'steps':['Run checks'],
        'verification':['Tests pass'], 'abstain_when':[]}}))
    with closing(connect_sqlite(db)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        applied = asyncio.run(client.request('POST', f"/api/workflows/{created['id']}/apply", body={'project_root':str(root)}))
        assert Path(applied['target_path']).exists()
        writer.rollback()
    job = runner.jobs.submit('refresh', {'otlp_traces': None, 'include_native_sessions': False})
    runner.jobs.claim(job.job_id, owner='test-writer')
    _execute_job(db, runner.jobs.path, job.job_id, 'test-writer')
    assert runner.jobs.get(job.job_id).state == 'succeeded', runner.jobs.get(job.job_id).error
    payload = asyncio.run(client.request('GET', '/api/impact-checks', params={'workflow_id': created['id']}))
    assert payload['count'] >= 1
    assert all(check['verdict'] != 'improved' for check in payload['impact_checks'])


def test_task_events_remain_durable_until_published(product_backend, tmp_path):
    from reflect.backend_runtime import _execute_job
    from reflect.mcp import reflect_complete, reflect_context, reflect_task_status
    from reflect.task_inbox import TaskEventInbox
    db, runner, _ = product_backend
    started = asyncio.run(reflect_context('Inspect current source and verify changes', path=str(tmp_path)))
    task_id = started['task_run_id']
    with closing(connect_sqlite(db)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        completed = asyncio.run(reflect_complete(task_id, outcome='success', verification_passed=True))
        assert completed['tracking_state'] == 'accepted_pending'
        writer.rollback()
    inbox = TaskEventInbox(db)
    inbox.drain()
    assert inbox._path(task_id).exists()
    assert asyncio.run(reflect_task_status(task_id))['tracking_state'] == 'accepted_pending'
    job = runner.jobs.queued()
    runner.jobs.claim(job.job_id, owner='projector')
    _execute_job(db, runner.jobs.path, job.job_id, 'projector')
    assert runner.jobs.get(job.job_id).state == 'succeeded', runner.jobs.get(job.job_id).error
    assert not inbox._path(task_id).exists()
    assert asyncio.run(reflect_task_status(task_id))['status'] == 'completed'


def test_mcp_reviewed_rollback_of_active_workflow(product_backend, tmp_path):
    import subprocess

    from reflect.mcp import reflect_apply_change, reflect_review_change
    db, _, client = product_backend
    root = tmp_path / 'rollback-project'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    created = asyncio.run(client.request('POST', '/api/workflows', body={'content': {
        'slug':'review-rollback', 'description':'Review rollback', 'steps':['Verify rollback'],
        'verification':[], 'abstain_when':[]}}))
    applied = asyncio.run(client.request('POST', f"/api/workflows/{created['id']}/apply", body={'project_root':str(root)}))
    review = asyncio.run(reflect_review_change('rollback_workflow', created['id'], project_root=str(root)))
    assert review['action'] == 'rollback_workflow'
    assert review['target_path'] == applied['target_path']
    rolled_back = asyncio.run(reflect_apply_change(review['approval_token']))
    assert rolled_back['state'] == 'applied'
    assert not Path(applied['target_path']).exists()


def test_successive_task_projections_publish_each_receipt(product_backend, tmp_path):
    from reflect.backend_runtime import _execute_job
    from reflect.mcp import reflect_complete, reflect_context, reflect_task_status
    from reflect.task_inbox import TaskEventInbox
    db, runner, _ = product_backend
    started = asyncio.run(reflect_context('Verify task projection generations', path=str(tmp_path)))
    first = runner.jobs.queued()
    runner.jobs.claim(first.job_id, owner='projector')
    _execute_job(db, runner.jobs.path, first.job_id, 'projector')
    generation = PublishedSnapshotStore(db).current().generation
    asyncio.run(reflect_complete(started['task_run_id'], outcome='success', verification_passed=True))
    second = runner.jobs.queued()
    runner.jobs.claim(second.job_id, owner='projector')
    _execute_job(db, runner.jobs.path, second.job_id, 'projector')
    assert runner.jobs.get(second.job_id).state == 'succeeded', runner.jobs.get(second.job_id).error
    assert PublishedSnapshotStore(db).current().generation != generation
    assert not TaskEventInbox(db)._path(started['task_run_id']).exists()
    assert asyncio.run(reflect_task_status(started['task_run_id']))['status'] == 'completed'


def test_completion_arriving_after_drain_gets_successor(product_backend, tmp_path, monkeypatch):
    from reflect.backend_runtime import _execute_job
    from reflect.mcp import reflect_complete, reflect_context, reflect_task_status
    from reflect.task_inbox import TaskEventInbox
    db, runner, _ = product_backend
    started = asyncio.run(reflect_context('Task projection race', path=str(tmp_path)))
    original = TaskEventInbox.drain
    injected = False
    def drain(inbox, **kwargs):
        nonlocal injected
        original(inbox, **kwargs)
        if not injected:
            injected = True
            asyncio.run(reflect_complete(started['task_run_id'], outcome='success'))
    monkeypatch.setattr(TaskEventInbox, 'drain', drain)
    first = runner.jobs.queued()
    runner.jobs.claim(first.job_id, owner='projector')
    _execute_job(db, runner.jobs.path, first.job_id, 'projector')
    successor = runner.jobs.queued()
    assert successor is not None and successor.job_id != first.job_id
    runner.jobs.claim(successor.job_id, owner='projector')
    _execute_job(db, runner.jobs.path, successor.job_id, 'projector')
    assert runner.jobs.get(successor.job_id).state == 'succeeded'
    assert asyncio.run(reflect_task_status(started['task_run_id']))['status'] == 'completed'
    assert not TaskEventInbox(db)._path(started['task_run_id']).exists()


def test_projection_backlog_over_batch_limit_finishes(product_backend, tmp_path):
    from uuid import uuid4

    from reflect.application import ReflectApplication
    from reflect.backend_resources import ProductResources
    from reflect.backend_runtime import _execute_job
    from reflect.task_inbox import TaskEventInbox
    db, runner, _ = product_backend
    resources = ProductResources(ReflectApplication(db), runner)
    with resources.context() as service:
        _, start = service.prepare_task(question='Batch projection', path=tmp_path)
    inbox = TaskEventInbox(db)
    identifiers = []
    for _ in range(101):
        identifier = 'mcp_task_' + uuid4().hex
        identifiers.append(identifier)
        inbox.start(start.model_copy(update={'task_run_id': identifier}))
    runner.jobs.submit('task_projection', {})
    for _ in range(2):
        job = runner.jobs.queued()
        assert job is not None
        runner.jobs.claim(job.job_id, owner='projector')
        _execute_job(db, runner.jobs.path, job.job_id, 'projector')
        assert runner.jobs.get(job.job_id).state == 'succeeded', runner.jobs.get(job.job_id).error
    assert runner.jobs.queued() is None
    assert inbox.health()['pending_count'] == 0
    assert all(not inbox._path(identifier).exists() for identifier in identifiers)


def test_product_usage_never_infers_backend_session(product_backend, monkeypatch):
    from reflect.backend_client import BackendError
    _, _, client = product_backend
    monkeypatch.setenv('CODEX_THREAD_ID', 'backend-is-not-the-caller')
    with pytest.raises(BackendError) as exc:
        asyncio.run(client.request('POST', '/api/usage', body={}))
    assert exc.value.status_code == 422


def test_saved_draft_is_available_to_shell_completion(product_backend):
    _, _, client = product_backend
    created = asyncio.run(client.request('POST', '/api/workflows', body={'content': {
        'slug':'completion-draft', 'description':'Saved draft', 'steps':['Inspect'],
        'verification':[], 'abstain_when':[]}}))
    matches = asyncio.run(client.request('GET', '/api/completions/workflow', params={'prefix': created['id']}))
    assert [match['value'] for match in matches] == [created['id']]


def test_demo_skill_discovery_uses_explicit_raw_evidence(product_backend, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from reflect.backend_operations import execute_agent_job
    db, _, _ = product_backend
    prompts = []
    run_kwargs = []
    def run(command, **kwargs):
        prompts.append(command[-1])
        run_kwargs.append(kwargs)
        return SimpleNamespace(returncode=0, stdout='[]', stderr='')
    monkeypatch.setattr('reflect.backend_operations.subprocess.run', run)
    result = execute_agent_job(db, 'skill_discovery', {
        'agent':'codex', 'demo':True, 'period':'all', 'project_root': str(tmp_path),
    })
    assert prompts
    assert run_kwargs[0]['cwd'] == tmp_path
    assert result['reason'] == 'No reusable candidates supported by the evidence'


def test_improve_demo_uses_only_packaged_sources(product_backend, monkeypatch):
    from reflect import preparation_pipeline
    _, _, client = product_backend
    original = preparation_pipeline.prepare_sql_report_db
    seen = []
    def prepare(database, **kwargs):
        seen.append((database, kwargs))
        return original(database, **kwargs)
    monkeypatch.setattr(preparation_pipeline, 'prepare_sql_report_db', prepare)
    result = asyncio.run(client.request('GET', '/api/demo/improvements'))
    assert 'observations' in result
    assert seen and seen[0][1]['include_native_sessions'] is False
    assert seen[0][1]['otlp_traces'].name == 'demo-traces.json'
    assert not seen[0][0].exists()


@pytest.mark.parametrize(('arguments','title'), [
    (['improve','--global','--period','all'], 'Observed Improvements'),
    (['loops'], 'Observed Loops'),
    (['skills'], 'Skills v2'),
    (['memory','list','--all'], 'Reflect Memories'),
    (['memory','providers'], 'Memory Providers'),
])
def test_cli_default_output_remains_human_readable(product_backend, arguments, title):
    db, _, _ = product_backend
    result = CliRunner().invoke(main, [*arguments, '--db-path', str(db)])
    assert result.exit_code == 0, result.output
    assert title in result.output


def test_feedback_is_durable_during_writer_and_projects_once(product_backend):
    from reflect.backend_runtime import _execute_job
    db, runner, client = product_backend
    with closing(connect_sqlite(db)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        feedback = asyncio.run(client.request('POST', '/api/feedback/sess-sql',
                               body={'outcome': 'corrected', 'reason': 'Needs a scoped check'}))
        assert feedback['tracking_state'] == 'accepted_pending'
        assert writer.execute("SELECT COUNT(*) FROM session_outcomes WHERE source='operator_feedback'").fetchone()[0] == 0
        writer.rollback()
    for number in range(2):
        job = runner.jobs.get(feedback['job_id']) if number == 0 else runner.jobs.submit('task_projection', {})
        assert runner.jobs.claim(job.job_id, owner='feedback-test')
        _execute_job(db, runner.jobs.path, job.job_id, 'feedback-test')
        assert runner.jobs.get(job.job_id).state == 'succeeded', runner.jobs.get(job.job_id).error
    with closing(connect_sqlite(db)) as conn:
        rows = conn.execute("SELECT outcome,verification_json FROM session_outcomes WHERE source='operator_feedback'").fetchall()
    assert len(rows) == 1 and rows[0][0] == 'corrected'
    assert feedback['id'] in rows[0][1]
    from reflect.store.snapshots import SnapshotReadContext
    with SnapshotReadContext(db) as read:
        assert read.connection.execute("SELECT outcome FROM session_outcomes WHERE source='operator_feedback'").fetchone()[0] == 'corrected'


def test_discovery_idempotency_replay_and_conflict(product_backend):
    from reflect.backend_client import BackendError
    _, runner, client = product_backend
    arguments = {'agent': 'codex', 'project_root': str(Path.cwd()), 'refresh': True}
    original = asyncio.run(client.request('POST', '/api/skills/discover', body=arguments, idempotency_key='discovery-once'))
    assert asyncio.run(client.request('POST', '/api/skills/discover', body=arguments, idempotency_key='discovery-once'))['job_id'] == original['job_id']
    for job in reversed(runner.jobs.list()):
        runner.jobs.claim(job.job_id, owner='fixture')
        runner.jobs.update(job.job_id, owner='fixture', state='succeeded')
    assert asyncio.run(client.request('POST', '/api/skills/discover', body=arguments, idempotency_key='discovery-once'))['job_id'] == original['job_id']
    assert len(runner.jobs.list()) == 2
    with pytest.raises(BackendError) as conflict:
        asyncio.run(client.request('POST', '/api/skills/discover', body={**arguments, 'period': 'month'}, idempotency_key='discovery-once'))
    assert conflict.value.status_code == 409
    assert len(runner.jobs.list()) == 2


def test_cold_discovery_accepts_scoped_dependency_before_evidence_exists(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from tests.backend_helpers import execute_job

    db = tmp_path / 'cold.db'
    runner = BackendJobRunner(db, refresh_arguments={'otlp_traces': '/wrong/default.jsonl', 'include_native_sessions': True})
    runner.jobs.initialize()
    app = build_dashboard_app(docs_dir=tmp_path, db_path=db)
    install_backend(app, runner, token='test', port=8765)
    client = TestClient(app, base_url='http://127.0.0.1:8765', headers={'Authorization': 'Bearer test'})
    assert client.get('/api/findings').status_code == 503
    response = client.post('/api/skills/discover', json={
        'agent': 'codex', 'project_root': str(tmp_path), 'demo': True, 'period': 'all',
    })
    assert response.status_code == 202, response.text
    job = runner.jobs.get(response.json()['job_id'])
    dependency = runner.jobs.get(job.arguments['_depends_on'])
    assert dependency.arguments['otlp_traces'] is None
    assert dependency.arguments['include_native_sessions'] is False
    assert runner.jobs.queued(agent=True) is None
    calls = []
    def agent(command, **kwargs):
        assert PublishedSnapshotStore(db).current() is not None
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout='[]', stderr='')
    monkeypatch.setattr('subprocess.run', agent)
    monkeypatch.setattr('reflect.preparation_pipeline._discover_rich_session_files', lambda: pytest.fail('demo must not ingest native history'))
    execute_job(runner, dependency.job_id)
    execute_job(runner, job.job_id)
    assert len(calls) == 1
    assert runner.jobs.get(job.job_id).result['candidate_ids'] == []


def test_capture_doctor_uses_backend_and_offline_status_is_unclassified(product_backend, monkeypatch, tmp_path):
    from reflect.cli.backend import inspect_segment_preparation
    db, _, client = product_backend
    segment = tmp_path / 'closed.jsonl'
    segment.write_text('{}\n')
    monkeypatch.setattr(BackendLocator, 'existing', lambda self: client.endpoint)
    status = inspect_segment_preparation(db, [segment])
    assert status.store_available is True
    assert status.pending_count == 1
    assert status.unclassified_count == 0
    monkeypatch.setattr(BackendLocator, 'existing', lambda self: None)
    offline = inspect_segment_preparation(db, [segment])
    assert offline.store_available is False
    assert offline.unclassified_count == 1
    assert offline.pending_count == 0


def test_usage_refresh_reads_the_default_hook_trace_source(product_backend, monkeypatch, tmp_path):
    import json

    from conftest import make_span, wrap_otlp

    from tests.backend_helpers import execute_job

    db, runner, _ = product_backend
    traces = tmp_path / 'hook-traces.jsonl'
    traces.write_text(wrap_otlp([make_span('Stop', session='hook-only', input_tokens=123, output_tokens=45)]) + '\n')
    monkeypatch.setattr('reflect.core.default_otlp_traces', lambda: traces)
    monkeypatch.setattr('reflect.preparation_pipeline._discover_rich_session_files', lambda: [])
    result = CliRunner().invoke(main, ['usage', '--session', 'hook-only', '--refresh', '--no-wait', '--json', '--db-path', str(db)])
    assert result.exit_code == 0, result.output
    job_id = json.loads(result.stdout)['job_id']
    execute_job(runner, job_id)
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT input_tokens,output_tokens FROM sessions WHERE id='hook-only'").fetchone() == (123, 45)
    assert runner.jobs.get(job_id).arguments['otlp_traces'] == str(traces.resolve())
    assert runner.jobs.get(job_id).arguments['native_session_ids'] == ['hook-only']


@pytest.mark.parametrize('apply_changes', [False, True])
def test_retention_preserves_unprojected_feedback_until_its_own_job(product_backend, apply_changes):
    import sqlite3

    from tests.backend_helpers import execute_job

    db, runner, client = product_backend
    feedback = asyncio.run(client.request('POST', '/api/feedback/sess-sql', body={'outcome': 'corrected'}))
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_outcomes WHERE source='operator_feedback'").fetchone()[0] == 0
    prune = asyncio.run(client.request('POST', '/api/maintenance/database/prune', body={'apply_changes': apply_changes}))
    result = execute_job(runner, prune['job_id'])
    if apply_changes:
        with sqlite3.connect(result['backup_path']) as backup:
            assert backup.execute("SELECT COUNT(*) FROM session_outcomes WHERE source='operator_feedback'").fetchone()[0] == 0
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_outcomes WHERE source='operator_feedback'").fetchone()[0] == 0
    assert runner.jobs.get(feedback['job_id']).state == 'queued'
    execute_job(runner, feedback['job_id'])
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_outcomes WHERE source='operator_feedback'").fetchone()[0] == 1
