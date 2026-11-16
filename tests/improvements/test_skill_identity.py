from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from reflect.improvements.skill_usage import SkillUsageProjector
from reflect.improvements.skills import SkillRegistryService
from reflect.store.migrate import load_migrations, migrate
from reflect.store.sqlite import connect_sqlite

NOW = '2026-11-08T10:00:00+00:00'


def _skill(root: Path, body: str = 'Run tests.') -> Path:
    path = root / 'verify-first' / 'SKILL.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('---\nname: verify-first\ndescription: Verify changes\n---\n' + body)
    return path


def test_independent_origins_and_identical_copies_keep_separate_versions(tmp_path):
    conn = connect_sqlite(tmp_path / 'reflect.db')
    registry = SkillRegistryService(conn)
    paths = [_skill(tmp_path / repo / '.agents' / 'skills') for repo in ('one', 'two')]
    registry.sync_paths(paths)
    skills = registry.list()
    assert len(skills) == 2
    by_path = {row.path: row.skill_id for item in skills for row in registry.show(item.id).installations}
    assert len(set(by_path.values())) == 2
    paths[0].write_text(paths[0].read_text() + '\nRecord the result.')
    registry.sync_paths(paths)
    assert registry.show(by_path[str(paths[0])]).skill.version_count == 2
    assert registry.show(by_path[str(paths[1])]).skill.version_count == 1
    assert len(registry.list()) == 2
    with pytest.raises(ValueError) as error:
        registry.show('verify-first')
    assert all(item.id in str(error.value) for item in skills)
    conn.close()


def test_exact_captured_origin_wins_and_ambiguous_name_is_unresolved(tmp_path):
    conn = connect_sqlite(tmp_path / 'reflect.db')
    registry = SkillRegistryService(conn)
    paths = [_skill(tmp_path / repo) for repo in ('one', 'two')]
    registry.sync_paths(paths)
    owners = {row[0]: row[1] for row in conn.execute('SELECT path,skill_id FROM skill_installations')}
    assert len(set(owners.values())) == 2
    projector = SkillUsageProjector(conn)
    assert projector.occurrences({'skill.name': 'verify-first'}, 'Skill', 'ok', '') == []
    observed = projector.occurrences({'gen_ai.client.file_path': str(paths[0]), 'skill.name': 'verify-first'}, 'Read', 'ok', '')
    assert [item.skill_id for item in observed] == [owners[str(paths[0])]]
    assert observed[0].version_id is None
    version = registry.show(owners[str(paths[1])]).skill.current_version_id
    captured = projector.occurrences({'skill.version_id': version}, 'Skill', 'ok', '')
    assert [(item.skill_id, item.version_id) for item in captured] == [(owners[str(paths[1])], version)]
    assert projector.occurrences({'skill.id': 'unknown-id', 'gen_ai.client.file_path': str(paths[0])}, 'Read', 'ok', '') == []
    conn.close()


def _legacy(conn, paths):
    prior = [item for item in load_migrations() if item.version <= 47]
    with patch('reflect.store.migrate.load_migrations', return_value=prior):
        migrate(conn)
    conn.execute("INSERT INTO skills VALUES ('legacy-id','verify-first','verify-first','Verify','imported','active','legacy-version',?,?,?,?)", (NOW,) * 4)
    conn.execute("""INSERT INTO skill_versions(id,skill_id,version,content_markdown,content_hash,source_kind,created_at,updated_at)
        VALUES ('legacy-version','legacy-id',1,'Run tests.','legacy-hash','filesystem',?,?)""", (NOW, NOW))
    for index, path in enumerate(paths):
        conn.execute("""INSERT INTO skill_installations(id,skill_id,skill_version_id,target_kind,target_ref,path,installed_hash,first_seen_at,last_seen_at,created_at,updated_at)
            VALUES (?,'legacy-id','legacy-version','repository','repo',?,'legacy-hash',?,?,?,?)""", (f'install-{index}', str(path), NOW, NOW, NOW, NOW))
    conn.execute("INSERT INTO agents(id,name,created_at,updated_at) VALUES ('a','codex',?,?)", (NOW, NOW))
    conn.execute("INSERT INTO sessions(id,agent_id,started_at,created_at,updated_at) VALUES ('s','a',?,?,?)", (NOW, NOW, NOW))
    conn.execute("""INSERT INTO skill_usage(id,skill_id,skill_version_id,session_id,state,evidence_json,observed_at,created_at,updated_at)
        VALUES ('usage','legacy-id','legacy-version','s','observed',?, ?,?,?)""", (json.dumps({'relationship': 'loaded'}), NOW, NOW, NOW))
    conn.commit()


def test_migration_preserves_unambiguous_installation_identity_and_history(tmp_path):
    path = _skill(tmp_path / 'one')
    conn = sqlite3.connect(tmp_path / 'reflect.db')
    conn.execute('PRAGMA foreign_keys=ON')
    _legacy(conn, [path])
    migrate(conn)
    registry = SkillRegistryService(conn)
    registry.sync_paths([path])
    assert len(registry.list()) == 1
    assert registry.show('verify-first').skill.id == 'legacy-id'
    assert registry.show('legacy-id').usage_occurrences[0].skill_version_id == 'legacy-version'
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    conn.close()


def test_migration_keeps_merged_history_unattributed_to_new_origins(tmp_path):
    paths = [_skill(tmp_path / repo) for repo in ('one', 'two')]
    conn = sqlite3.connect(tmp_path / 'reflect.db')
    conn.execute('PRAGMA foreign_keys=ON')
    _legacy(conn, paths)
    migrate(conn)
    registry = SkillRegistryService(conn)
    registry.sync_paths(paths)
    rows = conn.execute('SELECT id,identity_key FROM skills').fetchall()
    assert len(rows) == 3
    assert dict(rows)['legacy-id'] == 'legacy:legacy-id'
    history = registry.show('legacy-id').usage_occurrences
    assert len(history) == 1 and history[0].skill_version_id == 'legacy-version'
    owners = {row[0] for row in conn.execute('SELECT skill_id FROM skill_installations')}
    assert len(owners) == 2 and 'legacy-id' not in owners
    assert all(registry.show(owner).usage_occurrences == [] for owner in owners)
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert migrate(conn) == []
    conn.close()


def test_generated_identity_does_not_merge_import_or_distinct_contracts(tmp_path):
    from reflect.improvements.service import ImprovementService

    conn = connect_sqlite(tmp_path / 'reflect.db')
    service = ImprovementService(conn)
    path = _skill(tmp_path / 'repo')
    service.skills.sync_paths([path])
    imported = service.skills.list()[0]
    candidates = [service.stage_extracted_skills([{
        'name': 'verify-first', 'description': 'Verify changes', 'content': body,
        'behavior_type': 'verification',
    }], session_ids=[], source_agent='codex')[0] for body in ('Run tests.', 'Review the deployment.')]
    generated = [service.skills.skill_for_candidate(candidate) for candidate in candidates]
    assert len({imported.id, *(item.id for item in generated)}) == 3
    service.skills.sync_workflow_candidates(candidates)
    assert len(service.skills.list()) == 3
    conn.close()


def test_generated_installation_scan_preserves_canonical_workflow_owner(tmp_path):
    from reflect.improvements.service import ImprovementService

    conn = connect_sqlite(tmp_path / 'reflect.db')
    service = ImprovementService(conn)
    candidate = service.stage_extracted_skills([{
        'name': 'verify-first', 'description': 'Verify changes', 'content': 'Run tests.',
        'behavior_type': 'verification',
    }], session_ids=[], source_agent='codex')[0]
    skill = service.skills.skill_for_candidate(candidate)
    detail = service.skills.show(skill.id)
    path = tmp_path / 'repo' / 'verify-first' / 'SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text(conn.execute('SELECT content_markdown FROM skill_versions WHERE id=?', (detail.versions[0].id,)).fetchone()[0])
    service.skills._upsert_installation(skill_id=skill.id, version_id=detail.versions[0].id,
        path=path, installed_hash=detail.versions[0].content_hash, status='active', now=NOW)
    service.skills.sync_paths([path])
    assert len(service.skills.list()) == 1
    assert service.skills.show(skill.id).installations[0].skill_id == skill.id
    conn.close()


def test_migration_retains_canonical_generated_identity(tmp_path):
    from reflect.improvements.service import ImprovementService

    path = _skill(tmp_path / 'repo')
    conn = sqlite3.connect(tmp_path / 'reflect.db')
    conn.execute('PRAGMA foreign_keys=ON')
    _legacy(conn, [path])
    prior = [item for item in load_migrations() if item.version <= 47]
    with patch('reflect.store.migrate.load_migrations', return_value=prior):
        service = ImprovementService(conn)
        with patch.object(service.skills, 'sync_workflow_candidates'):
            candidate = service.stage_extracted_skills([{
                'name': 'verify-first', 'description': 'Verify changes', 'content': 'Run tests.',
                'behavior_type': 'verification',
            }], session_ids=[], source_agent='codex')[0]
    conn.execute("UPDATE skill_versions SET workflow_candidate_id=?,source_kind='agent_authored'", (candidate,))
    conn.commit()
    migrate(conn)
    registry = SkillRegistryService(conn)
    registry.sync_workflow_candidates([candidate])
    assert len(registry.list()) == 1
    assert registry.skill_for_candidate(candidate).id == 'legacy-id'
    assert registry.show('legacy-id').usage_occurrences[0].skill_version_id == 'legacy-version'
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    conn.close()


@pytest.mark.parametrize('retained_hash', [True, False])
def test_intervention_replay_uses_captured_hash_and_preserves_generated_owner(tmp_path, retained_hash):
    import hashlib

    from reflect.improvements.service import ImprovementService

    conn = connect_sqlite(tmp_path / 'reflect.db')
    service = ImprovementService(conn)
    candidate = service.stage_extracted_skills([{
        'name': 'verify-first', 'description': 'Verify changes', 'content': 'Run tests.',
        'behavior_type': 'verification',
    }], session_ids=[], source_agent='codex')[0]
    skill = service.skills.skill_for_candidate(candidate)
    original = conn.execute('SELECT id,content_markdown,content_hash FROM skill_versions WHERE skill_id=?', (skill.id,)).fetchone()
    path = tmp_path / 'repo' / 'verify-first' / 'SKILL.md'
    path.parent.mkdir(parents=True)
    installed = original[1] if retained_hash else original[1] + '\nAn unretained installed revision.\n'
    path.write_text(installed)
    captured_hash = hashlib.sha256(installed.encode()).hexdigest()
    conn.execute("INSERT INTO workflow_versions(id,candidate_id,version,content_json,content_hash,created_at) VALUES('wv',?,1,'{}','review',?)", (candidate, NOW))
    conn.execute("""INSERT INTO interventions(id,workflow_version_id,scope_type,scope_id,target_path,
        applied_hash,applied_content,exposure_started_at,created_at,updated_at)
        VALUES('applied','wv','repo','repo',?,?,?,?,?,?)""", (str(path), captured_hash, installed, NOW, NOW, NOW))
    # A newer presentation of the same persisted canonical workflow must not
    # replace the version captured by its earlier installation.
    content = json.loads(conn.execute('SELECT content_json FROM workflow_candidates WHERE id=?', (candidate,)).fetchone()[0])
    content['description'] = 'Verify changes and report the result'
    conn.execute('UPDATE workflow_candidates SET content_json=? WHERE id=?', (json.dumps(content), candidate))
    service.skills.sync_workflow_candidates([candidate])
    installation = service.skills.show(skill.id).installations[0]
    assert installation.skill_version_id == (original[0] if retained_hash else None)
    for _ in range(2):
        service.skills.refresh(scan_paths=[path])
        assert len(service.skills.list()) == 1
        installation = service.skills.show(skill.id).installations[0]
        assert installation.skill_version_id == (original[0] if retained_hash else None)
        assert installation.skill_id == skill.id
    conn.close()


def test_registry_refresh_preserves_historical_usage_without_replayable_steps(tmp_path):
    paths = [_skill(tmp_path / repo) for repo in ('one', 'two')]
    conn = sqlite3.connect(tmp_path / 'reflect.db')
    conn.execute('PRAGMA foreign_keys=ON')
    _legacy(conn, paths)
    migrate(conn)
    registry = SkillRegistryService(conn)
    registry.refresh(scan_paths=paths)
    assert len(registry.show('legacy-id').usage_occurrences) == 1
    registry.refresh(scan_paths=paths)
    assert len(registry.show('legacy-id').usage_occurrences) == 1
    conn.close()


def test_usage_graph_rejects_ambiguous_name_and_resolves_exact_identity(tmp_path):
    from reflect.dashboard_skill_graph import build_skill_usage_graph

    conn = connect_sqlite(tmp_path / 'reflect.db')
    registry = SkillRegistryService(conn)
    registry.sync_paths([_skill(tmp_path / repo) for repo in ('one', 'two')])
    skills = registry.list()
    with pytest.raises(ValueError) as error:
        build_skill_usage_graph(conn, 'verify-first')
    assert all(item.id in str(error.value) for item in skills)
    for skill in skills:
        assert build_skill_usage_graph(conn, skill.id)['skill_id'] == skill.id
    conn.close()


def test_usage_graph_api_rejects_ambiguous_name_and_accepts_exact_ids(tmp_path):
    from fastapi.testclient import TestClient

    from reflect.dashboard_server import build_dashboard_app

    db_path = tmp_path / 'reflect.db'
    conn = connect_sqlite(db_path)
    registry = SkillRegistryService(conn)
    registry.sync_paths([_skill(tmp_path / repo) for repo in ('one', 'two')])
    skills = registry.list()
    conn.commit()
    from test_dashboard_sql_api import _publish_report
    _publish_report(db_path)
    client = TestClient(build_dashboard_app(db_path=db_path, docs_dir=tmp_path))
    ambiguous = client.get('/api/skills/verify-first/usage-graph')
    assert ambiguous.status_code == 422
    assert all(item.id in ambiguous.json()['error'] for item in skills)
    for skill in skills:
        response = client.get(f'/api/skills/{skill.id}/usage-graph')
        assert response.status_code == 200
        assert response.json()['skill_id'] == skill.id
    conn.close()


def test_stable_path_rename_updates_current_name_and_keeps_history(tmp_path):
    conn = connect_sqlite(tmp_path / 'rename.db')
    registry = SkillRegistryService(conn)
    path = _skill(tmp_path / 'one')
    registry.sync_paths([path])
    original = registry.show('verify-first')
    path.write_text(path.read_text().replace('name: verify-first', 'name: after-name'))
    registry.sync_paths([path])
    renamed = registry.show('after-name')
    assert renamed.skill.id == original.skill.id
    assert renamed.skill.slug == renamed.skill.name == 'after-name'
    assert renamed.skill.version_count == 2
    assert {item.id for item in original.versions} < {item.id for item in renamed.versions}
    assert renamed.installations[0].id == original.installations[0].id
    with pytest.raises(KeyError):
        registry.resolve_id('verify-first')
    projector = SkillUsageProjector(conn)
    assert [item.skill_id for item in projector.occurrences({'skill.name': 'after-name'}, 'Skill', 'ok', '')] == [original.skill.id]
    assert projector.occurrences({'skill.name': 'verify-first'}, 'Skill', 'ok', '') == []
    other = _skill(tmp_path / 'two')
    other.write_text(other.read_text().replace('name: verify-first', 'name: after-name'))
    registry.sync_paths([other])
    with pytest.raises(ValueError, match='Ambiguous'):
        registry.resolve_id('after-name')
    conn.close()
