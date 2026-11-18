"""Local HTTP policy and durable job resources for the Reflect application."""
from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from reflect.backend_client import API_VERSION
from reflect.backend_runtime import BackendJobRunner


def install_backend(app, runner: BackendJobRunner, *, token: str, port: int) -> None:
    from fastapi import Request
    from fastapi.responses import JSONResponse
    globals()['Request'] = Request

    from reflect.jobs import JobConflict

    @app.exception_handler(JobConflict)
    async def job_conflict(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid_request(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing_resource(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=404)

    if not token:
        raise ValueError('A private backend token is required')
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application):
            await runner.start()
            try:
                yield
            finally:
                await runner.close()

    app.router.lifespan_context = lifespan
    app.state.backend = runner
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}

    @app.middleware('http')
    async def local_access(request, call_next):
        if request.headers.get('host', '') not in hosts:
            return JSONResponse({'error': 'Untrusted local Host'}, status_code=403)
        origin = request.headers.get('origin')
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme != 'http' or parsed.netloc != request.headers.get('host'):
                return JSONResponse({'error': 'Cross-origin access is not allowed'}, status_code=403)
        if request.url.path.startswith('/api/'):
            provided = request.headers.get('authorization', '').removeprefix('Bearer ')
            cookie = request.cookies.get('reflect_session', '')
            if not (hmac.compare_digest(provided, token) or hmac.compare_digest(cookie, token)):
                return JSONResponse({'error': 'Local client authorization required'}, status_code=401)
            version = request.headers.get('x-reflect-api-version')
            if version is not None and version != str(API_VERSION):
                return JSONResponse({'error': 'Incompatible Reflect API version'}, status_code=409)
        response = await call_next(request)
        if request.url.path == '/' and response.status_code == 200:
            response.set_cookie('reflect_session', token, httponly=True, samesite='strict')
        return response

    @app.get('/api/jobs')
    async def list_jobs(limit: int = 100):
        return {'jobs': [job.response() for job in await asyncio.to_thread(runner.jobs.list, limit=limit)]}

    @app.get('/api/jobs/{job_id}')
    async def get_job(job_id: str):
        job = await asyncio.to_thread(runner.jobs.get, job_id)
        if job is None:
            return JSONResponse({'error': 'Job not found'}, status_code=404)
        return job.response()

    @app.get('/api/backend')
    async def backend_identity():
        return {'api_version': API_VERSION, 'database': str(runner.db_path), 'instance_id': runner.owner}

    # Replace the old in-process preparation endpoints, retaining canonical URLs.
    app.router.routes[:] = [route for route in app.router.routes
                            if getattr(route, 'path', None) not in {'/api/refresh', '/api/status'}]

    @app.post('/api/refresh')
    async def refresh(request: Request):
        if runner.refresh_arguments is None:
            return JSONResponse({'error': 'Refresh is disabled for this server'}, status_code=409)
        from reflect.jobs import JobConflict
        try:
            job = await asyncio.to_thread(runner.jobs.submit, 'refresh', runner.refresh_arguments,
                                          idempotency_key=request.headers.get('idempotency-key'))
        except JobConflict as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)
        return JSONResponse(job.response(), status_code=202)

    @app.get('/api/status')
    async def status():
        from contextlib import closing
        from datetime import UTC, datetime, timedelta

        from reflect.store.snapshots import PublishedSnapshotStore
        from reflect.store.sqlite import connect_sqlite_read_only

        jobs = await asyncio.to_thread(runner.jobs.list, limit=100)
        refreshes = [job for job in jobs if job.kind in {'refresh', 'publish'}]
        latest = next((job for job in refreshes if job.state in {'queued', 'running'}),
                      refreshes[0] if refreshes else None)
        published = await asyncio.to_thread(PublishedSnapshotStore(runner.db_path).current)
        def user_revision():
            with closing(connect_sqlite_read_only(runner.jobs.path, profile=None)) as conn:
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='user_state_metadata'").fetchone()
                if not exists:
                    return 0
                row = conn.execute("SELECT value FROM user_state_metadata WHERE key='material_revision'").fetchone()
                return row[0] if row else 0
        raw_storage = None
        loader = getattr(runner, 'raw_storage_status_loader', None)
        if loader:
            try:
                raw_storage = await asyncio.to_thread(loader)
            except OSError as exc:
                raw_storage = {'accepting_telemetry': None, 'error': str(exc)}
        remaining = runner.next_refresh - asyncio.get_running_loop().time()
        next_run = (datetime.now(UTC) + timedelta(seconds=max(0, remaining))).isoformat() if remaining < float('inf') else ''
        return {
            'api_version': API_VERSION, 'database': str(runner.db_path),
            'instance_id': runner.owner, 'job': latest.response() if latest else None,
            'publication': {'generation': published.generation, 'completed_at': published.completed_at}
                           if published else None,
            'user_state_revision': await asyncio.to_thread(user_revision),
            'raw_storage': raw_storage,
            'action_recovery_conflicts': getattr(runner, 'action_recovery_conflicts', []),
            'preparation': {
                'state': ({'queued': 'running', 'running': 'running', 'succeeded': 'complete',
                           'failed': 'failed', 'interrupted': 'failed'}[latest.state]
                          if latest else 'idle'),
                'generation': published.generation if published else None,
                'error': latest.error if latest else None,
                'stage': latest.progress.get('stage') if latest else None,
                'message': latest.progress.get('message') if latest else None,
                'started_at': latest.created_at if latest else None,
                'finished_at': latest.updated_at if latest and latest.state not in {'queued', 'running'} else None,
                'result': {'details': latest.result} if latest and latest.result else None,
            },
            'refresh_available': runner.refresh_arguments is not None,
            'automatic_refresh': {'enabled': runner.refresh_arguments is not None,
                                  'interval_seconds': runner.refresh_interval,
                                  'next_run_at': next_run},
        }

    from reflect.backend_resources import install_product_resources
    install_product_resources(app, runner)

    # Static catch-all must follow newly installed API resources.
    from starlette.routing import Mount
    app.router.routes.sort(key=lambda route: isinstance(route, Mount))
