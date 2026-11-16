"""Terminal presentation and asynchronous backend transport; no store ownership."""
from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import click
import httpx

from reflect.backend_client import BackendClient, BackendError
from reflect.backend_lifecycle import BackendLocator
from reflect.cli.common import REFLECT_HOME, echo_json
from reflect.jobs import JobState


def request(db_path: Path, method: str, resource: str, *, params=None, body=None):
    async def send():
        endpoint = await BackendLocator(db_path).ensure()
        return await BackendClient(endpoint).request(method, resource, params=params, body=body,
            idempotency_key=uuid4().hex if method != 'GET' else None)
    try:
        return asyncio.run(send())
    except (BackendError, httpx.HTTPError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


def submit(db_path: Path, resource: str, body: dict | None = None, *, wait: bool = False,
           as_json: bool = False):
    payload = request(db_path, 'POST', resource, body=body)
    if wait:
        payload = wait_for_job(db_path, payload['job_id'])
    if as_json:
        echo_json(payload)
    else:
        click.echo(f"{payload['job_id']} · {payload['state']}")
        if payload.get('result') is not None:
            echo_json(payload['result'])
        if payload['state'] in {'queued', 'running'}:
            click.echo(f"Progress: reflect jobs show {payload['job_id']} --db-path {db_path}")
    if payload['state'] in {'failed', 'interrupted'}:
        raise click.ClickException(payload.get('error') or 'Background operation failed')
    return payload


def wait_for_job(db_path: Path, job_id: str):
    async def wait():
        endpoint = await BackendLocator(db_path).ensure()
        return (await BackendClient(endpoint).wait(job_id)).response()
    try:
        return asyncio.run(wait())
    except (BackendError, httpx.HTTPError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
def jobs():
    """Inspect durable background operations. Disconnecting does not cancel work."""


@jobs.command('list')
@click.option('--limit', type=click.IntRange(1, 500), default=20)
@click.option('--db-path', type=click.Path(path_type=Path), default=REFLECT_HOME / 'state' / 'reflect.db')
def jobs_list(limit, db_path):
    echo_json(request(db_path, 'GET', '/api/jobs', params={'limit': limit}))


@jobs.command('show')
@click.argument('job_id')
@click.option('--db-path', type=click.Path(path_type=Path), default=REFLECT_HOME / 'state' / 'reflect.db')
def jobs_show(job_id, db_path):
    from urllib.parse import quote
    echo_json(request(db_path, 'GET', '/api/jobs/' + quote(job_id, safe='')))


@jobs.command('wait')
@click.argument('job_id')
@click.option('--db-path', type=click.Path(path_type=Path), default=REFLECT_HOME / 'state' / 'reflect.db')
def jobs_wait(job_id, db_path):
    job = wait_for_job(db_path, job_id)
    echo_json(job)
    if job['state'] != JobState.SUCCEEDED:
        raise click.ClickException(job.get('error') or 'Background operation did not succeed')


def register_workflow_dispositions(group):
    for action in ('dismiss', 'restore'):
        def change(candidate_id, db_path, _action=action):
            from urllib.parse import quote
            echo_json(request(db_path, 'POST', '/api/workflows/' + quote(candidate_id, safe='') + '/' + _action, body={}))
        command = click.command(action)(click.argument('candidate_id')(
            click.option('--db-path', type=click.Path(path_type=Path), default=REFLECT_HOME / 'state' / 'reflect.db')(change)))
        group.add_command(command)


def inspect_segment_preparation(db_path, closed_paths):
    """Read capture checkpoints from an existing backend; offline status stays unknown."""
    from reflect.models import SegmentPreparationStatus
    paths = [Path(path) for path in closed_paths]
    endpoint = BackendLocator(db_path).existing()
    if endpoint is not None:
        payload = asyncio.run(BackendClient(endpoint).request('POST', '/api/capture/status',
                              body={'closed_paths': [str(path) for path in paths]}))
        return SegmentPreparationStatus(**payload)
    from contextlib import suppress
    sizes = []
    for path in paths:
        with suppress(OSError):
            sizes.append(path.stat().st_size)
    return SegmentPreparationStatus(0, 0, 0, 0, len(sizes), sum(sizes), None, False)
