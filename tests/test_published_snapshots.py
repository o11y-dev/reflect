import sqlite3

import pytest

from reflect.store.snapshots import PublishedSnapshotStore, SnapshotCapacityError
from reflect.store.sqlite import connect_sqlite, mark_snapshot


def store(tmp_path):
    path = tmp_path / 'reflect.db'
    conn = connect_sqlite(path)
    conn.execute('CREATE TABLE store_metadata(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)')
    conn.execute('CREATE TABLE facts(value TEXT)')
    conn.execute("INSERT INTO facts VALUES ('old')")
    mark_snapshot(conn, ready=True)
    conn.close()
    return path


def test_completed_generation_remains_readable_during_refresh(tmp_path):
    path = store(tmp_path)
    snapshots = PublishedSnapshotStore(path, reserve_bytes=0)
    first = snapshots.publish(evidence_revision='first')
    with snapshots.acquire() as pinned:
        writer = connect_sqlite(path)
        mark_snapshot(writer, ready=False)
        writer.execute("UPDATE facts SET value='new'")
        writer.commit()
        with pytest.raises(sqlite3.OperationalError):
            snapshots.publish(evidence_revision='first')
        assert snapshots.current() == first
        with sqlite3.connect(pinned.path.as_uri() + '?immutable=1', uri=True) as reader:
            assert reader.execute('SELECT value FROM facts').fetchone() == ('old',)
        mark_snapshot(writer, ready=True)
        writer.close()
        second = snapshots.publish(evidence_revision='second')
        assert second.generation != first.generation
        assert pinned.path.exists()
    assert snapshots.current() == second


def test_noop_publication_reuses_generation(tmp_path):
    snapshots = PublishedSnapshotStore(store(tmp_path), reserve_bytes=0)
    first = snapshots.publish(evidence_revision='first')
    writer = connect_sqlite(snapshots.db_path)
    mark_snapshot(writer, ready=True)
    writer.close()
    assert snapshots.publish(evidence_revision='first') == first


def test_low_disk_preserves_current_generation(tmp_path, monkeypatch):
    path = store(tmp_path)
    snapshots = PublishedSnapshotStore(path, reserve_bytes=0)
    first = snapshots.publish(evidence_revision='first')
    writer = connect_sqlite(path)
    mark_snapshot(writer, ready=True)
    writer.close()
    monkeypatch.setattr(snapshots, '_check_space', lambda additional=0: (_ for _ in ()).throw(SnapshotCapacityError('disk reserve')))
    with pytest.raises(SnapshotCapacityError):
        snapshots.publish(evidence_revision='second')
    assert snapshots.current() == first


def test_leased_generations_bound_publication(tmp_path):
    from contextlib import ExitStack
    path = store(tmp_path)
    snapshots = PublishedSnapshotStore(path, reserve_bytes=0)
    with ExitStack() as stack:
        for revision in range(3):
            writer = connect_sqlite(path)
            mark_snapshot(writer, ready=True)
            writer.close()
            snapshots.publish(evidence_revision=str(revision))
            stack.enter_context(snapshots.acquire())
        writer = connect_sqlite(path)
        mark_snapshot(writer, ready=True)
        writer.close()
        with pytest.raises(SnapshotCapacityError, match='leased'):
            snapshots.publish(evidence_revision='fourth')
    snapshots.publish(evidence_revision='fourth')


def test_interrupted_builds_are_reclaimed_before_publication(tmp_path):
    snapshots = PublishedSnapshotStore(store(tmp_path), reserve_bytes=0)
    snapshots.directory.mkdir()
    abandoned = snapshots.directory / '.generation.partial.backup.partial'
    abandoned.write_bytes(b'interrupted backup')
    snapshots.publish(evidence_revision='first')
    assert not abandoned.exists()


def test_generation_directory_is_durable_before_manifest_switch(tmp_path, monkeypatch):
    path = store(tmp_path)
    snapshots = PublishedSnapshotStore(path, reserve_bytes=0)
    first = snapshots.publish(evidence_revision='first')
    writer = connect_sqlite(path)
    mark_snapshot(writer, ready=True)
    writer.close()
    original = snapshots._sync_directory
    calls = []

    def sync():
        calls.append(snapshots.current())
        original()

    monkeypatch.setattr(snapshots, '_sync_directory', sync)
    second = snapshots.publish(evidence_revision='second')
    assert calls == [first, second]


def test_explicit_generation_can_be_acquired_after_new_publication(tmp_path):
    snapshots = PublishedSnapshotStore(store(tmp_path), reserve_bytes=0)
    first = snapshots.publish(evidence_revision='first')
    with snapshots.acquire(first):
        second = snapshots.publish(evidence_revision='second')
        with snapshots.acquire(first) as selected:
            assert selected.generation == first.generation
            assert selected.generation != second.generation


def test_nested_explicit_generation_mismatch_is_rejected(tmp_path):
    from reflect.store.snapshots import SnapshotReadContext

    path = store(tmp_path)
    snapshots = PublishedSnapshotStore(path, reserve_bytes=0)
    first = snapshots.publish(evidence_revision='first')
    with SnapshotReadContext(path, snapshot=first):
        second = snapshots.publish(evidence_revision='second')
        with pytest.raises(ValueError, match='same published generation'), SnapshotReadContext(path, snapshot=second):
            pass
        with SnapshotReadContext(path, snapshot=first) as nested:
            assert nested.snapshot == first
