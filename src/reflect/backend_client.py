"""Typed asynchronous transport shared by terminal and MCP adapters."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from reflect.jobs import Job, JobState

API_VERSION = 1


class BackendError(RuntimeError):
    def __init__(self, message: str, *, status_code: int, details: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details or {}


@dataclass(frozen=True)
class BackendEndpoint:
    url: str
    token: str
    database: str


class BackendClient:
    """HTTP only: this adapter never opens or repairs a Reflect database.

    Retrying mutations is the caller's decision and requires a stable idempotency
    key. A disconnected waiter leaves the durable backend job running.
    """

    def __init__(self, endpoint: BackendEndpoint, *, transport=None):
        self.endpoint = endpoint
        self.transport = transport

    async def request(self, method: str, path: str, *, params: dict | None = None,
                      body: dict | None = None, idempotency_key: str | None = None) -> Any:
        if not path.startswith('/api/') or '://' in path:
            raise ValueError('Only relative Reflect API resource paths are allowed')
        headers = {'Authorization': f'Bearer {self.endpoint.token}',
                   'X-Reflect-API-Version': str(API_VERSION)}
        if idempotency_key:
            headers['Idempotency-Key'] = idempotency_key
        async with httpx.AsyncClient(base_url=self.endpoint.url, headers=headers,
                                     transport=self.transport, timeout=30,
                                     follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, path, params=params, json=body)
        try:
            payload = response.json()
        except ValueError as exc:
            raise BackendError('Backend returned an invalid response', status_code=response.status_code) from exc
        if response.is_error or response.is_redirect:
            details = payload if isinstance(payload, dict) else {}
            raise BackendError(str(details.get('error') or details.get('detail') or response.reason_phrase),
                               status_code=response.status_code, details=details)
        return payload

    async def job(self, job_id: str) -> Job:
        if not job_id.startswith('job_') or not job_id[4:].isalnum():
            raise ValueError('Invalid job identifier')
        return Job.model_validate(await self.request('GET', f'/api/jobs/{job_id}'))

    async def wait(self, job_id: str, *, interval: float = 1.0) -> Job:
        if interval <= 0:
            raise ValueError('Polling interval must be positive')
        while True:
            job = await self.job(job_id)
            if job.state not in {JobState.QUEUED, JobState.RUNNING}:
                return job
            await asyncio.sleep(interval)
