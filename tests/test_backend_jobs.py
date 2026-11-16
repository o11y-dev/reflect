import pytest

from reflect.jobs import JobConflict, JobRegistry, JobState
from reflect.store.user_state import UserStateStore


def test_submission_join_and_restart(tmp_path):
    path = tmp_path / 'state.db'
    jobs = JobRegistry(path)
    jobs.initialize()
    first = jobs.submit('refresh', {'sources': ['native']}, idempotency_key='request')
    assert jobs.submit('refresh', {'sources': ['native']}).job_id == first.job_id
    assert jobs.submit('refresh', {'sources': ['other']}).job_id != first.job_id
    assert jobs.claim(first.job_id, owner='instance')
    assert not jobs.claim(first.job_id, owner='other')
    with pytest.raises(JobConflict):
        jobs.update(first.job_id, owner='other')
    jobs.update(first.job_id, owner='instance', state=JobState.SUCCEEDED, result={'count': 3})
    restarted = JobRegistry(path)
    restarted.initialize()
    assert restarted.get(first.job_id).result == {'count': 3}
    assert restarted.submit('refresh', {'sources': ['native']}, idempotency_key='request').job_id == first.job_id
    with pytest.raises(JobConflict):
        restarted.submit('refresh', {}, idempotency_key='request')


def test_jobs_available_before_cutover_and_do_not_change_material_revision(tmp_path):
    path = tmp_path / 'state.db'
    registry = JobRegistry(path)
    registry.initialize()
    job = registry.submit('refresh', {})
    UserStateStore(path).initialize(migrating=True)
    assert registry.get(job.job_id).state == JobState.QUEUED
    assert registry.claim(job.job_id, owner='old')
    registry.update(job.job_id, owner='old', progress={'stage': 'ingest'})
    assert registry.interrupt_owner('old') == 1
    assert registry.get(job.job_id).state == JobState.INTERRUPTED
    import sqlite3
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM user_state_metadata WHERE key='material_revision'").fetchone() == (0,)


def test_joined_submission_key_remains_bound_after_job_finishes(tmp_path):
    jobs = JobRegistry(tmp_path / 'state.db')
    jobs.initialize()
    first = jobs.submit('refresh', {}, idempotency_key='first')
    joined = jobs.submit('refresh', {}, idempotency_key='second')
    assert joined.job_id == first.job_id
    jobs.claim(first.job_id, owner='worker')
    jobs.update(first.job_id, owner='worker', state=JobState.SUCCEEDED)
    assert jobs.submit('refresh', {}, idempotency_key='second').job_id == first.job_id


def test_worker_publishes_completed_evidence_and_preserves_jobs_across_cutover(tmp_path, monkeypatch):
    from test_dashboard_sql_api import _seed_sql_report_db

    from reflect import preparation_pipeline
    from reflect.backend_runtime import _execute_job, action_store_path
    from reflect.store.snapshots import PublishedSnapshotStore
    from reflect.store.sqlite import connect_sqlite, mark_snapshot

    path = tmp_path / 'reflect.db'
    _seed_sql_report_db(path)
    conn = connect_sqlite(path)
    mark_snapshot(conn, ready=True)
    conn.close()
    registry = JobRegistry(action_store_path(path))
    registry.initialize()
    job = registry.submit('refresh', {})
    registry.claim(job.job_id, owner='worker')
    monkeypatch.setattr(preparation_pipeline, 'prepare_sql_report_db',
                        lambda *args, **kwargs: {'changed_sessions': 1})
    _execute_job(path, registry.path, job.job_id, 'worker')
    completed = registry.get(job.job_id)
    assert completed.state == JobState.SUCCEEDED, completed.error
    assert completed.result['publication']['generation'] == PublishedSnapshotStore(path).current().generation
    conn = connect_sqlite(path)
    try:
        assert conn.execute("SELECT value FROM store_metadata WHERE key='user_state_cutover_v1'").fetchone()
    finally:
        conn.close()


def test_runtime_reconciles_dead_owners_but_preserves_live_workers_and_queue(tmp_path):
    from reflect.backend_runtime import BackendJobRunner, _owner_lease

    runner = BackendJobRunner(tmp_path / 'reflect.db')
    runner.jobs.initialize()
    abandoned = runner.jobs.submit('refresh', {'source': 'a'})
    live = runner.jobs.submit('refresh', {'source': 'b'})
    queued = runner.jobs.submit('refresh', {'source': 'c'})
    runner.jobs.claim(abandoned.job_id, owner='dead')
    runner.jobs.claim(live.job_id, owner='live')
    with _owner_lease(runner.jobs.path, 'live'):
        runner._recover_abandoned()
        assert runner.jobs.get(live.job_id).state == JobState.RUNNING
    assert runner.jobs.get(abandoned.job_id).state == JobState.INTERRUPTED
    assert runner.jobs.get(queued.job_id).state == JobState.QUEUED
    runner._recover_abandoned()
    assert runner.jobs.get(live.job_id).state == JobState.INTERRUPTED


def _crashing_worker(db_path, state_path, job_id, owner):
    import os

    jobs = JobRegistry(state_path)
    if jobs.get(job_id).kind == 'crash':
        os._exit(17)
    jobs.update(job_id, owner=owner, state=JobState.SUCCEEDED, result={'completed': True})


def test_dead_process_does_not_poison_following_jobs(tmp_path, monkeypatch):
    import asyncio

    from reflect import backend_runtime

    monkeypatch.setattr(backend_runtime, '_execute_job', _crashing_worker)
    runner = backend_runtime.BackendJobRunner(tmp_path / 'reflect.db')
    runner.jobs.initialize()
    first = runner.jobs.submit('crash', {})
    second = runner.jobs.submit('refresh', {})

    async def run():
        await runner.start()
        try:
            async with asyncio.timeout(15):
                while runner.jobs.get(second.job_id).state == JobState.QUEUED or runner.jobs.get(second.job_id).state == JobState.RUNNING:
                    await asyncio.sleep(.05)
            assert runner.jobs.get(first.job_id).state == JobState.INTERRUPTED
            assert runner.jobs.get(second.job_id).state == JobState.SUCCEEDED
        finally:
            await runner.close()
    asyncio.run(run())


def test_real_refresh_can_run_again_after_user_state_cutover(tmp_path):
    from test_dashboard_sql_api import _seed_sql_report_db

    from reflect.backend_runtime import _execute_job, action_store_path

    path = tmp_path / 'reflect.db'
    _seed_sql_report_db(path)
    registry = JobRegistry(action_store_path(path))
    registry.initialize()
    for number in range(2):
        job = registry.submit('refresh', {'otlp_traces': None})
        registry.claim(job.job_id, owner=f'worker{number}')
        _execute_job(path, registry.path, job.job_id, f'worker{number}')
        assert registry.get(job.job_id).state == JobState.SUCCEEDED, registry.get(job.job_id).error


def test_backend_lifetime_owner_rejects_second_instance(tmp_path):
    import asyncio

    from reflect.backend_runtime import BackendJobRunner
    async def exercise():
        first = BackendJobRunner(tmp_path/'reflect.db')
        second = BackendJobRunner(tmp_path/'reflect.db')
        await first.start()
        try:
            with pytest.raises(RuntimeError, match='Another backend'):
                await second.start()
        finally:
            await first.close()
        await second.start()
        await second.close()
    asyncio.run(exercise())


def test_snapshot_only_start_publishes_existing_completed_evidence(tmp_path):
    import asyncio
    from contextlib import closing

    from test_dashboard_sql_api import _seed_sql_report_db

    from reflect.backend_runtime import BackendJobRunner
    from reflect.store.snapshots import PublishedSnapshotStore
    from reflect.store.sqlite import connect_sqlite, mark_snapshot

    db = tmp_path / 'reflect.db'
    _seed_sql_report_db(db)
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=True)
    async def exercise():
        runner = BackendJobRunner(db)
        await runner.start()
        try:
            for _ in range(100):
                if PublishedSnapshotStore(db).current():
                    break
                await asyncio.sleep(0.05)
            assert PublishedSnapshotStore(db).current() is not None
            assert runner.jobs.list()[0].kind == 'publish'
        finally:
            await runner.close()
    asyncio.run(exercise())


def test_agent_dependencies_wait_and_fail_without_execution(tmp_path):
    registry = JobRegistry(tmp_path / 'jobs.db')
    registry.initialize()
    refresh = registry.submit('refresh', {})
    discovery = registry.submit('skill_discovery', {'_depends_on': refresh.job_id})
    assert registry.queued(agent=True) is None
    assert registry.queued().job_id == refresh.job_id
    registry.claim(refresh.job_id, owner='writer')
    registry.update(refresh.job_id, owner='writer', state=JobState.FAILED, error='disk full')
    assert registry.queued(agent=True) is None
    assert registry.get(discovery.job_id).state == JobState.FAILED


def _two_lane_worker(db_path, state_path, job_id, owner):
    import time
    jobs = JobRegistry(state_path)
    job = jobs.get(job_id)
    if job.kind == 'refresh':
        jobs.update(job_id, owner=owner, progress={'waiting': True})
        deadline = time.monotonic() + 10
        while not db_path.with_suffix('.release').exists():
            if time.monotonic() > deadline:
                raise RuntimeError('test writer release timed out')
            time.sleep(.02)
    jobs.update(job_id, owner=owner, state=JobState.SUCCEEDED)


def test_agent_lane_runs_while_writer_is_busy(tmp_path, monkeypatch):
    import asyncio

    from reflect import backend_runtime
    monkeypatch.setattr(backend_runtime, '_execute_job', _two_lane_worker)
    runner = backend_runtime.BackendJobRunner(tmp_path / 'reflect.db')
    runner.jobs.initialize()
    refresh = runner.jobs.submit('refresh', {})
    discovery = runner.jobs.submit('skill_discovery', {})
    async def exercise():
        await runner.start()
        try:
            async with asyncio.timeout(8):
                while runner.jobs.get(discovery.job_id).state != JobState.SUCCEEDED:
                    await asyncio.sleep(.02)
            assert runner.jobs.get(refresh.job_id).state == JobState.RUNNING
        finally:
            runner.db_path.with_suffix('.release').touch()
            await runner.close()
    asyncio.run(exercise())


def test_projection_submission_during_running_job_queues_successor(tmp_path):
    jobs = JobRegistry(tmp_path / 'state.db')
    jobs.initialize()
    first = jobs.submit('task_projection', {})
    jobs.claim(first.job_id, owner='writer')
    successor = jobs.submit('task_projection', {})
    assert successor.job_id != first.job_id
    assert jobs.submit('task_projection', {}).job_id == successor.job_id


def test_refresh_can_initialize_existing_empty_database(tmp_path):
    from reflect.backend_runtime import _execute_job, action_store_path
    from reflect.store.snapshots import PublishedSnapshotStore
    database = tmp_path / 'empty.db'
    database.touch()
    jobs = JobRegistry(action_store_path(database))
    jobs.initialize()
    job = jobs.submit('refresh', {'otlp_traces':None, 'include_native_sessions':False})
    jobs.claim(job.job_id, owner='writer')
    _execute_job(database, jobs.path, job.job_id, 'writer')
    assert jobs.get(job.job_id).state == JobState.SUCCEEDED, jobs.get(job.job_id).error
    assert PublishedSnapshotStore(database).current() is not None


def test_dependent_submission_replay_has_no_new_dependency(tmp_path):
    jobs = JobRegistry(tmp_path / 'state.db')
    jobs.initialize()
    arguments = {'agent': 'codex', 'refresh': True}
    dependency = ('refresh', {'include_native_sessions': False})
    original = jobs.submit('skill_discovery', arguments, idempotency_key='stable', dependency=dependency)
    assert jobs.submit('skill_discovery', arguments, idempotency_key='stable', dependency=dependency) == original
    for job in reversed(jobs.list()):
        jobs.claim(job.job_id, owner='worker')
        jobs.update(job.job_id, owner='worker', state=JobState.SUCCEEDED)
    replay = jobs.submit('skill_discovery', arguments, idempotency_key='stable', dependency=dependency)
    assert replay.job_id == original.job_id
    assert len(jobs.list()) == 2
    with pytest.raises(JobConflict):
        jobs.submit('skill_discovery', {'agent': 'other'}, idempotency_key='stable', dependency=dependency)
    assert len(jobs.list()) == 2
