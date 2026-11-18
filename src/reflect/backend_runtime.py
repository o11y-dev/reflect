"""Process-isolated durable job execution owned by the local API lifecycle."""
from __future__ import annotations

import asyncio
import fcntl
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from reflect.jobs import JobRegistry, JobState


def action_store_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + '.user-state.db')


@contextmanager
def _owner_lease(state_path: Path, owner: str, *, blocking: bool = True):
    directory = state_path.with_name(state_path.name + '.workers')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / f'{owner}.lock').open('a+b') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def _execute_job(db_path: Path, state_path: Path, job_id: str, owner: str) -> None:
    with _owner_lease(state_path, owner):
        _execute_owned_job(db_path, state_path, job_id, owner)


def _execute_owned_job(db_path: Path, state_path: Path, job_id: str, owner: str) -> None:
    registry = JobRegistry(state_path)
    job = registry.get(job_id)
    if job is None or job.state != JobState.RUNNING or job.owner != owner:
        return
    try:
        if job.kind in {'skill_discovery', 'loop_build'}:
            from reflect.backend_operations import execute_agent_job
            result = execute_agent_job(db_path, job.kind, job.arguments)
            if job.kind == 'loop_build' and result.get('skill_ids'):
                projection = registry.submit('loop_promotion', {'loop_id': job.arguments['loop_id'], 'skill_id': result['skill_ids'][0]})
                result['projection_job_id'] = projection.job_id
            registry.update(job_id, owner=owner, state=JobState.SUCCEEDED, result=result)
            return
        from reflect.preparation import PreparationLock
        from reflect.preparation_pipeline import prepare_sql_report_db
        from reflect.store.evidence_revision import install_evidence_revision_tracking
        from reflect.store.sqlite import connect_sqlite

        if not db_path.exists() and job.kind not in {'publish', 'refresh'} and not job.kind.startswith('database_'):
            prepare_sql_report_db(db_path, otlp_traces=None, include_native_sessions=False)
        if db_path.exists() and not job.kind.startswith('database_'):
            with PreparationLock(db_path), closing(connect_sqlite(db_path)) as conn:
                if job.kind == 'refresh' or (job.kind not in {'publish', 'task_projection'} and not job.kind.startswith('database_')):
                    from reflect.store.migrate import migrate
                    migrate(conn)
                install_evidence_revision_tracking(conn)
                conn.commit()

        def progress(event):
            registry.update(job_id, owner=owner, progress={
                'stage': event.stage.value, 'message': event.message,
            })
        # Snapshot-attached user actions never mutate canonical outcomes.
        # Project the latest durable feedback before analysis/publication.
        # Retention must inspect/back up the original canonical state first;
        # its independently queued feedback job owns that projection.
        if db_path.exists() and job.kind != 'database_prune':
            from reflect.improvements.repository import ImprovementRepository
            with PreparationLock(db_path), closing(connect_sqlite(db_path)) as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='session_outcomes'").fetchone():
                    tables = {row[1] for row in conn.execute('PRAGMA database_list')}
                    schema = 'user_state' if 'user_state' in tables else 'main'
                    if conn.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE name='operator_feedback'").fetchone():
                        rows = conn.execute(f"SELECT session_id,outcome,id,reason_redacted,created_at FROM {schema}.operator_feedback ORDER BY created_at,rowid").fetchall()
                        latest = {row[0]: row for row in rows}
                        repository = ImprovementRepository(conn)
                        for row in latest.values():
                            if conn.execute('SELECT 1 FROM sessions WHERE id=?', (row[0],)).fetchone():
                                repository.project_feedback_outcome(*row)
                        conn.commit()
        arguments = {'otlp_traces': None, **{key: value for key, value in job.arguments.items() if not key.startswith('_')}}
        if arguments.get('otlp_traces') is not None:
            arguments['otlp_traces'] = Path(arguments['otlp_traces'])
        if job.kind == 'refresh':
            if arguments.get('spans_dir'):
                arguments['spans_dir'] = Path(arguments['spans_dir'])
            if arguments.get('analysis_session_ids') is not None:
                arguments['analysis_session_ids'] = set(arguments['analysis_session_ids'])
            result = prepare_sql_report_db(db_path, progress=progress, **arguments)
        elif job.kind == 'publish':
            result = {}
        elif job.kind == 'task_projection':
            from reflect.preparation import PreparationLock
            from reflect.task_inbox import TaskEventInbox
            with PreparationLock(db_path):
                TaskEventInbox(db_path).drain()
            result = {}
        else:
            from reflect.backend_operations import execute_maintenance
            result = execute_maintenance(db_path, job.kind, job.arguments)
            if job.kind == 'database_prune' and not job.arguments.get('apply_changes'):
                registry.update(job_id, owner=owner, state=JobState.SUCCEEDED, result=result)
                return
            if job.kind.startswith('database_'):
                with PreparationLock(db_path), closing(connect_sqlite(db_path)) as conn:
                    install_evidence_revision_tracking(conn)
                    conn.commit()
            prepare_sql_report_db(db_path, otlp_traces=None, progress=progress)
        from reflect.preparation import PreparationLock
        from reflect.store.snapshots import PublishedSnapshotStore
        from reflect.store.sqlite import connect_sqlite
        from reflect.store.user_state import UserStateStore
        from reflect.store.user_state_migration import UserStateMigration

        UserStateMigration(db_path, UserStateStore(state_path)).run()
        with PreparationLock(db_path), closing(connect_sqlite(db_path)) as conn:
            mutation = conn.execute("SELECT value FROM store_metadata WHERE key='canonical_mutation_revision'").fetchone()
            if mutation is None:
                raise RuntimeError("Canonical evidence revision tracking is missing")
            revision = f"canonical-{mutation[0]}"
            conn.execute("INSERT INTO store_metadata(key,value,updated_at) VALUES ('material_evidence_revision',?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (revision, datetime.now(UTC).isoformat()))
            conn.commit()
            publication = PublishedSnapshotStore(db_path).publish(evidence_revision=revision)
        from reflect.task_inbox import TaskEventInbox
        inbox = TaskEventInbox(db_path)
        inbox.acknowledge_publication(publication.path)
        health = inbox.health()
        if health["pending_count"] and not health["projection_errors"]:
            registry.submit("task_projection", {})
        result['publication'] = {'generation': publication.generation,
                                 'completed_at': publication.completed_at}
        registry.update(job_id, owner=owner, state=JobState.SUCCEEDED, result=result)
    except Exception as exc:
        registry.update(job_id, owner=owner, state=JobState.FAILED, error=str(exc))


class BackendJobRunner:
    """One canonical writer; durable queue is authoritative across disconnection.

    Shutdown waits for owned work rather than starting a replacement writer. The
    singleton backend startup lock is required before constructing this runner.
    """

    def __init__(self, db_path: Path, *, refresh_arguments: dict | None = None,
                 refresh_interval: int = 300):
        self.db_path = Path(db_path).expanduser().resolve()
        self.jobs = JobRegistry(action_store_path(self.db_path))
        self.owner = uuid4().hex
        self.refresh_arguments = refresh_arguments
        self.refresh_interval = refresh_interval
        self.next_refresh = 0.0
        self._pool = None
        self._task = None
        self._stopping = False
        self._agent_pool = None
        self._agent_task = None

    async def start(self) -> None:
        from reflect.backend_lifecycle import backend_state_dir
        directory = backend_state_dir(self.db_path)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lifetime_lock = (directory / 'backend-owner.lock').open('a+b')
        try:
            fcntl.flock(self._lifetime_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lifetime_lock.close()
            raise RuntimeError('Another backend owns this database') from exc
        await asyncio.to_thread(self.jobs.initialize)
        from reflect.workflow_journal import WorkflowFileJournal
        self.action_recovery_conflicts = await asyncio.to_thread(WorkflowFileJournal(self.jobs.path).recover)
        await asyncio.to_thread(self._recover_abandoned)
        if self.refresh_arguments is not None:
            await asyncio.to_thread(self.jobs.submit, 'refresh', self.refresh_arguments)
        elif self.db_path.exists():
            from reflect.store.snapshots import PublishedSnapshotStore
            if PublishedSnapshotStore(self.db_path).current() is None:
                await asyncio.to_thread(self.jobs.submit, 'publish', {})
        self._pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context('spawn'))
        self._task = asyncio.create_task(self._run())
        self._agent_pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        self._agent_task = asyncio.create_task(self._run(agent=True))

    def _recover_abandoned(self) -> None:
        for owner in self.jobs.running_owners():
            if owner in {self.owner, self.owner + '-agent'}:
                continue
            try:
                with _owner_lease(self.jobs.path, owner, blocking=False):
                    self.jobs.interrupt_owner(owner)
            except BlockingIOError:
                # An orphaned child is still completing its owned job. Never
                # replay it merely because its parent API process restarted.
                continue

    async def _run(self, *, agent: bool = False) -> None:
        owner = self.owner + '-agent' if agent else self.owner
        while not self._stopping:
            await asyncio.to_thread(self._recover_abandoned)
            job = await asyncio.to_thread(self.jobs.queued, agent=agent)
            if job is None:
                if not agent and self.refresh_arguments is not None and asyncio.get_running_loop().time() >= self.next_refresh:
                    await asyncio.to_thread(self.jobs.submit, 'refresh', self.refresh_arguments)
                    self.next_refresh = float('inf')
                await asyncio.sleep(0.25)
                continue
            if not await asyncio.to_thread(self.jobs.claim, job.job_id, owner=owner):
                continue
            try:
                await asyncio.get_running_loop().run_in_executor(
                    (self._agent_pool if agent else self._pool), _execute_job, self.db_path, self.jobs.path, job.job_id, owner)
            except Exception as exc:
                if isinstance(exc, BrokenProcessPool):
                    pool = self._agent_pool if agent else self._pool
                    pool.shutdown(wait=False, cancel_futures=True)
                    replacement = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context('spawn'))
                    if agent:
                        self._agent_pool = replacement
                    else:
                        self._pool = replacement
                current = await asyncio.to_thread(self.jobs.get, job.job_id)
                if current and current.state == JobState.RUNNING:
                    await asyncio.to_thread(self.jobs.update, job.job_id, owner=owner,
                                            state=JobState.INTERRUPTED, error=str(exc))
            finally:
                if not agent and job.kind in {"refresh", "publish"}:
                    self.next_refresh = asyncio.get_running_loop().time() + self.refresh_interval

    async def close(self) -> None:
        self._stopping = True
        if self._task:
            await self._task
        if self._agent_task:
            await self._agent_task
        if self._agent_pool:
            await asyncio.to_thread(self._agent_pool.shutdown, wait=True, cancel_futures=True)
        if self._pool:
            await asyncio.to_thread(self._pool.shutdown, wait=True, cancel_futures=True)
        if getattr(self, '_lifetime_lock', None):
            self._lifetime_lock.close()
