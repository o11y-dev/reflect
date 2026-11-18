"""All clients read completed registry evidence while working evidence changes."""
import asyncio
import json
from contextlib import closing

from click.testing import CliRunner

from reflect.core import main
from reflect.mcp import reflect_patterns, reflect_skills
from reflect.store.sqlite import connect_sqlite, mark_snapshot


def test_registry_reads_survive_incomplete_working_snapshot(product_backend):
    db, _, client = product_backend
    created = asyncio.run(client.request('POST', '/api/workflows', body={'content': {
        'slug': 'verify-work', 'description': 'Verify changed work', 'steps': ['Run relevant checks'],
        'verification': [], 'abstain_when': []}}))
    before = asyncio.run(reflect_patterns())
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)
        conn.execute('BEGIN IMMEDIATE')
        result = CliRunner().invoke(main, ['workflows', 'list', '--json', '--db-path', str(db)])
        assert result.exit_code == 0, result.output
        assert created['id'] in {row['id'] for row in json.loads(result.output)}
        after = asyncio.run(reflect_patterns())
        assert after['workflows'] == before['workflows']
        assert asyncio.run(reflect_skills())['analysis_pending'] is False
        conn.rollback()


def test_working_schema_changes_do_not_poison_completed_publication(product_backend):
    db, _, _ = product_backend
    with closing(connect_sqlite(db)) as conn:
        conn.execute('DELETE FROM schema_migrations WHERE version=(SELECT MAX(version) FROM schema_migrations)')
        conn.commit()
    result = CliRunner().invoke(main, ['skills', '--json', '--db-path', str(db)])
    assert result.exit_code == 0, result.output

pytest_plugins = ["test_backend_product_clients"]
