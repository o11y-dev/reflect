"""Explicit in-process HTTP wiring for product transport tests.

Callers prepare and publish their own evidence. No daemon, refresh, migration,
readiness override, or automatic publication is performed here.
"""
import httpx

from reflect.backend_api import install_backend
from reflect.backend_client import BackendClient, BackendEndpoint
from reflect.backend_lifecycle import BackendLocator
from reflect.backend_runtime import BackendJobRunner
from reflect.dashboard_server import build_dashboard_app


def bind_backend(monkeypatch, db_path, *, refresh_arguments=None):
    runner = BackendJobRunner(db_path, refresh_arguments=refresh_arguments)
    runner.jobs.initialize()
    app = build_dashboard_app(docs_dir=db_path.parent, db_path=db_path)
    install_backend(app, runner, token='isolated-test', port=8765)
    endpoint = BackendEndpoint('http://127.0.0.1:8765', 'isolated-test', str(db_path))
    transport = httpx.ASGITransport(app=app)
    async def ensure(self):
        assert self.db_path == db_path.resolve()
        return endpoint
    monkeypatch.setattr(BackendLocator, 'ensure', ensure)
    original = BackendClient.__init__
    def initialize(self, target, **kwargs):
        original(self, target, transport=transport)
    monkeypatch.setattr(BackendClient, '__init__', initialize)
    return runner


def publish_actions(db_path):
    """Complete explicitly seeded evidence and cut over its human action records."""
    from contextlib import closing

    from reflect.backend_runtime import action_store_path
    from reflect.store.snapshots import PublishedSnapshotStore
    from reflect.store.sqlite import connect_sqlite, mark_snapshot
    from reflect.store.user_state import UserStateStore
    from reflect.store.user_state_migration import UserStateMigration
    with closing(connect_sqlite(db_path)) as conn:
        mark_snapshot(conn, ready=True)
    UserStateMigration(db_path, UserStateStore(action_store_path(db_path))).run()
    return PublishedSnapshotStore(db_path, reserve_bytes=0).publish(evidence_revision='fixture')


def execute_job(runner, job_id):
    """Run one explicitly submitted fixture job without a daemon."""
    from reflect.backend_runtime import _execute_job
    runner.jobs.claim(job_id, owner='fixture')
    _execute_job(runner.db_path, runner.jobs.path, job_id, 'fixture')
    job = runner.jobs.get(job_id)
    assert job.state == 'succeeded', job.error
    return job.result


def complete_submission(backend, result):
    """Execute the job returned by one successful CLI submission in a test."""
    import json
    assert result.exit_code == 0, result.output
    job_id = json.loads(result.stdout)['job_id'] if result.stdout.startswith('{') else result.stdout.split()[0]
    result.job_result = execute_job(backend, job_id)
    return result
