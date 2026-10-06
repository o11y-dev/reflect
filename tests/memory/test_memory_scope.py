import json
from pathlib import Path

from reflect.memory import MemoryItem, MemoryService, MemorySourceMetadata
from reflect.store.ingest import ingest_local_spans_file
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.sqlite import connect_sqlite


def test_project_queries_include_user_and_ancestor_memories_without_sibling_leak(tmp_path):
    conn = connect_sqlite(tmp_path / 'memory.db')
    try:
        migrate(conn)
        service = MemoryService(conn, maintain_search_index=False)
        repo = tmp_path / 'repo_one'
        for name, scope, root in [('user', 'user', ''), ('project', 'project', str(repo)),
                                  ('sibling', 'project', str(tmp_path / 'repoXone'))]:
            service.remember(MemoryItem(
                id=name, content='release gate', type='instruction', scope=scope,
                source_metadata=MemorySourceMetadata(
                    source_kind='manual', source_ref=name, manual_note=True,
                    workspace_root=root, path=f'{root}/AGENTS.md' if root else '',
                ),
            ))
        for path in (repo, repo / 'src'):
            assert {r['id'] for r in service.search('release', path=path)} == {'user', 'project'}
            assert {r['id'] for r in service.list_memories(path=path)} == {'user', 'project'}
        assert {r['id'] for r in service.search('release', path=tmp_path / 'unrelated')} == {'user'}
    finally:
        conn.close()


def test_migration_normalizes_legacy_sources_and_preserves_canonical_metadata(tmp_path):
    conn = connect_sqlite(tmp_path / 'memory.db')
    try:
        migrate(conn)
        conn.execute('DELETE FROM schema_migrations WHERE version=34')
        for name, attrs, metadata in [
            ('hook', {'gen_ai.memory.source_path': '/repo/AGENTS.md', 'code.workspace.root': '/repo'}, {}),
            ('scan', {'path': '/repo/CLAUDE.md', 'workspace_root': '/repo'}, {}),
            ('canonical', {'path': '/wrong', 'workspace_root': '/wrong'}, {'path': '/repo/AGENTS.md', 'workspace_root': '/repo'}),
        ]:
            conn.execute('''INSERT INTO memories(id,scope,type,source,content_hash,
                raw_attrs_json,source_metadata_json,created_at,updated_at)
                VALUES (?, 'project', 'instruction', 'test', 'hash', ?, ?, 'now', 'now')''',
                (name, json.dumps(attrs), json.dumps(metadata)))
        conn.commit()
        assert migrate(conn) == [34]
        service = MemoryService(conn, maintain_search_index=False)
        rows = service.list_memories(path=Path('/repo/src'))
        assert {r['id'] for r in rows} == {'hook', 'scan', 'canonical'}
        assert all(r['source_metadata']['workspace_root'] == '/repo' for r in rows)
        assert migrate(conn) == []
    finally:
        conn.close()


def test_memory_exposure_preserves_validated_instruction_and_normalizes_new_sources(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    source = repo / 'AGENTS.md'
    source.write_text('Run the release gate before publishing.')
    conn = connect_sqlite(tmp_path / 'memory.db')
    try:
        migrate(conn)
        service = MemoryService(conn)
        service.sync_path(repo, home_root=tmp_path / 'empty-home')
        original = service.list_memories(path=repo)[0]
        service.validate(original['id'])
        spans = []
        for i, memory_id in enumerate((original['id'], 'new-hook-memory')):
            spans.append({
                'name': 'ContextExposure', 'traceId': 'trace', 'spanId': str(i),
                'start_time_ns': 1791288000000000000 + i,
                'attributes': {
                    'gen_ai.client.name': 'future-provider', 'gen_ai.client.session_id': 'session',
                    'gen_ai.client.hook.event': 'ContextExposure', 'gen_ai.memory.id': memory_id,
                    'gen_ai.memory.scope': 'project', 'gen_ai.memory.type': 'instruction',
                    'gen_ai.memory.content_hash': 'telemetry-hash',
                    'gen_ai.memory.content_preview': '[redacted exposure]',
                    'gen_ai.memory.source_path': str(source), 'code.workspace.root': str(repo),
                },
            })
        path = tmp_path / 'spans.jsonl'
        path.write_text('\n'.join(json.dumps(s) for s in spans))
        ingest_local_spans_file(conn, file_path=path)
        assert normalize_pending_raw_events(conn)['failed'] == 0
        preserved = service.inspect(original['id'])
        for key in ('content_hash', 'content_preview_redacted', 'source_metadata'):
            assert preserved[key] == original[key]
        assert preserved['validation_status'] == 'validated'
        assert service.inspect('new-hook-memory')['source_metadata']['workspace_root'] == str(repo)
        assert service.select_context('release', path=repo / 'src')[0]['context_content'] == source.read_text()
    finally:
        conn.close()


def test_context_does_not_inject_nested_instructions_or_optional_roles(tmp_path):
    repo = tmp_path / 'repo'
    files = {
        'AGENTS.md': 'Root release instructions.',
        '.github/copilot-instructions.md': 'Keep the changelog current.',
        'fixture/AGENTS.md': 'Nested service instructions.',
        '.cursor/agents/reviewer.md': 'Optional specialist instructions.',
    }
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    conn = connect_sqlite(tmp_path / 'memory.db')
    try:
        migrate(conn)
        service = MemoryService(conn, maintain_search_index=False)
        service.sync_path(repo, home_root=tmp_path / 'empty-home')
        for row in service.list_memories(path=repo):
            service.validate(row['id'])
        rows = service.select_context('unrelated task question', path=repo)
        assert {r['context_content'] for r in rows} == {files['AGENTS.md'], files['.github/copilot-instructions.md']}
        matched = service.select_context('Nested', path=repo)
        assert all(r['context_content'] != files['fixture/AGENTS.md'] for r in matched)
        nested = service.select_context('unrelated question', path=repo / 'fixture')
        assert files['fixture/AGENTS.md'] in {r['context_content'] for r in nested}
    finally:
        conn.close()
