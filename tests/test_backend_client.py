import asyncio

import httpx
import pytest

from reflect.backend_client import BackendClient, BackendEndpoint, BackendError


def test_client_uses_authorized_api_and_does_not_follow_redirects():
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(307, headers={'Location': 'http://example.com'}, json={'error': 'moved'})
    client = BackendClient(BackendEndpoint('http://127.0.0.1:8765', 'private', '/db'),
                           transport=httpx.MockTransport(handle))
    with pytest.raises(BackendError, match='moved'):
        asyncio.run(client.request('POST', '/api/refresh', body={}, idempotency_key='one'))
    assert len(seen) == 1
    assert seen[0].headers['Authorization'] == 'Bearer private'
    assert seen[0].headers['Idempotency-Key'] == 'one'


def test_wait_returns_terminal_job_and_rejects_external_paths():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={'job_id': 'job_abc', 'kind': 'refresh',
                             'state': 'succeeded', 'created_at': 'now', 'updated_at': 'now',
                             'result': {'sessions': 2}})
    client = BackendClient(BackendEndpoint('http://127.0.0.1:8765', 'private', '/db'),
                           transport=httpx.MockTransport(handle))
    assert asyncio.run(client.wait('job_abc')).result == {'sessions': 2}
    with pytest.raises(ValueError):
        asyncio.run(client.request('GET', 'https://example.com/api/jobs'))
    assert len(calls) == 1
