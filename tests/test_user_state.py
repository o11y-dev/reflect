import sqlite3
from contextlib import closing

import pytest

from reflect.store.user_state import UserStateConflict, UserStateStore


def test_review_is_independent_of_long_analysis_transaction(tmp_path):
    working = sqlite3.connect(tmp_path / 'reflect.db')
    working.execute('CREATE TABLE analysis(value INTEGER)')
    working.execute('BEGIN IMMEDIATE')
    working.execute('INSERT INTO analysis VALUES (1)')
    state = UserStateStore(tmp_path / 'user-state.db')
    state.initialize()
    try:
        state.dismiss('contract-a')
        assert state.is_dismissed('contract-a')
    finally:
        working.rollback()
        working.close()


def test_material_revision_changes_only_when_review_changes(tmp_path):
    state = UserStateStore(tmp_path / 'user-state.db')
    state.initialize()
    before = state.revision()
    state.dismiss('contract-a')
    after = state.revision()
    assert after == before + 1
    state.dismiss('contract-a')
    assert state.revision() == after
    state.restore('contract-a')
    assert not state.is_dismissed('contract-a')
    assert state.revision() == after + 1


def test_draft_survives_source_pruning_and_rejects_stale_edit(tmp_path):
    state = UserStateStore(tmp_path / 'user-state.db')
    state.initialize()
    draft = state.save_draft('draft-a', title='Example', content={'steps': ['Inspect']},
                             evidence={'generation': 'generation-a', 'references': ['execution-a']},
                             expected_revision=0)
    assert draft.revision == 1
    updated = state.save_draft('draft-a', title='Changed', content={'steps': ['Verify']},
                               evidence=draft.evidence, expected_revision=1)
    with pytest.raises(UserStateConflict):
        state.save_draft('draft-a', title='Stale', content={}, evidence={}, expected_revision=1)
    assert state.draft('draft-a') == updated


def test_workflow_service_creates_validated_draft_during_analysis(tmp_path):
    from reflect.improvements.workflows import WorkflowService

    state = UserStateStore(tmp_path / 'user-state.db')
    state.initialize()
    working = sqlite3.connect(tmp_path / 'reflect.db')
    working.execute('CREATE TABLE analysis(value INTEGER)')
    working.execute('BEGIN IMMEDIATE')
    try:
        draft = WorkflowService.create_draft(state, title='Review a change', content={
            'slug': 'review-change', 'steps': ['Resolve the change', 'Inspect its diff'],
        })
        assert draft.content['slug'] == 'review-change'
        assert draft.evidence == {'origin': 'manual', 'verification': 'unverified'}
        assert state.draft(draft.id) == draft
        with pytest.raises(ValueError, match='Unsafe workflow slug'):
            WorkflowService.create_draft(state, title='Unsafe', content={
                'slug': '../escape', 'steps': ['Inspect'],
            })
    finally:
        working.rollback()
        working.close()


def test_repository_overlay_pins_review_revision_while_analysis_is_busy(tmp_path):
    from test_dashboard_sql_api import _seed_improvement_ledger, _seed_sql_report_db

    from reflect.improvements.repository import ImprovementRepository
    from reflect.store.sqlite import connect_sqlite, connect_sqlite_read_only

    path = tmp_path / 'reflect.db'
    _seed_sql_report_db(path)
    _seed_improvement_ledger(path)
    state = UserStateStore(tmp_path / 'user-state.db')
    state.initialize()
    with closing(connect_sqlite_read_only(path)) as reader:
        candidate = ImprovementRepository(reader).list_candidates()[0]
        old_view = state.snapshot()
        writer = connect_sqlite(path)
        writer.execute('BEGIN IMMEDIATE')
        try:
            state.dismiss(candidate.contract_signature)
            old = ImprovementRepository(reader, review_state=old_view).get_candidate(candidate.id)
            new = ImprovementRepository(reader, review_state=state.snapshot()).get_candidate(candidate.id)
            assert old.status.value == 'pending'
            assert new.status.value == 'rejected'
            assert new.confidence == old.confidence
            assert new.lifecycle.measurement == old.lifecycle.measurement
        finally:
            writer.rollback()
            writer.close()


def test_migration_copies_verifies_and_moves_authority_once(tmp_path):
    from test_dashboard_sql_api import _seed_improvement_ledger, _seed_sql_report_db

    from reflect.store.sqlite import connect_sqlite
    from reflect.store.user_state_migration import UserStateMigration

    path = tmp_path / 'reflect.db'
    _seed_sql_report_db(path)
    _seed_improvement_ledger(path)
    state = UserStateStore(tmp_path / 'user-state.db')
    migration = UserStateMigration(path, state)
    with closing(connect_sqlite(path)) as conn:
        before = conn.execute('SELECT COUNT(*) FROM skills').fetchone()[0]
    assert migration.run()
    assert not migration.run()
    with closing(connect_sqlite(state.path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM skills').fetchone()[0] == before
    with closing(connect_sqlite(path)) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='skills'").fetchone()
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()


def test_interrupted_cutover_retains_authoritative_source_and_can_resume(tmp_path, monkeypatch):
    from test_dashboard_sql_api import _seed_improvement_ledger, _seed_sql_report_db

    from reflect.store.sqlite import connect_sqlite
    from reflect.store.user_state_migration import UserStateMigration

    path = tmp_path / 'reflect.db'
    _seed_sql_report_db(path)
    _seed_improvement_ledger(path)
    state = UserStateStore(tmp_path / 'user-state.db')
    migration = UserStateMigration(path, state)
    cutover = migration._cutover
    monkeypatch.setattr(migration, '_cutover', lambda _: (_ for _ in ()).throw(RuntimeError('interrupted')))
    with pytest.raises(RuntimeError, match='interrupted'):
        migration.run()
    with closing(connect_sqlite(path)) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='skills'").fetchone()
        assert not conn.execute("SELECT 1 FROM store_metadata WHERE key='user_state_cutover_v1'").fetchone()
    monkeypatch.setattr(migration, '_cutover', cutover)
    assert migration.run()


def test_action_connection_reads_generation_without_locking_working_writer(tmp_path):
    from test_dashboard_sql_api import _seed_improvement_ledger, _seed_sql_report_db

    from reflect.store.snapshots import PublishedSnapshotStore
    from reflect.store.sqlite import connect_sqlite, mark_snapshot
    from reflect.store.user_state import attach_evidence
    from reflect.store.user_state_migration import UserStateMigration

    path = tmp_path / 'reflect.db'
    _seed_sql_report_db(path)
    _seed_improvement_ledger(path)
    state = UserStateStore(tmp_path / 'user-state.db')
    UserStateMigration(path, state).run()
    with closing(connect_sqlite(path)) as conn:
        mark_snapshot(conn, ready=True)
    snapshot = PublishedSnapshotStore(path, reserve_bytes=0).publish(evidence_revision='migration')
    with closing(connect_sqlite(path)) as writer, closing(connect_sqlite(state.path)) as action:
        writer.execute('BEGIN IMMEDIATE')
        attach_evidence(action, snapshot.path)
        action.execute('BEGIN IMMEDIATE')
        assert action.execute('SELECT COUNT(*) FROM evidence.sessions').fetchone()[0] > 0
        action.execute("UPDATE user_state_metadata SET value=value+1 WHERE key='material_revision'")
        action.commit()
        writer.rollback()


def test_enable_uses_authoritative_state_while_analysis_writer_is_active(tmp_path):
    from test_dashboard_sql_api import _seed_improvement_ledger, _seed_sql_report_db

    from reflect.improvements.workflows import WorkflowService
    from reflect.store.snapshots import PublishedSnapshotStore
    from reflect.store.sqlite import connect_sqlite, mark_snapshot
    from reflect.store.user_state_migration import UserStateMigration

    path = tmp_path / 'reflect.db'
    _seed_sql_report_db(path)
    _seed_improvement_ledger(path)
    state = UserStateStore(tmp_path / 'user-state.db')
    UserStateMigration(path, state).run()
    with closing(connect_sqlite(path)) as conn:
        candidate_id, revision_hash = conn.execute('SELECT id,revision_hash FROM workflow_candidates LIMIT 1').fetchone()
        mark_snapshot(conn, ready=True)
    snapshot = PublishedSnapshotStore(path, reserve_bytes=0).publish(evidence_revision='migration')
    project = tmp_path / 'project'
    project.mkdir()
    (project / '.git').mkdir()
    before = state.revision()
    with closing(connect_sqlite(path)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        with state.workflow_action(snapshot.path, candidate_id, expected_revision_hash=revision_hash) as action:
            result = WorkflowService(action).apply(candidate_id, project_root=project)
        assert result['status'] == 'active'
        assert (project / '.agents/skills/verify-before-done/SKILL.md').is_file()
        assert state.revision() > before
        writer.rollback()
    with closing(connect_sqlite(state.path)) as conn:
        assert conn.execute('SELECT status FROM interventions').fetchone()[0] == 'active'


@pytest.fixture
def prepared_actions(tmp_path):
    from test_dashboard_sql_api import _seed_improvement_ledger, _seed_sql_report_db

    from reflect.store.snapshots import PublishedSnapshotStore
    from reflect.store.sqlite import connect_sqlite, mark_snapshot
    from reflect.store.user_state_migration import UserStateMigration

    path = tmp_path / 'reflect.db'
    _seed_sql_report_db(path)
    _seed_improvement_ledger(path)
    state = UserStateStore(tmp_path / 'user-state.db')
    UserStateMigration(path, state).run()
    with closing(connect_sqlite(path)) as conn:
        candidate_id, revision_hash = conn.execute('SELECT id,revision_hash FROM workflow_candidates LIMIT 1').fetchone()
        mark_snapshot(conn, ready=True)
    snapshots = PublishedSnapshotStore(path, reserve_bytes=0)
    published = snapshots.publish(evidence_revision='initial')
    project = tmp_path / 'project'
    project.mkdir()
    (project / '.git').mkdir()
    return path, state, snapshots, published, candidate_id, revision_hash, project


def test_rollback_survives_source_pruning(prepared_actions):
    from reflect.improvements.workflows import WorkflowService
    from reflect.store.sqlite import connect_sqlite, mark_snapshot

    path, state, snapshots, published, candidate_id, revision, project = prepared_actions
    with state.workflow_action(published.path, candidate_id, expected_revision_hash=revision) as action:
        WorkflowService(action).apply(candidate_id, project_root=project)
    with closing(connect_sqlite(path)) as writer:
        writer.execute('DELETE FROM workflow_candidates WHERE id=?', (candidate_id,))
        mark_snapshot(writer, ready=True)
    published = snapshots.publish(evidence_revision='pruned')
    with state.workflow_action(published.path, candidate_id, expected_revision_hash=revision, action_kind='rollback') as action:
        result = WorkflowService(action).rollback(candidate_id)
    assert result['status'] == 'rolled_back'
    assert not (project / '.agents/skills/verify-before-done/SKILL.md').exists()


def test_explicit_review_can_accept_changed_source(prepared_actions):
    import json

    from reflect.improvements.workflow_identity import WorkflowIdentity
    from reflect.improvements.workflows import WorkflowService
    from reflect.store.sqlite import connect_sqlite, mark_snapshot

    path, state, snapshots, published, candidate_id, revision, _ = prepared_actions
    with state.workflow_action(published.path, candidate_id, expected_revision_hash=revision) as action:
        WorkflowService(action).approve(candidate_id)
    with closing(connect_sqlite(path)) as writer:
        content = json.loads(writer.execute('SELECT content_json FROM workflow_candidates WHERE id=?', (candidate_id,)).fetchone()[0])
        new_revision = WorkflowIdentity.from_content('Updated procedure', content).revision_hash
        writer.execute('UPDATE workflow_candidates SET title=?,revision_hash=? WHERE id=?',
                       ('Updated procedure', new_revision, candidate_id))
        mark_snapshot(writer, ready=True)
    published = snapshots.publish(evidence_revision='updated')
    with (pytest.raises(UserStateConflict, match='evidence changed'),
          state.workflow_action(published.path, candidate_id, expected_revision_hash=revision)):
        pytest.fail('stale preview must not execute')
    with state.workflow_action(published.path, candidate_id, expected_revision_hash=new_revision, action_kind='review') as action:
        assert WorkflowService(action).approve(candidate_id)['status'] == 'approved'
    with state.workflow_action(published.path, candidate_id, expected_revision_hash=new_revision) as action:
        assert WorkflowService(action).show(candidate_id).title == 'Updated procedure'


def test_legacy_dismissal_contract_is_migrated(tmp_path):
    from test_dashboard_sql_api import _seed_improvement_ledger, _seed_sql_report_db

    from reflect.improvements.workflows import WorkflowService
    from reflect.store.sqlite import connect_sqlite
    from reflect.store.user_state_migration import UserStateMigration

    path = tmp_path / 'reflect.db'
    _seed_sql_report_db(path)
    _seed_improvement_ledger(path)
    with closing(connect_sqlite(path)) as conn:
        candidate_id, contract = conn.execute('SELECT id,contract_signature FROM workflow_candidates LIMIT 1').fetchone()
        WorkflowService(conn).dismiss_suggestion(candidate_id)
    state = UserStateStore(tmp_path / 'user-state.db')
    UserStateMigration(path, state).run()
    assert state.is_dismissed(contract)
    with closing(connect_sqlite(path)) as conn:
        assert conn.execute('SELECT status FROM workflow_candidates WHERE id=?', (candidate_id,)).fetchone()[0] == 'pending'
    state.restore(contract)
    assert not state.is_dismissed(contract)


def test_cutover_boundary_failure_blocks_actions_until_recovered(tmp_path, monkeypatch):
    from test_dashboard_sql_api import _seed_improvement_ledger, _seed_sql_report_db

    from reflect.store.user_state_migration import UserStateMigration

    path = tmp_path / 'reflect.db'
    _seed_sql_report_db(path)
    _seed_improvement_ledger(path)
    state = UserStateStore(tmp_path / 'user-state.db')
    migration = UserStateMigration(path, state)
    ready = migration._mark_ready
    monkeypatch.setattr(migration, '_mark_ready', lambda: (_ for _ in ()).throw(RuntimeError('boundary crash')))
    with pytest.raises(RuntimeError, match='boundary crash'):
        migration.run()
    with (pytest.raises(UserStateConflict, match='migration is not complete'),
          state.workflow_action(path, 'candidate', expected_revision_hash='revision')):
        pytest.fail('action must not execute')
    monkeypatch.setattr(migration, '_mark_ready', ready)
    assert not migration.run()
    assert state.path.exists()


def test_interrupted_copy_blocks_restore_dismiss_and_draft_until_retry(tmp_path, monkeypatch):
    from test_dashboard_sql_api import _seed_improvement_ledger, _seed_sql_report_db

    from reflect.improvements.workflows import WorkflowService
    from reflect.store.sqlite import connect_sqlite
    from reflect.store.user_state_migration import UserStateMigration

    path = tmp_path / 'reflect.db'
    _seed_sql_report_db(path)
    _seed_improvement_ledger(path)
    with closing(connect_sqlite(path)) as conn:
        candidate, contract = conn.execute('SELECT id,contract_signature FROM workflow_candidates LIMIT 1').fetchone()
        WorkflowService(conn).dismiss_suggestion(candidate)
    state = UserStateStore(tmp_path / 'user-state.db')
    migration = UserStateMigration(path, state)
    cutover = migration._cutover
    monkeypatch.setattr(migration, '_cutover', lambda _: (_ for _ in ()).throw(RuntimeError('interrupted copy')))
    with pytest.raises(RuntimeError, match='interrupted copy'):
        migration.run()
    for action in (lambda: state.restore(contract), lambda: state.dismiss('another'),
                   lambda: state.save_draft('draft', title='Draft', content={}, evidence={}, expected_revision=0)):
        with pytest.raises(UserStateConflict, match='migration is not complete'):
            action()
    monkeypatch.setattr(migration, '_cutover', cutover)
    migration.run()
    assert state.is_dismissed(contract)
    state.restore(contract)
    assert not state.is_dismissed(contract)


def test_composed_read_pins_evidence_and_user_state(prepared_actions):
    from reflect.store.snapshots import SnapshotReadContext
    from reflect.store.sqlite import connect_sqlite_read_only

    path, state, _, _, candidate_id, _, _ = prepared_actions
    with SnapshotReadContext(path) as context:
        contract = context.connection.execute('SELECT contract_signature FROM workflow_candidates WHERE id=?', (candidate_id,)).fetchone()[0]
        before = context.review_state.revision
        state.dismiss(contract)
        with closing(connect_sqlite_read_only(path)) as nested:
            assert nested is context.connection
            assert not nested.execute('SELECT 1 FROM user_state.workflow_dismissals WHERE contract_signature=?', (contract,)).fetchone()
        assert context.connection.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] > 0
        assert context.review_state.revision == before
    with SnapshotReadContext(path) as context:
        assert context.review_state.revision > before
        assert contract in context.review_state.dismissed_contracts


def test_registry_sync_writes_state_separately_from_analysis(prepared_actions):
    from reflect.improvements.skills import SkillRegistryService
    from reflect.store.sqlite import connect_sqlite

    path, state, _, _, _, _, _ = prepared_actions
    with closing(connect_sqlite(path)) as analysis:
        analysis.execute('BEGIN IMMEDIATE')
        result = SkillRegistryService(analysis, initialize_schema=False).sync_workflow_candidates()
        assert result['skills'] >= 1
        # The analysis transaction still owns the canonical writer; registry
        # updates committed on the independent action store.
        assert analysis.in_transaction
        state.dismiss('independent-contract')
        assert state.is_dismissed('independent-contract')
        analysis.rollback()


def test_filesystem_registry_scan_keeps_action_writer_independent(prepared_actions):
    from reflect.improvements.skills import SkillRegistryService
    from reflect.store.sqlite import connect_sqlite

    path, state, _, _, _, _, project = prepared_actions
    skill = project / 'SKILL.md'
    skill.write_text('---\nname: local-check\ndescription: Check local changes\n---\nRun focused tests.\n')
    with closing(connect_sqlite(path)) as analysis:
        analysis.execute('BEGIN IMMEDIATE')
        result = SkillRegistryService(analysis, initialize_schema=False).sync_paths([skill])
        assert result['skills'] == 1
        state.dismiss('another-contract')
        assert state.is_dismissed('another-contract')
        analysis.rollback()
    with closing(connect_sqlite(state.path)) as conn:
        assert conn.execute("SELECT 1 FROM skills WHERE slug='local-check'").fetchone()


def test_authored_workflow_survives_source_pruning_without_resurrecting_aggregates(tmp_path):
    from test_dashboard_sql_api import _seed_sql_report_db

    from reflect.improvements.service import ImprovementService
    from reflect.store.snapshots import PublishedSnapshotStore, SnapshotReadContext
    from reflect.store.sqlite import connect_sqlite, mark_snapshot
    from reflect.store.user_state_migration import UserStateMigration

    path = tmp_path / 'reflect.db'
    _seed_sql_report_db(path)
    with closing(connect_sqlite(path)) as conn:
        created = ImprovementService(conn).create_workflow(content={
            'slug': 'saved-work', 'description': 'Keep my instructions',
            'steps': ['Inspect the exact inputs'], 'verification': [], 'abstain_when': [],
        })
    state = UserStateStore(tmp_path / 'state.db')
    UserStateMigration(path, state).run()
    with closing(connect_sqlite(path, attach_user_state=False)) as writer:
        writer.execute('PRAGMA foreign_keys=OFF')
        writer.execute('DELETE FROM workflow_candidates WHERE id=?', (created['id'],))
        writer.execute('DELETE FROM observations WHERE id=?', (created['observation_id'],))
        writer.commit()
        mark_snapshot(writer, ready=True)
    PublishedSnapshotStore(path, reserve_bytes=0).publish(evidence_revision='pruned')
    with SnapshotReadContext(path) as read:
        repository = ImprovementService(read.connection, initialize_schema=False).repository
        saved = repository.get_candidate(created['id'])
        assert saved is not None
        assert saved.content['steps'] == ['Inspect the exact inputs']
        assert saved.id in {candidate.id for candidate in repository.list_candidates()}
        assert repository.get_observation(created['observation_id']).id == created['observation_id']
        assert read.connection.execute('SELECT COUNT(*) FROM main.observations WHERE id=?',
                                       (created['observation_id'],)).fetchone()[0] == 0
