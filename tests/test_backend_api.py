from fastapi import FastAPI
from fastapi.testclient import TestClient

from reflect.backend_api import install_backend
from reflect.backend_runtime import BackendJobRunner


def test_local_authorization_and_job_bootstrap_before_evidence(tmp_path):
    app = FastAPI()
    @app.get('/')
    def index():
        return {'ready': True}
    runner = BackendJobRunner(tmp_path / 'missing.db')
    install_backend(app, runner, token='secret', port=8765)
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        assert client.get('/api/jobs').status_code == 401
        assert client.get('/api/jobs', headers={'Authorization': 'Bearer secret'}).json() == {'jobs': []}
        assert client.get('/api/jobs', headers={'Host': 'evil.example:8765'}).status_code == 403
        assert client.get('/').status_code == 200
        assert client.get('/api/backend').json()['api_version'] == 1
        assert client.get('/api/jobs', headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.get('/api/jobs', headers={'X-Reflect-API-Version': '999'}).status_code == 409
        assert client.get('/api/jobs/unknown').status_code == 404
    assert not runner.db_path.exists()
