import sqlite3

from reflect.workflow_journal import WorkflowFileJournal


def test_recovery_restores_uncommitted_write_but_never_overwrites_user_edits(tmp_path):
    database = tmp_path / 'state.db'
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE interventions(id TEXT PRIMARY KEY, status TEXT)')
    journal = WorkflowFileJournal(database)
    target = tmp_path / 'SKILL.md'
    target.write_text('approved-new')
    journal.begin(target=target, before='previous', after='approved-new', intervention_id='lost', operation='apply')
    assert journal.recover() == []
    assert target.read_text() == 'previous'
    target.write_text('user-edited')
    journal.begin(target=target, before='previous', after='approved-new', intervention_id='lost', operation='apply')
    assert journal.recover() == [str(target)]
    assert target.read_text() == 'user-edited'
    assert list(journal.directory.glob('*.json'))


def test_recovery_keeps_committed_action_and_reconciles_rollback(tmp_path):
    database = tmp_path / 'state.db'
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE interventions(id TEXT PRIMARY KEY, status TEXT)')
        conn.execute("INSERT INTO interventions VALUES ('saved','active')")
    journal = WorkflowFileJournal(database)
    target = tmp_path / 'SKILL.md'
    target.write_text('new')
    journal.begin(target=target, before=None, after='new', intervention_id='saved', operation='apply')
    assert journal.recover() == []
    assert target.read_text() == 'new'
    target.unlink()
    journal.begin(target=target, before='new', after=None, intervention_id='saved', operation='rollback')
    assert journal.recover() == []
    assert target.read_text() == 'new'
