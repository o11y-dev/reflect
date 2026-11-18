from contextlib import closing

from fastapi.testclient import TestClient
from test_dashboard_sql_api import _seed_sql_report_db

from reflect.backend_api import install_backend
from reflect.backend_runtime import BackendJobRunner, action_store_path
from reflect.dashboard_server import build_dashboard_app
from reflect.store.snapshots import PublishedSnapshotStore
from reflect.store.sqlite import connect_sqlite, mark_snapshot
from reflect.store.user_state import UserStateStore
from reflect.store.user_state_migration import UserStateMigration


def test_real_api_reads_and_creates_during_evidence_write(tmp_path):
    db = tmp_path / 'reflect.db'
    _seed_sql_report_db(db)
    UserStateMigration(db, UserStateStore(action_store_path(db))).run()
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=True)
    PublishedSnapshotStore(db, reserve_bytes=0).publish(evidence_revision='first')
    app = build_dashboard_app(docs_dir=tmp_path, db_path=db)
    install_backend(app, BackendJobRunner(db), token='test', port=8765)
    with (TestClient(app, base_url='http://127.0.0.1:8765', headers={'Authorization': 'Bearer test'}) as client,
          closing(connect_sqlite(db)) as writer):
        writer.execute('BEGIN IMMEDIATE')
        response = client.get('/api/workflows')
        assert response.status_code == 200, response.text
        created = client.post('/api/workflows', json={'content': {
            'slug': 'api-test', 'description': 'API draft', 'steps': ['Verify inputs'],
            'verification': [], 'abstain_when': [],
        }})
        assert created.status_code == 201, created.text
        assert client.get('/api/workflows').status_code == 200
        assert client.get('/api/data?range=all').status_code == 200
        writer.rollback()


def test_cold_api_operational_endpoints_do_not_wait_for_snapshot(tmp_path):
    db = tmp_path / 'reflect.db'
    app = build_dashboard_app(docs_dir=tmp_path, db_path=db)
    install_backend(app, BackendJobRunner(db), token='test', port=8765)
    with TestClient(app, base_url='http://127.0.0.1:8765', headers={'Authorization': 'Bearer test'}) as client:
        assert client.get('/api/jobs').status_code == 200
        assert client.get('/api/backend').status_code == 200
        assert client.get('/api/status').status_code == 200
        response = client.get('/api/data')
        assert response.status_code == 503
        assert response.json()['state'] == 'preparing'
