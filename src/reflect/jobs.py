"""Durable local jobs, independent of evidence preparation and user revisions."""
from __future__ import annotations

import hashlib
from contextlib import closing
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from reflect.store.sqlite import connect_sqlite
from reflect.utils import _json_dumps, _json_loads


class JobState(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    INTERRUPTED = 'interrupted'


class Job(BaseModel):
    job_id: str
    kind: str
    state: JobState
    arguments: dict = Field(default_factory=dict)
    progress: dict = Field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    owner: str | None = None

    @property
    def status_url(self) -> str:
        return f'/api/jobs/{self.job_id}'

    def response(self) -> dict:
        return {**self.model_dump(mode='json'), 'status_url': self.status_url}


class JobConflict(ValueError):
    pass


class JobRegistry:
    """Atomic submission/claim/finish with private short-lived connections.

    Tables may be initialized before the user-state cutover. No material-revision
    triggers apply to job records. Worker ownership is a runtime-instance identity,
    not a reusable PID; only the lifecycle owner may reconcile abandoned runs.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(connect_sqlite(self.path, strict_durability=True)) as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS backend_jobs (
                    job_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL, arguments TEXT NOT NULL,
                    progress TEXT NOT NULL DEFAULT '{}', result TEXT, error TEXT,
                    owner TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backend_job_requests (
                    idempotency_key TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES backend_jobs(job_id),
                    kind TEXT NOT NULL, fingerprint TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS backend_jobs_queue
                    ON backend_jobs(state, created_at);
                DROP INDEX IF EXISTS backend_jobs_active_request;
                CREATE UNIQUE INDEX IF NOT EXISTS backend_jobs_active_request
                    ON backend_jobs(kind, fingerprint)
                    WHERE state IN ('queued','running') AND kind != 'task_projection';
                CREATE UNIQUE INDEX IF NOT EXISTS backend_jobs_queued_projection
                    ON backend_jobs(kind, fingerprint)
                    WHERE state='queued' AND kind='task_projection';
            ''')
            conn.commit()

    @staticmethod
    def _decode(row) -> Job:
        values = dict(zip(('job_id', 'kind', 'state', 'arguments', 'progress',
                           'result', 'error', 'created_at', 'updated_at', 'owner'), row, strict=True))
        for name in ('arguments', 'progress', 'result'):
            if values[name] is not None:
                values[name] = _json_loads(values[name])
        return Job.model_validate(values)

    @staticmethod
    def _select(conn, clause: str, params: tuple):
        return conn.execute('SELECT job_id,kind,state,arguments,progress,result,error,'
                            'created_at,updated_at,owner FROM backend_jobs ' + clause, params)

    def submit(self, kind: str, arguments: dict, *, idempotency_key: str | None = None,
               dependency: tuple[str, dict] | None = None) -> Job:
        with closing(connect_sqlite(self.path, strict_durability=True)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            job = self._submit(conn, kind, arguments, idempotency_key=idempotency_key,
                               dependency=dependency)
            conn.commit()
            return job

    def _submit(self, conn, kind: str, arguments: dict, *, idempotency_key=None,
                dependency=None) -> Job:
        # Fingerprint the logical request before adding execution-only dependency IDs.
        import json
        serialized = json.dumps(arguments, sort_keys=True, separators=(',', ':'))
        fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
        now = datetime.now(UTC).isoformat()
        if idempotency_key:
            row = conn.execute('SELECT job_id,kind,fingerprint FROM backend_job_requests '
                               'WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if row:
                if row[1:] != (kind, fingerprint):
                    raise JobConflict('Idempotency key belongs to a different request')
                return self._decode(self._select(conn, 'WHERE job_id=?', (row[0],)).fetchone())
        states = "('queued')" if kind == "task_projection" else "('queued','running')"
        existing = self._select(conn, f"WHERE kind=? AND fingerprint=? AND state IN {states}",
                                (kind, fingerprint)).fetchone()
        if existing:
            if idempotency_key:
                conn.execute('INSERT INTO backend_job_requests VALUES(?,?,?,?)',
                             (idempotency_key, existing[0], kind, fingerprint))
            return self._decode(existing)
        if dependency is not None:
            prerequisite = self._submit(conn, dependency[0], dependency[1])
            serialized = json.dumps({**arguments, '_depends_on': prerequisite.job_id}, sort_keys=True, separators=(',', ':'))
        job_id = 'job_' + uuid4().hex
        conn.execute('INSERT INTO backend_jobs(job_id,kind,fingerprint,state,'
                     'arguments,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                     (job_id, kind, fingerprint, JobState.QUEUED,
                      serialized, now, now))
        if idempotency_key:
            conn.execute('INSERT INTO backend_job_requests VALUES(?,?,?,?)',
                         (idempotency_key, job_id, kind, fingerprint))
        job = self._decode(self._select(conn, 'WHERE job_id=?', (job_id,)).fetchone())
        return job

    def get(self, job_id: str) -> Job | None:
        with closing(connect_sqlite(self.path)) as conn:
            row = self._select(conn, 'WHERE job_id=?', (job_id,)).fetchone()
            return self._decode(row) if row else None

    def list(self, *, limit: int = 100) -> list[Job]:
        with closing(connect_sqlite(self.path)) as conn:
            return [self._decode(row) for row in self._select(
                conn, 'ORDER BY created_at DESC, job_id DESC LIMIT ?', (max(1, min(limit, 500)),))]

    def queued(self, *, agent: bool = False) -> Job | None:
        with closing(connect_sqlite(self.path)) as conn:
            rows = self._select(conn, "WHERE state='queued' ORDER BY created_at,job_id", ()).fetchall()
            row = None
            for candidate in rows:
                decoded = self._decode(candidate)
                if (decoded.kind in {'skill_discovery', 'loop_build'}) != agent:
                    continue
                dependency_id = decoded.arguments.get('_depends_on')
                if dependency_id:
                    dependency = self._select(conn, 'WHERE job_id=?', (dependency_id,)).fetchone()
                    if dependency is None or dependency[2] in {'failed', 'interrupted'}:
                        conn.execute("UPDATE backend_jobs SET state='failed',error='Required refresh did not complete' WHERE job_id=?", (decoded.job_id,))
                        conn.commit()
                        continue
                    if dependency[2] != 'succeeded':
                        continue
                row = candidate
                break
            return self._decode(row) if row else None

    def claim(self, job_id: str, *, owner: str) -> bool:
        with closing(connect_sqlite(self.path, strict_durability=True)) as conn:
            changed = conn.execute("UPDATE backend_jobs SET state='running',owner=?,updated_at=? "
                                   "WHERE job_id=? AND state='queued'",
                                   (owner, datetime.now(UTC).isoformat(), job_id)).rowcount
            conn.commit()
            return bool(changed)

    def update(self, job_id: str, *, owner: str, progress: dict | None = None,
               state: JobState = JobState.RUNNING, result: dict | None = None,
               error: str | None = None) -> None:
        if state == JobState.QUEUED:
            raise ValueError('A claimed job cannot return to queued')
        with closing(connect_sqlite(self.path, strict_durability=True)) as conn:
            changed = conn.execute(
                'UPDATE backend_jobs SET state=?,progress=COALESCE(?,progress),result=?,error=?,updated_at=? '
                "WHERE job_id=? AND owner=? AND state='running'",
                (state, _json_dumps(progress) if progress is not None else None,
                 _json_dumps(result) if result is not None else None, error,
                 datetime.now(UTC).isoformat(), job_id, owner)).rowcount
            if not changed:
                raise JobConflict('Job is not running under this worker ownership')
            conn.commit()

    def running_owners(self) -> list[str]:
        with closing(connect_sqlite(self.path)) as conn:
            return [row[0] for row in conn.execute(
                "SELECT DISTINCT owner FROM backend_jobs WHERE state='running' AND owner IS NOT NULL")]

    def interrupt_owner(self, owner: str) -> int:
        """Call only after verifying that the old worker has stopped."""
        with closing(connect_sqlite(self.path, strict_durability=True)) as conn:
            changed = conn.execute(
                "UPDATE backend_jobs SET state='interrupted',error='Worker stopped before completion',updated_at=? "
                "WHERE owner=? AND state='running'", (datetime.now(UTC).isoformat(), owner)).rowcount
            conn.commit()
            return changed
